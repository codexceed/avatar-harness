# ADR 0007 — Dynamic, no-dependency verification-plan resolution

- **Status:** Accepted — implemented 2026-06-11 (maintainer call)
- **Date:** 2026-06-11
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8) — design discussion 2026-06-11, prompted by a dogfood crash: an `edit` run in an env without `ruff` died with an unhandled `FileNotFoundError` from the verifier's `ruff check`, surfacing that the static `test_command`/`lint_command` defaults assume a Python/ruff/pytest toolchain the harness has no right to assume.
- **Related:** `HARNESS_DESIGN.md` §5 (no self-certification), §12 (task kinds as verification contracts), §15 (pinned baseline); ADR-0006 (git-independent project scope — **same convention-first philosophy, verification axis**); `DECISIONS.md` (verifier is harness-owned, not a tool).

## Context

The verifier runs `config.test_command` / `config.lint_command` *itself* (§5/§12) — harness-owned, never model-mediated — and the §12 pass criteria require at least one of those checks to **actually pass** as the positive external signal. But the defaults are `pytest -q` / `ruff check`: a single ecosystem's toolchain, hard-coded into a harness whose whole point is to wrap an LLM working on **arbitrary** repos. Three failures follow:

1. **Wrong tool for the repo.** A JS/Go/Rust/Make repo has no `ruff`/`pytest`; the configured command is missing or meaningless.
2. **Crash, not signal.** A missing binary raised `FileNotFoundError` straight through the verifier (the runtime is supposed to *surface* failures, never raise into the loop).
3. **No path to a per-repo contract.** The only knob is a global string in config; there is no mechanism to learn what *this* repo considers "passing."

The tempting fix — "make an LLM call to decide the verification steps, then freeze them" — is half right. Freezing early stops the model from *moving the goalposts later*, but if the model **authors** the rubric it can simply set it low up front (`lint_command="true"`, the one test it knows passes). Freeze caches a compromised rubric; it does not fix authorship. The key realization: **most repos already declare their verification contract, machine-readably** — Makefile targets, `package.json` scripts, `pyproject/tox/nox`, `Cargo.toml`, `go.mod`, `.pre-commit-config.yaml`, and above all **CI configs** (`.github/workflows`, `.gitlab-ci.yml`, …), which *are* the gate the project already trusts. The model's job is to **discover** that contract, not invent one.

## Decision (proposed)

Replace the static `test_command`/`lint_command` defaults with a per-session **verification plan**, resolved once and frozen, by a new harness-owned collaborator (the **resolver** / `VerificationPlanner`). Resolution order — the ADR-0006 pattern on the verification axis:

1. **Config override (always wins).** An explicit declared file (`avatar.toml` / `.avatar/verify.*`) or `AVATAR_TEST_COMMAND`/`AVATAR_LINT_COMMAND`. The user's stated contract is never overridden.
2. **Deterministic detection (no-dependency, language-agnostic).** Read repo artifacts — Makefile, `package.json`, `pyproject/tox/nox`, `Cargo.toml`, `go.mod`, `.pre-commit-config.yaml`, CI workflows — and extract their declared test/lint invocations. No LLM, no Python assumption; just text. **CI-derived commands rank above arbitrary Makefile targets** (least gameable — projects actually gate merges on CI).
3. **LLM fallback (evidence-grounded only).** When detection is ambiguous, the model may *propose* a command, but only **citing the artifact** it came from; the harness validates the citation (the script/target actually exists) before accepting. A proposal with no provenance is rejected.

Three concerns kept distinct (the invariant §5 survives only if they are):

| Concern | Owner | Note |
| --- | --- | --- |
| **Discovery** — propose candidate commands | detector → LLM fallback | side-stepping-prone, so it only ever *proposes* |
| **Commitment** — freeze the plan for the session | a **control gate**: human ack (interactive) / policy (autonomous) | "freeze" = authority transfer *away* from the model, not a cache |
| **Execution + judgment** — run the frozen commands, read real exit codes, apply §12 | the `Verifier`, unchanged | model picks *which* command, never forges its result |

Mechanics, on the existing machine:
- Resolve during the **`investigating`** phase; freeze onto `TaskState` (immutable; source of truth, invariant #1) **before** the `investigating → editing` transition. The phase boundary is the freeze point.
- **Journal the frozen plan** (each command + its provenance) as an event — every run becomes auditable: *what rubric graded this, and where did each check come from?*
- The `Verifier` gains **zero** language knowledge; it stays a pure executor over the frozen plan. All per-repo intelligence lives in the swappable resolver.

Prerequisite robustness (independent of this ADR, needed regardless): `Workspace.run` handles a missing binary gracefully (no crash), and verification commands invoke via `python -m <tool>` where applicable so an installed-but-not-on-PATH tool still resolves.

## Alternatives considered

- **Static per-ecosystem defaults (status quo):** rejected — Python-biased, crashes on a missing binary, no per-repo contract.
- **LLM-first ("model decides the steps"):** rejected as the *primary* path — the model authoring its own rubric is the self-grading hole §5 exists to close; freezing caches it rather than fixing it. LLM is retained only as an evidence-grounded *fallback*.
- **Bundle `ruff`/`pytest` as runtime deps:** rejected (prior discussion) — fixes one default, not the class; a bundled `pytest` runner can't run the target repo's tests without the repo's own environment; bundled tool/version skew yields a *false* signal; imposes heavy deps on library consumers.
- **Pure detection, no override:** rejected — some repos need an explicit human-declared contract; the override tier is non-negotiable.

## Consequences

- The verifier becomes genuinely no-dependency and language-agnostic: it runs a frozen list of `(name, command, kind, provenance)` and applies §12 — nothing more.
- Runs gain an **auditable, journaled rubric**; the model can choose *which* existing check, never forge a result.
- **Reproducibility caveat:** deterministic detection is reproducible; an LLM-resolved ambiguous case may differ run-to-run. Mitigated by journaling the frozen plan and preferring detection.
- **Open question — greenfield / no-contract repos** (e.g. the chatbot demo): nothing to discover, yet an `edit` still needs a positive signal. Requires a **universal minimal signal** that isn't Python-specific ("the changed files parse/build"), or, in interactive mode, "diff present + no secrets + human ack". This is the case the old Python default was silently papering over, and it is the hardest part — left open here. *Disposition (2026-06-11 implementation): deliberately not invented. When nothing resolves, the harness freezes an **empty plan** and verification fails legibly ("no verification contract discovered — declare one via `AVATAR_TEST_COMMAND` / `AVATAR_LINT_COMMAND`"), keeping the existing non-command structural guards (diff present, no secrets). The universal minimal signal remains future work.* **Superseded 2026-06-14 by [ADR-0014](0014-greenfield-self-authored-verification.md):** the greenfield floor is a model-**authored**, harness-**executed** smoke check — a fourth tier beneath override/detection/cited-LLM, resolved at verification time rather than at the phase boundary. The rest of this ADR stands.
- **Open risk — vacuous checks:** a no-op `test:` target or `echo ok` CI step passes (exit 0) and gives a false positive; §12 does not catch a check that passes vacuously. Ranking CI above arbitrary Makefile targets reduces but does not eliminate this.
- **Open risk — mid-run artifact tampering (recorded 2026-06-11 as a follow-up, not fixed here):** the freeze fixes the command *text*, not the artifact behind it. A frozen `make test` still dereferences the Makefile at verification time, and the Makefile is an ordinary editable file — a model could rewrite the `test:` target body mid-run and the frozen plan would faithfully execute the gutted check. The same shape as the vacuous-check risk, but model-inducible. Candidate mitigations for the follow-up: hash/pin the resolved provenance artifacts at freeze time and fail verification on drift, or deny `apply_patch`/`write_file` against the plan's provenance files for the session.
- Supersedes the static `test_command`/`lint_command` contract in §12; those config keys survive as the **override tier**, not the default.
- Trigger to implement: ~~deferred per Principle C until the first non-Python dogfood/eval target~~ — **implemented 2026-06-11 by maintainer call** (pulled forward rather than waiting for the non-Python target; the dogfood `FileNotFoundError` crash made the floor urgent and the resolver landed with it). Shipped: the no-crash + `python -m` floor, `VerificationPlanner` (config override → deterministic detection → citation-validated LLM fallback), freeze-at-phase-boundary onto `TaskState`, the typed `verification_plan_frozen` journal event, and the verifier as pure plan executor. Commitment is autonomous-policy only (highest-ranked resolution auto-freezes, journaled); the interactive human-ack gate remains a documented seam.
