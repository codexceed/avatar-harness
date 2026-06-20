# ADR 0027 — Sandboxed execution trust + self-verification calibration (Eval-0)

- **Status:** Proposed — **R3** (bounded command output) implemented 2026-06-20; **R1 (trusted auto-approve) is BLOCKED on R4 (a real isolation boundary)** — see Decision
- **Date:** 2026-06-20
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0004 (eval harness), ADR-0014 (greenfield smoke floor — unattended *non-executing* allowlist), ADR-0020 (guard probes), ADR-0022 (kind-aware prompt lever), ADR-0024/0025 (improvement loop + `failure_mode`), §13 (permission gate), §23.5 (verification authority). Evidence: catalog **C3** + proposal `CP-edit-run-artifact-before-done`.

## Context — the eval forbids the capability it measures

```
 jo-cli (real user)              Eval-0 (unattended=True)
 ─────────────────               ────────────────────────
 run_command("python app.py")    run_command("python app.py")
        │                                │
   permission gate                  permission gate
        │                                │
   HUMAN approves ✔                 AUTO-DENY ✘  (tier-3, no human)
        │                                │
   program RUNS                     model falls back to `py_compile`
   (functional check)               (syntactic check)  ──►  C3
```

The gate's threat model is *protect the user's machine*, so an unattended run auto-denies tier-3. Net effect: we grade "can it do SWE unsupervised" in a box where *running the program* — the core of functional self-verification — is impossible.

> **⚠️ Correction (review P1 #2): "the disposable scratch repo makes execution safe" is FALSE.** `provision()` disposes a working *directory*; it is **not** a process boundary. `Workspace.run` is `subprocess.run(argv, cwd=root)` (`workspace.py:476`) — **no** container, namespace, seccomp, user, or network isolation. A process launched there can read `~/.ssh/*`, exfiltrate env secrets, `curl` out, or mutate the grader's own assets. So opening execution is **not** free: R1 is **gated on a real isolation layer (R4)**. What ships now is only R3 + the measurement design.

### Capability ≠ authority (why this is safe to relax)

|                         | weak reach (no exec)             | strong reach (executes)        |
| ----------------------- | -------------------------------- | ------------------------------ |
| **untrusted** (gradee)  | —                                | **agent `run_command`**        |
| **trusted** (ext+fixed) | in-loop Verifier (`py_compile`)  | **eval probe** ◄── the grader  |

The probe already occupies *strong + trusted*. So opening execution moves the agent into *strong + untrusted* — where it can fool **itself**, never the **score** (the probe re-checks independently). Execution-trust can only *improve* self-verification; it cannot corrupt the measurement.

**This is a claim about *grading integrity* only** — two safety properties were conflated and must be kept apart:

| property | "can the agent fake the score?" | "can the process harm the host?" |
| --- | --- | --- |
| name | **grading integrity** | **host containment** |
| holds because | the probe re-checks, hidden + external | *requires real isolation* |
| under `cwd=` substrate | ✅ holds (probe is independent) | ❌ **does NOT hold** |
| established by | ADR-0004/0020 (the probe) | **R4** (this ADR) — not yet built |

Execution-trust is safe for **measurement validity**. It says **nothing** about host safety — that is R4's job, and it must exist *before* R1.

## Decision

**Decouple the two levers. Open the permission lever in the sandbox; keep the grading lever (probe) untouched.**

```
 permission lever  ──►  per-task sandbox policy   (NEW; this ADR)
 grading lever     ──►  hidden external probe     (unchanged; ADR-0004/0020)
```

**R1 · `TaskSpec.sandbox` policy** at the permission gate (`before_tool_call`), scoped to the eval `RunDeps`. **Blocked on R4** — `trusted` is only acceptable *inside* a real isolation boundary, never on the bare `cwd=` substrate:

| policy        | command execution | default for           | rationale                                   |
| ------------- | ----------------- | --------------------- | ------------------------------------------- |
| `trusted`     | auto-approve **inside the R4 boundary** | capability tasks | the **container/VM (R4)** is the safety boundary — *not* `provision()` |
| `strict`      | keep tier-3 deny  | `secret-safety`       | exec/denylist **is** the thing under test (no exfil-via-stdout); kept even inside R4 |

> **R1 ships only after R4 exists.** Until then the gate keeps auto-denying tier-3. `trusted` on the current substrate would be a host RCE/exfil hole, not a sandbox.

**R2 · Do NOT widen `_SMOKE_ALLOWED`.** The unattended smoke floor (ADR-0014) stays non-executing — it protects the *shipped product*, a separate concern. Functional verification lives in the agent's *permission-gated* `run_command`, never the harness's frozen floor.

**R3 · Bounded execution output (implemented).** A "run anything" path re-exposes the 875 MB journal blowup, so command output is capped at both boundaries:

| | before | after |
| --- | --- | --- |
| budget | `2000`, hardcoded | `config.command_output_budget = 16_000` (configurable) |
| shape | `text[:budget]` (tail dropped) | **head + tail kept, middle elided** (40 / 60 split) |
| why | drops the trailing exception | a failure's densest signal trails — keep it |

`evals.run` keeps the journal's `ToolEnd.content` at the same excerpt (distillability).

**R4 · A real isolation boundary — the prerequisite for R1 (proposed, future build).** `trusted` is acceptable *only* inside a layer that actually confines the process. Run each eval cell in one of, lightest → strongest:

| layer | isolation | notes |
| --- | --- | --- |
| **rootless container** (Podman/Docker) | namespaces + cgroups; `--network none`, `--read-only` host, dropped caps, non-root user, tmpfs scratch | default; the eval already provisions a throwaway dir to mount |
| **microVM / gVisor** (Firecracker) | separate kernel | stronger; for untrusted-model runs |
| **ephemeral CI runner** | fresh VM per job | the eval already fits a disposable-runner model |

Design: thread an **executor abstraction** through `evals/run.py` so `harness + run_command` run *inside* the sandbox while the **probe runs outside** (trusted grader, unchanged). The boundary is the container/VM — **not** `provision()`. Invariants: **network egress denied by default** for capability tasks (closes the `curl`-exfil channel); host mounts read-only except the scratch tmpfs; `secret-safety` stays `strict` even inside R4 (defense in depth). This is a build item with its own ADR; **no R1 rollout precedes it.**

## Measurement — turn execution into signal (extends ADR-0025)

**M1 · Attempted functional verification** — a trajectory bit: did the agent propose a run-the-artifact command before concluding?

**M2 · `sandbox` as a matrix axis** — run capability tasks under `strict` ⊕ `trusted`; the Δpass-rate *quantifies how much each model's SWE depends on being allowed to execute* — i.e. "minimal-input autonomy", measured.

**M3 · Calibration** = agent self-verdict × probe verdict (the real "trust it unsupervised" metric):

| self-verdict ↓ / probe → | **pass ✔**          | **fail ✗**            |
| ------------------------ | ------------------- | --------------------- |
| **done**                 | calibrated ✓        | **overconfident — C3**|
| **kept going / unsure**  | underconfident (C1) | calibrated (stuck)    |

C3 = `done ✗` cell. The diagonal is a trustworthy agent.

## Consequences

| | |
| --- | --- |
| ✅ | Functional self-verification becomes possible → C3 is *fixable behavior*, not a structural dead-end |
| ✅ | New axes (M1–M3) measure autonomy + calibration, not just pass@1 |
| ✅ | R3 ships now (configurable, head+tail) regardless of R1/R2 timeline |
| ⚠️ | Per-task policy is load-bearing: `secret-safety` MUST stay `strict` (exfil channel) |
| ⚠️ | `trusted` raises wall-clock/token cost (agent runs things) — bounded by R3 + existing budgets |
| ⛔ | **R1 blocked on R4.** The current substrate is `cwd=` only (`workspace.py:476`) — no process isolation. `trusted` without a container/VM is a host exfil/RCE hole, not a sandbox. |
| ⛔ | Self-verdict (M1/M3) is **never** `outcome="success"` — the probe remains sole authority (invariant #3) |

## Alternatives rejected

| option | why not |
| --- | --- |
| Simulate interactive approvals | deterministic ⇒ collapses to R1; LLM-driven ⇒ violates no-LLM-judge (ADR-0004) + cost/variance |
| Widen the smoke-floor allowlist | weakens shipped-product safety (ADR-0014) for an eval-only need |
| Couple to strict/`--auto` verification | drags the weak in-loop verifier back as gate; permission ⟂ grading |
| Treat `provision()`'s scratch dir as the sandbox | `cwd=` confines a *directory*, not a *process* — reads host secrets, exfiltrates, mutates grader assets. Real isolation (R4) is mandatory before R1. |
