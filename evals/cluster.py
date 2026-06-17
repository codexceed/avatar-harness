"""Group failed runs into clusters and run the deterministic triage prefilter (no model).

Workflow A's read-only spine: load results → distill → **cluster** (group failed runs by
task + outcome) → triage each cluster vs `failure-modes.md` + open ADRs. The triage here is a
coarse **prefilter** (token overlap over the cluster symptom); a cluster it can confidently map
to a known mode is dropped from the fan-out, the rest go to Workflow A's judge subagent which
makes the authoritative novel/known call. See ADR-0024 / the design doc §3-§4.

``python -m evals.cluster <results>.jsonl`` prints the per-cluster triage report.
"""

import argparse
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from evals.distill import TrajectoryDigest, distill_results
from evals.journal_read import row_events
from evals.result import ResultRow, load_results
from evals.triage import AdrEntry, CatalogEntry, TriageResult, parse_adr_index, parse_catalog, triage

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOKEN = re.compile(r"[a-z_]{3,}")
_SYMPTOM_TOKENS = 8  # most-common action tokens folded into a cluster's triage symptom


class Cluster(BaseModel):
    """A group of failed runs sharing a (task, outcome) signature — the unit Workflow A triages."""

    task: str
    outcome: str
    models: list[str] = Field(default_factory=list)
    runs: int = 0
    symptom: str = ""  # task + outcome + top action tokens, for the triage prefilter
    sample_actions: list[str] = Field(default_factory=list)


class ClusterVerdict(BaseModel):
    """A cluster paired with its (prefilter) triage verdict."""

    cluster: Cluster
    triage: TriageResult


def cluster_failures(rows: Sequence[ResultRow], digests: Sequence[TrajectoryDigest]) -> list[Cluster]:
    """Group the failed runs into clusters by ``(task, outcome)``.

    Args:
        rows: The result rows (solved rows are ignored).
        digests: The matching trajectory digests (paired by ``(task, model, seed)``); supply the
            cluster's action vocabulary.

    Returns:
        One `Cluster` per distinct ``(task, outcome)`` among the failed rows, in sorted order.
    """
    by_key = {(d.task, d.model, d.seed): d for d in digests}
    groups: dict[tuple[str, str], list[ResultRow]] = {}
    for row in rows:
        if row.solved:
            continue
        groups.setdefault((row.task, row.outcome or "unknown"), []).append(row)

    clusters: list[Cluster] = []
    for (task, outcome), members in sorted(groups.items()):
        member_digests = [d for r in members if (d := by_key.get((r.task, r.model, r.seed)))]
        actions = [a for d in member_digests for a in d.actions]
        tokens = Counter(t for a in actions for t in _TOKEN.findall(a.lower()))
        symptom = " ".join([task, outcome, *(tok for tok, _ in tokens.most_common(_SYMPTOM_TOKENS))])
        clusters.append(
            Cluster(
                task=task,
                outcome=outcome,
                models=sorted({r.model for r in members}),
                runs=len(members),
                symptom=symptom,
                sample_actions=member_digests[0].actions[:10] if member_digests else [],
            )
        )
    return clusters


def triage_report(
    clusters: Sequence[Cluster],
    catalog: Sequence[CatalogEntry],
    adrs: Sequence[AdrEntry],
) -> list[ClusterVerdict]:
    """Run the deterministic triage prefilter over every cluster.

    Args:
        clusters: The failure clusters.
        catalog: Parsed `failure-modes.md` entries.
        adrs: Parsed ADR index rows.

    Returns:
        One `ClusterVerdict` per cluster (the prefilter verdict; the workflow's judge refines it).
    """
    return [ClusterVerdict(cluster=c, triage=triage(c.symptom, catalog, adrs)) for c in clusters]


def main(argv: list[str] | None = None) -> int:
    """Cluster a results file's failures and print the triage prefilter report.

    Args:
        argv: CLI args (``<results>.jsonl [--catalog ...] [--adr-index ...]``); `None` uses ``sys.argv``.

    Returns:
        Process exit code (0).
    """
    parser = argparse.ArgumentParser(prog="evals.cluster", description="Cluster failures + triage prefilter.")
    parser.add_argument("results", help="a results JSONL file")
    parser.add_argument("--catalog", default=str(_REPO_ROOT / "docs/research/failure-modes.md"))
    parser.add_argument("--adr-index", default=str(_REPO_ROOT / "docs/adr/README.md"))
    args = parser.parse_args(argv)
    rows = load_results(Path(args.results))
    digests = distill_results(rows, events_for=row_events)
    clusters = cluster_failures(rows, digests)
    catalog = parse_catalog(Path(args.catalog).read_text(encoding="utf-8"))
    adrs = parse_adr_index(Path(args.adr_index).read_text(encoding="utf-8"))
    for verdict in triage_report(clusters, catalog, adrs):
        print(verdict.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
