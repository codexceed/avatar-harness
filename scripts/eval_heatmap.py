#!/usr/bin/env python3
"""Render an eval results JSONL as a committable model x task solved-rate heatmap (SVG).

A static, version-controllable companion to `scripts/eval_report.py` (terminal view) and
`scripts/build_eval_notebook.py` (interactive notebook): it emits a single **deterministic**
SVG for embedding in a markdown research note. The figure is a model x task grid coloured by
per-cell pass@1 (solved rate), with a marginal ``Overall`` column (per-model pass@1) and an
``all models`` row (per-task pass@1 — the "task success rate"), plus the grand overall in the
corner. Numbers reuse the harness's own `evals.metrics.pass_at_1`, so they match
`evals.run` / `evals.diff` / `eval_report.py` exactly.

Two deliberate choices vs. the notebook's heatmap cell:
  * **Colormap is perceptually-uniform and colorblind-safe** (`viridis`), not the notebook's
    `RdYlGn` — a red↔green diverging map is the canonical CVD-unsafe choice, and pass rate is
    ordered [0,1] data that wants a sequential map, not a diverging one.
  * **Output is byte-stable** — `svg.hashsalt` is pinned, glyphs embed as paths, the SVG
    ``Date`` metadata is stripped, and trailing whitespace is removed (matplotlib pads every
    path-data line, which would otherwise fail ``git diff --check``); re-running on unchanged
    input produces an identical file. This is what makes the figure safe to commit next to the note.

Usage:
    uv run python scripts/eval_heatmap.py [RESULTS.jsonl] [-o OUT.svg] [--title TITLE]
    uv run python scripts/eval_heatmap.py            # newest evals/results/*.jsonl
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless, deterministic — no display backend

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

# Import the harness's own scoring so this view can never drift from `evals.run`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.metrics import pass_at_1  # noqa: E402
from evals.result import ResultRow, load_results  # noqa: E402

_OVERALL_COL = "Overall"
_OVERALL_ROW = "all models"
# A perceptually-uniform, colorblind-safe sequential map for ordered [0,1] pass-rate data.
_CMAP = "viridis"


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
    by_model: dict[str, list[ResultRow]] = defaultdict(list)
    by_task: dict[str, list[ResultRow]] = defaultdict(list)
    for r in rows:
        by_model[_short_model(r.model)].append(r)
        by_task[r.task].append(r)

    models = sorted(by_model, key=lambda m: (-pass_at_1(by_model[m]), m))
    tasks = sorted(by_task, key=lambda t: (pass_at_1(by_task[t]), t))

    cell: dict[tuple[str, str], list[ResultRow]] = defaultdict(list)
    for r in rows:
        cell[(_short_model(r.model), r.task)].append(r)

    data = {t: [pass_at_1(cell[(m, t)]) for m in models] + [pass_at_1(by_task[t])] for t in tasks}
    data[_OVERALL_COL] = [pass_at_1(by_model[m]) for m in models] + [pass_at_1(rows)]
    return pd.DataFrame(data, index=[*models, _OVERALL_ROW])


def render(frame: pd.DataFrame, title: str, out: Path) -> Path:
    """Render the solved-rate heatmap to a deterministic SVG at `out`.

    Args:
        frame: The matrix from `solved_frame` (pass@1 values in ``[0, 1]``).
        title: The figure title.
        out: Destination ``.svg`` path (parent dirs are created).

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
        cbar_kws={"label": "pass@1 (solved rate)"},
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


def _newest_results() -> Path | None:
    """The most recent ``evals/results/*.jsonl``, or `None` when none exist."""
    results = sorted((_REPO_ROOT / "evals" / "results").glob("*.jsonl"))
    return results[-1] if results else None


def main(argv: Sequence[str]) -> int:
    """Render the solved-rate heatmap for the given (or newest) results file.

    Args:
        argv: ``[prog, [RESULTS.jsonl], [-o OUT], [--title TITLE]]``.

    Returns:
        Process exit code (0 on success, 2 on a missing/empty results file).
    """
    parser = argparse.ArgumentParser(prog="eval_heatmap", description=__doc__)
    parser.add_argument("results", nargs="?", help="results JSONL (default: newest under evals/results/)")
    parser.add_argument(
        "-o", "--out", help="destination SVG (default: docs/research/assets/<stem>-solved-heatmap.svg)"
    )
    parser.add_argument("--title", help="figure title (default: derived from the results filename)")
    args = parser.parse_args(argv[1:])

    path = Path(args.results) if args.results else _newest_results()
    if path is None or not path.exists():
        print("usage: eval_heatmap.py RESULTS.jsonl  (no results file found)", file=sys.stderr)
        return 2
    rows = load_results(path)
    if not rows:
        print(f"no rows in {path}", file=sys.stderr)
        return 2

    default_out = _REPO_ROOT / "docs" / "research" / "assets" / f"{path.stem}-solved-heatmap.svg"
    out = Path(args.out) if args.out else default_out
    title = args.title or f"Solved rate per (model, task) — {path.stem}"
    written = render(solved_frame(rows), title, out)
    try:
        shown = written.resolve().relative_to(_REPO_ROOT)
    except ValueError:
        shown = written
    n_models, n_tasks = len({r.model for r in rows}), len({r.task for r in rows})
    print(f"wrote {shown}  ({len(rows)} runs, {n_models} models x {n_tasks} tasks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
