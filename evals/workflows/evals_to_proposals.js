// Workflow A — `evals-to-proposals` (ADR-0024, Increment 2).
//
// The read-only half of the evals-driven improvement loop: an eval results file → a brief,
// human-readable proposals digest (`evals/proposals/<stamp>/proposals.md`) — an "At a glance"
// index plus one self-contained entry per novel failure (the issue, related history in plain
// language, the proposed change, how we'd verify). ZERO eval spend (it never re-runs the matrix);
// it spends only Claude tokens on the reasoning leaves, and only for *novel* clusters.
//
// The digest is the human-facing control surface; it deliberately uses NO failure-modes.md catalog
// codes (they're invisible to a reader). The structured ChangeProposal seam (evals/proposal.py),
// the typed hand-off to Workflow B, is NOT emitted here yet — it returns when Workflow B is built.
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
  description: 'Eval results → a brief, human-readable proposals digest (read-only, zero eval spend)',
  whenToUse: 'After a human-gated `make eval --no-cleanup` run, to triage failures into reviewed proposals.',
  phases: [
    { title: 'Triage', detail: 'Layer-1 deterministic prefilter: cluster failures + token-overlap dedup' },
    { title: 'Analyze', detail: 'one judge subagent per cluster — confirm novel vs known' },
    { title: 'Propose', detail: 'one subagent per novel mode — a brief issue + change entry' },
    { title: 'Reconcile', detail: 'barrier: dedupe/score, assemble the human digest (proposals.md)' },
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
        required: ['task', 'bucket', 'runs', 'symptom', 'prefilter_novel'],
        properties: {
          task: { type: 'string' },
          bucket: { type: 'string' }, // the grading-truth failure mode (evals.classify), not the harness outcome (B4)
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

// A single human-readable digest entry — NOT the structured ChangeProposal schema. The typed
// Workflow-A→B seam (evals/proposal.py) is intentionally NOT emitted here yet: this workflow's
// output is a digest a human reads and controls; structured output returns when Workflow B exists.
const ENTRY_SCHEMA = {
  type: 'object',
  required: ['slug', 'title', 'whats_wrong', 'the_fix', 'size', 'risk', 'meta_line', 'body'],
  properties: {
    slug: { type: 'string', description: 'kebab-case id for ordering/reference only' },
    title: { type: 'string', description: 'plain-language headline, NO catalog codes (e.g. "A network crash is disguised as a new secret-safety failure")' },
    whats_wrong: { type: 'string', description: 'ONE sentence for the index table — the symptom a human cares about' },
    the_fix: { type: 'string', description: 'ONE sentence for the index table — the change in plain language' },
    size: { type: 'string', description: 'rough effort, e.g. "~20-line triage fix" / "prompt tweak" / "new guardrail check"' },
    risk: { type: 'string', description: 'Low/Medium/High + a clause of why, e.g. "Low — triage label only, grading untouched"' },
    meta_line: { type: 'string', description: 'one-line summary rendered as a blockquote under the heading, e.g. "1 of 15 failed runs · triage label only · no eval spend to validate"' },
    body: {
      type: 'string',
      description:
        'The per-issue markdown, brief and balancing prose with ONE small visual (an ASCII sketch or a tiny before/after table). ' +
        'Exactly these H3 sections, in order: "### The issue", "### Related history", "### The proposed change", "### How we\'d verify". ' +
        'Related history MUST be plain language with NO catalog codes (say "a known network failure mode", never "A9"). ' +
        'How we\'d verify is a 2-3 bullet test sketch. Do NOT include the "## N · title" heading or the meta blockquote — Reconcile adds those.',
    },
  },
}

// `args` can arrive as an object or, depending on how Workflow was invoked, as a JSON string —
// coerce so a direct `Workflow({scriptPath, args})` call works without a wrapper.
const a = typeof args === 'string' ? JSON.parse(args) : (args ?? {})
const results = a.results
const stamp = a.stamp ?? 'run'
if (!results) throw new Error('evals-to-proposals: args.results (a results JSONL path) is required')

// ── Phase 1 · Triage (deterministic Layer-1 prefilter, run via a thin shell-out agent) ─────────
phase('Triage')
const triaged = await agent(
  `Run the deterministic Layer-1 prefilter and return its output verbatim as JSON.\n` +
    `Execute exactly: \`python -m evals.cluster ${results}\`\n` +
    `That prints one JSON object per line (a ClusterVerdict: {cluster:{task,bucket,models,runs,symptom,` +
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
        { label: `analyze:${c.task}/${c.bucket}`, phase: 'Analyze', schema: VERDICT_SCHEMA },
      ),
    (verdict, c) => {
      if (!verdict || !verdict.novel) {
        log(`known: ${c.task}/${c.bucket} → ${verdict?.mode ?? 'linked'} (no proposal)`)
        return null // known mode: linked to the catalog/ADR, dropped from the fan-out
      }
      return agent(
        `Write ONE brief, human-readable proposal entry for this novel failure mode, adhering strictly to the ` +
          `project goals in HARNESS_DESIGN.md / ARCHITECTURE.md / README.md and ADR-0024 (evals/improvement-loop-design.md).\n` +
          `Cluster: ${JSON.stringify(c)}\nMode: ${JSON.stringify(verdict)}\n` +
          `Failed tasks: ${(c.models || []).join(',')} on ${c.task}.\n\n` +
          `AUDIENCE: a human reviewer who will NOT look anything up. The entry must be self-contained and skimmable — ` +
          `balance short prose with ONE small visual (an ASCII sketch or a tiny before/after table), not walls of text.\n` +
          `CRITICAL: never cite failure-modes.md catalog codes (A6, B4, ADR-0028, …) — they are invisible to the reader. ` +
          `You MAY read docs/research/failure-modes.md to ground the "Related history" section, but describe any prior/related ` +
          `mode in plain language ("a known network failure mode", "a fix for X is already planned"), never by its code.\n` +
          `Spot-check the file(s) the fix would touch so "The proposed change" and "How we'd verify" are concrete and correct. ` +
          `Do NOT implement anything and do NOT run evals.`,
        { label: `propose:${verdict.mode}`, phase: 'Propose', schema: ENTRY_SCHEMA },
      )
    },
  )
).filter(Boolean)

log(`drafted ${proposals.length} entry(ies) for novel mode(s)`)
if (proposals.length === 0) return { stamp, proposals: [], note: 'all clusters mapped to known modes — nothing novel to propose' }

// ── Phase 4 · Reconcile (barrier) — dedupe/score across entries, assemble the human digest ──────
// The single output is `proposals.md`: a digest a human reads and controls. The structured
// Workflow-A→B seam (evals/proposal.py) is intentionally NOT emitted yet — it returns with B.
const summaryPath = results.replace(/\.jsonl$/, '.summary.json')
phase('Reconcile')
const reconciled = await agent(
  `Assemble a SINGLE human-readable proposals digest from these draft entries. The reader will not look anything up.\n` +
    `Draft entries (one per novel cluster): ${JSON.stringify(proposals)}\n\n` +
    `Steps:\n` +
    `  1. Spot-check the file(s) each fix names so every entry is compatible with the current codebase; drop or merge ` +
    `any entry that duplicates another (same root cause) — keep the digest tight.\n` +
    `  2. Order the surviving entries by impact (biggest share of failures first) and number them 1..N.\n` +
    `  3. Read ${summaryPath} for the run header numbers (models, tasks/specs, seeds, overall pass@1, total failed runs).\n` +
    `  4. WRITE exactly ONE file, evals/proposals/${stamp}/proposals.md, with this structure and NOTHING else ` +
    `(no YAML front-matter, no catalog codes anywhere):\n` +
    `       # Eval → Change Proposals · run \`${stamp}\`\n` +
    `       <one summary line: N models · M tasks · K seeds · **pass@1 X.XX** · F failed runs → **P proposed change(s)**>\n` +
    `       ## At a glance\n` +
    `       <a markdown table with columns: # | What's wrong | The fix | Size | Risk — one row per entry, from its whats_wrong/the_fix/size/risk>\n` +
    `       ---\n` +
    `       <then each entry as: "## <n> · <title>", a "> <meta_line>" blockquote, then its body verbatim>\n` +
    `  5. Append any newly-confirmed novel modes to docs/research/failure-modes.md (the durable memory — that file ` +
    `MAY use catalog codes; the digest may NOT).\n` +
    `Do NOT implement anything, do NOT run evals, and do NOT write any other file in evals/proposals/${stamp}/ ` +
    `(no per-proposal .md, no .jsonl) — this workflow is read-only/zero-spend and emits only the digest.`,
  { label: 'reconcile', phase: 'Reconcile' },
)

return { stamp, digest: `evals/proposals/${stamp}/proposals.md`, count: proposals.length, summary: reconciled }
