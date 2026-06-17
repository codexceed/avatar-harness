"""Dedup a failure cluster against institutional memory — `failure-modes.md` + open ADRs.

Only *novel* clusters reach Workflow A's analysis fan-out; a known or already-proposed mode
(e.g. C1 → ADR-0022) is linked, never re-debugged. A deterministic significant-token-overlap
prefilter (no model); the Workflow-A judge refines. See ADR-0024 / the design doc §4.

``python -m evals.triage "<symptom>"`` prints the match against the repo's catalog + ADR index.
"""

import argparse
import re
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

_CATALOG_HEADING = re.compile(r"^###\s+([A-D]\d+)\s+·\s+(.+?)\s*$", re.MULTILINE)
_ADR_ROW = re.compile(r"^\|\s*\[(\d{4})\]\([^)]+\)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|", re.MULTILINE)
_STATUS_ICONS = "✅🔧📋"
_TOKEN = re.compile(r"[a-z0-9]{3,}")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "not",
        "but",
        "its",
        "it",
        "that",
        "this",
        "with",
        "are",
        "was",
        "from",
        "via",
        "per",
    }
)
_DEFAULT_THRESHOLD = 2  # this many shared significant tokens reads as a match


class CatalogEntry(BaseModel):
    """One `failure-modes.md` entry (an A/B/C/D bucket id, its title, and status icon)."""

    id: str
    bucket: str
    title: str
    status: str


class AdrEntry(BaseModel):
    """One row of the ADR index (`docs/adr/README.md`)."""

    num: str
    title: str
    status: str


class TriageResult(BaseModel):
    """The verdict for one cluster: novel, or matched to a catalog entry / open ADR."""

    novel: bool
    catalog_match: str | None = None
    adr_match: str | None = None
    score: int = 0


def _tokens(text: str) -> set[str]:
    """Significant lowercase tokens of `text` (≥3 chars, stopwords dropped).

    Args:
        text: Free text (a title or a cluster symptom).

    Returns:
        The set of significant tokens.
    """
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}


def parse_catalog(md: str) -> list[CatalogEntry]:
    """Parse `failure-modes.md` into its catalog entries.

    Args:
        md: The catalog markdown.

    Returns:
        One `CatalogEntry` per ``### <ID> · <title> <icon>`` heading, in document order.
    """
    entries: list[CatalogEntry] = []
    for match in _CATALOG_HEADING.finditer(md):
        ident, rest = match.group(1), match.group(2)
        title, status = rest, ""
        for icon in _STATUS_ICONS:
            if icon in rest:
                title, status = rest.split(icon, 1)[0].strip(), icon
                break
        entries.append(CatalogEntry(id=ident, bucket=ident[0], title=title, status=status))
    return entries


def parse_adr_index(md: str) -> list[AdrEntry]:
    """Parse the ADR index table (`docs/adr/README.md`) into rows.

    Args:
        md: The ADR index markdown.

    Returns:
        One `AdrEntry` per ``| [NNNN](...) | Title | Status |`` row (header/separator skipped).
    """
    return [
        AdrEntry(num=m.group(1), title=m.group(2).strip(), status=m.group(3).strip())
        for m in _ADR_ROW.finditer(md)
    ]


def _best(
    symptom_tokens: set[str], candidates: Sequence[tuple[str, str]], threshold: int
) -> tuple[str | None, int]:
    """The highest-overlap candidate at or above `threshold` shared tokens.

    Args:
        symptom_tokens: The cluster symptom's significant tokens.
        candidates: ``(id, title)`` pairs to score against.
        threshold: The minimum shared-token count to count as a match.

    Returns:
        ``(best_id, best_score)`` — `best_id` is `None` when nothing meets the threshold.
    """
    best_id: str | None = None
    best_score = 0
    for ident, title in candidates:
        score = len(symptom_tokens & _tokens(title))
        if score >= threshold and score > best_score:
            best_id, best_score = ident, score
    return best_id, best_score


def triage(
    symptom: str,
    catalog: Sequence[CatalogEntry],
    adrs: Sequence[AdrEntry],
    *,
    threshold: int = _DEFAULT_THRESHOLD,
) -> TriageResult:
    """Match a cluster symptom against the catalog and *open* (Proposed) ADRs.

    Args:
        symptom: A free-text description of the failure cluster (task + bucket + keywords).
        catalog: Parsed `failure-modes.md` entries.
        adrs: Parsed ADR index rows (only Proposed ones are eligible to advance).
        threshold: Minimum shared significant tokens to count as a match.

    Returns:
        A `TriageResult`; `novel` is true only when neither the catalog nor an open ADR matches.
    """
    tokens = _tokens(symptom)
    cat_id, cat_score = _best(tokens, [(e.id, e.title) for e in catalog], threshold)
    adr_id, adr_score = _best(
        tokens, [(a.num, a.title) for a in adrs if "proposed" in a.status.lower()], threshold
    )
    return TriageResult(
        novel=cat_id is None and adr_id is None,
        catalog_match=cat_id,
        adr_match=adr_id,
        score=max(cat_score, adr_score),
    )


def main(argv: list[str] | None = None) -> int:
    """Triage a free-text symptom against the repo's catalog + ADR index.

    Args:
        argv: CLI args (``<symptom> [--catalog ...] [--adr-index ...]``); `None` uses ``sys.argv``.

    Returns:
        Process exit code (0).
    """
    parser = argparse.ArgumentParser(prog="evals.triage", description="Dedup a failure symptom vs memory.")
    parser.add_argument("symptom", help="free-text symptom / cluster description")
    parser.add_argument("--catalog", default="docs/research/failure-modes.md")
    parser.add_argument("--adr-index", default="docs/adr/README.md")
    args = parser.parse_args(argv)
    catalog = parse_catalog(Path(args.catalog).read_text(encoding="utf-8"))
    adrs = parse_adr_index(Path(args.adr_index).read_text(encoding="utf-8"))
    print(triage(args.symptom, catalog, adrs).model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
