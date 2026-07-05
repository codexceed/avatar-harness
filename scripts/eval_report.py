#!/usr/bin/env python3
"""Render an eval results JSONL as terminal tables, stats, and ASCII histograms.

A read-only reporting view over an `evals/results/<stamp>.jsonl` matrix. It reuses the
eval harness's own metrics so the numbers match `evals.run`/`evals.diff` exactly:
`pass_at_1` / `pass_caret_k` (capability vs. reliability) and the deterministic failure
classifier (`evals.classify`). Pure stdlib + the `evals` package — no extra dependencies,
no network, no eval spend.

Usage:
    uv run python scripts/eval_report.py [RESULTS.jsonl]
    uv run python scripts/eval_report.py            # newest evals/results/*.jsonl

Sections: headline (pass@1 / pass^k per model), the model x task solved matrix, the
failure-mode histogram (overall + per model), and cost stats (tokens · dollars · latency).
Dollars come from the shared `evals/pricing.json` and latency from the `wall_clock_seconds`
field, so the numbers match `tools/eval-dashboard` exactly.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from statistics import mean

# Import the harness's own scoring so this view can never drift from `evals.run`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.classify import resolve_failure_mode  # noqa: E402
from evals.cost import (  # noqa: E402
    cost_per_solved_usd,
    load_pricing,
    mean_run_cost_usd,
    median_wall_clock_seconds,
    run_cost_usd,
)
from evals.metrics import pass_at_1, pass_caret_k  # noqa: E402
from evals.result import ResultRow, load_results  # noqa: E402

BAR = "█"
HALF = "▏▎▍▌▋▊▉"  # sub-cell eighths for smoother bars


def _short_model(model: str) -> str:
    """Drop the provider prefix for compact column headers (`z-ai/glm-5.2` -> `glm-5.2`)."""
    return model.split("/", 1)[-1]


def _bar(value: float, peak: float, width: int) -> str:
    """A proportional unicode bar of `value` relative to `peak`, up to `width` cells."""
    if peak <= 0:
        return ""
    cells = (value / peak) * width
    full = int(cells)
    out = BAR * full
    frac = cells - full
    if frac > 0 and full < width:
        out += HALF[min(len(HALF) - 1, int(frac * len(HALF)))]
    return out


def _rule(title: str) -> str:
    return f"\n\033[1m{title}\033[0m\n" + "─" * max(40, len(title))


def headline(rows: Sequence[ResultRow]) -> str:
    """Per-model pass@1 / pass^k table plus the overall aggregate."""
    by_model: dict[str, list[ResultRow]] = defaultdict(list)
    for r in rows:
        by_model[r.model].append(r)

    lines = [_rule("HEADLINE — pass@1 (capability) · pass^k (reliability)")]
    lines.append(f"{'model':<28} {'pass@1':>7} {'pass^k':>7} {'n':>4}  capability")
    for model in sorted(by_model):
        mr = by_model[model]
        p1, pk = pass_at_1(mr), pass_caret_k(mr)
        bar = _bar(p1, 1.0, 20)
        lines.append(f"{_short_model(model):<28} {p1:>7.2f} {pk:>7.2f} {len(mr):>4}  {bar}")
    lines.append("─" * 60)
    lines.append(f"{'OVERALL':<28} {pass_at_1(rows):>7.2f} {'—':>7} {len(rows):>4}")
    return "\n".join(lines)


def solved_matrix(rows: Sequence[ResultRow]) -> str:
    """Model x task grid of solved/total counts (the per-cell capability picture)."""
    tasks = sorted({r.task for r in rows})
    models = sorted({r.model for r in rows})
    cell: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for r in rows:
        cell[(r.model, r.task)].append(r.solved)

    by_task: dict[str, list[ResultRow]] = defaultdict(list)
    for r in rows:
        by_task[r.task].append(r)

    width = max(14, *(len(t) for t in tasks)) + 1
    lines = [_rule("SOLVED MATRIX — solved / seeds, per (model, task); % = per-task pass@1")]
    header = f"{'model':<22}" + "".join(f"{t:>{width}}" for t in tasks)
    rate = f"{'pass@1':<22}" + "".join(f"{f'{pass_at_1(by_task[t]):.0%}':>{width}}" for t in tasks)
    lines.append(header)
    lines.append(rate)
    for model in models:
        cells = []
        for t in tasks:
            res = cell[(model, t)]
            s = sum(res)
            mark = "✓" if res and s == len(res) else ("·" if s == 0 else "◐")
            cells.append(f"{f'{s}/{len(res)} {mark}':>{width}}")
        lines.append(f"{_short_model(model):<22}" + "".join(cells))
    return "\n".join(lines)


def histogram(rows: Sequence[ResultRow]) -> str:
    """ASCII failure-mode histogram (overall, then per model)."""

    def buckets(rs: Sequence[ResultRow]) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for r in rs:
            if not r.solved:
                out[resolve_failure_mode(r)] += 1
        return dict(out)

    overall = buckets(rows)
    lines = [_rule("FAILURE-MODE HISTOGRAM (non-solved runs; persisted journal-refined bucket)")]
    if not overall:
        lines.append("  (no failures — every run solved)")
        return "\n".join(lines)

    peak = max(overall.values())
    for bucket, count in sorted(overall.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {bucket:<20} {count:>3}  {_bar(count, peak, 30)}")

    by_model: dict[str, list[ResultRow]] = defaultdict(list)
    for r in rows:
        by_model[r.model].append(r)
    lines.append("\n  by model:")
    for model in sorted(by_model):
        b = buckets(by_model[model])
        detail = ", ".join(f"{k}={v}" for k, v in sorted(b.items(), key=lambda kv: -kv[1]))
        lines.append(f"    {_short_model(model):<24} {detail or 'clean (0 failures)'}")
    return "\n".join(lines)


def _usd(x: float | None) -> str:
    """Format a dollar amount, or ``—`` when unpriced/undefined."""
    return f"${x:.3f}" if x is not None else "—"


def _secs(x: float | None) -> str:
    """Format a wall-clock in seconds, or ``—`` when unrecorded."""
    return f"{x:.0f}s" if x is not None else "—"


def cost_stats(rows: Sequence[ResultRow]) -> str:
    """Cost per model — tokens, **dollars** ($/run and $/solved), and agent-loop **latency**.

    Dollars use the shared `evals/pricing.json` (so this matches the dashboard exactly). ``$/solved``
    amortizes failed-run spend across the successes — the true cost of a *result*, not a run. Latency
    is the *median* agent-loop wall-clock: the slowest runs are right-censored at the task budget, so
    the mean would be inflated. Sorted cheapest-$/solved first; unpriced models sink to the end.

    Args:
        rows: The result rows to summarize.

    Returns:
        The rendered cost section.
    """
    pricing = load_pricing()
    by_model: dict[str, list[ResultRow]] = defaultdict(list)
    for r in rows:
        by_model[r.model].append(r)

    def sort_key(m: str) -> tuple[bool, float]:
        c = cost_per_solved_usd(by_model[m], pricing)
        return (c is None, c if c is not None else 0.0)

    lines = [_rule("COST — tokens · dollars · latency per run")]
    lines.append(f"{'model':<22} {'tok/run':>9} {'$/run':>8} {'$/solved':>9} {'med wall':>9} {'iters':>6}")
    for model in sorted(by_model, key=sort_key):
        rs = by_model[model]
        toks = mean(r.prompt_tokens + r.completion_tokens for r in rs)
        iters = mean(r.iterations for r in rs)
        lines.append(
            f"{_short_model(model):<22} {toks:>9,.0f} {_usd(mean_run_cost_usd(rs, pricing)):>8} "
            f"{_usd(cost_per_solved_usd(rs, pricing)):>9} {_secs(median_wall_clock_seconds(rs)):>9} "
            f"{iters:>6.1f}"
        )
    grand_tok = sum(r.prompt_tokens + r.completion_tokens for r in rows)
    grand_usd = sum(c for c in (run_cost_usd(r, pricing) for r in rows) if c is not None)
    lines.append("─" * 66)
    lines.append(
        f"{'TOTAL':<22} {grand_tok:>9,} tok   {_usd(grand_usd)} across {len(rows)} runs "
        f"(prices: evals/pricing.json)"
    )
    return "\n".join(lines)


def _newest_results() -> Path | None:
    results = sorted((_REPO_ROOT / "evals" / "results").glob("*.jsonl"))
    return results[-1] if results else None


def main(argv: Sequence[str]) -> int:
    """Render the report for the given (or newest) results file."""
    path = Path(argv[1]) if len(argv) > 1 else _newest_results()
    if path is None or not path.exists():
        print("usage: eval_report.py RESULTS.jsonl  (no results file found)", file=sys.stderr)
        return 2

    rows = load_results(path)
    if not rows:
        print(f"no rows in {path}", file=sys.stderr)
        return 1

    print(
        f"\033[1mEval report\033[0m  ·  {path.name}  ·  {len(rows)} runs, "
        f"{len({r.model for r in rows})} models x {len({r.task for r in rows})} tasks"
    )
    print(headline(rows))
    print(solved_matrix(rows))
    print(histogram(rows))
    print(cost_stats(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
