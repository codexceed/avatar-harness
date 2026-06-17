// Workflow A — `evals-to-proposals` (ADR-0024, Increment 2).
//
// The read-only half of the evals-driven improvement loop: an eval results dir → a reviewed,
// scored, deduplicated set of ChangeProposals. ZERO eval spend (it never re-runs the matrix);
// it spends only Claude tokens on the reasoning leaves, and only for *novel* clusters.
//
// This is a Claude `Workflow`-tool script (Layer 2). It is EXECUTED ON DEMAND, opt-in, via the
// Workflow tool — it is not run by `make`/pytest and is intentionally not covered by the Python
// gates. The deterministic work (load → distill → cluster → triage prefilter) lives in Layer-1
// Python (`python -m evals.cluster`); this script only orchestrates the reasoning subagents and
// the reconciliation barrier around it. See evals/improvement-loop-design.md §3–§5.
//
//   Invoke:  Workflow({ scriptPath: "evals/workflows/evals_to_proposals.js",
//                       args: { results: "evals/results/<stamp>.jsonl", stamp: "<stamp>" } })

export const meta = {
  name: 'evals-to-proposals',
  description: 'Eval results → deduplicated, scored ChangeProposals (read-only, zero eval spend)',
  whenToUse: 'After a human-gated `make eval --no-cleanup` run, to triage failures into reviewed proposals.',
  phases: [
    { title: 'Triage', detail: 'Layer-1 deterministic prefilter: cluster failures + token-overlap dedup' },
    { title: 'Analyze', detail: 'one judge subagent per cluster — confirm novel vs known, classify A/B/C/D' },
    { title: 'Propose', detail: 'one subagent per novel mode — a TDD ChangeProposal' },
    { title: 'Reconcile', detail: 'barrier: mutual + codebase compatibility; write the proposals dir' },
  ],
}

const CLUSTER_SCHEMA = {
  type: 'object',
  required: ['clusters'],
  properties: {
    clusters: {
      type: 'array',
      items: {
        type: 'object',
        required: ['task', 'outcome', 'runs', 'symptom', 'prefilter_novel'],
        properties: {
          task: { type: 'string' },
          outcome: { type: 'string' },
          models: { type: 'array', items: { type: 'string' } },
          runs: { type: 'integer' },
          symptom: { type: 'string' },
          sample_actions: { type: 'array', items: { type: 'string' } },
          prefilter_novel: { type: 'boolean' },
          prefilter_catalog_match: { type: ['string', 'null'] },
          prefilter_adr_match: { type: ['string', 'null'] },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['novel', 'mode'],
  properties: {
    novel: { type: 'boolean' },
    mode: { type: 'string', description: 'catalog id (e.g. "C1") / ADR (e.g. "0022") if known, else "novel:<slug>"' },
    bucket: { type: 'string', enum: ['A', 'B', 'C', 'D'], description: 'A harness · B measurement · C model · D security' },
    rationale: { type: 'string' },
  },
}

const PROPOSAL_SCHEMA = {
  type: 'object',
  required: ['id', 'mode', 'title', 'remediation_type', 'blast_radius', 'tdd_plan', 'body'],
  properties: {
    id: { type: 'string' },
    mode: { type: 'string' },
    title: { type: 'string' },
    remediation_type: { type: 'string', enum: ['prompt_instruction', 'guardrail_check', 'code_logic', 'doc_only'] },
    blast_radius: { type: 'string', enum: ['local', 'global'] },
    touches_grader: { type: 'boolean' },
    target_tasks: { type: 'array', items: { type: 'string' } },
    tdd_plan: { type: 'array', items: { type: 'string' } },
    evidence: { type: 'array', items: { type: 'string' } },
    body: { type: 'string' },
  },
}

const results = args?.results
const stamp = args?.stamp ?? 'run'
if (!results) throw new Error('evals-to-proposals: args.results (a results JSONL path) is required')

// ── Phase 1 · Triage (deterministic Layer-1 prefilter, run via a thin shell-out agent) ─────────
phase('Triage')
const triaged = await agent(
  `Run the deterministic Layer-1 prefilter and return its output verbatim as JSON.\n` +
    `Execute exactly: \`python -m evals.cluster ${results}\`\n` +
    `That prints one JSON object per line (a ClusterVerdict: {cluster:{task,outcome,models,runs,symptom,` +
    `sample_actions}, triage:{novel,catalog_match,adr_match,score}}). Collect them and return a single object ` +
    `{clusters:[...]} where each item flattens the cluster fields plus prefilter_novel=triage.novel, ` +
    `prefilter_catalog_match=triage.catalog_match, prefilter_adr_match=triage.adr_match. Do not analyze — just run and report.`,
  { label: 'triage:cluster', phase: 'Triage', schema: CLUSTER_SCHEMA, effort: 'low' },
)
const clusters = triaged?.clusters ?? []
log(`triaged ${clusters.length} cluster(s); prefilter flagged ${clusters.filter((c) => c.prefilter_novel).length} candidate-novel`)
if (clusters.length === 0) return { stamp, proposals: [], note: 'no failures to triage' }

// ── Phases 2–3 · Analyze (judge confirms novelty) → Propose, pipelined per cluster ─────────────
// The prefilter is coarse (token overlap can't see the catalog's descriptive vocabulary), so the
// JUDGE is authoritative: every cluster is examined; a confidently-known one is linked, not re-debugged.
const proposals = (
  await pipeline(
    clusters,
    (c) =>
      agent(
        `You are triaging a failure cluster against institutional memory. Decide if it is NOVEL or a KNOWN mode.\n` +
          `Cluster: ${JSON.stringify(c)}\n` +
          `Read docs/research/failure-modes.md (the A/B/C/D catalog) and docs/adr/README.md (open/Proposed ADRs). ` +
          `The deterministic prefilter said novel=${c.prefilter_novel} (catalog=${c.prefilter_catalog_match}, adr=${c.prefilter_adr_match}) ` +
          `— treat that as a hint only; you make the call from the symptom + sample_actions + the catalog text. ` +
          `If it matches a catalogued mode or an open ADR, return novel=false with that id. Else novel=true and classify the A/B/C/D bucket.`,
        { label: `analyze:${c.task}/${c.outcome}`, phase: 'Analyze', schema: VERDICT_SCHEMA },
      ),
    (verdict, c) => {
      if (!verdict || !verdict.novel) {
        log(`known: ${c.task}/${c.outcome} → ${verdict?.mode ?? 'linked'} (no proposal)`)
        return null // known mode: linked to the catalog/ADR, dropped from the fan-out
      }
      return agent(
        `Write ONE test-driven ChangeProposal for this novel failure mode, adhering strictly to the project goals in ` +
          `HARNESS_DESIGN.md / ARCHITECTURE.md / README.md and ADR-0024 (evals/improvement-loop-design.md).\n` +
          `Cluster: ${JSON.stringify(c)}\nMode: ${JSON.stringify(verdict)}\n` +
          `Set remediation_type (prompt_instruction | guardrail_check | code_logic | doc_only) and blast_radius ` +
          `(local | global); set touches_grader=true if it edits specs/probes/fixtures/verifier/scoring. Name the ` +
          `failed tasks (${(c.models || []).join(',')} on ${c.task}) in target_tasks for validation, and give a concrete tdd_plan.`,
        { label: `propose:${verdict.mode}`, phase: 'Propose', schema: PROPOSAL_SCHEMA },
      )
    },
  )
).filter(Boolean)

log(`drafted ${proposals.length} proposal(s) for novel mode(s)`)
if (proposals.length === 0) return { stamp, proposals: [], note: 'all clusters mapped to known modes — nothing novel to propose' }

// ── Phase 4 · Reconcile (barrier) — mutual + codebase compatibility, then write the artifacts ──
phase('Reconcile')
const reconciled = await agent(
  `Reconcile these draft ChangeProposals so they are mutually compatible and compatible with the current codebase ` +
    `(spot-check the files each would touch). Resolve overlaps, dedupe, and score each with score_impact ` +
    `(impact 0-10 = cluster share of failures) and predicted_validation_cost. Then WRITE the artifacts:\n` +
    `  • evals/proposals/${stamp}/<id>.md  (front-matter + body) for every proposal, using the ChangeProposal schema; and\n` +
    `  • append any newly-confirmed novel modes to docs/research/failure-modes.md (the durable memory).\n` +
    `Do NOT implement anything and do NOT run evals — this workflow is read-only/zero-spend; Workflow B builds funded proposals.\n` +
    `Proposals: ${JSON.stringify(proposals)}`,
  { label: 'reconcile', phase: 'Reconcile' },
)

return { stamp, dir: `evals/proposals/${stamp}/`, count: proposals.length, summary: reconciled }
