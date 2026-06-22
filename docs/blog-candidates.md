# Tech blog candidates — what avatar-harness does differently

> Working doc for choosing a flagship article. Comparison target is **Pi** (pi.dev,
> `@earendil-works/pi-coding-agent`) — the mature harness our own design spec
> (`HARNESS_DESIGN.md` §19) names as the shape we adapted — alongside the mainstream
> terminal-agent set: **opencode, Aider, Claude Code / Codex CLI, OpenHands**.

> **Naming key** (these collide if you're not warned):
> - **`#1`–`#7`** = the original *candidate articles* (e.g. `#1` "model proposes, harness disposes", `#5` "the verifier is the scorer").
> - **`T1`–`T5`** = *prospective targets* on the self-improvement ladder (`T1` verifier integrity, `T2` scaffold-not-model, `T3` what-pass@1-hides, `T4` capstone, `T5` oscillation warm-up).
> - **"Tier-1/2/3" / "T1 metrics"** = a *separate, unrelated* idea — the provenance tier of a number (Tier-1 = harness-native aggregate metric). **Not** article T1.
> - Evidence base for the empirical posts lives in [`research/failure-modes.md`](research/failure-modes.md), [`research/eval-baseline-2026-06-15.md`](research/eval-baseline-2026-06-15.md), and the corrected [`research/eval-baseline-2026-06-15-post-fixes.md`](research/eval-baseline-2026-06-15-post-fixes.md).

## Locked roadmap (Codex-cross-validated, 2026-06-16)

**This is the current plan; everything below it is the reasoning that produced it.** Cross-validated
with Codex (gpt-5.4, high effort) — strong agreement on content/framing; the **distribution layer is
Claude's addition** (Codex's blind spot — it critiqued only the content-risk axis, never visibility).

**Two guardrails on every post:**
- **Directional evidence / case study, never a model leaderboard** (n=5 seeds, few tasks, one project, evolving harness). Say "failure-mode discovery," "directional," not "model X beats Y."
- **The 6-question template:** *what motivated this · what we did · what we measured · what we inferred · what we're uncertain about · where we go from here.* That arc is the line between a research-engineering post and a vibes essay — and it generalizes past evals as the blog's scope grows.

**The spine** (publish in order; each gated on real evidence; home = personal blog, then cross-post):

| # | Post | Core + evidence artifact | Cross-post |
| --- | --- | --- | --- |
| 0 | "A verification-first harness, by its first eval run" | Context *through* the run — verifier-owned `done`, `TaskState`, permission chokepoint, journal-as-dataset, eval-is-scorer — introduced *only as needed*, framed "what this design made **observable**," not "why it's special." Evidence: post-fixes baseline. **Keep short, or merge into Post 1**; defer the full "what is this" to the repo + `ARCHITECTURE.md`. | repo README link |
| 1 | "Your agent benchmark may be measuring your **scaffold**, not the model" | Gemini **0.10 → 0.75**: pre-fix was a tool-schema/provider-compat bug, post-fix the real number. Strongest early public post — concrete, non-boastful, broadly useful. Evidence: `eval-baseline-…-post-fixes.md` + ADR-0019. | HN |
| 2 | "What **pass@1** hides" | secret-safety: gpt-5.1 concludes in 4 turns; sonnet/gemini avoid leaking but fail to conclude — ~88× tokens, end `incomplete`. Construct-validity / cost-per-solved. Evidence: post-fixes baseline + ADR-0020 + trajectories. | HN / Latent Space |
| 3 | "Failure modes from the first eval loop" | The catalog as narrative — scaffold/metric/model/security taxonomy, *classify before you judge*. **Appendix-grade: summarize, link the full catalog.** Evidence: `failure-modes.md`. | HF (if a dataset) |
| 4 | "A verifier is not a tool" / "the verifier is the scorer" | Methodology: `run_tests`/probes produce *evidence* but must not own *terminal success*; frozen plan; verifier-as-eval-scorer. Evidence: ADR-0007/0020 + verifier code. | Latent Space |
| 5 | "The model proposes, the harness disposes" | Synthesis manifesto — *after* the empirical posts earn it. Never the opener. | — |
| ⭐ | Flagship (later): **verifier integrity / oracle-gaming** | Goodhart, held-out checks, agents gaming evaluators, trustworthy self-improvement loops. **Build the demo first.** | LessWrong / Alignment Forum |

**Three amendments to the spine (Claude, post-cross-validation):**
1. **Distribution layer — the gap the roadmap *and* Codex both miss.** A sequence is not a publishing plan; for an unknown author **invisibility, not being-wrong, is the dominant risk.** Per post: choose the cross-post venue, write one X thread that hooks a *specific* community, and — highest-leverage — treat the **repo + `ARCHITECTURE.md` as credential #1** and **engage with others' eval/agent work** (reply, build on) instead of broadcasting into a void.
2. **Tighten 0 ↔ 1.** They draw on the same Gemini run; either merge, or keep 0 a short landing so it neither spends Post 1's punch nor relapses into a product pitch.
3. **Parallelize the flagship.** Posts 1/2/4 are good but *increasingly crowded* takes; the only non-commodity claim is oracle integrity under self-modification. **Build the oracle-gaming demo now, in parallel**, so the differentiated post isn't perpetually last — and consider a short "why oracle integrity is the hard problem" teaser to plant the flag early.

**Cadence:** pick a rhythm (≈ one post / 1–2 weeks). Seven posts without a cadence is a backlog, not a presence.

## Distribution plan (the layer amendment #1 names)

**Goals this serves:** contribute to public knowledge · get real feedback on process/conclusions ·
credential as an applied AI engineer pushing the frontier. **The governing risk for an unknown
author is invisibility, not being-wrong** (so syndicate deliberately), bounded by the rollout's
error-risk discipline below (so don't swing on an unverified claim).

**Canonical home — your own domain.** Every post lands first on an **owned static blog**
(`yourname.dev/writing`, Astro/Hugo/11ty — *not* Substack-as-primary, *not* Medium):
- Credibility compounds on a URL you own; a body of work *is* the credential and accrues SEO + backlinks. Platform posts credential the platform.
- Cross-posts set **`rel=canonical`** back to your copy, so syndication never cannibalizes your search authority.
- You control format/length/code-rendering — load-bearing for dense empirical posts.
- Run a **newsletter (Substack/Buttondown) in parallel as the owned-audience capture layer** (email > algorithm), *mirrored* from the blog, never the home.

**Per-post landing + cross-post (escalation ladder — warm up before you swing):**

| Post | Canonical | Primary cross-post | Also | Notes |
| --- | --- | --- | --- | --- |
| 1 scaffold-not-model | blog + X thread | **HN** | Lobste.rs | strongest hook → leads; validates voice while unproven. HN-shaped: empirical, counterintuitive, debuggable, no promo smell. |
| 2 pass@1-hides | blog + X | HN | Latent Space orbit | methodology crowd; pairs with #1. |
| 3 failure-mode catalog | blog + X | Lobste.rs, r/LocalLLaMA | HF (if a dataset) | reference/appendix piece; practitioner audience. |
| 4 verifier-is-not-a-tool | blog + X | HN | **LessWrong** | architecture + first safety-adjacent angle. |
| 5 manifesto | blog + X | **X-primary** | — | highest format-risk → ride the audience you've built, *not* a cold HN swing. |
| ⭐ oracle-gaming (flagship) | blog + X | **HN + LessWrong/AF** | HF (result file) | reward-hacking/eval-gaming is live in applied *and* alignment circles — the credibility peak. Bring the experiment, not an argument. |
| 0 context piece | blog | (evergreen) | repo README | publish *after* 1–2 land, as the "what is this" anchor everything links back to. |

**The arc (build the base before the peak):**
1. **Posts 1–2 — establish empirical credibility** on HN/X while the voice is unproven. Low-risk artifact-backed claims; an ignored post costs nothing.
2. **Posts 3–4 — convert readers into an owned audience** (RSS/newsletter/X follows). By here you're off the HN-roulette dependency.
3. **Manifesto (5) + flagship (⭐) cash it in** — the two highest-risk/highest-reward pieces ride the accumulated base + the research venues (LessWrong/AF), where the frontier-contributor credential actually lands.

**Two feedback rules (goal: real critique, visibly metabolized):**
- **A venue's comments on post N become post N+1's "what we're uncertain about" section** — visibly incorporating critique is itself a credibility signal and closes the loop.
- **Stagger, don't blast.** blog + X on day 0, HN day 1–2 (Tue–Thu AM ET land best), Reddit/LW later that week — so issues surfaced early are fixed before the bigger swing.

> Older sections below (**Risk-calibrated rollout**, **Distribution targets**, **Recommended
> publishing sequence**) are the prior reasoning; this section is the current plan and supersedes
> their venue sequencing where they differ.

---

## Framing — set the honest baseline first

avatar-harness is a ~12-day, TDD, phased MVP. Pi, opencode, and Aider have provider
maturity, LSP, broad language support, real users, and ecosystems we don't. **A post
that implies "we beat Pi/opencode" reads as naïve.** The differentiation is *architectural
stance*, not feature surface — and that's the more interesting story anyway. Lean into
"here is a different way to **shape** an agent," not "here is a better agent."

The cleanest hook is straight out of §19: Pi is the harness we **studied and copied
low-level mechanics from**, then **deliberately diverged from on three load-bearing
decisions**. "We forked the mechanics and inverted the spine" is a credible, specific,
non-marketing thesis.

## Consolidated thesis

The article should not argue that avatar-harness is more feature-complete than Pi,
opencode, Aider, Claude Code, Codex CLI, or OpenHands. It is not. The article should
argue that avatar-harness optimizes around a different source of authority:

> Popular coding agents are mostly **session/product-first**: the interactive workflow,
> transcript, tool stream, and user experience are the center of gravity. avatar-harness
> is **verification-first**: the task, structured state, permission gate, event journal,
> and external verifier are the center of gravity.

That gives us three clean contrasts:

- **Pi:** the closest architectural relative. We adopted low-level mechanics, then made
  `TaskState`, verifier-owned completion, and structured evidence the spine instead of
  the transcript/session.
- **opencode / mainstream terminal agents:** stronger product surfaces and provider
  maturity; our difference is not terminal polish but the kernel contract: "done" is
  disposed by external evidence, not by conversational stopping.
- **Aider / test-running agents:** running tests and feeding failures back is useful,
  but it is not the same as making verification the non-bypassable terminal authority
  with typed outcomes and a frozen rubric.

## The three deliberate divergences from Pi (§19) — the spine of every candidate

| Concern | Pi (message-centric) | avatar-harness | Article it powers |
| --- | --- | --- | --- |
| **What is "truth"** | `state.messages` is the source of truth | `TaskState` is primary; the message history is *derived* each turn | #2 State ≠ transcript |
| **What ends the loop** | `terminate: true` ends the loop | `terminate`/`final_answer` is a *proposal*; the harness-owned **Verifier** ends it on external evidence | #1 Verifier owns "done" |
| **How context shrinks** | a `compactionSummary` message | structured `evidence` degraded in place (recent verbatim → summary → names-only) | #2 / #5 observability |

What we **adopted unchanged** from Pi (worth naming, for credit + credibility): model-visible
`content` split from event/artifact detail; cancellation tokens; the observation-only event
emitter; a before-tool-call **control hook distinct from the emitter**; phase/capability-based
tool selection.

## Comparison lens — who owns what?

Use this as a compact section inside the flagship post, not as the entire post.

| Question | Pi / product-first agents | opencode / mainstream terminal agents | avatar-harness |
| --- | --- | --- | --- |
| **Who owns "done"?** | Session/tool/model flow. Pi's `terminate` shape is direct completion. | Conversational workflow with plan/build and user control. | Harness-owned `Verifier`; `final_answer` and `terminate` are proposals. |
| **What is state?** | The long-lived session and messages are central. | The interactive task/session is central. | Pydantic `TaskState`; messages are derived each turn. |
| **Where is permission?** | User- or extension-wired hooks / external sandboxing. | Product permission config and approval behavior. | Awaited control hook in the runner, plus workspace confinement and denylist. |
| **What is the artifact?** | Session history / changed files / user-facing output. | Conversation, plan/build activity, diffs, undo/share flows. | Terminal `TaskState` + artifact with outcome, files, commands, verification, diff ref. |
| **How do we improve it?** | Product ergonomics, provider support, extensions, sessions. | Product workflow and model/provider execution. | Eval-0: verifier/probe-scored tasks, JSONL trajectories, cost/tokens/failure taxonomy. |

## Raw differentiators (the material to draw from)

1. **Verification-terminated loop.** The harness-owned `Verifier` sets `outcome=success`
   only on tests/lint/diff evidence; the model never self-certifies. Aider's `--auto-test`
   feeds errors back, but the model/user still decides completion; Pi's `terminate: true`
   ends the loop outright; opencode/Claude Code end conversationally.
2. **TaskState, not the transcript, is the source of truth** — derived messages enable
   replay, journaling, and degrade-don't-drop compaction. This is the explicit Pi divergence.
3. **`task_kind` selects the verification contract** (`edit` / `investigate` /
   `test_only`) — investigative tasks aren't forced through "a diff must exist" gates,
   and explain-shaped work folds into `investigate` because it has the same contract.
4. **The verification plan is discovered, *frozen*, and journaled (ADR-0007)** — an
   authority transfer: the model may *pick among* frozen checks but never *authors* the
   rubric. Closes the "model grades its own homework" hole.
5. **Security as a single chokepoint** — path-confined `Workspace`; sensitive-path denylist
   blocks `.env`/keys *at the gate, before contents are read*; content-scrubbing redaction
   was **explicitly rejected** (prevention, not detection).
6. **Control vs. observation is a hard line** — permission is an awaited control hook;
   events are fire-and-forget and cannot alter control flow; the TUI cockpit is a pure
   subscriber, never in the loop.
7. **Replay/eval for free** — append-only JSONL + write-ahead journal (lossless *before*
   the lossy fan-out) + two-plane session; the **same Verifier that gates a run is the eval
   scorer**, and dogfood incidents become regression tests (ADR-0004 / 0011 / 0012).
8. **The build process itself** — TDD-phased, dogfood-driven roadmap (the secret-leak and
   oscillation incidents literally drove Phase 2.5), four worktree-isolated agents merging
   clean, ADR discipline, a rule-of-three complexity ceiling.

## Candidate articles

### #1 — "The model proposes, the harness disposes" ⭐ recommended flagship
- **Thesis:** model-self-certification is the central failure mode; a harness-owned verifier
  with typed outcomes and a *frozen* verification plan fixes it.
- **Alternate title:** "Your coding agent shouldn't decide when it's done."
- **Pi contrast:** Pi's `terminate: true` ends the loop; we route the same signal through
  the verifier. Clean, specific, sourced from §19.
- **opencode / mainstream contrast:** product-first agents are optimized around session
  ergonomics, planning/build modes, provider breadth, and user control. We are making a
  narrower claim: completion authority should sit outside the model/tool stream.
- **Draws on:** #1, #3, #4 + comparison lens.
- **Caveat to write honestly:** needs a "this requires a real test/lint signal; no-contract
  repos fail legibly" section — already designed for (ADR-0007 empty-plan path).
- **Why flagship:** it is the clearest wedge and lets every other differentiator become a
  supporting mechanism rather than a grab bag.

### #2 — "State is not a transcript: structured state as the spine of an agent"
- **Thesis:** deriving the message history from pydantic `TaskState` (vs. *being* the chat
  log) unlocks replay, journaling, and degrade-don't-drop compaction.
- **Pi contrast:** Pi is message-centric (`state.messages` is truth) and compacts via a
  summary message; we keep `TaskState` primary and compact structured evidence in place.
- **opencode / mainstream contrast:** session UX remains important, but the transcript is
  not the runtime's source of truth.
- **Draws on:** #2, #6, #7. Strongest systems-architecture piece.

### #3 — "A verifier is not a tool"
- **Thesis:** running tests as a model-callable tool is helpful but insufficient. A verifier
  must be outside the tool stream, outside the model's authority, and responsible for the
  terminal outcome.
- **Contrast:** `run_tests`/`run_command` can produce evidence; they cannot decide success.
  The verifier runs a frozen plan and records structured `VerifierResult`s.
- **Draws on:** #1, #4, #7. Best short technical essay if we want a sharper, narrower post
  than #1.

### #4 — "We let an agent read `.env` once. Here's what we changed"
- **Thesis:** a real dogfood incident (the `sk-or-v1` leak to log + context + a third-party
  API) → denylist-at-the-gate, and *why we rejected redaction*.
- **Contrast:** prevention-at-chokepoint vs. detection/scrubbing.
- **Draws on:** #5. Most narrative/clickable; security-flavored; very honest.
- **Caveat:** state this as in-harness defense, not host isolation. We still need the
  execution sandbox story for untrusted code.

### #5 — "Measuring agents without vibes: the verifier is the scorer"
- **Thesis:** turn dogfood failures into a scored regression suite; verifier-as-oracle;
  integrity under self-improvement (fingerprinted oracle, held-out checks).
- **Draws on:** #7 + ADR-0004 / 0011. Best for an eval-methodology thought-leadership piece.
- **Update from current repo:** Eval-0 has started, so this can be written as "what we built
  first and what integrity requires next," not only as a proposal.

### #6 — "Building a coding agent *with* coding agents"
- **Thesis:** the meta-story — TDD-phased, dogfood-driven, four worktree-isolated agents
  merging clean, ADRs as the decision log.
- **Draws on:** #8. Pairs with the existing `docs/project-timeline.html` graphic.

### #7 — "The cockpit is a subscriber, not the runtime"
- **Thesis:** a rich interactive UI does not have to swallow the control loop. The Textual
  cockpit observes typed events and sends approvals/cancel decisions back through explicit
  control methods.
- **Contrast:** terminal-agent products naturally center the long-lived session. We keep the
  session as a wrapper over verified tasks.
- **Draws on:** #6, #7 and ADR-0001/0002. Good second-tier post after the flagship.

## Ranking for the stated goals

Scoring lens: brief/dense read, real evidence, professional/researcher audience fit, does
not posture as an incumbent competitor, improves the project's path toward closed-loop
self-improvement.

| Rank | Candidate | Why it ranks here | Evidence required before posting |
| --- | --- | --- | --- |
| **1** | **#5 — Measuring agents without vibes: the verifier is the scorer** | Best match to the long-term thesis: reliable eval signals as the foundation for closed-loop improvement. It can be short, empirical, and non-competitive. | One Eval-0 table: task id, solved?, probe/verifier signal, turns, tokens, cost, failure bucket. Include one journal excerpt. |
| **2** | **#3 — A verifier is not a tool** | Strongest bite-sized systems insight. It directly explains why eval/probe signals need to sit outside the model/tool stream. | A tiny trace diagram or real run snippet showing `run_tests` as evidence vs. verifier-owned `outcome`. |
| **3** | **#1 — The model proposes, the harness disposes** | Best broad flagship, but it risks sounding manifesto-like unless backed by #3/#5 evidence. Use it after at least one empirical post. | Before/after or failure example: model says done, verifier rejects, repair loop acts. |
| **4** | **#4 — We let an agent read `.env` once** | Very readable and concrete. Good for HN-style curiosity, but less central to self-improvement unless tied to eval regression. | The dogfood incident path, the denied-path test, and a regression task proving zero secret bytes in state/journal. |
| **5** | **#2 — State is not a transcript** | High-quality architecture piece, but more abstract. Strong once readers already understand verifier/eval stakes. | Show a context packet before/after compaction and why replay stays possible. |
| **6** | **#7 — The cockpit is a subscriber, not the runtime** | Useful implementation note. Lower priority because UI/control-plane separation is a supporting insight, not the research arc. | Event/control sequence diagram plus one approval event/control resolution example. |
| **7** | **#6 — Building a coding agent with coding agents** | Good personal credibility story, but weakest for research/professional signal unless paired with data about parallel worktree agents or review outcomes. | Concrete stats: PRs, test counts, defects caught only at integration, time-to-merge. |

Practical implication: publish **#5 first if we can produce one credible Eval-0 table**. If not,
publish **#3 first** as the smallest defensible technical insight, then use #5 once the table is
ready. Demote #1 from "first post" to "synthesis post" unless it carries empirical evidence.

## Prospective article targets by developmental pathway

These are future posts that should be *earned by project work*, not written ahead of evidence.

### The capability ladder (the spine the paths below hang on)

Closed-loop self-improvement is a staircase; each rung is a publishable finding, and the
**highest-ceiling, most credentialing posts live on the upper rungs** because that is where
the *open* problems are. Rung A is well-trodden in the SWE-agent/verifier literature — the
novelty starts at B.

| Rung | Capability | Repo status | Open problem → post |
| --- | --- | --- | --- |
| **A** | Reliable *single-run* verification | built (§12, ADR-0007) | (solved) → manifesto only (#1) |
| **B** | Reliable *cross-run* measurement | Eval-0 in progress (PR #47) | harness-vs-capability attribution → Path A.2, "scaffold not model" |
| **C** | Eval signal *gates change* | partial (evals held to gates, PR #49) | what metric to gate on → A.3 cost-per-solved, "what pass@1 hides" |
| **D** | Agent proposes changes to *itself/its harness* | not built | first real loop → A.5 rejection-sampling/best-of-N |
| **E** | Oracle stays honest under D (anti-Goodhart) | **designed (ADR-0011), not demonstrated** | **the crux → A.4, elevated below** |

**Elevation call:** for a *research* audience, **A.4 "Verifier integrity before
self-improvement" is the real flagship, not a mid-path item.** It is the Goodhart /
spec-gaming / RSI-safety problem in a *mechanized, reproducible* form — the single piece
most likely to credential serious-contributor status (LessWrong / Alignment Forum land).
The empirical demo that unlocks it is small and concrete: **construct one task where the
cheapest path to green is to edit a test or the verifier; show the agent taking it; show the
fingerprinted/held-out oracle catching it; report gamed-pass-rate vs. true-pass-rate.** That
one experiment is also the highest-leverage *build* item — it simultaneously validates rung
E, produces the flagship's evidence, and de-risks the whole closed-loop direction. Treat #5
(verifier-as-scorer) and A.4 as a **two-post pair**: #5 establishes the signal exists; A.4
establishes the signal can't be gamed.

### Path A — Eval signal quality → closed-loop self-improvement

1. **"What counts as solved? Probe-owned success vs. verifier-owned success."**
   - Evidence: compare no-probe verifier tasks to probe-authored tasks; show one case where strict verifier failed a working artifact and why the probe became authoritative.
   - Purpose: explain the Eval-0 scoring revision without overclaiming.
2. **"Failure buckets for coding agents: budget, verifier, blocked, loop, probe."**
   - Evidence: a small histogram over Eval-0 runs.
   - Purpose: show that the harness produces actionable learning signals, not just pass/fail.
3. **"Cost per solved task is the only model benchmark I care about."**
   - Evidence: model matrix with pass@1, tokens, dollars, turns, and failure modes.
   - Purpose: professional signal for practical AI engineering; directly supports model selection.
4. **"Verifier integrity before self-improvement."**
   - Evidence: protected oracle paths, fingerprinted checks, hidden probes, or at least one tamper demonstration.
   - Purpose: bridge from eval harness to trustworthy closed-loop improvement.
5. **"A tiny rejection-sampling loop for coding agents."**
   - Evidence: best-of-N on Eval-0 with verifier/probe scoring; show uplift and failure cases.
   - Purpose: first real closed-loop self-improvement result.

### Path B — Harness architecture as research instrumentation

1. **"The journal is the dataset."**
   - Evidence: one JSONL trajectory turned into a compact failure report.
   - Purpose: position the project as instrumentation for agent research.
2. **"Structured state beats transcript scraping."**
   - Evidence: same run analyzed from transcript vs. `TaskState`/events; show what transcript misses.
   - Purpose: systems insight for agent engineers.
3. **"Control hooks are not events."**
   - Evidence: approval event plus `resolve_approval()` control path; show subscriber failure cannot alter execution.
   - Purpose: crisp runtime-design lesson.

### Path C — Safety and reliability from dogfood incidents

1. **"The `.env` incident became a regression test."**
   - Evidence: original failure class, denylist gate, resolved-path backstop, secret-safety eval.
   - Purpose: concrete reliability story.
2. **"Silent truncation is a bug, not a context strategy."**
   - Evidence: before/after context packet and an eval/dogfood case where visible truncation changed behavior.
   - Purpose: bite-sized context-engineering lesson.
3. **"Native tool calls fixed one class of patch failures, not all editing failures."**
   - Evidence: malformed JSON decision trace before; successful `write_file`/tool-call trace after.
   - Purpose: pragmatic agent-tooling lesson.

### Path D — Product/kernel boundary

1. **"Why the TUI is not the agent runtime."**
   - Evidence: typed event stream and two-plane session API.
   - Purpose: credible engineering architecture post.
2. **"The task, not the session, is the unit of truth."**
   - Evidence: multi-turn session with per-goal `TaskState`s and artifacts.
   - Purpose: differentiates from product-first terminal agents without attacking them.

## Risk-calibrated rollout (early-stage) — visibility vs. error risk

We are an unknown author starting out. The governing principle: **match a platform's reach and
scrutiny to the *confidence* of the claim.** High-reach, permanent-record, expert-scrutiny venues
(HN, LessWrong/AF, X) punish a wrong claim hard — and a single confidently-wrong technical post is
an expensive credibility hit for a new name. Owned, editable, low-amplification channels let you
build a track record and fix mistakes cheaply.

This session is itself the cautionary tale: the "sonnet thrashes against the denylist" claim was
*confidently wrong* and only caught by reading the trajectories. Had that shipped to HN/LW first,
it would have been dissected publicly. So:

**Format risk gradient (low → high error-exposure):**
1. **Reproducible artifact** (the repo, an ADR, the failure-mode catalog, an eval result file + the command to reproduce it). Hardest to be "wrong" — you ship the data. *Safest and, for a technical/research audience, most credentialing.*
2. **Empirical post** ("here's a failure mode, the trajectory, the mechanism"). Bounded risk **iff** you separate *measured fact* from *interpretation* (the sonnet lesson) and show the artifact.
3. **Thesis / manifesto** (`#1` "agents should be built this way"). A claim about the world — invites "you're wrong about X." Hold until empirical posts back it.
4. **Comparison** (vs. Pi / opencode / Aider). Highest risk: claims about *other people's* systems; one stale detail and the thread is about your error, not your idea. Keep out of headlines; verify against their current source first.

**Platform ladder (earn your way up):**
- **Phase 0 — now, ~zero risk:** ship *artifacts* on the **personal/project blog + GitHub**. The repo, ADRs, `research/failure-modes.md`, and `research/eval-baseline-2026-06-15.md` are already publishable-grade and near-impossible to nitpick. Surface them with low-key **X threads**. Goal: establish an empirical, mechanism-first voice and a citable trail.
- **Phase 1 — after 2–3 solid artifact/empirical posts:** take the *single strongest, most-verified* empirical piece to **Hacker News** (the failure-mode catalog or the Eval-0 table — concrete, reproducible, non-promotional). One at a time; let it stand on evidence.
- **Phase 2 — after the eval methodology is mature:** **Latent Space / AI-Engineer orbit** for `#5`/`#3` and cost-per-solved; **Hugging Face** once a result file/dataset is the artifact.
- **Phase 3 — after a *real experiment* (the oracle-gaming demo, T1/A.4):** **LessWrong / Alignment Forum**. That audience is the least forgiving of hand-waving — bring an experiment, not an argument.

**Error-armor for every post:** (1) a visible *"what I measured" vs. "what I think it means"* split; (2) the reproduce command + raw artifact link; (3) a skeptical pre-publish pass (run the draft through an adversarial review, like a PR review) before any Phase-1+ venue; (4) hedge interpretation, never the data.

## Distribution targets

The menu below is the full set; the **rollout above sequences it by error-risk**. Use a "home post
first, then cross-post/link" strategy — the canonical source is the personal/project blog so the
archive accrues to the author and project; external sites are for discovery.

| Target | Best-fit posts | Why it fits | Post shape |
| --- | --- | --- | --- |
| **Latent Space / AI Engineer orbit** | #5, #3, model-matrix/cost-per-solved posts | Audience is explicitly AI engineers and agent-infra builders. AI Engineer describes itself as serving AI engineers, founders, and AI architects; Latent Space brands itself around agents, models, infra, and AI for science. | Dense 800-1200 word technical note with a table and repo link. |
| **Hacker News** | `.env` incident, "A verifier is not a tool", "silent truncation" | HN rewards intellectually interesting engineering stories and punishes promotion. Their guidelines explicitly emphasize curiosity and original sources. | Plain title, no hype, concrete failure + fix + caveats. |
| **Hugging Face Community Blog** | Eval-0, model matrix, probe-owned success | HF has community articles, research/eval content, and agent/benchmark posts. Strong fit once there is a reproducible artifact or dataset/result file. | Results-first post with task specs, reproducibility commands, and artifacts. |
| **LessWrong / Alignment Forum** | verifier integrity, closed-loop self-improvement, oracle tampering | Better fit after empirical results and integrity framing; audience is more research/safety/evals oriented. | Argument + experiment. Avoid product framing; emphasize Goodhart, oracle integrity, and failure modes. |
| **Personal/project blog + X/LinkedIn threads** | All posts | Best for author signal and canonical archive. Threads can summarize the result and route attention to the canonical post. | One insight per post; include one chart/table and one concrete trace. |

## Style constraints for all posts

- **Length:** 700-1200 words, or 5-8 dense sections.
- **Evidence:** every post needs at least one of: run table, event/journal excerpt, diff, failed/passing test pair, eval result, or architecture diagram tied to source.
- **Tone:** "here is a failure mode and a mechanism" over "our harness is better."
- **Claim boundary:** distinguish implemented behavior, proposed ADRs, and future pathway.
- **Reader takeaway:** one reusable principle an AI engineer can apply without adopting avatar-harness.

## Note on a comparison-table post
A "how 5 harnesses answer: who owns 'done', what is state, where's the security boundary"
table is compelling but invites "you got Pi/opencode wrong" nitpicks. **Use it as a section
inside #1 or #2, not as a standalone post** — and verify each competitor's current behavior
before publishing (our notes are design-level, not a code audit of theirs).

## Decisions locked (2026-06-15)

- **First artifact to produce:** the **Eval-0 baseline matrix** — run the existing Eval-0
  harness across a model matrix to get real pass@1 / iterations / tokens / cost / failure-bucket
  data. This is the data dependency for #5, "what pass@1 hides," and the scaffold-vs-capability
  (T2) post — three posts unblocked by one build.
- **Canonical home:** a **personal/project blog** holds the canonical version of every post;
  external venues are discovery-only cross-posts/links (HN, Latent Space orbit, LessWrong),
  so the archive and author signal accrue to one place.

## Recommended publishing sequence (post-decision)

1. **Post #0 — warm-up, ships now (no build):** **T5, "Anatomy of an oscillating agent run."**
   Data already in the dogfood logs. Home blog → cross-post HN. Establishes the empirical,
   mechanism-first voice while the baseline runs.
2. **Build gate:** run the **Eval-0 baseline matrix** → the publishable result table.
3. **Post #1 — data-backed lead:** **#5, "the verifier is the scorer,"** opened with the
   baseline table. Home blog → cross-post Latent Space orbit.
4. **Post #2 — the validity argument:** **T2 / "what pass@1 hides,"** reusing the same matrix
   (efficiency + oscillation profiles behind equal pass@1).
5. **Post #3 — synthesis manifesto:** **#1, "the model proposes, the harness disposes,"** now
   earned by the two data posts above.
6. **Build + Post #4 — the credentialing flagship:** the **oracle-gaming demo** → **A.4,
   "verifier integrity before self-improvement."** Home blog → cross-post LessWrong / Alignment Forum.
7. **Post #5 — capstone:** the closed-loop self-improvement agenda, anchored to the A.4 + #5 data.

## Open items before drafting
- [x] Pick the synthesis flagship: #1. Publishing order is conditional: lead with #5 if
      Eval-0 has enough data; otherwise lead with #3.
- [x] Decide audience/venue: AI-agent engineers and eval-minded researchers; keep posts short,
      empirical, and mechanism-first.
- [x] First artifact = Eval-0 baseline matrix. Canonical home = personal/project blog. (2026-06-15)
- [ ] Define the Eval-0 baseline run scope: model list, task set, repeats, budget cap.
- [ ] Verify Pi/opencode/Aider specifics against their current docs before any public claim.
- [ ] Run or collect the first Eval-0 result table suitable for publication.
- [ ] Decide whether to reuse the timeline graphic and the "5 invariants" framing. Use the
      invariants only as a compact sidebar, not as the article frame.
- [ ] Decide whether to name OpenCode/Pi/Aider in the headline or keep them in a comparison
      section to reduce nitpick surface.
