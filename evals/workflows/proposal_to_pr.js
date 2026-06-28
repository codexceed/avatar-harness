// Workflow B — `proposal-to-pr` (ADR-0024, Increment 3).
//
// The SPENDING half of the evals-driven improvement loop, run once per *funded* proposal (Gate 1
// already happened: a human read the Workflow-A digest and chose to build this entry). It turns one
// proposal into a TDD'd, statistically-validated PR — and NEVER merges it (Gate 2 stays human).
//
// Shape (design §5.1 / §6):
//   Scope    → reconstruct the typed ChangeProposal from the funded digest entry; route on
//              blast-radius/risk. A global or grader-touching change is ADR-routed (drafted as an
//              ADR-proposal PR, ZERO eval spend) — never auto-implemented.
//   Build    → a fresh git worktree (isolation); a TDD subagent drives the fix to local green.
//   Validate → shell out to the deterministic Layer-1 spender `python -m evals.validate`: the canary
//              ladder (local → 1-seed canary on affected models → full matrix) against grading assets
//              FROZEN from a trusted ref, verdict via paired McNemar + per-model agnosticism.
//   Rework   → bounded loop: a STOP/FAIL verdict is fed back to the builder; a hard cap then escalates
//              to the ADR route instead of burning more eval budget.
//   PR       → open a PR citing baseline rows + the digest entry + the validate verdict. No merge.
//
// This is the ONLY workflow that spends eval budget (the validate re-runs are live), which is why it
// is gated behind human funding. Like Workflow A it is a Layer-2 Claude `Workflow` script: executed
// on demand via the Workflow tool, not by make/pytest, and not covered by the Python gates — the
// determinism (the ladder, the McNemar verdict, the frozen assets) lives in `evals/validate.py`,
// which IS TDD'd. See evals/improvement-loop-design.md §3–§6 and evals/CLAUDE.md §1,§6,§7,§8.
//
//   Invoke:  Workflow({ scriptPath: "evals/workflows/proposal_to_pr.js", args: {
//              digest: "evals/proposals/<stamp>/proposals.md",  // the funded digest
//              entry: 1,                                        // which "## N · …" entry to build
//              baseline: "evals/results/<stamp>.jsonl",         // the pinned baseline to validate against
//              trusted_ref: "main",                             // ref to freeze grading assets from
//              models: "gpt-5.1,sonnet,gemini",                 // full matrix models (default: baseline's)
//              max_rework: 2 } })                               // hard rework cap before ADR-escalation

export const meta = {
  name: 'proposal-to-pr',
  description: 'One funded proposal → a TDD\'d, McNemar-validated PR (the only eval spender; never merges)',
  whenToUse: 'After Gate 1: a human funded a specific entry in a Workflow-A proposals digest and wants it built + validated.',
  phases: [
    { title: 'Scope', detail: 'reconstruct the typed ChangeProposal from the funded entry; route on blast-radius' },
    { title: 'Build', detail: 'fresh worktree + TDD subagent → local green' },
    { title: 'Validate', detail: 'shell out to evals.validate: canary ladder over frozen assets + McNemar' },
    { title: 'Open PR', detail: 'cite rows · digest · verdict; never merge (Gate 2 is human)' },
  ],
}

// The typed A→B seam (mirrors evals/proposal.py ChangeProposal). Workflow A's human digest dropped
// the machine fields (ADR-0031); we reconstruct them here from the funded entry + the baseline, so
// the scope/route are explicit and the validate command is derived, not guessed.
const PROPOSAL_SCHEMA = {
  type: 'object',
  required: ['id', 'title', 'blast_radius', 'touches_grader', 'remediation_type', 'target_tasks', 'affected_models', 'tdd_plan'],
  properties: {
    id: { type: 'string', description: 'kebab-case id (the entry slug)' },
    title: { type: 'string' },
    blast_radius: { type: 'string', enum: ['local', 'global'], description: 'global = always-on / cross-cutting (e.g. a prompt rule)' },
    touches_grader: { type: 'boolean', description: 'edits specs/probes/fixtures/verifier/scoring — always ADR-routed' },
    remediation_type: { type: 'string', enum: ['prompt_instruction', 'guardrail_check', 'code_logic', 'doc_only'] },
    target_tasks: { type: 'array', items: { type: 'string' }, description: 'the task id(s) this fix targets (the canary tasks)' },
    affected_models: { type: 'array', items: { type: 'string' }, description: 'models that failed the target (the canary models)' },
    tdd_plan: { type: 'array', items: { type: 'string' }, description: 'the failing-test list to write first (red), then make green' },
    evidence: { type: 'array', items: { type: 'string' }, description: 'baseline row refs / digest section the fix answers' },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['passed', 'stage_reached', 'summary'],
  properties: {
    passed: { type: 'boolean', description: 'the candidate cleared the full ladder (validate exit 0)' },
    stage_reached: { type: 'string', enum: ['local', 'canary', 'matrix'] },
    summary: { type: 'string', description: 'the verdict line validate printed (pass@1 delta + McNemar)' },
    raw: { type: 'string', description: 'the full stdout/stderr from python -m evals.validate, for the PR body' },
  },
}

const BUILD_SCHEMA = {
  type: 'object',
  required: ['ok', 'detail'],
  properties: {
    ok: { type: 'boolean', description: 'local green reached (tests written + passing, make check clean) in the worktree' },
    detail: { type: 'string', description: 'one line: what was changed + the make check result' },
    files_changed: { type: 'array', items: { type: 'string' } },
  },
}

// `args` may arrive as an object or a JSON string — coerce so a direct Workflow({scriptPath, args}) works.
const a = typeof args === 'string' ? JSON.parse(args) : (args ?? {})
const digest = a.digest
const entry = a.entry
const baseline = a.baseline
const trustedRef = a.trusted_ref ?? 'main'
const maxRework = Number.isInteger(a.max_rework) ? a.max_rework : 2
if (!digest || entry == null || !baseline) {
  throw new Error('proposal-to-pr: args.digest, args.entry, and args.baseline are required')
}

// ── Phase 1 · Scope — reconstruct the typed proposal, then route on blast-radius ────────────────
phase('Scope')
const proposal = await agent(
  `You are scoping a FUNDED change proposal for the avatar-harness improvement loop (ADR-0024).\n` +
    `Read entry #${entry} ("## ${entry} · …") of the digest ${digest}, plus the baseline summary ` +
    `${baseline.replace(/\.jsonl$/, '.summary.json')} (for the models/tasks) and the failing rows in ${baseline}.\n` +
    `Reconstruct the typed ChangeProposal fields (the machine seam the digest omitted per ADR-0031):\n` +
    `  • target_tasks / affected_models — the task(s) and model(s) the failure occurred on (from the rows).\n` +
    `  • blast_radius — "global" if the fix is always-on / cross-cutting (a prompt rule, a default), else "local".\n` +
    `  • touches_grader — true if the fix would edit specs/probes/fixtures/the verifier/scoring.\n` +
    `  • remediation_type — prompt_instruction | guardrail_check | code_logic | doc_only.\n` +
    `  • tdd_plan — the concrete failing-test list to write first (the red phase), grounded in the real files.\n` +
    `Spot-check the file(s) the fix would touch so the plan is correct. Do NOT implement anything or run evals.`,
  { label: `scope:${entry}`, phase: 'Scope', schema: PROPOSAL_SCHEMA },
)

// Deterministic governance route — mirrors evals/proposal.py ChangeProposal.route(): a global or
// grader-touching change must be PROPOSED as an ADR (a human decides), never auto-built+validated.
const route = proposal.blast_radius === 'global' || proposal.touches_grader ? 'adr_only' : 'implement'
log(`scoped "${proposal.title}" → ${proposal.blast_radius}${proposal.touches_grader ? ' · grader-touching' : ''} → route=${route}`)

async function draftAdrPr(reason) {
  // ADR route / rework-exhausted escalation: draft an ADR-proposal PR with ZERO eval spend — a human
  // decides a global/grader-touching (or stubbornly-unvalidated) change; we never auto-implement it.
  phase('Open PR')
  return agent(
    `Draft an ADR-proposal PR for this harness change — do NOT implement source or run evals.\n` +
      `Proposal: ${JSON.stringify(proposal)}\nReason for the ADR route: ${reason}\n` +
      `Write a new Nygard-style ADR under docs/adr/ (next number; add it to docs/adr/README.md), capturing the ` +
      `decision, the rejected alternatives, and the blast-radius/grading-surface risk that forces a human decision. ` +
      `Open a branch + PR (the repo's Conventional-Commit + PR-template conventions) citing the digest entry #${entry} ` +
      `and the baseline ${baseline}. Do NOT merge.`,
    { label: 'adr-pr', phase: 'Open PR' },
  )
}

if (route === 'adr_only') {
  const out = await draftAdrPr('global or grader-touching blast-radius (ADR-0024 §safety; ADR-0011 unbuilt)')
  return { id: proposal.id, route, merged: false, result: out }
}

// ── Phase 2–3 · Build → Validate, in one persistent worktree, with bounded rework ───────────────
// One worktree persists across the rework loop so the candidate's commits survive for the PR (the
// Workflow `isolation:'worktree'` option is per-agent/ephemeral, so we manage the worktree explicitly).
const branch = `feat/loop-${proposal.id}`
const wt = await agent(
  `Create an isolated git worktree to build a candidate harness change in.\n` +
    `Run: \`git worktree add -b ${branch} ../wt-${proposal.id} ${trustedRef}\` from the repo root (adjust the path if it ` +
    `exists). Return ONLY the absolute path to the new worktree on the last line.`,
  { label: 'worktree', phase: 'Build' },
)
const worktree = (wt || '').trim().split('\n').pop().trim()
if (!worktree) throw new Error('proposal-to-pr: could not create a worktree')

const modelsArg = a.models ?? (proposal.affected_models || []).join(',')
const tasksArg = a.tasks // optional explicit full-matrix task set; else validate uses the baseline's
let verdict = null
for (let attempt = 0; attempt <= maxRework; attempt++) {
  const note = attempt === 0 ? '' : `\nThe previous attempt FAILED validation: ${verdict?.summary}\nRevise the fix accordingly.`
  // Build / rework (TDD) in the worktree.
  const build = await agent(
    `You are implementing a FUNDED harness fix under strict TDD, in the worktree at ${worktree} (work ONLY there).\n` +
      `Proposal: ${JSON.stringify(proposal)}${note}\n` +
      `Protocol: (1) write the failing test(s) from tdd_plan first and confirm they fail; (2) implement the smallest ` +
      `fix to make them pass; (3) get \`make check\` clean. Do NOT touch the grading surface (evals/tasks, evals/probes, ` +
      `evals/fixtures, the verifier, scoring) — that is ADR-routed, not built here. Commit your work on ${branch}.\n` +
      `Return ok=true only if the new tests pass AND make check is clean.`,
    { label: `build:${attempt}`, phase: 'Build', schema: BUILD_SCHEMA },
  )
  if (!build?.ok) {
    log(`build attempt ${attempt} did not reach local green: ${build?.detail}`)
    continue
  }
  // Validate — the only eval spend: shell out to the deterministic canary ladder over FROZEN assets.
  phase('Validate')
  const cmd =
    `cd ${worktree} && uv run python -m evals.validate ` +
    `--baseline ${baseline} --worktree ${worktree} --trusted-ref ${trustedRef} ` +
    `--affected-models ${(proposal.affected_models || []).join(',')} ` +
    `--target-tasks ${(proposal.target_tasks || []).join(',')} ` +
    `--models ${modelsArg}` +
    (tasksArg ? ` --tasks ${tasksArg}` : '')
  verdict = await agent(
    `Run the validation ladder and report its verdict verbatim. Execute EXACTLY this (it spends eval budget — ` +
      `run it once):\n\`${cmd}\`\n` +
      `It prints one "[stage] PASS/STOP" line per rung then "verdict: PASS|FAIL (reached <stage>)" and a summary; ` +
      `exit 0 = PASS. Return passed (from the exit code / verdict line), stage_reached, the summary line, and the ` +
      `full output as raw. Do NOT edit any files or re-run it.`,
    { label: `validate:${attempt}`, phase: 'Validate', schema: VERDICT_SCHEMA },
  )
  log(`attempt ${attempt}: validate → ${verdict?.passed ? 'PASS' : 'FAIL'} (${verdict?.stage_reached}) — ${verdict?.summary}`)
  if (verdict?.passed) break
}

// ── Phase 4 · Open PR (or escalate) — never merge ───────────────────────────────────────────────
if (!verdict?.passed) {
  log(`exhausted ${maxRework} rework attempt(s) without a passing matrix → escalating to the ADR route`)
  const out = await draftAdrPr(`rework cap (${maxRework}) hit without a validated improvement: ${verdict?.summary ?? 'no green build'}`)
  // Clean up the candidate worktree (its branch is abandoned).
  await agent(`Remove the abandoned worktree: \`git worktree remove --force ${worktree}\` and delete branch ${branch}.`, {
    label: 'cleanup', phase: 'Open PR',
  })
  return { id: proposal.id, route, validated: false, merged: false, result: out }
}

phase('Open PR')
const pr = await agent(
  `Open a pull request for the VALIDATED harness fix on branch ${branch} (worktree ${worktree}). Do NOT merge it — ` +
    `Gate 2 (review + merge) stays human.\n` +
    `Push the branch and open the PR following the repo conventions (Conventional-Commit title; the PR template's ` +
    `Description / Motivation / Changes / Testing sections). The body MUST make the review tractable by citing the ` +
    `evidence: the funded digest entry (#${entry} of ${digest}), the baseline (${baseline}), and the validation verdict ` +
    `below verbatim so a reviewer can confirm "solved, not gamed":\n${verdict.raw ?? verdict.summary}\n` +
    `Return the PR URL.`,
  { label: 'open-pr', phase: 'Open PR' },
)

return {
  id: proposal.id,
  route,
  validated: true,
  merged: false, // never — Gate 2 is human (ADR-0024 §safety)
  verdict: { passed: verdict.passed, stage: verdict.stage_reached, summary: verdict.summary },
  pr,
}
