// Data loader — emits the shared per-model price table the dashboard's $ math uses.
//
// Reads the repo's `evals/pricing.json` (the SINGLE SOURCE OF TRUTH, shared with `evals/cost.py`
// and `scripts/eval_report.py`) and emits its `models` object, so the dashboard and the terminal
// report can never disagree on price. Resolved relative to the dashboard dir (where npm runs).
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const src = resolve(process.cwd(), "../../evals/pricing.json");
process.stdout.write(JSON.stringify(JSON.parse(readFileSync(src, "utf-8")).models ?? {}));
