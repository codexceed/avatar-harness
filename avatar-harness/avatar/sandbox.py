"""Hermetic execution at the command seam (ADR-0042).

Every command the harness runs — the verifier's frozen checks *and* the model's
`run_command` — funnels through `Workspace._run_unlogged`. A `Sandbox` transforms
that one call: it computes the environment the child runs under and (in the OS/
container backends) wraps the argv in a launcher that denies network and confines
writes. It never *runs* the command — the `Workspace` still does — so the seam
stays a single chokepoint and `prepare()` is a pure transform (design invariant #5).

Scope is **Threat C only** (ADR-0042): it closes runtime/substrate gaming — an
inherited `PYTEST_ADDOPTS`/`PYTHONPATH`, a phone-home, a fork bomb — not a model
that authors weak tests (Threat B) or guts the graded ones (Threat A). Workspace-
*discovered* config (`conftest.py`, `pyproject` addopts) is a file, not env, and is
out of scope here by decision (the adequacy/diff-scope lever, ADR-0040).

Backends, weakest→strongest, all behind the same `prepare()`:

- `none` — identity argv + the full inherited environment (today's behavior; the
  back-compat escape hatch).
- `hermetic-env` — identity argv + an env scrubbed to a language-neutral allowlist,
  so `PYTEST_ADDOPTS`/`NODE_OPTIONS`/`RUBYOPT`/`PYTHONPATH`/`CLASSPATH` vanish *by
  construction* (no per-language whack-a-mole). The portable floor: pure Python,
  every OS, no dependencies. **The default.**
- `sandbox-exec` — the allowlisted env + argv wrapped in the macOS launcher with a
  network-deny profile (Apple-deprecated, an optional native fast-path).
- `bwrap` — Linux bubblewrap: the allowlisted env + a read-only root with only the
  workspace writable + a network namespace unshared (Increment 2).
- `container` — a Podman/Docker run: net-deny, read-only rootfs with the workspace
  bind-mounted writable, a clean env, kernel-enforced pid cap. The one genuinely
  cross-platform strong sandbox (Increment 2); needs the runtime + an image.

Resource limits (`RLimits`) ride `ExecSpec.preexec_fn` for the direct-exec backends
and container flags for the container backend. They ship **off by default**:
`preexec_fn` runs between `fork` and `exec`, which is not thread-safe against the
multithreaded eval runner (ADR-0026), so they are an opt-in toggle, not baked into
the default.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

try:  # POSIX-only; absent on Windows. rlimits degrade to a no-op where unavailable.
    import resource as _resource
except ImportError:  # pragma: no cover — the harness targets POSIX
    _resource = None  # type: ignore[assignment]

# The language-neutral env allowlist: pass *only* this safe set through, so every
# injection vector (`PYTEST_ADDOPTS`, `PYTHONPATH`, `NODE_OPTIONS`, `RUBYOPT`,
# `CLASSPATH`, `*_OPTS`, `LD_PRELOAD`, …) is dropped by construction. `VIRTUAL_ENV`
# and `PATH` are kept so the `uv`-managed interpreter and `python -m pytest` still
# resolve (the one real implementation risk, ADR-0042); `SSL_CERT_*` keep TLS trust
# working for a network-allowed run.
_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "TZ",
        "VIRTUAL_ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
# Whole prefix families kept intact (locale). Anything not named or prefixed is dropped.
_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("LC_",)

# What a container run forwards INTO the guest: only portable vars — never the host
# `PATH`/`VIRTUAL_ENV` (meaningless inside the image, whose own toolchain provides them).
_CONTAINER_ENV_FORWARD: frozenset[str] = frozenset({"LANG", "TZ"})


def hermetic_env(source: Mapping[str, str]) -> dict[str, str]:
    """Filter `source` to the language-neutral allowlist — the portability trick (ADR-0042).

    Dropping everything unlisted removes env-based rigging for *every* language at once,
    rather than blocklisting each runtime's injection var. `PATH`/`VIRTUAL_ENV` survive so
    legitimate verification (`python -m pytest`, `py_compile`) still resolves the interpreter.

    Args:
        source: The environment to filter (usually `os.environ`).

    Returns:
        A new dict holding only the allowlisted keys (exact names + `LC_*`).
    """
    return {
        key: value
        for key, value in source.items()
        if key in _ENV_ALLOWLIST or key.startswith(_ENV_ALLOWLIST_PREFIXES)
    }


@dataclass(frozen=True)
class RLimits:
    """POSIX resource ceilings a backend imposes on the child (ADR-0042 Increment 2).

    Off by default (opt-in): applied via `preexec_fn` for the direct-exec backends, which
    is not thread-safe between `fork` and `exec` against the multithreaded eval runner
    (ADR-0026). Best-effort — a limit that cannot be lowered is skipped, never a crash.

    Args:
        cpu_seconds: RLIMIT_CPU ceiling (CPU-seconds) — a runaway loop is killed.
        fsize_bytes: RLIMIT_FSIZE ceiling — a command cannot fill the disk.
        pids: The container pid cap (`--pids-limit`) — a fork bomb cannot wedge the host.
    """

    cpu_seconds: int = 300
    fsize_bytes: int = 512 * 1024 * 1024
    pids: int = 1024

    def preexec(self) -> Callable[[], None] | None:
        """Return a child-setup callable that lowers CPU/FSIZE limits, or `None` off-POSIX.

        Returns:
            A no-arg callable for `subprocess`' `preexec_fn`, or `None` when `resource` is
            unavailable (non-POSIX).
        """
        if _resource is None:
            return None
        wants = (
            (_resource.RLIMIT_CPU, self.cpu_seconds),
            (_resource.RLIMIT_FSIZE, self.fsize_bytes),
        )

        def _apply() -> None:
            for res, want in wants:
                try:  # best-effort: honor a lower existing hard limit, never raise into exec
                    _soft, hard = _resource.getrlimit(res)
                    ceiling = want if hard == _resource.RLIM_INFINITY else min(want, hard)
                    _resource.setrlimit(res, (ceiling, hard))
                except (ValueError, OSError):
                    pass

        return _apply


@dataclass(frozen=True)
class ExecSpec:
    """What to exec and under what environment — the pure output of `Sandbox.prepare`.

    A small spec rather than a bare `(argv, env)` tuple because resource limits can't be
    expressed as env: `preexec_fn` carries POSIX child setup for the backends that use it.
    The `Workspace` executes this; the sandbox never does.

    Args:
        argv: The argument vector to actually exec (identity, or launcher-wrapped).
        env: The complete environment the child runs under (never inherited implicitly).
        preexec_fn: Optional POSIX child setup (resource limits); `None` for the env-only
            backends, which keeps execution thread-safe (see `RLimits`).
    """

    argv: list[str]
    env: dict[str, str]
    preexec_fn: Callable[[], None] | None = None


@runtime_checkable
class Sandbox(Protocol):
    """Transforms a command invocation for hermetic execution (ADR-0042); never runs it."""

    mode: str

    def prepare(self, argv: list[str], cwd: Path) -> ExecSpec:
        """Return the `ExecSpec` the `Workspace` should exec for `argv` under `cwd`.

        Args:
            argv: The command's argument vector (post `shlex.split`).
            cwd: The workspace root the command runs in.

        Returns:
            The transformed `ExecSpec` (argv, environment, optional child setup).
        """
        ...


class NoSandbox:
    """`none`: identity — the full inherited environment, exactly today's behavior (escape hatch)."""

    mode = "none"

    def prepare(self, argv: list[str], cwd: Path) -> ExecSpec:  # noqa: ARG002 — cwd unused (identity)
        """Return `argv` unchanged under the full inherited environment.

        Args:
            argv: The command's argument vector.
            cwd: The working directory (unused — identity backend imposes no confinement).

        Returns:
            An `ExecSpec` with `argv` verbatim and the whole `os.environ`.
        """
        return ExecSpec(argv=list(argv), env=dict(os.environ))


class HermeticEnv:
    """`hermetic-env`: identity argv + an allowlist-scrubbed environment (the portable default).

    Closes the env-injection sub-route of Threat C on every OS with no dependencies. It does
    *not* deny network (no OS mechanism at this layer) — that needs `sandbox-exec`/`bwrap`/
    `container`.

    Args:
        rlimits: Optional resource ceilings applied via `preexec_fn` (opt-in; see `RLimits`).
    """

    mode = "hermetic-env"

    def __init__(self, *, rlimits: RLimits | None = None) -> None:
        self._rlimits = rlimits

    def prepare(self, argv: list[str], cwd: Path) -> ExecSpec:  # noqa: ARG002 — cwd unused (env-only)
        """Return `argv` unchanged under an allowlist-scrubbed environment.

        Args:
            argv: The command's argument vector.
            cwd: The working directory (unused — this backend transforms only the environment).

        Returns:
            An `ExecSpec` with `argv` verbatim and the environment filtered to the allowlist.
        """
        preexec = self._rlimits.preexec() if self._rlimits else None
        return ExecSpec(argv=list(argv), env=hermetic_env(os.environ), preexec_fn=preexec)


class SandboxExec:
    """`sandbox-exec` (macOS): allowlisted env + argv wrapped in the launcher, network denied.

    Increment 1 confines the *network* only; write-confinement is deferred to the container
    backend (Increment 2), which does it cleanly with a read-only rootfs — macOS write
    profiles are finicky and the launcher itself is Apple-deprecated (kept as a native
    fast-path, never the sole backend, ADR-0042).

    Args:
        allow_network: When `True`, skip the wrap and run env-scrubbed only (network open).
        rlimits: Optional resource ceilings applied via `preexec_fn` (opt-in; see `RLimits`).
    """

    mode = "sandbox-exec"
    _LAUNCHER = "/usr/bin/sandbox-exec"

    def __init__(self, *, allow_network: bool = False, rlimits: RLimits | None = None) -> None:
        self._allow_network = allow_network
        self._rlimits = rlimits

    def prepare(self, argv: list[str], cwd: Path) -> ExecSpec:  # noqa: ARG002 — cwd unused (net-deny only)
        """Return the allowlisted env with `argv` wrapped in the launcher (unless network is allowed).

        Args:
            argv: The command's argument vector.
            cwd: The working directory (unused — Increment 1 confines network, not writes).

        Returns:
            An `ExecSpec` under the scrubbed environment: `argv` verbatim when network is allowed,
            else wrapped in `sandbox-exec` with a network-deny profile.
        """
        env = hermetic_env(os.environ)
        preexec = self._rlimits.preexec() if self._rlimits else None
        if self._allow_network:
            return ExecSpec(argv=list(argv), env=env, preexec_fn=preexec)
        # allow-by-default keeps the toolchain readable; deny only network egress.
        profile = "(version 1)(allow default)(deny network*)"
        wrapped = [self._LAUNCHER, "-p", profile, *argv]
        return ExecSpec(argv=wrapped, env=env, preexec_fn=preexec)


class Bwrap:
    """`bwrap` (Linux): a bubblewrap sandbox — read-only root, writable workspace, no network.

    The Linux-native strong backend (ADR-0042 Increment 2). Binds the whole root read-only,
    re-binds the workspace writable, mounts a fresh `/tmp`, and (unless network is allowed)
    unshares the network namespace. The env is allowlist-scrubbed via the child's inherited
    environment (no `--clearenv` needed — `subprocess` sets it).

    Args:
        allow_network: When `True`, keep the network namespace (no `--unshare-net`).
        rlimits: Optional resource ceilings applied via `preexec_fn` (opt-in; see `RLimits`).
    """

    mode = "bwrap"
    _LAUNCHER = "bwrap"

    def __init__(self, *, allow_network: bool = False, rlimits: RLimits | None = None) -> None:
        self._allow_network = allow_network
        self._rlimits = rlimits

    def prepare(self, argv: list[str], cwd: Path) -> ExecSpec:
        """Wrap `argv` in bubblewrap with `cwd` the only writable path.

        Args:
            argv: The command's argument vector.
            cwd: The workspace root, re-bound writable inside the otherwise read-only sandbox.

        Returns:
            An `ExecSpec` whose argv is the `bwrap` invocation, under the scrubbed environment.
        """
        root = str(cwd)
        net = [] if self._allow_network else ["--unshare-net"]
        wrapped = [
            self._LAUNCHER,
            "--die-with-parent",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",  # noqa: S108 — a guest mount point inside the sandbox, not host temp
            "--bind", root, root,
            "--chdir", root,
            *net,
            *argv,
        ]
        preexec = self._rlimits.preexec() if self._rlimits else None
        return ExecSpec(argv=wrapped, env=hermetic_env(os.environ), preexec_fn=preexec)


class Container:
    """`container`: a Podman/Docker run — the cross-platform strong sandbox (ADR-0042 Increment 2).

    Net-denied by default, a read-only rootfs with the workspace bind-mounted writable at
    `/workspace`, a fresh tmpfs `/tmp`, and a kernel-enforced pid cap. The guest env is the
    image's own toolchain plus only the portable vars forwarded via `-e` — the host `PATH`/
    `VIRTUAL_ENV` are meaningless inside and are not passed. Needs the runtime on `PATH` and
    an image carrying the task's toolchain.

    Args:
        image: The container image (e.g. `docker.io/library/python:3.12-slim`).
        runtime: The container CLI (`podman` or `docker`).
        allow_network: When `True`, use `--network bridge` instead of `--network none`.
        rlimits: Resource ceilings; the pid cap is always applied, CPU/FSIZE are guest-managed.
    """

    mode = "container"

    def __init__(
        self,
        *,
        image: str,
        runtime: str = "podman",
        allow_network: bool = False,
        rlimits: RLimits | None = None,
    ) -> None:
        self._image = image
        self._runtime = runtime
        self._allow_network = allow_network
        self._rlimits = rlimits or RLimits()

    def prepare(self, argv: list[str], cwd: Path) -> ExecSpec:
        """Wrap `argv` in a `podman run` (or `docker run`) with the workspace bind-mounted.

        Args:
            argv: The command's argument vector, run as the container's command.
            cwd: The workspace root, bind-mounted read-write at `/workspace`.

        Returns:
            An `ExecSpec` whose argv is the container-runtime invocation; its own env is the
            host environment (so the runtime CLI resolves), while the guest env is set via `-e`.
        """
        network = "bridge" if self._allow_network else "none"
        env_flags: list[str] = []
        for key, value in os.environ.items():
            if key in _CONTAINER_ENV_FORWARD or key.startswith(_ENV_ALLOWLIST_PREFIXES):
                env_flags += ["-e", f"{key}={value}"]
        run = [
            self._runtime,
            "run",
            "--rm",
            "-i",
            "--network", network,
            "--read-only",
            "--tmpfs", "/tmp",  # noqa: S108 — a guest mount point inside the container, not host temp
            "--pids-limit", str(self._rlimits.pids),
            "-v", f"{cwd}:/workspace:rw",
            "-w", "/workspace",
            *env_flags,
            self._image,
            *argv,
        ]
        # The outer process is the runtime CLI (needs the host env to resolve); isolation is
        # the container's job, not the outer env's, so no preexec here.
        return ExecSpec(argv=run, env=dict(os.environ))


def make_sandbox(
    mode: str,
    *,
    allow_network: bool = False,
    rlimits: RLimits | None = None,
    image: str = "",
    runtime: str = "podman",
) -> Sandbox:
    """Construct the `Sandbox` for a config `sandbox_mode` (ADR-0042).

    Args:
        mode: One of `none` / `hermetic-env` / `sandbox-exec` / `bwrap` / `container`.
        allow_network: Passed to the OS/container backends; ignored by `none`/`hermetic-env`,
            which cannot gate network at their layer.
        rlimits: Optional resource ceilings (opt-in; ignored by `none`).
        image: The container image — required when `mode == "container"`.
        runtime: The container CLI for `mode == "container"` (`podman` or `docker`).

    Returns:
        The matching `Sandbox`.

    Raises:
        ValueError: For an unknown mode, or `container` without an image.
    """
    if mode == "none":
        return NoSandbox()
    if mode == "hermetic-env":
        return HermeticEnv(rlimits=rlimits)
    if mode == "sandbox-exec":
        return SandboxExec(allow_network=allow_network, rlimits=rlimits)
    if mode == "bwrap":
        return Bwrap(allow_network=allow_network, rlimits=rlimits)
    if mode == "container":
        if not image:
            raise ValueError("sandbox_mode='container' requires an image (AVATAR_SANDBOX_IMAGE)")
        return Container(image=image, runtime=runtime, allow_network=allow_network, rlimits=rlimits)
    raise ValueError(f"unknown sandbox_mode: {mode!r}")
