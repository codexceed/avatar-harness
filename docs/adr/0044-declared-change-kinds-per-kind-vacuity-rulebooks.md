# ADR 0044 — Declared `change_kinds` select per-kind vacuity rulebooks; the diff audits the declaration

- **Status:** Proposed
- **Date:** 2026-07-10
- **Deciders:** Sarthak Joshi
- **Related:** [ADR-0038](0038-model-declared-semi-frozen-verification-contract.md) (the declared contract + non-vacuity guard this amends), [ADR-0007] (frozen plan), [ADR-0014] (smoke floor), [ADR-0041](0041-retire-test-only-task-kind.md) (`task_kind` selects the contract shape — this is the same cure one level down). Seams: `avatar/planner.py` (the vacuity analyzer), `avatar/tools/verification.py` (`declare_verification`/`alter_verification`), `avatar/runner.py` (`_freeze_plan`), `avatar/verifier.py` (`_verify_edit`), `avatar/state.py`/`avatar/event_types.py` (journaled contract).

## Context

The ADR-0038 non-vacuity guard rejects a declared contract when no check "executes the code" — every `&&`/`;`/`|` stage resolves to a no-op/inspector program (`echo`, `grep`, `test`, …). That definition presumes the deliverable is *runnable*. Dogfood run `tetris_glm/events/8216e26b…jsonl` (goal: "provide me a design spec in markdown") showed the category error end-to-end:

1. The model declared a **legitimate, artifact-anchored doc check** — `test -f DESIGN.md && grep -q '^# ASCII Tetris' DESIGN.md && grep -q 'Acceptance Criteria' DESIGN.md && … && echo 'DESIGN.md OK'`. Every stage's program is in `_VACUOUS_PROGRAMS`, so the guard rejected it: "at least one check must RUN what you build."
2. Two turns later the model **laundered the same content assertions through `python3 -c "…"`** — an unknown-therefore-real program — and the contract was accepted.

Net effect: two burned turns, a transcript that teaches obfuscation, zero integrity gained. For a markdown deliverable there is nothing to run; `grep` against the artifact **is** the strongest external evidence available. The blocklist conflates "read-only inspector" with "proves nothing" — true for code, false for text.

The guard cannot fix this alone because it validates at declaration time, when no diff exists to reveal what kind of change is coming (greenfield by definition). Someone has to say what kind of change this is, *ahead of time*, so the right rulebook applies at the moment of rejection — the cheap, legible feedback point.

## Decision

1. **The model declares `change_kinds: list` alongside its contract.** `declare_verification` / `alter_verification` gain a `change_kinds` field — a list of the kinds of change the contract must validate: `"code"` (functional — behavior lives in executable artifacts) and `"content"` (non-functional textual artifacts — docs, specs). A *list*, not a scalar: a mixed change (feature + README) declares both, and each kind demands its own coverage. Omitted → `["code"]` (backward compatible, fails toward strictness). Empty or unknown kinds → model-correctable rejection.

2. **Per-kind coverage at declaration time.** For **each** declared kind, **at least one** check must satisfy that kind's rulebook (coverage per kind — *not* "every check satisfies every kind", which is incoherent):
   - **`code`** — the existing ADR-0038 rule, unchanged: the check has at least one stage that executes something real (not in `_VACUOUS_PROGRAMS` after unwrapping).
   - **`content`** — **anchored + falsifiable** replaces "must run what you build": the check must *name a content artifact* (a `.md`/`.rst`/`.txt`/`.adoc` path — the same suffixes the diff-side classifier calls `content`, so the two halves agree; a plain `pytest` check never silently "covers" the docs half of a mixed change) and must be able to exit non-zero on a wrong artifact — at least one stage asserts something (an assertive inspector — `test`, `grep`, `cmp`, `diff`, … — with an operand, or a real executor), and no can't-fail `||` fallback (`|| true`, `|| echo fine`) neutralizes the line. Inspectors flip from blocklisted to first-class: for text they are the verification.
   Checks need no kind tags — the per-segment analyzer (the per-segment/builtin work from PR #110) classifies each check mechanically, and one check may count toward both kinds. A declared kind with no covering check is rejected with a per-kind message ("no check covers `content`"). Companion checks satisfying no rulebook are tolerated once every kind is covered (judge-contracts-whole, PR-#110 review — rejecting companions re-imports the burn-a-turn failure).

3. **The diff audits the declaration at verification time.** Self-declared kinds without reconciliation would be self-certification one level up (declare `content`, ship code, dodge execution checks). So when a declared contract froze, the verifier classifies the changed paths and requires **`kinds(diff) ⊆ declared change_kinds`** — an undeclared kind present in the diff fails a required `change_kind_coverage` check, legibly, naming the kind and the offending paths. Path classification fails toward strictness: `.md`/`.rst`/`.txt`/`.adoc` → `content`; everything else — including behavior-bearing config (`pyproject.toml`, CI yaml) — → `code`. Only *under*-declaration is policed; over-declaring is self-inflicted strictness the model can fix via the gated `alter_verification`.

4. **The declaration is journaled.** `change_kinds` rides the frozen plan (`TaskState`, `VerificationPlanFrozen`), so every run's rubric — and the model's stated intent — stays auditable, and held-out evals (ADR-0040) can grade declaration honesty (declared `content`, shipped code) as its own integrity signal.

The immutable floor beneath the declared contract (ADR-0038) is untouched — it remains the anchor no declaration can amend away. The model is trusted to *state* its intent, never to be the last word on it.

## Alternatives considered

- **Trust the model with verification integrity entirely for non-functional changes** (no vacuity guard; held-out evals catch low-integrity models). Rejected: evals grade models in aggregate, but the verdict is a live per-run promise to the human in the cockpit — `python3 -c "print('ok')"` as a valid contract makes the green ✓ self-certification in effect. The journal's rejected check passes anchored+falsifiable easily, so the desired UX needs no trust expansion.
- **Infer the change kind from the diff only, at verification time** (no declaration). Rejected: declaration-time validation is the cheap feedback point, and at greenfield declaration there is no diff — the guard would have to accept everything and reject late, after the work. Kept as the *audit* (decision 3), where the diff genuinely exists.
- **A scalar `change_kind` with "mixed declares the strictest".** Rejected: strictest-wins quietly under-validates the content half of a mixed change — one pytest check satisfies a `code` contract and the README ships unverified. The list demands coverage per kind. (Scalar→list later is also a journal-schema migration; starting as a list costs nothing.)
- **Per-check kind tags** (`DeclaredCheckInput.kind` extended). Rejected: redundant — the segment analyzer classifies checks mechanically, and self-tagged checks would need their own audit.

## Consequences

- The journal's doc-task contract is accepted on turn one; the `python3 -c` laundering incentive for content checks disappears. The laundering hole *remains* for the `code` rulebook — inherent to a fail-open blocklist; the immutable floor stays the real anchor there (already conceded in ADR-0038).
- A model can no longer dodge execution checks by mislabeling code work: the diff-inclusion audit fails the run legibly, and the amendment path (tier-3 `alter_verification`) is the recovery.
- New kinds (`config`, `dependency`, `schema`) slot in as one rulebook + one path classifier each, without schema migration.
- `VerificationPlanFrozen` gains a `change_kinds` field (schema-additive); `None` means no declaration — which is also what journals written before this ADR read back as.
- Non-greenfield contracts (tiers 1–3 detected/cited) are untouched — no declaration exists, so no reconciliation runs.
