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
| **Deferred** | directory + stub exist, but pulled from the queue | listed in the status board with a rationale; **not** moved into *Backlog* (which means *no directory*) |
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
| `02` | Don't judge an agent by its pass@1 *(slug `02-dont-judge-by-pass-at-1`)* | 4 | 4 | 4 | **4** | **Live** — dated 2026-07-11, merged 2026-07-16 (PR #7). Carries the **pass^k-is-a-six-point-scale** finding: the 2026-07-14 rerun inverts the baseline reliability ranking (glm 0.50→0.67, gpt-oss 0.67→0.17, the latter from 8 Groq tool-validation deaths). |
| `03` | Failure modes from the first eval loop | 3 | 3 | 3 | 3 | **Deferred** — 2026-08-23. Fails the broadcast filter: an internal taxonomy addressed by its own labels (`A9`, `B3`) is reference material for this repo, not a post someone outside it can use. The catalog stays the canonical artifact other posts cite. |
| `04` | A verifier is not a tool | 4 | 4 | 3 | 3 | **Deferred** — 2026-08-23. Two problems: it re-argues `00` (which already moved the authority for "done" out of the model), and the framing is harness-shaped rather than portable. Its best content — ADR-0007's *a model that authors its own rubric sets it low up front* — is **reassigned as a section of `06`**. |
| `05` | The model proposes, the harness disposes | 3 | 4 | 3 | 2 | **Deferred** — 2026-08-23. Lowest uniqueness in the series (U2) and pure architecture-stance. Its own gate was "earned by the empirical posts beneath it"; with `03`/`04` deferred there is even less under it. Revisit only once the queue below has landed. |
| `06` | When the agent games the verifier | 5 | 4 | 3 | 3 | **Stub** — the flagship; **blocked on the demo** |

**Snapshot (2026-08-23):** 3 live · 0 in review · 3 deferred · 1 stub (`06`, demo-gated) · 17 backlog
(counted from the table below — the four queued rows still live there until they get directories).
Nothing has shipped since `02` merged on 2026-07-16 — five weeks against a 1–2 week cadence. The queue
below is re-cut from that date, and the three deferrals are the reason it is now led by backlog items
rather than by the numbered stubs.

---

## The queue — what ships next

Cadence: **one post every 1–2 weeks.** Seven posts without a cadence is a backlog, not a presence.
Each item is gated on real evidence; nothing ships ahead of its gate.

**The selection filter — added 2026-08-23:** *does the lesson transfer to a reader who will never
use this harness?* A post that only makes sense to someone holding our component names is
reference material, not broadcast material. This filter is what deferred `03`, `04`, and `05`, and
what promoted three full-kit backlog items over them. It sits alongside *Standing rules* — those
govern **how** a post is written, this governs **whether it is written at all**.

| # | Ship | Why here | Gate | Target |
| --- | --- | --- | --- | --- |
| **1** | `provider-reliability` → next free number | A provider hung ~5 min and returned a `\x00` body as **HTTP 200** — an error wearing a success costume — and it looked like a code regression until a serial control run (20/20, 0 NULs) exonerated the model. The rule (**classify by layer:** dead/empty = transport, malformed-but-present = model, slow-but-streaming = neither) applies to anyone calling an LLM API. Six-row run table already written. | none — 21KB kit | 2026-08-29 |
| **2** | `shell-syntax-boundary` → next free number | **The most broadcastable item in the backlog.** The exposure is specific — a harness that hands model-authored command *strings* to `shlex.split` with `shell=False` and never checks them for shell syntax; one that takes structured argv, or rejects metacharacters at the seam, does not inherit it. In ours, `&&` became argv words and a declared 10-section verification chain **passed having verified 1 section** (`grep -q` exits 0 on first match while patterns 2–10 became unopenable filenames). Deterministic one-line repro; E5/P4/U4. *Draft the post to the same scope: name the pattern, don't generalize to every harness.* | none — kit | 2026-09-12 |
| **3** | `eval-probe-false-rejections` → next free number | **Three models flipped FAIL→PASS with zero capability change** — the only delta was a sentence added to the task spec. That is "your benchmark is measuring your benchmark" with a number attached, plus a false *pass* (the raw-mode staircase) for symmetry. E5/P4/U4. | none — kit | 2026-09-26 |
| **4** | `deterministic-grader` → next free number | A construction, not a claim about a model — the hardest item here to nitpick. Schedule-invariant assertions over a genuinely nondeterministic scenario, randomness pushed into a probe stub, no LLM judge. A web scan for this recipe fell through to formal-methods papers. | none — ADR-0036 + baseline | 2026-10-10 |
| **5** | `06` oracle-gaming *(flagship)* | The credibility peak and the only non-commodity claim in the series; also the one topic with a pre-existing public audience. **Now also carries `04`'s argument** as a section — evidence-producing tools vs. the harness-owned scorer — since `04` will not ship on its own. | **the demo** | demo-gated |

**Sequencing note:** #2 and #3 are siblings — both are *the thing measuring the agent was broken*.
If they read as repetitive, swap #4 between them, or open #3 explicitly as the grader-side
companion to #2 (which is what its kit already calls itself).

**Running in parallel, starting now: build the oracle-gaming demo.** The `increment-4-plan.md`
referenced by earlier revisions of this doc **no longer exists at the repo root** — it was
untracked and is gone, so the flagship's gate currently has *no artifact behind it*. Rebuilding
that plan is the first task. This parallel track is deliberate: `06` must not be
perpetually-last, and its moat is *shrinking* as the field races in (SpecBench, Verification
Horizon, the External Anchor Principle; UTBoost found 15.7% of "passing" SWE-bench Verified
patches were gamed). "I ran it in a live self-modifying harness" is the entire differentiator, and
it decays. The demo is small and concrete: **construct one task where the cheapest path to green
is to edit a test or the verifier; show the agent taking it; show the fingerprinted/held-out
oracle catching it; report gamed-pass-rate vs. true-pass-rate.** The four queued posts above buy
roughly six weeks of cadence to build it.


---

## Backlog

Unwritten, deduplicated, one row per article concept. Promote by giving it the next free
number and a directory. Rows marked **→ queued #N** are already ordered in *The queue* above and
stay here only until they get a directory. **Readiness and uniqueness are inversely correlated here** — the
write-now items tend to be the crowded ones. Publish those as *"our data confirming a known
effect,"* not as reveals.

| ID | Post | Core | R | E | P | U | Ready? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `provider-reliability` | Errors disguised as success | A provider hung ~5 min then returned a `\x00` NUL body as **HTTP 200** — an error wearing a success costume. It dropped pass@1 0.90→0.85 and *looked like a code regression*; a serial control run (20/20, 0 NULs) proved transport, not capability. Principle: **classify provider failures by layer** — dead/empty reply = transport (re-issue), malformed-but-present = model (re-prompt), slow-but-streaming = neither (leave it). **Caveat:** the *recovery* half is unexercised by a real hang (needs fault injection); pass@1 deltas aren't significant. | 3 | 5 | 3 | **4** | ✓ **full kit** · **→ queued #1** |
| `deterministic-grader` | A deterministic grader for a nondeterministic scenario | Scoring a concurrency/ACID task with schedule-invariant assertions, randomness pushed into a probe stub keyed on the echoed `user_id`, SQLite-lock-as-intended-difficulty, no LLM judge. A recipe nobody has written *for agent evals* — a web scan fell through to formal-methods papers. | 3 | 5 | 3 | **4** | ✓ · **→ queued #4** |
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
| `shell-syntax-boundary` | Your agent's shell command was never running in a shell | `shlex.split` + `shell=False` turns model-authored `&&` chains into argv garbage: a 10-section verification chain **passed having verified 1 section** (quiet false pass); a heredoc hang seeded a 24-turn spiral (loud). Fix: a quote-aware normalize-or-reject boundary at the command seam. Deterministic one-line repro. | 3 | 5 | 4 | **4** | ✓ **full kit** · **→ queued #2** |
| `eval-probe-false-rejections` | My eval was wrong five times before any model was | Developing `tetris-tui`: five probe artifacts (three README-wording false rejections, a farewell-frame count, a streaming under-spec) plus one false *pass* (the raw-mode staircase, closed with a stdlib-pty terminal emulator) — three models flipped FAIL→PASS on spec changes alone; what survived (reverse-order bag, budget exhaustion) was the real signal. `vacuity-guard-false-lesson`'s grader-side companion; *task*-not-model as `01` was scaffold-not-model. | 3 | 5 | 4 | **4** | ✓ **full kit** · **→ queued #3** |

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
| `provider-reliability` | **HN** | r/LocalLLaMA, Lobste.rs | An HTTP 200 that is an error — broad practitioner appeal, no harness knowledge required. |
| `shell-syntax-boundary` | **HN** | Lobste.rs | The widest-reach item: a bug most agent builders have shipped without knowing. One-line repro travels on its own. |
| `eval-probe-false-rejections` | HN | r/LocalLLaMA, Latent Space orbit | The eval-methodology audience; sibling to `02`. |
| `deterministic-grader` | Lobste.rs | HN (if the recipe reads cleanly) | A construction piece — lower ceiling, near-zero nitpick surface. |
| `06` oracle-gaming | **HN + LessWrong/AF** | HF (result file) | The credibility peak. Bring the experiment, not an argument. Absorbs `04`'s safety-adjacent angle. |
| `00` context piece | (evergreen) | repo README | The "what is this" anchor everything links back to. |

> Deferred (`03`, `04`, `05`) are out of the distribution plan until they are re-queued.

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

Re-verified 2026-08-23: every **linked** path below resolves. One artifact does **not** — the
oracle-gaming build plan (`increment-4-plan.md`), recorded as missing in its own row; it is
cited as a bare filename rather than a link precisely because there is nothing to link to. Research docs are date-prefixed (commit
`7ca2e64`) — the old `name-YYYY-MM-DD.md` forms are dead links.

| Artifact | Path | Feeds |
| --- | --- | --- |
| Failure-mode catalog | [`../research/failure-modes.md`](../research/failure-modes.md) | `03` *(deferred)* — stays the canonical artifact other posts cite |
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
| Oracle-gaming build plan | **missing** — `increment-4-plan.md` was untracked at the repo root and is gone as of 2026-08-23; rebuild it before `06` | `06` |
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
