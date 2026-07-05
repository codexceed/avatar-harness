// Metrics and small helpers for the eval dashboard.
//
// The two headline metrics are FAITHFUL ports of `evals/metrics.py` so the
// dashboard numbers match the harness (`evals.run` / `scripts/eval_report.py`)
// exactly:
//   - passAt1  = capability = mean(solved)                       (per run)
//   - passCaretK = reliability = fraction of *tasks* whose every seed solved
//
// Keep these in lockstep with evals/metrics.py — a change there must be
// mirrored here (there is no shared runtime; the dashboard is JS, the harness
// is Python).

/**
 * Fraction of runs that were solved. Port of `evals.metrics.pass_at_1`.
 * @param {Array<{solved: boolean}>} rows
 * @returns {number} mean solved rate, or 0 for empty input
 */
export function passAt1(rows) {
  if (!rows.length) return 0;
  return rows.filter((r) => r.solved).length / rows.length;
}

/**
 * Fraction of tasks whose EVERY seed was solved. Port of
 * `evals.metrics.pass_caret_k`. Groups by task only (as the harness does), so
 * when called on one model's rows it answers "for how many tasks did this model
 * solve all seeds" — matching `eval_report.headline`.
 *
 * NOTE: with a seed filter active this becomes "all *selected* seeds"; keep all
 * seeds selected to reproduce the harness value exactly.
 * @param {Array<{task: string, solved: boolean}>} rows
 * @returns {number}
 */
export function passCaretK(rows) {
  const byTask = new Map();
  for (const r of rows) {
    if (!byTask.has(r.task)) byTask.set(r.task, []);
    byTask.get(r.task).push(r.solved);
  }
  if (byTask.size === 0) return 0;
  let n = 0;
  for (const solved of byTask.values()) if (solved.every(Boolean)) n++;
  return n / byTask.size;
}

/**
 * Drop the provider prefix for compact labels. Port of `_short_model`:
 * `openai/gpt-5.3-codex` -> `gpt-5.3-codex`, `z-ai/glm-5.2` -> `glm-5.2`.
 * @param {string} model
 * @returns {string}
 */
export function shortModel(model) {
  const i = model.indexOf("/");
  return i === -1 ? model : model.slice(i + 1);
}

/** Total tokens for a run (prompt + completion), matching eval_report cost. */
export function totalTokens(r) {
  return (r.prompt_tokens ?? 0) + (r.completion_tokens ?? 0);
}

/** Group an array into a Map keyed by `keyFn(row)`. */
export function groupBy(rows, keyFn) {
  const m = new Map();
  for (const r of rows) {
    const k = keyFn(r);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(r);
  }
  return m;
}

/** Distinct values of `keyFn`, sorted. */
export function distinct(rows, keyFn) {
  return Array.from(new Set(rows.map(keyFn))).sort();
}

// ---------------------------------------------------------------------------
// Palette — the validated, CVD-safe reference palette from the `dataviz` skill
// (references/palette.md). Categorical worst adjacent CVD ΔE = 24.2 (well clear
// of the >=12 target). Used unchanged, so no re-validation is needed.
//
// The model x task pass-rate heatmap uses a *sequential* scale — CIVIDIS — set
// directly on the Plot color scale (see index.md). Cividis is perceptually
// uniform and CVD-optimal, satisfying the hard "no red-green" requirement.
// ---------------------------------------------------------------------------

/** Categorical hues in fixed CVD-safe order (light-mode steps). */
export const CATEGORICAL = [
  "#2a78d6", // 1 blue
  "#1baf7a", // 2 aqua
  "#eda100", // 3 yellow
  "#008300", // 4 green
  "#4a3aa7", // 5 violet
  "#e34948", // 6 red
  "#e87ba4", // 7 magenta
  "#eb6834", // 8 orange
];

/**
 * Stable colour map for failure modes: colour follows the *entity*, not its
 * rank, so filtering never repaints a bucket. Ordering is fixed here.
 */
export const FAILURE_ORDER = [
  "probe_failed",
  "guard_violation",
  "budget_exhausted",
  "harness_error",
  "verifier_rejected",
  "no_diff",
  "unknown",
];

/** Build a {mode -> colour} map covering all modes seen, in a stable order. */
export function failureColorMap(rows) {
  const seen = distinct(
    rows.filter((r) => !r.solved),
    (r) => r.failure_mode || "unknown",
  );
  const ordered = [
    ...FAILURE_ORDER.filter((m) => seen.includes(m)),
    ...seen.filter((m) => !FAILURE_ORDER.includes(m)),
  ];
  const map = {};
  ordered.forEach((m, i) => (map[m] = CATEGORICAL[i % CATEGORICAL.length]));
  return { domain: ordered, range: ordered.map((m) => map[m]) };
}

/** Format a ratio as a whole-percent string, e.g. 0.667 -> "67%". */
export function pct(x) {
  return `${Math.round(x * 100)}%`;
}

// --- Cost + latency — a faithful mirror of `evals/cost.py` (same null-handling). Prices come from
// the shared `evals/pricing.json` (loaded via the pricing data loader), so $ matches eval_report.py.

/** $ cost of one run: prompt_tokens*prompt_price + completion_tokens*completion_price; null if unpriced. */
export function runCostUsd(r, pricing) {
  const p = pricing[r.model];
  if (!p) return null;
  return r.prompt_tokens * p.prompt + r.completion_tokens * p.completion;
}

/** Mean $ / run over the priced rows; null if none priced. */
export function meanRunCostUsd(rows, pricing) {
  const cs = rows.map((r) => runCostUsd(r, pricing)).filter((c) => c != null);
  return cs.length ? cs.reduce((a, b) => a + b, 0) / cs.length : null;
}

/** Amortized $ / solved run — total priced spend (incl. failures) / solved count; null if none/zero. */
export function costPerSolvedUsd(rows, pricing) {
  const cs = rows.map((r) => runCostUsd(r, pricing)).filter((c) => c != null);
  const solved = rows.filter((r) => r.solved).length;
  if (!cs.length || solved === 0) return null;
  return cs.reduce((a, b) => a + b, 0) / solved;
}

/** Median of the non-null `wall_clock_seconds` (agent-loop latency); null if none. Matches Python. */
export function medianWallClockSeconds(rows) {
  const v = rows
    .map((r) => r.wall_clock_seconds)
    .filter((x) => x != null)
    .sort((a, b) => a - b);
  if (!v.length) return null;
  const mid = Math.floor(v.length / 2);
  return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

/** Sample standard deviation (for the $/run whisker); 0 for <2 values. */
export function stddev(xs) {
  if (xs.length < 2) return 0;
  const m = xs.reduce((a, b) => a + b, 0) / xs.length;
  return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / (xs.length - 1));
}

/** Format a $ amount to 3 decimals, or "—" when null/undefined. */
export function usd(x) {
  return x == null ? "—" : `$${x.toFixed(3)}`;
}
