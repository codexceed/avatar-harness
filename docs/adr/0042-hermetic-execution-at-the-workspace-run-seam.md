# ADR 0042 — Hermetic execution at the `Workspace.run` seam (Threat C)

- **Status:** Proposed
- **Date:** 2026-07-09
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8) — 2026-07-08/09 design session.
- **Implements:** [ADR-0009](0009-execution-sandbox-isolation.md) (execution sandbox isolation — accepted, deferred; `Workspace` as the seam). This ADR is the concrete build, staged.
- **Related:** [ADR-0011](0011-verifier-integrity-under-self-improvement.md) (verifier integrity — the D3 held-out oracle is the Threat A/B lever); [ADR-0027](0027-sandboxed-execution-trust-and-self-verification-calibration.md) (capability ≠ authority); [ADR-0038](0038-model-declared-semi-frozen-verification-contract.md) (the frozen contract this executes); [ADR-0040](0040-held-out-verified-vs-self-reported-success.md) (held-out oracle — deferred; the only fix for the classes this ADR scopes out). Seam: `avatar/workspace.py`.
- **Scope:** Threat C (runtime/substrate gaming) only. **Out of scope:** the ADR-0040 held-out oracle; test-adequacy (coverage/mutation) — a separate follow-on.

## Context

The verifier runs the frozen contract itself and grades on the real exit code — the model never self-certifies (design invariant #3). But **the model owns the entire workspace the contract runs in.** It can plant a `conftest.py`, a `pyproject` `addopts` line, a `sitecustomize.py`, or lean on inherited env (`PYTEST_ADDOPTS`, `PYTHONPATH`) to make an honest-looking command pass for dishonest reasons. Nothing stops that today: `Workspace._run_unlogged` shells out with the full inherited environment, in the model's own workspace, with the network open.

This matters most where **jo runs unattended**: no human approves, and (by decision) the ADR-0040 held-out oracle is deferred — so the pass signal is entirely inside the model's reach. We cannot make that *trustworthy* here, only *expensive to game*. This change buys the cheapest, highest-leverage slice: the execution *substrate* can no longer be rigged.

ADR-0009 already decided *that* isolation slots in behind `Workspace.run` and named it deferred-not-designed-out. This ADR decides *how* and stages the build.

> **Honest ceiling.** A sandbox does **not** stop a model from writing weak tests or gutting existing ones — the graded files live in the mounted workspace by necessity. It closes the *substrate* class only. Test-adequacy is the separate, larger lever (see *Consequences* and ADR-0040).

## Threat model

The contract text is frozen and amendment is human-gated (ADR-0038). These are the routes that don't touch the contract at all:

| Threat | Attack | This sandbox |
| --- | --- | --- |
| **A** — edit the graded tests | Weaken/delete the pre-existing tests a detected contract runs; empty them so `pytest` collects nothing (exit-5 → tolerated skip). | ❌ no cover |
| **B** — author weak tests | Declare an executing contract whose tests assert almost nothing, or teach the code to the test. | ❌ no cover |
| **C** — rig the runtime | `conftest.py`/`sitecustomize.py`, inherited `PYTEST_ADDOPTS`/`PYTHONPATH`, `PATH` shadowing, phone-home. | ✅ **closed** |

A and B need test-adequacy or a held-out oracle (ADR-0040). **C is substrate — that's what execution isolation is for, and it's the only route with no other defense today.**

## Decision (proposed)

Introduce one small `Sandbox` abstraction on `Workspace`, injected via `RunDeps` like every other collaborator, that transforms the command call without moving it — verifier, tools, and runner stay untouched (design invariant #5, "everything reversible and observable"). Every command — the verifier's frozen checks **and** the model's `run_command` — already funnels through `Workspace._run_unlogged` (the single command chokepoint, design invariant #2); the sandbox wraps exactly that one call.

**1. `prepare()` is a pure transform returning an `ExecSpec`.** It never runs the command (the `Workspace` still does). It owns three decisions — compute the env (allowlist filter + hardening vars), wrap the argv (identity, or a sandbox/container launcher encoding net-deny + write-confine), and supply child setup for resource limits. Because rlimits can't be expressed as env, it returns a small spec rather than a bare `(argv, env)` tuple:

```python
@dataclass(frozen=True)
class ExecSpec:
    argv: list[str]                      # what to actually exec (maybe sandbox-wrapped)
    env: dict[str, str]                  # the environment to run under
    preexec_fn: Callable | None = None   # POSIX child setup — resource limits

class Sandbox(Protocol):
    def prepare(self, argv: list[str], cwd: Path) -> ExecSpec: ...

# the seam (Workspace._run_unlogged) becomes:
spec = self.sandbox.prepare(argv, self.root)
proc = subprocess.run(spec.argv, cwd=str(self.root), env=spec.env,
                      preexec_fn=spec.preexec_fn, ...)
```

**2. The default is `hermetic-env` — an OS-agnostic env allowlist + POSIX rlimits, no dependencies.** The allowlist is the portability trick: pass *only* a safe set, so `PYTEST_ADDOPTS` / `NODE_OPTIONS` / `RUBYOPT` / `PYTHONPATH` / `CLASSPATH` all vanish *by construction* (no per-language whack-a-mole). This is the generic floor that ships now.

**3. Stronger backends sit behind the same interface, added without seam changes.** FS/network isolation is OS-enforced, so it stages above the floor:

| Mode | argv | env | Adds over previous |
| --- | --- | --- | --- |
| `none` | unchanged | `os.environ` | today's behavior (back-compat escape hatch) |
| **`hermetic-env`** | unchanged | allowlist | **default.** Strips env injection for every language; + POSIX rlimits |
| `sandbox-exec` | wrapped | allowlist | + network-deny + write-confine (macOS native) |
| `bwrap` | wrapped | allowlist | same isolation, Linux native |
| `container` | wrapped | clean | full isolation + a real `/workspace` (generic, cross-platform) |

`sandbox-exec` is pragmatic for the current dev machine but is Apple-deprecated and must never be the *only* backend. A **container runtime** (Podman/Docker) is the one genuinely cross-platform strong sandbox — net-deny, read-only rootfs, a real `/workspace` — with the runtime dependency + per-run latency as its cost; `sandbox-exec`/`bwrap` are optional native fast-paths.

**4. Config** (`avatar/config.py`):

```python
sandbox_mode: Literal["none", "hermetic-env", "sandbox-exec", "bwrap", "container"] = "hermetic-env"
sandbox_allow_network: bool = False
```

**5. The one real implementation risk is gated on a green suite.** Scrubbing the environment is safe *except* that our own verification commands (`python -m pytest`, `py_compile`) must still resolve to the `uv`-managed interpreter. The allowlist therefore **keeps** `PATH`, `HOME`, `LANG`, `LC_*`, `TERM`, `TMPDIR`, `TZ`, **`VIRTUAL_ENV`** and **drops** everything else (`PYTEST_ADDOPTS`, `PYTHONPATH`, `PYTHONSTARTUP`, `NODE_OPTIONS`, `RUBYOPT`, `*_OPTS`, …). **Acceptance gate:** the full suite must stay green with `hermetic-env` as the default; if tuning the allowlist against `uv`/venv resolution proves fiddly, ship it **opt-in** (`none` stays default) until validated — the isolation is worthless if it breaks legitimate verification.

### Staging

1. **Increment 1 (now):** the `Sandbox` seam + `hermetic-env` (allowlist + rlimits) + macOS `sandbox-exec`. Covers Threat C on the current platform. Default flip gated on a green suite.
2. **Increment 2 (same interface):** a container backend (Podman) as the generic cross-platform mode, plus `bwrap` for Linux CI. No seam changes — new `Sandbox` implementations only.
3. **Follow-on (separate ADR):** test-adequacy — coverage-over-diff, then mutation. The real lever for Threats A/B and the greenfield-unattended cell.

## Effects, per run mode

The sandbox wraps every `ws.run` uniformly, but its **value** and the **residual exposure** differ by mode:

| Mode | Contract | Sandbox value | Residual |
| --- | --- | --- | --- |
| **Non-greenfield · unattended** | detected suite | **high** — runs env-scrubbed + net-denied; no human would catch a planted `conftest` | ⚠️ **A open** — model can still edit the detected tests; exit-5 laundering |
| **Greenfield · attended** | model declares (ADR-0038) | real — a human can't eyeball a subtle env rig | ✅ best defended; B partly caught by the watching human |
| **Greenfield · unattended** | model declares, self-grades | **load-bearing** — model authors code + tests + contract and grades itself unwatched; the sandbox is the only thing stopping a rigged env | 🔴 **highest risk** — C closed, A/B wide open, no backstop |

The sandbox raises the floor everywhere, but on its own it converts "trivially gameable" into "trustworthy" in **zero** of these cells. It is a necessary first course, not the meal.

## Alternatives considered

| Option | Verdict |
| --- | --- |
| **Do nothing; rely on the frozen contract + amendment gate** | Rejected — the contract text being frozen is orthogonal to the substrate it runs in; Threat C never touches the contract. |
| **macOS `sandbox-exec` as the primary backend** | Rejected as *primary* — Apple-deprecated and non-portable. Kept as an optional native fast-path behind the interface. |
| **Container-only (Podman/Docker) from day one** | Rejected for Increment 1 — a hard runtime dependency + per-run latency for a capability we can approximate on every OS with a pure-Python allowlist. Staged as the generic-strong mode (Increment 2). |
| **Per-language runtime flags (`PYTHONSAFEPATH`, `NODE_OPTIONS=--disable-proto`, …) instead of an allowlist** | Rejected as the foundation — per-language whack-a-mole that misses the next language. The allowlist strips injection *by construction*; runtime flags are a bonus on top. |
| **Return a bare `(argv, env)` tuple from `prepare()`** | Rejected — resource limits can't be expressed as env; `ExecSpec` carries `preexec_fn` too, and stays extensible without churning callers. |
| **Isolate only the verifier's checks, not the model's `run_command`** | Rejected — both funnel through the same seam; a split would let the model rig state in an un-sandboxed `run_command` that a later sandboxed check inherits. Uniform wrapping is simpler and tighter. |

## Implementation notes (Increment 1, 2026-07-09)

Two refinements surfaced at build time and narrow Increment 1 without changing the decision:

- **rlimits ship gated *off* (`preexec_fn = None`).** `preexec_fn` runs between `fork` and `exec`; the eval runner is multithreaded (ADR-0026), where a child touching a lock another thread held at fork can deadlock. The load-bearing, thread-safe half — the env allowlist, applied via `env=` — ships on by default; POSIX rlimits become a follow-on toggle rather than a hazard bundled into the default. `ExecSpec` still carries `preexec_fn` so the container backend and an opt-in rlimit mode slot in without a seam change.
- **`sandbox-exec` ships network-deny only.** Write-confinement via a macOS `sandbox-exec` profile is finicky (and the launcher is Apple-deprecated); it is deferred to the **container** backend (Increment 2), which confines writes cleanly with a read-only rootfs. The env allowlist already removes the env-injection route on macOS; `sandbox-exec` adds egress-deny on top.
- **The `Sandbox` is a `Workspace` constructor collaborator, not a `RunDeps` one.** The seam (`_run_unlogged`) is a `Workspace` method and `Workspace` must not depend on `RunDeps`; the default `NoSandbox` keeps a bare `Workspace(root)` (and every read-only inspection site) at today's behavior, while `harness.py` injects `make_sandbox(config.sandbox_mode)`. **Acceptance gate met:** the full suite is green with `hermetic-env` as the shipped default (652 passed).

## Implementation notes (Increment 2, 2026-07-09)

- **`bwrap` and `container` shipped, behind the same `prepare()`.** `Bwrap` (Linux) binds the whole root read-only, re-binds the workspace writable, mounts a fresh `/tmp`, and unshares the network namespace unless `sandbox_allow_network`. `Container` (Podman/Docker) runs `--network none --read-only --tmpfs /tmp --pids-limit N -v {cwd}:/workspace:rw`, forwarding only portable guest env (never the host `PATH`/`VIRTUAL_ENV`); it requires `sandbox_image` (a mode-with-no-image is a config error, not a silent degrade). No seam change — new `Sandbox` implementations only. Both are **shape-tested** in the suite; end-to-end execution is gated on `bwrap`/`podman` availability, so on the macOS dev box only the argv/env construction is exercised — the guest isolation itself is unverified there and needs a Linux/CI run.
- **The rlimit toggle shipped (`sandbox_rlimits`, opt-in, default off).** `RLimits` rides `ExecSpec.preexec_fn` for the direct-exec backends (CPU + FSIZE, best-effort) and `--pids-limit` for the container backend (kernel-enforced, always on there). Still off by default for the thread-safety reason above; a Linux single-process consumer can enable it.
- **The exit-5 laundering fix (Threat A, below) landed alongside** — a small, adjacent verifier change (662 passed).

## Consequences

- Threat **C is closed** at a single seam; verifier, tools, and runner are unchanged — only the `Workspace` backend gains a collaborator. `none` reproduces today's behavior exactly (back-compat escape hatch).
- **This ADR does not solve Threats A or B.** They are recorded here so the boundary is explicit; the interim menu and the durable fix live below.
- **Threat A — interim mitigations** (weakening the *graded* tests; a non-greenfield concern — it needs pre-existing tests to weaken, so it does *not* arise in jo's dominant greenfield-unattended cell, where the risk is B). The clean fix is the ADR-0040 held-out oracle; everything local raises cost rather than proving intent, because you cannot distinguish a legitimate test update from a weakening without an independent spec. Ranked:
  1. **Close the exit-5 laundering** — **DONE (2026-07-09).** On an edit task a `kind="test"` check that collects zero tests became a tolerated *skip* (`verifier.py` `_ALLOWED_SKIPS`), so emptying the tests hid a failure. Fixed: `Workspace.baseline_paths()` reads the pinned-baseline tree; if the baseline *had* tests (`_is_test_path`) but the frozen check now collects nothing, the verifier returns **fail, not skip** ("suppression, not absence"). A genuinely test-less repo still skips; declared/`kind!="test"` checks were never affected (they already fail on exit-5). Conservative and structural — it only distinguishes absence from suppression, not weak-vs-strong tests (that is B, and the held-out oracle).
  2. **Diff-scope guard** *(universal policy).* When a pass depended on the diff touching the test/discovery files the contract runs (test files, `conftest`, `pyproject [tool.pytest]`), treat it as suspect: attended → surface it (a diff callout, like the amendment gate but for the substrate); unattended → count the pass as **unverified**.
  3. **Pinned-baseline regression check** *(strong, scoped).* The `Workspace` already pins HEAD; run the *pristine* baseline tests against the model's source diff as a required check. Holds for behavior-preserving edits; a task that intentionally changes behavior is expected to change old tests, so it doesn't apply there.
  4. **Coverage / mutation** *(structural).* Weakened tests stop covering the diff (coverage) or fail to kill mutants (mutation). Catches A *and* B; folds into the adequacy follow-on.
- **For jo specifically, B outranks A** — greenfield has nothing pre-existing to weaken. The exit-5 fix and the diff-scope guard are small and belong with (or just after) Increment 1; pinned-baseline and coverage/mutation fold into the adequacy work.
- **Workspace-discovered config** (`conftest.py`, `pyproject` `addopts`, `package.json` scripts) is legitimately in the workspace; only adequacy or the diff-scope suspicion touches it — the sandbox scrubs the *env*, not the tree.
- The **ADR-0040 held-out oracle remains the only mechanism** that makes the greenfield-unattended cell genuinely trustworthy; it is out of scope here by decision.

> **The invariant to hold onto:** you cannot fully verify a party that controls the entire signal. The honest goal for unattended jo is to make gaming *cost nearly as much as doing the task* — hermetic execution removes the cheapest shortcut; adequacy checking removes the next.
