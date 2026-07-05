"""Cost and latency metrics for eval runs — the canonical, shared definitions.

Sits beside `evals.metrics` (capability/reliability) and is the single Python source of truth for
the *cost* view: dollars (tokens x per-model price) and wall-clock latency. `scripts/eval_report.py`
imports these; the `tools/eval-dashboard` JS mirrors them and reads the SAME `pricing.json`, so the
terminal report and the dashboard can never disagree on cost.

Two deliberate points:
  * **Dollars, not tokens.** Per-token prices vary ~90x across models, so token *count* is a poor
    cost proxy; `run_cost_usd` applies the per-model prompt/completion price from `pricing.json`.
  * **`cost_per_solved_usd` is the decision metric** — total spend (including the runs that failed)
    divided by the number of *solved* runs, i.e. the amortized cost to actually get a passing result.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from statistics import median

from evals.result import ResultRow

_PRICING_PATH = Path(__file__).resolve().parent / "pricing.json"

# A per-model price table: ``{model_id: {"prompt": usd_per_token, "completion": usd_per_token}}``.
Pricing = dict[str, dict[str, float]]


def load_pricing(path: Path | None = None) -> Pricing:
    """Load the per-model price table from `pricing.json` (the shared source of truth).

    Args:
        path: The pricing JSON to read; `None` uses the bundled `evals/pricing.json`.

    Returns:
        A mapping ``model_id -> {"prompt": $/token, "completion": $/token}`` (the file's ``models``
        object); an empty mapping if the file is absent.
    """
    p = path or _PRICING_PATH
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("models", {})


def run_cost_usd(row: ResultRow, pricing: Pricing) -> float | None:
    """Dollar cost of one run: ``prompt_tokens x prompt_price + completion_tokens x completion_price``.

    Args:
        row: The scored run row.
        pricing: The price table from `load_pricing`.

    Returns:
        The run's USD cost, or `None` when the row's model has no entry in `pricing` (so callers can
        show "—" rather than a misleading $0).
    """
    price = pricing.get(row.model)
    if price is None:
        return None
    return row.prompt_tokens * price["prompt"] + row.completion_tokens * price["completion"]


def mean_run_cost_usd(rows: Sequence[ResultRow], pricing: Pricing) -> float | None:
    """Mean USD cost per run over `rows`.

    Args:
        rows: The run rows (typically one model's).
        pricing: The price table from `load_pricing`.

    Returns:
        The mean per-run cost over the priced rows, or `None` if none are priced.
    """
    costs = [c for c in (run_cost_usd(r, pricing) for r in rows) if c is not None]
    return sum(costs) / len(costs) if costs else None


def cost_per_solved_usd(rows: Sequence[ResultRow], pricing: Pricing) -> float | None:
    """Amortized USD cost per *solved* run — total spend (incl. failures) / solved count.

    This is the decision metric: it charges the wasted spend on failed attempts against the successes,
    so a flaky-but-cheap model and a reliable-but-pricey one compare on the true cost of a result.

    Args:
        rows: The run rows (typically one model's).
        pricing: The price table from `load_pricing`.

    Returns:
        Total priced cost divided by the number of solved runs, or `None` when nothing is priced or
        no run solved.
    """
    costs = [c for c in (run_cost_usd(r, pricing) for r in rows) if c is not None]
    solved = sum(1 for r in rows if r.solved)
    if not costs or solved == 0:
        return None
    return sum(costs) / solved


def median_wall_clock_seconds(rows: Sequence[ResultRow]) -> float | None:
    """Median agent-loop wall-clock (seconds) over `rows` that recorded it.

    Median, not mean, because the slowest runs are right-censored at the task's wall-clock budget, so
    the mean is inflated by capped runs.

    Args:
        rows: The run rows.

    Returns:
        The median of the non-null `wall_clock_seconds`, or `None` if no row recorded one (e.g. rows
        written before the field existed and never backfilled).
    """
    vals = [r.wall_clock_seconds for r in rows if r.wall_clock_seconds is not None]
    return median(vals) if vals else None
