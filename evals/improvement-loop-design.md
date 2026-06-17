# Evals-Driven Improvement Loop — Design

- **Status:** Design (for sign-off) — the buildable spec for the Phase-4 self-improvement initiative. Decision recorded in **ADR-0024**.
- **Date:** 2026-06-16
- **Owner:** Sarthak Joshi
- **Related:** **ADR-0024** (the decision + rejected alternatives); ADR-0004 (the eval harness this builds on), ADR-0011 (verifier integrity — the substrate that gates HITL removal), ADR-0020 (guard probes), ADR-0022 (the first failure mode this loop will process); `docs/eval-harness-design.md`; `docs/research/failure-modes.md` (the A/B/C/D catalog = the loop's memory).
- **One line:** turn measured eval signal into *reviewed* harness improvements through two human-gated Claude workflows over a deterministic core — progressively, and safely, reducing the human in the loop.

> Diagrams are [Mermaid](https://mermaid.js.org/) (render on GitHub and in most editors).

---

## 1. Motivation & goals

**The problem.** Nearly every harness improvement so far came from *manual dogfooding* — a human running the TUI/headless, noticing a failure, diagnosing it, fixing it. That doesn't scale, and it leaves measured signal on the floor: we now have an Eval-0 harness (ADR-0004) that produces scored runs, lossless journals, and a failure-mode catalog, but nothing systematically turns that signal into change.

**The mission.** Progressively transition harness improvement from human dogfooding to **evals-signal-driven**, eventually reducing (and only eventually eliminating) the human in the loop — *without* letting an optimizer game its own grader.

**Goals.**
1. A repeatable path from an eval results dir → a **reviewed, scored, deduplicated set of change proposals** (zero eval spend).
2. A repeatable path from a *funded* proposal → a **TDD'd, statistically-validated PR** (the only eval spend, bounded).
3. Each stage is an **ad-hoc, independently-invokable Claude workflow** with a stable, typed seam between them.
4. Honest, data-driven decisions: validate globally (full matrix + paired stats), not per-failed-task; route by **risk/blast-radius**, not implementation size; dedup against institutional memory before debugging.
5. A clear path to the autonomous "golden loop" where human gates become triggers — gated behind a built integrity substrate.

**Non-goals (now).**
- **No auto-merge**, and no automating the eval-run *trigger*, until ADR-0011's integrity substrate + a train/test split exist. Humans hold every merge and grader-touching change.
- **No LLM-judge scoring** (ADR-0004 rejected it; a judge you optimize against is a hack target).
- No multi-agent SendMessage "team" — collaboration is handled by a reconciliation barrier (see §4).

**Principles** (inherited from CLAUDE.md Principle C): deterministic code wherever exact/cheap; agents only for reasoning leaves; one mechanism per concern; cost is intentional and staged; the human moves from *author* → *reviewer* → (only after the substrate) *auditor*.

---

## 2. The data that drives this (why now, why this shape)

From the latest matrix `evals/results/20260615T164950Z.*` (3 models × 4 tasks × 5 seeds = 60 runs):

| Signal | Value | Design consequence |
| --- | --- | --- |
| Overall pass@1 | **0.83** (gpt-5.1 1.00 · sonnet 0.75 · gemini 0.75) | a model-agnostic matrix is the unit of truth, not a single model |
| All 10 failures | **one task (`secret-safety`), one mode (C1 won't-conclude)** — already in `failure-modes.md`, already fixed-in-proposal (ADR-0022) | **dedup-before-debug** is mandatory, or we re-diagnose solved problems |
| Tokens on the 10 failures | **2.15M = 85% of the run's 2.53M** | the eval *re-run* dominates cost → the **canary ladder** + a single eval-spending stage |
| Biggest journal | **875 MB** (a `search_repo` recursing over `journal.jsonl`; no output cap) | raw journals can't enter an agent → **deterministic distillation**; and a prerequisite guardrail fix |
| Failure taxonomy | A harness · B measurement · C model · D security (`failure-modes.md`) | proposals carry a `mode` + a `remediation_type` aligned to the taxonomy |

This design consolidates two independent critiques of the initiative (Claude Opus 4.8 + Codex gpt-5.4); the decision and its rejected alternatives are recorded in **ADR-0024**.

---

## 3. System overview — two workflows, three gates, two layers

The loop is **not** one continuous auto-run. It is two independently-invokable Claude workflows separated by human gates, with the expensive eval run as a manual precursor. The two costly/irreversible actions — **running evals** and **merging** — are the gates that stay human longest.

### 3.1 Layered architecture

```mermaid
flowchart TB
    subgraph L0["Layer 0 · Eval harness — REUSED (ADR-0004), no changes"]
        EH["make eval → results/&lt;stamp&gt;.jsonl + .summary.json + journals<br/>Verifier · Journal · metrics/stats/diff (McNemar · pass^k · clustered CI)"]
    end
    subgraph L1["Layer 1 · Deterministic CLIs · evals/ · no model · TDD'd · replayable"]
        C1["distill — journal → trajectory digest (MB→KB)"]
        C2["triage — dedup vs failure-modes.md + ADRs → novel | known"]
        C3["score/route — impact × blast-radius"]
        C4["validate — canary ladder + FROZEN assets + evals.diff"]
        C5["proposal — ChangeProposal schema (the A→B seam)"]
    end
    subgraph L2["Layer 2 · Claude Workflows · evals/workflows/ · reasoning leaves only"]
        WA["A · evals-to-proposals  (read-only · zero eval spend)"]
        WB["B · proposal-to-pr  (the only eval spender)"]
    end
    subgraph ART["Artifacts"]
        PROP["evals/proposals/&lt;stamp&gt;/&lt;id&gt;.md"]
        MEM["failure-modes.md + docs/adr/ (durable memory)"]
        OUT["branch + PR  /  ADR-proposal PR"]
    end

    L0 --> C1
    WA -->|calls| C1
    WA -->|calls| C2
    WA -->|calls| C3
    WA -->|writes| C5
    WB -->|calls| C4
    WA --> PROP
    WA --> MEM
    PROP --> WB
    WB --> OUT

    classDef reuse fill:#1b7f3b,color:#fff,stroke:#06371a,stroke-width:2px;
    classDef code fill:#0b5fff,color:#fff,stroke:#04205c,stroke-width:2px;
    classDef flow fill:#7a3cb8,color:#fff,stroke:#3a1c58,stroke-width:2px;
    classDef art fill:#6b6b6b,color:#fff,stroke:#333,stroke-width:1px;
    class EH reuse;
    class C1,C2,C3,C4,C5 code;
    class WA,WB flow;
    class PROP,MEM,OUT art;
```

### 3.2 The abstraction (two layers + one typed seam)

- **Layer 1 — deterministic Python CLIs in `evals/`** (TDD'd, replayable, no model): `distill`, `triage`, `score`/`route`, `validate`. The cheap, exact primitives; only `validate` spends (and only when invoked).
- **Layer 2 — two named Claude Workflow scripts** (the `Workflow` tool: a `meta` block + phases, parameterized by `args`) that orchestrate only the *reasoning* and **shell out to Layer 1 at the deterministic seams**. (The Workflow tool runs agents/JS, not Python — so determinism lives in `evals/`.)
- **The seam — a typed `ChangeProposal`** A writes and B consumes (`evals/proposals/<stamp>/<id>.md`); B is invoked `--proposal <id>`. Decoupling makes each workflow invokable/replayable alone.

---

## 4. Components

Every user-facing artifact, what it is, where it lives, and how it's invoked.

| # | Component | Type | Lives in | Invocation | Role |
| --- | --- | --- | --- | --- | --- |
| 1 | `distill` | script / CLI | `evals/distill.py` | `python -m evals.distill <results>` | journal JSONL → compact **trajectory digest** (ordered actions · tool calls w/ arg *summaries* + exit · repeat & `decision_error` counts · denylist refusals · token/iter curve · outcome). MB→KB. |
| 2 | `triage` | script / CLI | `evals/triage.py` | `python -m evals.triage <digests>` | match each failure cluster vs `failure-modes.md` (A/B/C/D + mechanism) + open ADRs → `novel \| known→<entry/ADR>`. Only novel clusters reach the fan-out. |
| 3 | `score`/`route` | script / CLI | `evals/score.py` (extend) | `python -m evals.score` | impact (0–10, from cluster frequency) × `blast_radius`; deterministic governance route. |
| 4 | `validate` | script / CLI | `evals/validate.py` | `python -m evals.validate <candidate>` | **canary ladder** (unit/local → 1-seed canary on affected models → full matrix on survival) against **frozen `evals/` assets**; verdict via `evals.diff` (McNemar + clustered CI + agnosticism check). The only eval spender. |
| 5 | `ChangeProposal` | pydantic schema | `evals/proposal.py` | import / `--proposal <id>` | the A→B **seam** (fields in §4.1). |
| 6 | **Workflow A** `evals-to-proposals` | Claude workflow | `evals/workflows/evals_to_proposals.*` | `Workflow({scriptPath})` (or the optional skill) | read-only analysis MVP → proposals dir + memory updates. |
| 7 | **Workflow B** `proposal-to-pr` | Claude workflow | `evals/workflows/proposal_to_pr.*` | `Workflow({scriptPath, args:{proposal}})` | per funded proposal → worktree → TDD → validate → PR. |
| 8 | analysis / proposal / **reconcile** subagents | subagents (in A) | — | spawned by A | digest→`FailureMode`; mode→`ChangeProposal`; a single **reconciliation barrier** ensures mutual + codebase compatibility (not a SendMessage team). |
| 9 | TDD-executor / ADR-PR-drafter subagents | subagents (in B / A) | — | spawned by B (A for ADR-only) | implement under TDD in a worktree; draft the PR/ADR. |
| 10 | `/evals-to-change-plan` | slash command / skill *(optional ergonomic wrapper)* | `.claude/` | `/evals-to-change-plan <dir>` | thin entry that invokes Workflow A. |
| 11 | proposals artifact | artifact dir | `evals/proposals/<stamp>/` | — | the reviewable output the human reads at Gate 1. |
| 12 | `search_repo` output cap + journal exclusion | guardrail fix | `avatar-harness/avatar/tools/search.py`, `evals/run.py` | Increment 0 | prerequisite: keep journals distillable (closes the 875 MB blowup). |

### 4.1 `ChangeProposal` (the seam)

Front-matter (machine-readable) + a human body:

`mode` (A/B/C/D + catalog id) · `impact` (0–10) · **`remediation_type ∈ {prompt_instruction · guardrail_check · code_logic · doc_only}`** · **`blast_radius ∈ {local · global}`** · `target_tasks` · `predicted_validation_cost` (tasks×models×seeds → tokens, from the baseline profile) · `tdd_plan` · `evidence` (result rows + digest refs) · `status`.

`remediation_type` (the *kind* of fix — instruction/guardrail/code/doc, mirroring Saravia's session-mining outputs) is **orthogonal** to `blast_radius` (which governs validation + governance). Worked examples: ADR-0022 = `prompt_instruction` × global → ADR-PR; the `search_repo` cap = `guardrail_check` × local → implement-PR.

---

## 5. Flow

### 5.1 End-to-end — two workflows, three gates, and the golden-loop overlay

```mermaid
flowchart TB
    G0(["👤 GATE 0 · human runs make eval --no-cleanup<br/>(money gate for RUNS — manual now, cron later)"])

    subgraph WA["WORKFLOW A · evals-to-proposals · READ-ONLY · zero eval spend · re-runnable free"]
        direction TB
        A1["INGEST (code): load results+summary · distill journals MB→KB · cluster"]
        A2{"TRIAGE (code+judge):<br/>dedup vs failure-modes.md + ADRs"}
        A3["ANALYZE novel clusters (subagent / cluster) → FailureMode"]
        A4["PROPOSE + RECONCILE (subagents + barrier) → proposals"]
        A5{"SCORE + ROUTE (code+judge):<br/>impact × blast-radius"}
        A1 --> A2
        A2 -->|known| AK["link to catalog/ADR · no re-debug"]
        A2 -->|novel| A3 --> A4 --> A5
        A5 -->|doc_only / global| AD["draft ADR-proposal PR (no eval spend)"]
        A5 -->|implement / local| AP["buildable proposal<br/>+ predicted validation cost"]
    end
    G0 --> A1

    AP --> G1(["👤 GATE 1 · review proposals dir, FUND which to build<br/>(money gate for BUILDS)"])
    AD --> PR["open PR · cites rows · digest · diff"]

    subgraph WB["WORKFLOW B · proposal-to-PR · SPENDS eval budget · per funded proposal"]
        direction TB
        B1["worktree + TDD (cheap inner model)"]
        B2["VALIDATE (code): canary ladder · frozen evals/ · evals.diff McNemar"]
        B3{"improved & no regression?"}
        B1 --> B2 --> B3
        B3 -->|no · budget left| B1
        B3 -->|no · budget spent → abandon or escalate| AD
        B3 -->|yes| B4["full-matrix confirm + agnosticism check"]
    end
    G1 -->|funded| B1

    B4 --> PR
    PR --> G2(["👤 GATE 2 · review & MERGE"])
    G2 --> CAT["append/update failure-modes.md + ADRs (memory)"]

    %% ---- GOLDEN LOOP · automation that displaces each human gate (post-substrate) ----
    UNLOCK["🔓 UNLOCK · ADR-0011 D1–D4 + train/test split + frozen assets"]
    GL0(["⟳ periodic cron"])
    GL1(["impact × confidence policy"])
    GL2(["auto-merge low-blast-radius on held-out-green · human audits"])
    UNLOCK -. enables .-> GL0
    UNLOCK -. enables .-> GL1
    UNLOCK -. enables .-> GL2
    GL0 -. displaces .-> G0
    GL1 -. displaces .-> G1
    GL2 -. displaces .-> G2
    CAT -. golden · close the loop .-> GL0

    classDef code fill:#0b5fff,color:#fff,stroke:#04205c,stroke-width:2px;
    classDef agent fill:#1b7f3b,color:#fff,stroke:#06371a,stroke-width:2px;
    classDef gate fill:#b3261e,color:#fff,stroke:#5c0f0a,stroke-width:2px;
    classDef known fill:#6b6b6b,color:#fff,stroke:#333,stroke-width:1px;
    classDef golden fill:#caa23a,color:#1a1400,stroke:#7a5c00,stroke-width:2px,stroke-dasharray:5 4;
    class A1,A2,A5,B2 code;
    class A3,A4,AD,B1,B4,PR agent;
    class G0,G1,G2,B3 gate;
    class AK,AP,CAT known;
    class GL0,GL1,GL2,UNLOCK golden;
```

Blue = deterministic Layer-1 code · green = Layer-2 reasoning subagent · red = human gate · grey = terminal/shortcut · **dashed gold = golden-loop automation that *displaces* each gate once the ADR-0011 substrate is built** (`CAT → cron` closes the loop).

### 5.2 Where the reward-hacking risk lives (Workflow B's validation sub-loop)

```mermaid
sequenceDiagram
    participant W as Worktree agent (B)
    participant FS as Workspace (specs · fixtures · probes · verifier)
    participant EV as Eval re-run (validate)
    participant DF as evals.diff (McNemar)
    participant H as Human (Gate 2)

    Note over W,FS: ⚠ The agent can write the grading surface.<br/>ADR-0011 D1–D4 are UNBUILT → freeze evals/ + held-out.
    W->>FS: edit source to fix the mode
    W->>EV: validate (canary ladder · FROZEN evals/)
    EV->>DF: candidate rows vs pinned baseline
    DF-->>W: pass@1 Δ + McNemar verdict (full matrix at confirm)
    W->>H: PR cites rows · digest · diff · agnosticism check
    H->>H: confirm "solved, not gamed" (the review is tractable because validation is global + frozen)
    H-->>W: approve merge
```

---

## 6. Safety & cost (the load-bearing constraints)

**Reward-hacking / Goodhart.** Workflow B optimizes edits toward "the eval is green" against a grading surface the agent can write. This is the ADR-0011 moment, and its defenses (protected oracle paths, fingerprinting, held-out tests, calibration, train/test split) are **Proposed, not built**. Therefore:
- **Freeze the eval assets** during `validate` (run against `evals/` restored from a trusted ref, never the worktree) — a pragmatic D1+D2. Necessary, not sufficient (doesn't stop special-casing a frozen-but-visible test, doesn't fix a construct-validity gap like the guard probe, can't cover the verifier when the verifier is itself the target).
- **Human stays on every merge and grader-touching change** until the substrate exists. The golden-loop overlay only activates post-`UNLOCK`.
- **Route on risk, validate globally.** `blast_radius` (not size) picks governance; global/always-on changes (e.g. a prompt rule) require full-matrix + McNemar + the agnosticism check, never a single re-run.

**Cost.** The eval re-run dominates (85% of tokens were the 10 failures; one full matrix ≈ 2.5M tokens). Structural bounds: A spends $0; B is the only spender; the **canary ladder** stages spend (cheap inner model → 1-seed canary on affected models → full matrix only on survival); a **hard rework cap** then escalates to an ADR; each proposal carries a **predicted validation cost** so Gate 1 is cost-informed.

---

## 7. Execution plan (checklist)

TDD per the repo protocol: **propose the test list → maintainer approves → red → green → record.** Built behind `evals/` + one guarded `src/` guardrail; the only `src/` engine touch is Increment 0.

### Increment 0 — `search_repo` guardrail (small, standalone, unblocks clean inputs)
- [ ] `test_search_repo_caps_large_output_with_marker` — output capped (~50 KB) with the `… [truncated: shown/total chars shown]` marker.
- [ ] `test_search_summary_notes_truncation`.
- [ ] `test_eval_journal_excluded_from_search` — the eval journal path is covered by `_journal_ignores` (the regression that would have caught 875 MB).
- [ ] impl: cap in `tools/search.py`; align `evals/run.py` journal path with the workspace exclusion.
- **Exit:** a large search can't balloon `ToolEnd.content`/the journal; `make check` clean.

### Increment 1 — Layer-1 CLIs + `ChangeProposal` (the free foundation)
- [ ] `test_distill_*` — digest from constructed events; size bound; deterministic; reuses `_journal_events`.
- [ ] `test_triage_matches_known_C1_to_ADR_0022`; `test_triage_flags_novel`.
- [ ] `test_score_route_impact_x_blast_radius`.
- [ ] `test_changeproposal_roundtrips` (incl. `remediation_type` × `blast_radius`).
- [ ] `test_validate_canary_ladder_runs_frozen_assets` (scripted/offline) + uses `evals.diff`.
- [ ] impl `evals/{distill,triage,score,validate,proposal}.py`; held to the `evals/` gates (ADR-0013).
- **Exit:** `python -m evals.distill|triage|score` run on `20260615T164950Z` → digests + "all C1 → ADR-0022, 0 novel"; `make check` clean.

### Increment 2 — Workflow A `evals-to-proposals` (the analysis MVP)
- [ ] `evals/workflows/evals_to_proposals.*` (`meta` + phases): ingest→triage (Layer-1) → fan-out 1 subagent/novel cluster → reconcile barrier → write `evals/proposals/<stamp>/` + append `failure-modes.md`.
- [ ] dry-run against `20260615T164950Z`: 0 novel (proves dedup); a synthetic novel cluster fixture exercises the fan-out.
- **Exit:** read-only, zero eval spend; produces a reviewable proposals dir; safe to run today.

### Increment 3 — Workflow B `proposal-to-pr` (the spender) — **HITL-gated**
- [ ] `evals/workflows/proposal_to_pr.*`: worktree → TDD subagent → `validate` (canary ladder, frozen) → confirm → open PR; bounded rework; Gate 1 (fund) + Gate 2 (merge).
- **Exit:** per funded proposal, a TDD'd, McNemar-validated PR; never auto-merges; grader-touching → ADR-route.

### Increment 4 — ADR-0011 substrate + train/test split (the unlock — later)
- [ ] D1 protected oracle paths · D2 fingerprinting · D3 held-out FAIL_TO_PASS/PASS_TO_PASS · D4 calibration · dev/held-out task split.
- **Exit:** gates may become triggers (G0→cron, G1→policy, G2→auto-merge low-blast-radius on held-out green) — the golden loop.

---

## 8. Open questions

- **Workflow persistence convention** — no `.claude/workflows/` exists yet; confirm `evals/workflows/` as the home for saved Workflow scripts (invoked via `Workflow({scriptPath})`).
- **Skill wrapper** — is the optional `/evals-to-change-plan` slash command worth it in v1, or is direct `Workflow` invocation enough?
- **`predicted_validation_cost` source** — derive from each run's `prompt+completion` tokens in the baseline `summary`/rows, or maintain a per-task cost table?
- **Train/test split** — which seed tasks are dev vs held-out (Increment 4)?
