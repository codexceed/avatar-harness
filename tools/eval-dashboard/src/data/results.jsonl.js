// Data loader — emits the eval-results JSONL the dashboard renders.
//
// Observable runs this at build/preview time; its stdout becomes the
// `FileAttachment("data/results.jsonl")` the page reads. Target a specific run by
// setting the EVAL_RESULTS environment variable to a results-file path — absolute,
// or relative to this dashboard directory (where you invoke npm):
//
//   EVAL_RESULTS=../../evals/results/20260705T151224Z.jsonl npm run build
//   EVAL_RESULTS=/abs/path/to/results.jsonl              npm run dev
//
// Unset → the committed sample (results.sample.jsonl). NOTE: Observable caches
// data-loader output and does not treat the env var as an input, so `npm run
// build`/`dev` after changing EVAL_RESULTS will serve stale data — run `npm run
// clean` first, or use `npm run build:results` / `npm run dev:results`, which clean
// for you.
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const sample = fileURLToPath(new URL("./results.sample.jsonl", import.meta.url));
const target = process.env.EVAL_RESULTS ? resolve(process.cwd(), process.env.EVAL_RESULTS) : sample;

if (!existsSync(target)) {
  throw new Error(
    `EVAL_RESULTS points at a file that does not exist: ${target}\n` +
      "Pass a path to an evals/results/<stamp>.jsonl (absolute, or relative to this dashboard dir).",
  );
}

process.stdout.write(readFileSync(target, "utf-8"));
