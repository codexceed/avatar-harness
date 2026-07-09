#!/usr/bin/env python3
"""Render an eval results JSONL as committable model x task heatmaps (SVG).

A static, version-controllable companion to `scripts/eval_report.py` (terminal view) and
`scripts/build_eval_notebook.py` (interactive notebook): it emits **deterministic** SVGs for
embedding in a markdown research note. Two complementary figures, each a model x task grid:

  * **pass@1 (capability)** — coloured by per-cell solved rate, with a marginal ``Overall``
    column (per-model pass@1) and an ``all models`` row (per-task pass@1 — the "task success
    rate"), plus the grand overall in the corner.
  * **pass^k (reliability)** — coloured by whether a model solved a task on *every* seed (a
    binary cell: the "works every time" lens). Its margins read as fractions: the ``Overall``
    column is the share of a model's tasks that are fully reliable (== that model's pass^k),
    the ``all models`` row is the share of models that reliably solve a task, and the corner is
    the share of all (model, task) pairs that are reliable.

Numbers reuse the harness's own `evals.metrics.pass_at_1` / `pass_caret_k`, so they match
`evals.run` / `evals.diff` / `eval_report.py` exactly. pass^k is only meaningful with several
seeds per (model, task); on a one-seed run it degenerates to pass@1.

Two deliberate choices vs. the notebook's heatmap cell:
  * **Colormap is perceptually-uniform and colorblind-safe** (`viridis`), not the notebook's
    `RdYlGn` — a red↔green diverging map is the canonical CVD-unsafe choice, and pass rate is
    ordered [0,1] data that wants a sequential map, not a diverging one.
  * **Output is byte-stable** — `svg.hashsalt` is pinned, glyphs embed as paths, the SVG
    ``Date`` metadata is stripped, and trailing whitespace is removed (matplotlib pads every
    path-data line, which would otherwise fail ``git diff --check``); re-running on unchanged
    input produces an identical file. This is what makes the figures safe to commit next to the note.

Usage:
    uv run python scripts/eval_heatmap.py [RESULTS.jsonl] [--metric {pass1,passk,both}]
    uv run python scripts/eval_heatmap.py [RESULTS.jsonl] --metric passk -o OUT.svg [--title TITLE]
    uv run python scripts/eval_heatmap.py            # newest results, both heatmaps
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

import matplotlib

matplotlib.use("Agg")  # headless, deterministic — no display backend

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

# Import the harness's own scoring so this view can never drift from `evals.run`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.metrics import pass_at_1, pass_caret_k  # noqa: E402
from evals.result import ResultRow, load_results  # noqa: E402

_OVERALL_COL = "Overall"
_OVERALL_ROW = "all models"
# A perceptually-uniform, colorblind-safe sequential map for ordered [0,1] pass-rate data.
_CMAP = "viridis"


class _Metric(NamedTuple):
    """One heatmap flavour: its frame builder, output-file suffix, colorbar label, and title noun."""

    frame: Callable[[Sequence[ResultRow]], pd.DataFrame]
    suffix: str  # default output stem suffix, e.g. "solved-heatmap"
    cbar_label: str
    noun: str  # leads the default title, e.g. "Solved rate (pass@1)"


def _short_model(model: str) -> str:
    """Drop the provider prefix for compact row labels (`z-ai/glm-5.2` -> `glm-5.2`)."""
    return model.split("/", 1)[-1]


def _pin_determinism() -> None:
    """Pin matplotlib so the emitted SVG is byte-identical across runs on unchanged input.

    Matplotlib's SVG writer is otherwise non-deterministic: element ids are salted from a
    per-process random hash, and the file embeds a wall-clock ``Date``. We fix the salt, embed
    glyphs as paths (so the output doesn't depend on the viewer's fonts), and strip the date at
    ``savefig`` time — together these make a committed figure show no spurious git diff.
    """
    matplotlib.rcParams["svg.hashsalt"] = "avatar-eval-heatmap"
    matplotlib.rcParams["svg.fonttype"] = "path"


def _groups(
    rows: Sequence[ResultRow],
) -> tuple[
    dict[str, list[ResultRow]],
    dict[str, list[ResultRow]],
    dict[tuple[str, str], list[ResultRow]],
]:
    """Bucket rows by short model name, by task, and by (model, task) cell — shared by both frames."""
    by_model: dict[str, list[ResultRow]] = defaultdict(list)
    by_task: dict[str, list[ResultRow]] = defaultdict(list)
    cell: dict[tuple[str, str], list[ResultRow]] = defaultdict(list)
    for r in rows:
        m = _short_model(r.model)
        by_model[m].append(r)
        by_task[r.task].append(r)
        cell[(m, r.task)].append(r)
    return by_model, by_task, cell


def _mean(values: Sequence[float]) -> float:
    """Mean over the non-NaN entries (NaN marks a (model, task) pair absent from the matrix)."""
    present = [v for v in values if not math.isnan(v)]
    return sum(present) / len(present) if present else float("nan")


def solved_frame(rows: Sequence[ResultRow]) -> pd.DataFrame:
    """Build the model x task solved-rate matrix with pass@1 marginals.

    Rows are ordered by descending per-model pass@1 (best model first); task columns by
    ascending per-task pass@1 (hardest task first), so the discriminating structure reads as a
    gradient. A trailing ``Overall`` column and ``all models`` row carry the marginals.

    Args:
        rows: The result rows for one eval matrix.

    Returns:
        A `DataFrame` of pass@1 in ``[0, 1]``, indexed by short model name (plus ``all models``)
        and columned by task (plus ``Overall``).
    """
    by_model, by_task, cell = _groups(rows)

    models = sorted(by_model, key=lambda m: (-pass_at_1(by_model[m]), m))
    tasks = sorted(by_task, key=lambda t: (pass_at_1(by_task[t]), t))

    data = {t: [pass_at_1(cell[(m, t)]) for m in models] + [pass_at_1(by_task[t])] for t in tasks}
    data[_OVERALL_COL] = [pass_at_1(by_model[m]) for m in models] + [pass_at_1(rows)]
    return pd.DataFrame(data, index=[*models, _OVERALL_ROW])


def reliability_frame(rows: Sequence[ResultRow]) -> pd.DataFrame:
    """Build the model x task reliability matrix — pass^k per cell, with fractional marginals.

    A cell is binary: ``1.0`` iff the model solved that task on *every* seed, else ``0.0`` (the
    "works every time" lens). Unlike `solved_frame`, the marginals are means of those binary
    cells rather than run-weighted rates: the ``Overall`` column is each model's pass^k (the
    share of its tasks that are fully reliable), the ``all models`` row is the share of models
    that reliably solve a task, and the corner is the share of all (model, task) pairs that are
    reliable. Rows/columns are ordered by descending/ascending reliability so the structure reads
    as a gradient, same as the pass@1 figure.

    Args:
        rows: The result rows for one eval matrix (with several seeds per cell, or pass^k
            collapses to pass@1).

    Returns:
        A `DataFrame` in ``[0, 1]``, indexed by short model name (plus ``all models``) and
        columned by task (plus ``Overall``); absent (model, task) cells are ``NaN``.
    """
    by_model, by_task, cell = _groups(rows)

    # Per (model, task): reliable iff solved on every seed → a binary {0.0, 1.0} cell.
    rel = {mt: float(all(r.solved for r in cell[mt])) for mt in cell}

    task_rel = {t: _mean([rel.get((m, t), float("nan")) for m in by_model]) for t in by_task}
    models = sorted(by_model, key=lambda m: (-pass_caret_k(by_model[m]), m))
    tasks = sorted(by_task, key=lambda t: (task_rel[t], t))

    data = {t: [rel.get((m, t), float("nan")) for m in models] + [task_rel[t]] for t in tasks}
    data[_OVERALL_COL] = [pass_caret_k(by_model[m]) for m in models] + [_mean(list(rel.values()))]
    return pd.DataFrame(data, index=[*models, _OVERALL_ROW])


def render(frame: pd.DataFrame, title: str, out: Path, cbar_label: str) -> Path:
    """Render a heatmap frame to a deterministic SVG at `out`.

    Args:
        frame: The matrix from `solved_frame` or `reliability_frame` (values in ``[0, 1]``).
        title: The figure title.
        out: Destination ``.svg`` path (parent dirs are created).
        cbar_label: The colorbar legend label (e.g. ``"pass@1 (solved rate)"``).

    Returns:
        The written path.
    """
    _pin_determinism()
    n_tasks = frame.shape[1] - 1  # exclude the Overall marginal column
    n_models = frame.shape[0] - 1  # exclude the all-models marginal row

    fig, ax = plt.subplots(figsize=(1.35 * frame.shape[1] + 3, 0.9 * frame.shape[0] + 2.5))
    sns.heatmap(
        frame,
        annot=True,
        fmt=".0%",
        cmap=_CMAP,
        vmin=0.0,
        vmax=1.0,
        linewidths=1,
        linecolor="white",
        cbar_kws={"label": cbar_label},
        annot_kws={"fontsize": 11},
        ax=ax,
    )
    # Fat surface-coloured lines fence off the marginal row/column as a visual gutter.
    ax.axvline(n_tasks, color="white", lw=6)
    ax.axhline(n_models, color="white", lw=6)

    ax.set_title(title, weight="bold", pad=12)
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.set_yticklabels(ax.get_yticklabels(), rotation=0)
    ax.set_xticklabels(ax.get_xticklabels(), rotation=30, ha="right")
    fig.tight_layout()

    out.parent.mkdir(parents=True, exist_ok=True)
    # `metadata={"Date": None}` strips the wall-clock stamp the SVG writer would embed.
    fig.savefig(out, format="svg", facecolor="white", metadata={"Date": None})
    plt.close(fig)
    # matplotlib ends every SVG path-data line with a trailing space; strip trailing
    # whitespace (safe — the newline still separates path tokens) so the committed figure
    # passes `git diff --check`. Deterministic, so the file stays byte-stable across runs.
    out.write_text(
        "".join(f"{line.rstrip()}\n" for line in out.read_text(encoding="utf-8").splitlines()),
        encoding="utf-8",
    )
    return out


# Registered heatmap flavours; ``both`` (the CLI default) renders each in turn. Declared here,
# after the frame builders it references.
_METRICS: dict[str, _Metric] = {
    "pass1": _Metric(solved_frame, "solved-heatmap", "pass@1 (solved rate)", "Solved rate"),
    "passk": _Metric(
        reliability_frame, "reliability-heatmap", "pass^k (all k seeds solved)", "Reliability (pass^k)"
    ),
}


def _newest_results() -> Path | None:
    """The most recent ``evals/results/*.jsonl``, or `None` when none exist."""
    results = sorted((_REPO_ROOT / "evals" / "results").glob("*.jsonl"))
    return results[-1] if results else None


def main(argv: Sequence[str]) -> int:
    """Render the pass@1 and/or pass^k heatmap(s) for the given (or newest) results file.

    Args:
        argv: ``[prog, [RESULTS.jsonl], [--metric {pass1,passk,both}], [-o OUT], [--title TITLE]]``.

    Returns:
        Process exit code (0 on success, 2 on a missing/empty results file or a bad flag combo).
    """
    parser = argparse.ArgumentParser(prog="eval_heatmap", description=__doc__)
    parser.add_argument("results", nargs="?", help="results JSONL (default: newest under evals/results/)")
    parser.add_argument(
        "--metric",
        choices=[*_METRICS, "both"],
        default="both",
        help="which heatmap(s) to render: pass1 (capability), passk (reliability), or both (default)",
    )
    parser.add_argument(
        "-o",
        "--out",
        help="destination SVG (requires a single --metric; "
        "default: docs/research/assets/<stem>-<solved|reliability>-heatmap.svg)",
    )
    parser.add_argument(
        "--title", help="figure title (requires a single --metric; default: derived from the filename)"
    )
    args = parser.parse_args(argv[1:])

    keys = list(_METRICS) if args.metric == "both" else [args.metric]
    if len(keys) > 1 and (args.out or args.title):
        print("-o/--title require a single --metric (not 'both')", file=sys.stderr)
        return 2

    path = Path(args.results) if args.results else _newest_results()
    if path is None or not path.exists():
        print("usage: eval_heatmap.py RESULTS.jsonl  (no results file found)", file=sys.stderr)
        return 2
    rows = load_results(path)
    if not rows:
        print(f"no rows in {path}", file=sys.stderr)
        return 2

    n_models, n_tasks = len({r.model for r in rows}), len({r.task for r in rows})
    assets = _REPO_ROOT / "docs" / "research" / "assets"
    for key in keys:
        metric = _METRICS[key]
        out = Path(args.out) if args.out else assets / f"{path.stem}-{metric.suffix}.svg"
        title = args.title or f"{metric.noun} per (model, task) — {path.stem}"
        written = render(metric.frame(rows), title, out, metric.cbar_label)
        try:
            shown = written.resolve().relative_to(_REPO_ROOT)
        except ValueError:
            shown = written
        print(f"wrote {shown}  ({len(rows)} runs, {n_models} models x {n_tasks} tasks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
