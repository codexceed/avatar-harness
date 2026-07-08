"""Hermetic execution at the command seam (ADR-0042).

Every command the harness runs — the verifier's frozen checks *and* the model's
`run_command` — funnels through `Workspace._run_unlogged`. A `Sandbox` transforms
that one call: it computes the environment the child runs under and (in the OS
backends) wraps the argv in a launcher that denies network. It never *runs* the
command — the `Workspace` still does — so the seam stays a single chokepoint and
`prepare()` is a pure transform (design invariant #5).

Scope is **Threat C only** (ADR-0042): it closes runtime/substrate gaming — an
inherited `PYTEST_ADDOPTS`/`PYTHONPATH`, a phone-home — not a model that authors
weak tests (Threat B) or guts the graded ones (Threat A). Workspace-*discovered*
config (`conftest.py`, `pyproject` addopts) is a file, not env, and is out of
scope here by decision (the adequacy/diff-scope lever, ADR-0040).

Backends, weakest→strongest, all behind the same `prepare()`:

- `none` — identity argv + the full inherited environment (today's behavior; the
  back-compat escape hatch).
- `hermetic-env` — identity argv + an env scrubbed to a language-neutral allowlist,
  so `PYTEST_ADDOPTS`/`NODE_OPTIONS`/`RUBYOPT`/`PYTHONPATH`/`CLASSPATH` vanish *by
  construction* (no per-language whack-a-mole). The portable floor: pure Python,
  every OS, no dependencies. **The default.**
- `sandbox-exec` — the allowlisted env + argv wrapped in the macOS launcher with a
  network-deny profile (Apple-deprecated, an optional native fast-path).

`bwrap` and `container` (ADR-0042 Increment 2) are not built yet; requesting them
raises rather than silently degrading.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

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
class ExecSpec:
    """What to exec and under what environment — the pure output of `Sandbox.prepare`.

    A small spec rather than a bare `(argv, env)` tuple because resource limits can't be
    expressed as env: `preexec_fn` carries POSIX child setup for the backends (and the
    future container mode) that use it. The `Workspace` executes this; the sandbox never does.

    Args:
        argv: The argument vector to actually exec (identity, or launcher-wrapped).
        env: The complete environment the child runs under (never inherited implicitly).
        preexec_fn: Optional POSIX child setup (resource limits); `None` for the env-only
            backends, which keeps execution thread-safe (the eval runner is multithreaded,
            ADR-0026, and `preexec_fn` between fork and exec is not thread-safe).
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
    *not* deny network (no OS mechanism at this layer) — that needs `sandbox-exec`/`container`.
    """

    mode = "hermetic-env"

    def prepare(self, argv: list[str], cwd: Path) -> ExecSpec:  # noqa: ARG002 — cwd unused (env-only)
        """Return `argv` unchanged under an allowlist-scrubbed environment.

        Args:
            argv: The command's argument vector.
            cwd: The working directory (unused — this backend transforms only the environment).

        Returns:
            An `ExecSpec` with `argv` verbatim and the environment filtered to the allowlist.
        """
        return ExecSpec(argv=list(argv), env=hermetic_env(os.environ))


class SandboxExec:
    """`sandbox-exec` (macOS): allowlisted env + argv wrapped in the launcher, network denied.

    Increment 1 confines the *network* only; write-confinement is deferred to the container
    backend (Increment 2), which does it cleanly with a read-only rootfs — macOS write
    profiles are finicky and the launcher itself is Apple-deprecated (kept as a native
    fast-path, never the sole backend, ADR-0042).

    Args:
        allow_network: When `True`, skip the wrap and run env-scrubbed only (network open).
    """

    mode = "sandbox-exec"
    _LAUNCHER = "/usr/bin/sandbox-exec"

    def __init__(self, *, allow_network: bool = False) -> None:
        self._allow_network = allow_network

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
        if self._allow_network:
            return ExecSpec(argv=list(argv), env=env)
        # allow-by-default keeps the toolchain readable; deny only network egress.
        profile = "(version 1)(allow default)(deny network*)"
        wrapped = [self._LAUNCHER, "-p", profile, *argv]
        return ExecSpec(argv=wrapped, env=env)


def make_sandbox(mode: str, *, allow_network: bool = False) -> Sandbox:
    """Construct the `Sandbox` for a config `sandbox_mode` (ADR-0042).

    Args:
        mode: One of `none` / `hermetic-env` / `sandbox-exec` (`bwrap` / `container` are
            reserved for Increment 2 and raise here rather than silently degrading).
        allow_network: Passed to the OS backends; ignored by the env-only backends, which
            cannot gate network at their layer.

    Returns:
        The matching `Sandbox`.

    Raises:
        NotImplementedError: For a valid-but-unbuilt backend (`bwrap` / `container`).
        ValueError: For an unknown mode.
    """
    if mode == "none":
        return NoSandbox()
    if mode == "hermetic-env":
        return HermeticEnv()
    if mode == "sandbox-exec":
        return SandboxExec(allow_network=allow_network)
    if mode in ("bwrap", "container"):
        raise NotImplementedError(f"sandbox_mode={mode!r} is ADR-0042 Increment 2 (not built yet)")
    raise ValueError(f"unknown sandbox_mode: {mode!r}")
