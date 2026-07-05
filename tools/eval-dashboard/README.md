# Eval Harness Dashboard (prototype)

An interactive, **statically-exportable** dashboard over the eval runner's
per-run JSONL (`evals/results/<stamp>.jsonl`). Filter by model / task / seed and
read the same numbers the harness reports — `pass@1` (capability) and `pass^k`
(reliability) — plus a colourblind-safe model×task heatmap, a failure-mode
drill-down, and cost.

This is **exploratory prototype** tooling; it lives self-contained under
`tools/eval-dashboard/` and does not touch the harness.

## Tool choice: Observable Framework (vs Quarto)

Our tooling research shortlisted the two tool-agnostic options that consume our
JSONL directly **and** produce a committable static artifact: **Quarto** and
**Observable Framework**. Both would work. I picked **Observable Framework** for
this prototype:

| | Observable Framework | Quarto |
|---|---|---|
| On PATH in this env | ✅ `node` 25 / `npm` 11 present — built & verified here | ❌ `quarto` not installed (needs a separate binary) |
| Static export | `npm run build` → fully self-contained `dist/` (hashed local JS + data, GitHub-Pages-ready) | `quarto render` → static HTML |
| Reads our JSONL | `FileAttachment(...).text()` + `JSON.parse` per line | OJS/Python cell |
| Reactive filters | first-class (`view()` + implicit dataflow) | OJS inputs (also fine) |
| Dependency surface | one npm dev-dep; Plot/D3 resolved & **bundled** at build (no CDN) | Quarto CLI + (for interactivity) an OJS/Python toolchain |

**Deciding factor:** Observable Framework is installed-and-runnable in this
environment, so the prototype is *verified working end-to-end* (built, rendered
in a browser, numbers cross-checked against the harness). Quarto would have
shipped as un-run source. Framework's build also inlines every dependency and the
data into `dist/`, giving exactly the "git-friendly, regenerable-from-JSONL"
static artifact the research called for. Quarto remains a fine alternative,
especially if you want Python cells that `import evals.metrics` directly instead
of a JS re-implementation of the metrics.

## What's here

```
tools/eval-dashboard/
├── README.md                 # this file
├── package.json              # one dev-dep: @observablehq/framework
├── package-lock.json         # pinned for reproducible installs
├── observablehq.config.js    # title, auto light/dark theme, single page
├── .gitignore                # node_modules/, dist/, cache
└── src/
    ├── index.md              # the dashboard (filters + 4 views)
    ├── components/
    │   └── metrics.js        # faithful JS port of evals/metrics.py + palette
    └── data/
        ├── results.jsonl.js     # data loader — reads EVAL_RESULTS, else the sample
        ├── results.sample.jsonl # committed SAMPLE run (72 rows) — see data/README.md
        └── README.md            # data provenance + how to re-point
```

### The four views (mirroring `scripts/eval_report.py`)

1. **Model × task `pass@1` heatmap** — per-cell solved-rate, with per-model and
   per-task marginals and per-cell hover (solved/total, mean iterations, mean
   tokens). Colour is **cividis**, a perceptually-uniform, CVD-safe sequential
   scale — **no red–green** (hard requirement). Mirrors the report's *solved matrix*.
2. **`pass@1` vs `pass^k` per model** — capability vs reliability; the gap is the
   reliability deficit. Mirrors the report's *headline*.
3. **Failure-mode breakdown** — non-solved runs bucketed by `failure_mode`, with a
   selector to drill into exactly which runs failed (a table with
   model/task/seed/outcome/probe/iterations/workspace). Mirrors the *histogram*.
4. **Cost** — mean tokens/run per model, and a per-run tokens-vs-iterations
   scatter (solved vs not-solved as a non-colour channel). Mirrors *cost stats*.

Global **filters** (model / task / seed) drive every view reactively.

### Faithful metrics

`src/components/metrics.js` is a line-by-line port of `evals/metrics.py`:

- `passAt1` = mean(`solved`) over runs — capability.
- `passCaretK` = fraction of **tasks** whose **every seed** solved — reliability.

Verified: per-model and overall values match `scripts/eval_report.py` exactly on
the sample (e.g. `gpt-5.3-codex` 0.94/0.83, `gpt-oss-120b` 0.67/0.67, overall
pass@1 0.83). If `evals/metrics.py` changes, mirror it here — there is no shared
runtime (harness is Python, dashboard is JS).

**Cost + latency** mirror `evals/cost.py`: `runCostUsd` / `costPerSolvedUsd`
(dollars, the decision metric — amortizes failed-run spend over successes) and
`medianWallClockSeconds`. **Prices are not hardcoded** — the loader
`src/data/pricing.json.js` reads the *shared* `evals/pricing.json` (the same file
`scripts/eval_report.py` reads), so $ can never drift between the two views;
latency reads the results JSONL's `wall_clock_seconds` field. Sanity-checked vs
the report on the 210-run landscape: `$/solved` codex ≈ $0.148, gpt-oss ≈ $0.009,
total ≈ $11.01; median wall-clock codex ≈ 17s, deepseek ≈ 88s.

> Note: `pass^k` respects the **seed filter** — it means "all *selected* seeds
> solved". Keep every seed checked to reproduce the harness value exactly.

## Run it

From `tools/eval-dashboard/`:

```sh
npm install         # one-time; installs @observablehq/framework (+ Plot/D3, bundled at build)
npm run dev         # interactive dev server at http://localhost:3000 (hot-reload)
```

## Static export (the committable artifact)

```sh
npm run build       # → dist/  (self-contained static site; ~780 KB, 49 files)
```

`dist/` is a fully static site — all JS and the JSONL are bundled with content
hashes; **no CDN or network at runtime** (the only remote reference is a Google
Fonts stylesheet that degrades gracefully to system fonts — see Rough edges).
Open it directly:

```sh
open dist/index.html                       # or:
python3 -m http.server -d dist 8080        # http://localhost:8080
```

### Serving from GitHub Pages

`dist/` is gitignored by default (standard Framework practice — regenerate,
don't commit build output). Two ways to ship it:

- **CI build (recommended):** a GitHub Action runs `npm ci && npm run build` and
  deploys `dist/` to Pages. Regenerable-from-JSONL, nothing binary in git.
- **Commit the artifact:** remove `dist/` from `.gitignore` and commit it — it is
  self-contained and Pages-ready as-is.

If you deploy under a **project** path
(`https://<user>.github.io/<repo>/…`), set `base` in `observablehq.config.js` to
that sub-path and rebuild (a commented example is in the config).

## Point it at a different results file

Pass the target file as the **`EVAL_RESULTS`** argument — a path, absolute or
relative to this directory. The `src/data/results.jsonl.js` loader reads it at
build/preview time; unset, it falls back to the committed sample
(`src/data/results.sample.jsonl`).

```sh
# interactive, against a specific run:
EVAL_RESULTS=../../evals/results/<stamp>.jsonl npm run dev:results

# static export, against a specific run:
EVAL_RESULTS=../../evals/results/<stamp>.jsonl npm run build:results
```

Use the **`:results`** scripts (not plain `dev`/`build`) whenever you change
`EVAL_RESULTS`: Observable caches data-loader output and does not treat the env var
as an input, so these variants clear the cache first (`npm run clean`) to force a
re-read. Any `evals/results/<stamp>.jsonl` works unchanged — identical schema
(`evals/result.py::ResultRow`). A missing/mistyped path fails fast with a clear
error. See `src/data/README.md` for provenance of the bundled sample.

## Built & verified in this environment

- ✅ `npm install` + `npm run build` succeed (Node 25, npm 11).
- ✅ Rendered `dist/index.html` in a headless browser: all four views draw, **zero
  JS console errors** (only a cosmetic `favicon.ico` 404).
- ✅ Numbers cross-checked against `scripts/eval_report.py` — exact match.
- ✅ Interactivity confirmed in the *static* build: unchecking a model recomputes
  every view (e.g. dropping `gpt-oss-120b` moves overall pass@1 83% → 89%).

## Rough edges / follow-ups

- **Web fonts:** Framework's theme references Google Fonts. The page is otherwise
  fully offline; fonts fall back to system-ui if unreachable. For a strictly
  air-gapped artifact, self-host the fonts or strip the theme's font import.
- **Dark mode:** the theme is auto light/dark and Plot text adapts, but the
  categorical/cividis mark colours are a single validated (light) set, not a
  separately-tuned dark ramp. Fine for a prototype; a production version would
  add dark-tuned steps per the `dataviz` skill.
- **Marginal readability:** the per-model marginal bars are compact at narrow
  widths; the heatmap + section 2 carry the same information more legibly.
- **Metric drift:** the JS port must be kept in lockstep with `evals/metrics.py`
  by hand. A tiny CI check (build + compare against `eval_report.py` on a fixture)
  would catch drift.
```
