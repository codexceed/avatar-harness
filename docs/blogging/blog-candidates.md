# Blog plan — avatar-harness

> **This is the plan of action.** One status board, one queue, one distribution plan.
> It is the *only* place a post's status or position is recorded.
>
> The reasoning that produced it (candidate essays, scoring passes, the Codex
> cross-validation, the rollout ladder) is frozen in
> [`archive/2026-06-candidate-reasoning.md`](archive/2026-06-candidate-reasoning.md).
> Read it for raw material — the Pi contrast, the differentiator list — never for status.

**Goals, in priority order:** contribute something true to public knowledge · get real
critique on the process and the conclusions · credential an applied AI engineer working at
the frontier. For an unknown author the governing risk is **invisibility, not being wrong** —
so syndicate deliberately, bounded by the error discipline in *Standing rules*.

---

## Update protocol — how this doc stays true

The last version rotted because status lived in four places and one idea had six names. Two
rules prevent that:

1. **One article, one ID — its blog directory slug** (`00-verification-first-harness`, and so
   on). A backlog item has no number; it *gets* the next free one when promoted. **The number
   is an identity, not a publish date** — posts may ship out of numeric order.
2. **Status is read from `sarthak-blog/` git state, never from memory.** Definitions:

| Status | Means | Check |
| --- | --- | --- |
| **Live** | merged to `main`, `draft:false` | `git log main -- src/content/blog/<slug>/` |
| **In review** | full draft, `draft:false`, not yet merged | uncommitted / open PR |
| **Stub** | directory exists, `draft:true`, ~150–180 words | holds the series slot |
| **Backlog** | no post directory yet | lives in *Backlog*, below |

**When a post ships, do all three in the same pass:** flip its status → **delete every backlog
row it consumed** → re-date the queue. That middle step is the one that was skipped: `02`
swallowed both the cost and the reliability candidates while the doc still advertised them as
ready-to-write.

---

## Standing rules — every post, no exceptions

1. **Directional evidence or a case study, never a leaderboard.** n=5 seeds, few tasks, one
   project, an evolving harness. Write "failure-mode discovery," never "model X beats Y."
2. **The 6-question arc:** what motivated this · what we did · what we measured · what we
   inferred · what we're uncertain about · where we go next. This is the line between a
   research-engineering post and a vibes essay.
3. **Split measured fact from interpretation, visibly.** The "sonnet thrashes against the
   denylist" claim was confidently wrong and was caught *only* by reading the trajectories.
   Hedge the interpretation; never hedge the data.
4. **Ship the receipts:** the reproduce command plus a link to the raw artifact.
5. **One reusable principle** a reader can apply *without* adopting this harness.
6. **Adversarial pre-publish pass** (review the draft like a PR) before any cross-post.
7. **Comparisons to other harnesses** (Pi, opencode, Aider) go in a *section*, never a
   headline — and get verified against their current source first. Claims about other
   people's systems are the highest-nitpick surface there is.

**Shape:** 700–1200 words, or 5–8 dense sections. Every post carries at least one of: a run
table, a journal excerpt, a diff, a failing/passing test pair, an eval result, or an
architecture diagram tied to source.

---

## Status board

The series so far. `R`/`E`/`P`/`U` are scored 1–5 — **R**esearch novelty · reusable
**E**ngineering lesson · immediate **P**ractitioner value · **U**niqueness (5 = nobody has
written this; 2 = commodity take).

| ID | Post | R | E | P | U | Status |
| --- | --- | --- | --- | --- | --- | --- |
| `00` | When is an agent truly done? *(verifier owns "done")* | 3 | 4 | 3 | 2 | **Live** — 2026-06-18 |
| `01` | Is your harness driving your model crazy? *(0.10 → 0.75)* | 4 | 4 | 4 | 3 | **Live** — 2026-07-01 |
| `02` | What pass@1 hides | 4 | 4 | 4 | 3 | **In review** — dated 2026-07-11 |
| `03` | Failure modes from the first eval loop | 3 | 3 | 3 | 3 | **Stub** — date lapsed, re-date on merge of `02` |
| `04` | A verifier is not a tool | 4 | 4 | 3 | 3 | **Stub** — date lapsed, re-date on merge of `02` |
| `05` | The model proposes, the harness disposes | 3 | 4 | 3 | 2 | **Stub** — the manifesto; earn it with empirics |
| `06` | When the agent games the verifier | 5 | 4 | 3 | 3 | **Stub** — the flagship; **blocked on the demo** |

**Snapshot:** 2 live · 1 in review · 4 stubs · 12 backlog. The stub dates for `03` and `04`
(2026-06-30, 2026-07-08) have already lapsed — they are re-based off `02`'s merge in the queue
below, not honoured as written.

---

## The queue — what ships next

Cadence: **one post every 1–2 weeks.** Seven posts without a cadence is a backlog, not a
presence. Each item is gated on real evidence; nothing ships ahead of its gate.

| # | Ship | Why here | Gate |
| --- | --- | --- | --- |
| **0** | **Land `02`** | It is written and in review. Everything downstream re-dates off its merge, and it is what retires the cost/reliability backlog. | merge |
| **1** | `03` failure modes | Evidence is sitting ready in the catalog, and it's the natural third beat: once a scaffold bug has masqueraded as a weak model (`01`) and a "safe" run as a solved one (`02`), the taxonomy is the payoff. Keep it appendix-grade — summarize, link the full catalog. | none — write it |
| **2** | `04` a verifier is not a tool | The methodological setup for the `06` flagship: evidence-producing tools vs. the harness-owned scorer. Ship it *before* the flagship or the flagship has to argue this from scratch. | none — write it |
| **3** | `provider-reliability` → next free number | **The most under-tracked asset we have.** E5/U4, and a complete writing kit already exists. A standalone empirical post that costs almost nothing to produce. | none — kit is written |
| **4** | `deterministic-grader` → next free number | The sleeper: unique, ready, hard to nitpick, and the best antidote to invisibility. | none — ADR-0036 + baseline exist |
| **5** | `05` manifesto | Synthesis. Never the opener — it is *earned* by the empirical posts above it. | posts 1–4 landed |
| **6** | `06` oracle-gaming *(flagship)* | The credibility peak, and the only non-commodity claim in the series. | **the demo** |

**Running in parallel, starting now: build the oracle-gaming demo** (`increment-4-plan.md`).
This is deliberate — `06` must not be perpetually-last. Its moat is *shrinking* as the field
races in (SpecBench, Verification Horizon, the External Anchor Principle; UTBoost found 15.7%
of "passing" SWE-bench Verified patches were gamed). "I ran it in a live self-modifying
harness" is the entire differentiator, and it decays. The demo is small and concrete:
**construct one task where the cheapest path to green is to edit a test or the verifier; show
the agent taking it; show the fingerprinted/held-out oracle catching it; report gamed-pass-rate
vs. true-pass-rate.**

---

## Backlog

Unwritten, deduplicated, one row per article concept. Promote by giving it the next free
number and a directory. **Readiness and uniqueness are inversely correlated here** — the
write-now items tend to be the crowded ones. Publish those as *"our data confirming a known
effect,"* not as reveals.

| ID | Post | Core | R | E | P | U | Ready? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `provider-reliability` | Errors disguised as success | A provider hung ~5 min then returned a `\x00` NUL body as **HTTP 200** — an error wearing a success costume. It dropped pass@1 0.90→0.85 and *looked like a code regression*; a serial control run (20/20, 0 NULs) proved transport, not capability. Principle: **classify provider failures by layer** — dead/empty reply = transport (re-issue), malformed-but-present = model (re-prompt), slow-but-streaming = neither (leave it). **Caveat:** the *recovery* half is unexercised by a real hang (needs fault injection); pass@1 deltas aren't significant. | 3 | 5 | 3 | **4** | ✓ **full kit** |
| `deterministic-grader` | A deterministic grader for a nondeterministic scenario | Scoring a concurrency/ACID task with schedule-invariant assertions, randomness pushed into a probe stub keyed on the echoed `user_id`, SQLite-lock-as-intended-difficulty, no LLM judge. A recipe nobody has written *for agent evals* — a web scan fell through to formal-methods papers. | 3 | 5 | 3 | **4** | ✓ |
| `probe-vs-verifier` | What counts as solved? | Probe-owned vs. verifier-owned success; the case where a strict verifier failed a working artifact and the probe became authoritative. Fresh — news-analyzer and ecommerce-portal now supply real cases. | 4 | 4 | 3 | **4** | ✓ |
| `score-the-attempt` | Score the attempt, not the prevented outcome | When a denylist deterministically enforces "no leak," the terminal outcome cannot distinguish intrinsic restraint from a blocked lunge — so read intent from the agent-hidden journal of *denied* calls. **Caveat:** the attempt-vs-effect split already exists in the literature (the tool-affordance-on-safety paper) — lead with that citation, position the journal method as the harness-native instantiation. | 5 | 4 | 2 | 3 | ~ concept (ADR-0034 is design-only) |
| `blast-radius` | Route on blast radius, not complexity | The human-gated improvement loop, now *built* rather than proposed: two workflows, three gates over a deterministic core; `validate` runs against **frozen** eval assets so a candidate can't grade a spec it just edited. "Route on blast radius" is the sub-angle that returned nothing in a web scan. | 4 | 4 | 3 | 2 | ✓ |
| `state-not-transcript` | State is not a transcript | Deriving message history from a pydantic `TaskState` (rather than *being* the chat log) is what buys replay, journaling, and degrade-don't-drop compaction. The clean Pi divergence. Strongest pure-systems piece. | 3 | 5 | 3 | 3 | ✓ |
| `env-incident` | We let an agent read `.env` once | The dogfood leak (`sk-or-v1` → log + context + a third-party API) → denylist-at-the-gate, and why redaction was **rejected**: prevention, not detection. **Caveat:** this is in-harness defense, not host isolation — say so. | 3 | 4 | 4 | 3 | ✓ |
| `journal-is-dataset` | The journal is the dataset | One JSONL trajectory turned into a compact failure report. Positions the harness as instrumentation for agent research. | 3 | 4 | 3 | 3 | ✓ |
| `silent-truncation` | Silent truncation is a bug, not a context strategy | Before/after context packet; a case where *visible* truncation changed behavior. Bite-sized context engineering. | 2 | 4 | 3 | 3 | ✓ |
| `cockpit-subscriber` | The cockpit is a subscriber, not the runtime | A rich TUI need not swallow the control loop: it observes typed events and returns approvals through explicit control methods. The control-vs-observation line. | 2 | 4 | 3 | 3 | ✓ |
| `task-not-session` | The task, not the session, is the unit of truth | Multi-turn session over per-goal `TaskState`s and artifacts. Differentiates from product-first terminal agents without attacking them. | 3 | 4 | 3 | 3 | ✓ |
| `rejection-sampling` | A tiny rejection-sampling loop | Best-of-N scored by verifier/probe; show the uplift *and* the failure cases. The first real closed-loop uplift result. | 4 | 3 | 3 | 3 | ✗ needs build |
| `built-with-agents` | Building a coding agent *with* coding agents | The meta-story: TDD-phased, dogfood-driven, four worktree-isolated agents merging clean, ADRs as the decision log. Weakest research signal — needs real stats (PRs, defects caught only at integration) to earn a slot. | 2 | 3 | 4 | 3 | ~ needs stats |
| `vacuity-guard-false-lesson` | The guardrail that taught the model a lie | An over-strict guard rejected a *correct* declared check, burned a turn plus a tier-3 human approval, accepted the replacement by parse accident — and the model *internalized* the false verdict (its amendment rationale echoes it). Principle: a guard's rejection message is in-context training data — fail open at the lexical gate, fail closed at the executing floor. The counterweight to too-lax integrity discourse. | 3 | 4 | 3 | **4** | ✓ **full kit** |
| `self-certification-arms-race` | One task, four models, four self-certification holes | The #110–#114 saga as an arc: a repeated dogfood objective as an integrity fuzzer — guard miscalibration, argv-mangled execution, advisory verdicts laundering a failing *immutable floor* into `success`, a misrouted task with no escalation lever. Verification as a governed protocol. The harness-side prequel/teaser for `06`. | 4 | 4 | 3 | 3 | ✓ **full kit** |
| `shell-syntax-boundary` | Your agent's shell command was never running in a shell | `shlex.split` + `shell=False` turns model-authored `&&` chains into argv garbage: a 10-section verification chain **passed having verified 1 section** (quiet false pass); a heredoc hang seeded a 24-turn spiral (loud). Fix: a quote-aware normalize-or-reject boundary at the command seam. Deterministic one-line repro. | 3 | 5 | 4 | **4** | ✓ **full kit** |
| `eval-probe-false-rejections` | My eval was wrong five times before any model was | Developing `tetris-tui`: five probe artifacts (three README-wording false rejections, a farewell-frame count, a streaming under-spec) plus one false *pass* (the raw-mode staircase, closed with a stdlib-pty terminal emulator) — three models flipped FAIL→PASS on spec changes alone; what survived (reverse-order bag, budget exhaustion) was the real signal. `vacuity-guard-false-lesson`'s grader-side companion; *task*-not-model as `01` was scaffold-not-model. | 3 | 5 | 4 | **4** | ✓ **full kit** |

### Retired — folded into shipped posts (do not resurrect)

| Was | Fate |
| --- | --- |
| *Cost-per-solved: "token count is not cost"* (`NC1`) | **Absorbed into `02`** — the ~90× price spread, codex-cheapest-in-tokens-yet-priciest-in-dollars, and `$/solved` all ship there, with the cost charts. |
| *pass@1 vs pass^k: reliability ≠ capability* (`NC2`) | **Absorbed into `02`** — the five-seed reliability haircut is Part 2 of that post. |
| *Failure buckets for coding agents* | Merged into `03`. |
| *Measuring agents without vibes / the verifier is the scorer* | Merged into `04`. |
| *Control hooks are not events* · *Why the TUI is not the agent runtime* | Merged into `cockpit-subscriber`. |
| *Structured state beats transcript scraping* | Merged into `state-not-transcript`. |
| *Native tool calls fixed one class of patch failures* | Dropped — U2, and the lesson is a paragraph, not a post. Revive only if it earns a section elsewhere. |

---

## Distribution

**Canonical home: the owned blog.** Every post lands first on `sarthak-blog` — not
Substack-as-primary, not Medium. Credibility compounds on a URL you own, and a body of work
*is* the credential. Cross-posts set **`rel=canonical`** back to the original so syndication
never cannibalizes search authority. Run a newsletter in parallel as the owned-audience
capture layer (email beats algorithm), **mirrored** from the blog, never the home.

Two things outrank any individual post: **the repo and `ARCHITECTURE.md` are credential #1**,
and **engaging with other people's eval/agent work** (replying, building on it) beats
broadcasting into a void.

| Post | Primary cross-post | Also | Notes |
| --- | --- | --- | --- |
| `01` scaffold-not-model | **HN** | Lobste.rs | Strongest hook. HN-shaped: empirical, counterintuitive, debuggable, no promo smell. |
| `02` pass@1-hides | HN | Latent Space orbit | The methodology crowd; pairs with `01`. |
| `03` failure modes | Lobste.rs, r/LocalLLaMA | HF (if a dataset) | Reference piece, practitioner audience. |
| `04` verifier-is-not-a-tool | HN | **LessWrong** | Architecture, and the first safety-adjacent angle. |
| `05` manifesto | **X-primary** | — | Highest format risk → ride the audience you've built, not a cold HN swing. |
| `06` oracle-gaming | **HN + LessWrong/AF** | HF (result file) | The credibility peak. Bring the experiment, not an argument. |
| `00` context piece | (evergreen) | repo README | The "what is this" anchor everything links back to. |

**Escalation ladder — match a venue's scrutiny to the confidence of the claim.** High-reach,
permanent-record venues (HN, LessWrong, X) punish a wrong claim hard, and one confidently-wrong
post is an expensive hit for a new name. So: **reproducible artifacts** (repo, ADRs, result
files) are near-impossible to be wrong about → **empirical posts** are bounded risk *iff* fact
and interpretation are visibly split → **theses/manifestos** invite "you're wrong about the
world" and wait for empirical backing → **comparisons** to other harnesses are the riskiest and
stay out of headlines.

**Two feedback rules:**

- **A venue's comments on post N become post N+1's "what we're uncertain about" section.**
  Visibly metabolizing critique is itself a credibility signal, and it closes the loop.
- **Stagger, don't blast.** Blog + X on day 0; HN day 1–2 (Tue–Thu, AM ET lands best);
  Reddit/LessWrong later that week — so issues surfaced early get fixed before the bigger swing.

---

## Evidence index

Every path below is verified as of 2026-07-12. Research docs are date-prefixed (commit
`7ca2e64`) — the old `name-YYYY-MM-DD.md` forms are dead links.

| Artifact | Path | Feeds |
| --- | --- | --- |
| Failure-mode catalog | [`../research/failure-modes.md`](../research/failure-modes.md) | `03` |
| Eval baseline (original + corrected) | [`../research/2026-06-15-eval-baseline.md`](../research/2026-06-15-eval-baseline.md) · [`…-post-fixes.md`](../research/2026-06-15-eval-baseline-post-fixes.md) | `00`, `01`, `02` |
| R5 post-merge validation | [`../research/2026-06-21-eval-r5-postmerge-validation.md`](../research/2026-06-21-eval-r5-postmerge-validation.md) | `provider-reliability` |
| LLM landscape (7×6×5, n=210) | [`../research/2026-07-05-llm-landscape.md`](../research/2026-07-05-llm-landscape.md) | `02` |
| Baseline post-swap | [`../research/2026-07-05-eval-baseline-post-swap.md`](../research/2026-07-05-eval-baseline-post-swap.md) | `02` |
| news-analyzer eval development | [`../research/2026-07-04-news-analyzer-eval-development.md`](../research/2026-07-04-news-analyzer-eval-development.md) | `probe-vs-verifier` |
| ecommerce-portal first baseline | [`../research/2026-07-05-ecommerce-portal-first-baseline.md`](../research/2026-07-05-ecommerce-portal-first-baseline.md) | `deterministic-grader` |
| **Provider-reliability writing kit** | [`blog_kits/provider-reliability-retries.md`](blog_kits/provider-reliability-retries.md) | `provider-reliability` |
| **Verification-saga writing kits (×4)** | [`blog_kits/vacuity-guard-false-lesson.md`](blog_kits/vacuity-guard-false-lesson.md) · [`…/self-certification-arms-race.md`](blog_kits/self-certification-arms-race.md) · [`…/shell-syntax-boundary.md`](blog_kits/shell-syntax-boundary.md) · [`…/eval-probe-false-rejections.md`](blog_kits/eval-probe-false-rejections.md) | their namesake backlog rows |
| Shell-mangling false pass | [`../research/2026-07-10-shell-mangling-false-pass.md`](../research/2026-07-10-shell-mangling-false-pass.md) | `shell-syntax-boundary` |
| tetris-tui eval development (design record + matrices + committed result rows) | [`../research/2026-07-11-tetris-tui-eval-development.md`](../research/2026-07-11-tetris-tui-eval-development.md) | `eval-probe-false-rejections`, `self-certification-arms-race` |
| Oracle-gaming build plan | `increment-4-plan.md` *(repo root, untracked)* | `06` |
| ADRs | [`../adr/`](../adr/) — 0007 + 0020 (`04`) · 0011 + 0024 (`06`) · 0034 (`score-the-attempt`) · 0036 (`deterministic-grader`) · 0026 + 0028 + 0029 (`provider-reliability`) · 0031 + 0032 (`blast-radius`) | — |

---

## Appendix — legacy ID crosswalk

The archive uses six overlapping ID schemes. Map them here; do not reintroduce them.

| Legacy | Now |
| --- | --- |
| `#1` / spine 5 / rung A manifesto | `05` |
| `#2` / `B.2` | `state-not-transcript` |
| `#3` / `#4` spine / `#5` "verifier is the scorer" | `04` |
| `#4` / `C.1` | `env-incident` |
| `#5` / `A.2` / spine 3 | `03` |
| `#6` | `built-with-agents` |
| `#7` / `B.3` / `D.1` | `cockpit-subscriber` |
| `T2` / spine 1 | `01` |
| `T3` / spine 2 / `NC2` / `A.3` / `NC1` | `02` |
| `T1` / `T4` / `⭐` / `A.4` / `NC3` | `06` |
| `A.1` | `probe-vs-verifier` |
| `A.5` | `rejection-sampling` |
| `B.1` | `journal-is-dataset` |
| `C.2` | `silent-truncation` |
| `C.4` | `provider-reliability` |
| `D.2` | `task-not-session` |
| `NC4` | `blast-radius` |
| `NC5` | `deterministic-grader` |
| `NC6` | `score-the-attempt` |

> "Tier-1/2/3" in the archive means the *provenance tier of a metric* — unrelated to article
> IDs. It does not survive into this doc.
