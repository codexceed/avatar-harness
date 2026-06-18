#!/usr/bin/env python3
"""Generate `notebooks/eval_report.ipynb` — rich seaborn/matplotlib charts over an eval matrix.

This authors the notebook programmatically (so it stays diffable and regenerable) rather than
hand-editing JSON. Run it, then execute the notebook:

    uv run python scripts/build_eval_notebook.py [RESULTS.jsonl]
    uv run jupyter nbconvert --to notebook --execute --inplace notebooks/eval_report.ipynb

The notebook reuses the harness's own `evals.metrics` / `evals.classify`, so every number
matches `evals.run` / `evals.diff`. Charts: pass@1 vs pass^k bars, a solved-rate heatmap, a
stacked failure-mode histogram, and a token-cost distribution.
"""

from __future__ import annotations

import sys
from pathlib import Path

import nbformat as nbf
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

DEFAULT_RESULTS = "evals/results/20260618T162508Z.jsonl"


def md(text: str) -> nbf.NotebookNode:
    """Build a markdown cell."""
    return new_markdown_cell(text)


def code(src: str) -> nbf.NotebookNode:
    """Build a code cell from a source string (leading/trailing newlines stripped)."""
    return new_code_cell(src.strip("\n"))


def build(results_path: str) -> nbf.NotebookNode:
    """Assemble the report notebook over the given results file."""
    nb = new_notebook()
    nb.cells = [
        md(
            "# Eval-0 results — visual report\n\n"
            f"Rendered from `{results_path}`. Numbers reuse the harness's own "
            "`evals.metrics` (pass@1 / pass^k) and `evals.classify` (failure buckets), so they "
            "match `evals.run` / `evals.diff` exactly. Read-only, zero eval spend.\n\n"
            "- **pass@1** — capability, averaged over seeds.\n"
            "- **pass^k** — reliability, *all* k seeds of a task pass.\n"
            "- Failure buckets read each row's persisted `failure_mode` (journal-refined at scoring "
            "time, ADR-0025), so `loop_oscillation` / `decision_error` are already distinguished from "
            "plain `budget_exhausted`. Results files predating the field fall back to a row-only "
            "classification."
        ),
        code(
            f"""
import sys
from pathlib import Path

REPO = Path.cwd()
REPO = REPO if (REPO / "evals").is_dir() else REPO.parent
sys.path.insert(0, str(REPO))

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

from evals.result import load_results
from evals.metrics import pass_at_1, pass_caret_k
from evals.classify import resolve_failure_mode

sns.set_theme(style="whitegrid", context="talk", palette="deep")
FIGS = REPO / "notebooks" / "figures"
FIGS.mkdir(parents=True, exist_ok=True)

RESULTS = REPO / "{results_path}"
rows = load_results(RESULTS)


def short(m):
    return m.split("/", 1)[-1]


df = pd.DataFrame([
    dict(
        task=r.task, model=short(r.model), seed=r.seed, solved=r.solved,
        outcome=r.outcome, iterations=r.iterations,
        tokens=r.prompt_tokens + r.completion_tokens,
        bucket=resolve_failure_mode(r),  # persisted journal-refined bucket ("solved" for solved runs)
    )
    for r in rows
])
print(f"{{len(df)}} runs · {{df.model.nunique()}} models x {{df.task.nunique()}} tasks")
df.head()
"""
        ),
        md("## 1 · Capability vs. reliability — pass@1 and pass^k per model"),
        code(
            """
summary = (
    df.groupby("model")
    .apply(lambda g: pd.Series({
        "pass@1": pass_at_1([r for r in rows if short(r.model) == g.name]),
        "pass^k": pass_caret_k([r for r in rows if short(r.model) == g.name]),
        "n": len(g),
    }), include_groups=False)
    .sort_values("pass@1", ascending=False)
)
display(summary)

m = summary[["pass@1", "pass^k"]].reset_index().melt(
    id_vars="model", var_name="metric", value_name="score"
)
fig, ax = plt.subplots(figsize=(11, 6))
sns.barplot(data=m, x="score", y="model", hue="metric", ax=ax,
            order=summary.index, palette=["#2a9d8f", "#e76f51"])
ax.set_xlim(0, 1.0)
ax.set_title("pass@1 (capability) vs. pass^k (reliability)", weight="bold")
ax.set_xlabel("score"); ax.set_ylabel("")
for c in ax.containers:
    ax.bar_label(c, fmt="%.2f", padding=3, fontsize=12)
ax.legend(title="", loc="lower right")
fig.tight_layout(); fig.savefig(FIGS / "01_pass_rates.png", dpi=144, bbox_inches="tight")
plt.show()
"""
        ),
        md("## 2 · Where capability lives — solved-rate heatmap (model x task)"),
        code(
            """
pivot = df.pivot_table(index="model", columns="task", values="solved", aggfunc="mean")
pivot = pivot.loc[summary.index]  # order by overall pass@1
fig, ax = plt.subplots(figsize=(11, 6))
sns.heatmap(pivot, annot=True, fmt=".0%", cmap="RdYlGn", vmin=0, vmax=1,
            linewidths=1, linecolor="white", cbar_kws={"label": "solved rate"}, ax=ax)
ax.set_title("Solved rate per (model, task)", weight="bold")
ax.set_xlabel(""); ax.set_ylabel("")
ax.set_yticklabels(ax.get_yticklabels(), rotation=0)          # horizontal model names
ax.set_xticklabels(ax.get_xticklabels(), rotation=30, ha="right")
fig.tight_layout(); fig.savefig(FIGS / "02_solved_heatmap.png", dpi=144, bbox_inches="tight")
plt.show()
"""
        ),
        md(
            "## 3 · Failure-mode histogram — stacked by model\n\n"
            "Only non-solved runs. Stack segments are the deterministic `classify` buckets."
        ),
        code(
            """
fails = df[~df.solved]
ct = fails.pivot_table(index="model", columns="bucket", values="seed",
                       aggfunc="count", fill_value=0)
ct = ct.reindex(summary.index).fillna(0)
display(ct.astype(int))

fig, ax = plt.subplots(figsize=(11, 6))
ct.plot(kind="barh", stacked=True, ax=ax, colormap="tab20c", edgecolor="white")
ax.set_title("Failure modes by model (non-solved runs)", weight="bold")
ax.set_xlabel("failed runs"); ax.set_ylabel("")
ax.legend(title="bucket", bbox_to_anchor=(1.01, 1), loc="upper left")
fig.tight_layout(); fig.savefig(FIGS / "03_failure_modes.png", dpi=144, bbox_inches="tight")
plt.show()
"""
        ),
        md(
            "## 4 · Cost — token distribution per run\n\n"
            "Box = IQR, points = individual runs, coloured by whether the run solved. The "
            "won't-conclude pathology shows as expensive *unsolved* runs."
        ),
        code(
            """
order = df.groupby("model").tokens.median().sort_values(ascending=False).index
fig, ax = plt.subplots(figsize=(11, 6))
sns.boxplot(data=df, x="tokens", y="model", order=order, ax=ax,
            color="#ccd5ae", fliersize=0, width=0.6)
sns.stripplot(data=df, x="tokens", y="model", order=order, ax=ax,
              hue="solved", palette={True: "#2a9d8f", False: "#e63946"},
              size=8, jitter=0.2, alpha=0.85, edgecolor="white", linewidth=0.5)
ax.set_title("Tokens per run, by model (point = run, colour = solved)", weight="bold")
ax.set_xlabel("total tokens"); ax.set_ylabel("")
ax.legend(title="solved", loc="lower right")
fig.tight_layout(); fig.savefig(FIGS / "04_token_cost.png", dpi=144, bbox_inches="tight")
plt.show()
"""
        ),
        md(
            "## 5 · Cost vs. work — tokens against iterations\n\n"
            "Each point is a run; shape/colour split solved vs. failed. Failed runs drifting to "
            "the high-iteration, high-token corner are the budget-exhaustion give-ups."
        ),
        code(
            """
fig, ax = plt.subplots(figsize=(11, 7))
sns.scatterplot(data=df, x="iterations", y="tokens", hue="model", style="solved",
                s=140, alpha=0.85, ax=ax)
ax.set_title("Tokens vs. iterations per run", weight="bold")
ax.set_xlabel("iterations"); ax.set_ylabel("total tokens")
ax.legend(bbox_to_anchor=(1.01, 1), loc="upper left", fontsize=11)
fig.tight_layout(); fig.savefig(FIGS / "05_tokens_vs_iters.png", dpi=144, bbox_inches="tight")
plt.show()
"""
        ),
        md(
            "---\nFigures saved under `notebooks/figures/`. Regenerate the notebook with "
            "`uv run python scripts/build_eval_notebook.py <results.jsonl>` and re-execute with "
            "`uv run jupyter nbconvert --to notebook --execute --inplace notebooks/eval_report.ipynb`."
        ),
    ]
    return nb


def main(argv: list[str]) -> int:
    """Write the notebook for the given (or default) results file."""
    results = argv[1] if len(argv) > 1 else DEFAULT_RESULTS
    nb = build(results)
    out = Path(__file__).resolve().parent.parent / "notebooks" / "eval_report.ipynb"
    out.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(nb, out)
    print(f"wrote {out} (results = {results})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
