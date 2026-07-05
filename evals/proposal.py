"""The `ChangeProposal` — the typed seam between Workflow A (proposals) and B (PRs).

Workflow A writes scored, routed proposals; Workflow B is invoked per *funded* proposal.
`remediation_type` (the *kind* of fix) is orthogonal to `blast_radius` (which governs
validation + governance). See ADR-0024 and `evals/improvement-loop-design.md` §4.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

RemediationType = Literal["prompt_instruction", "guardrail_check", "code_logic", "doc_only"]
BlastRadius = Literal["local", "global"]
Route = Literal["adr_only", "implement"]
Status = Literal["proposed", "funded", "building", "merged", "rejected"]

_IMPACT_MAX = 10


class ChangeProposal(BaseModel):
    """One proposed harness change, derived from a deduplicated failure cluster."""

    id: str
    mode: str  # catalog id (e.g. "C1") or "novel:<slug>"
    title: str
    impact: int  # 0-10, a proxy for likelihood of recurrence
    remediation_type: RemediationType
    blast_radius: BlastRadius
    touches_grader: bool = False  # edits specs/probes/fixtures/verifier/scoring
    target_tasks: list[str] = Field(default_factory=list)
    predicted_validation_cost_tokens: int = 0
    tdd_plan: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)  # result-row / digest refs
    status: Status = "proposed"
    body: str = ""

    def route(self) -> Route:
        """Governance route for the change.

        Returns:
            ``"adr_only"`` when the change is global or touches the grading surface (it must be
            proposed as an ADR, never auto-implemented); ``"implement"`` otherwise.
        """
        if self.blast_radius == "global" or self.touches_grader:
            return "adr_only"
        return "implement"

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line.

        Returns:
            A one-line JSON string (no trailing newline).
        """
        return self.model_dump_json()

    def to_markdown(self) -> str:
        """Render as a `<id>.md` artifact: YAML front-matter + the human body.

        Returns:
            Markdown with a ``---``-fenced front-matter block followed by `body`.
        """
        lines = ["---"]
        for key, value in self.model_dump(exclude={"body"}).items():
            # `json.dumps` output is valid YAML (JSON is a subset of YAML 1.2) and quotes/escapes
            # every scalar — so a colon-bearing title (e.g. "Edit mission: run it"), which a raw
            # scalar would make YAML read as a nested mapping, can no longer break the front-matter.
            lines.append(f"{key}: {json.dumps(value)}")
        lines.extend(["---", "", self.body])
        return "\n".join(lines)


def score_impact(cluster_size: int, total_failures: int) -> int:
    """Impact (0-10) as the cluster's share of all failures (a proxy for likelihood).

    Args:
        cluster_size: The number of failing runs in this cluster.
        total_failures: The total number of failing runs in the matrix.

    Returns:
        ``round(10 * cluster_size / total_failures)``, or 0 when there are no failures.
    """
    if total_failures <= 0:
        return 0
    return round(_IMPACT_MAX * cluster_size / total_failures)


def load_proposals(path: Path) -> list[ChangeProposal]:
    """Load proposals from a JSONL file (the inverse of `to_jsonl`).

    Args:
        path: The JSONL file.

    Returns:
        The proposals, in file order (blank lines skipped).
    """
    proposals: list[ChangeProposal] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            proposals.append(ChangeProposal.model_validate_json(line))
    return proposals


def write_proposals(proposals: Sequence[ChangeProposal], path: Path) -> None:
    """Write proposals as JSONL (one per line).

    Args:
        proposals: The proposals.
        path: The destination file.
    """
    Path(path).write_text("".join(p.to_jsonl() + "\n" for p in proposals), encoding="utf-8")
