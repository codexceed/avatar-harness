"""TaskState — the structured source of truth for one task (§7).

The model's message history is *derived* from this; it is not the source of
truth. State is explicit, append-mostly, and fully serializable so a run can be
inspected and replayed. The runner owns all mutation (§8); these helpers are the
only sanctioned mutations.
"""

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """A test output, command result, file finding, or error (§7)."""

    step: int
    kind: str
    summary: str
    detail: str | None = None


class DecisionRecord(BaseModel):
    """Why the agent chose what it chose, and how it turned out (§7).

    `outcome` is filled in after the action runs (tool summary/error, verifier
    verdict, block reason) so the action ledger can show `chosen → outcome`.
    """

    step: int
    rationale: str
    chosen: str  # human-readable brief, shown in the action ledger
    key: str = ""  # canonical, order-independent identity for repeat detection
    rejected: list[str] = Field(default_factory=list)
    outcome: str = ""


class CommandRecord(BaseModel):
    """A command the harness ran on the workspace's behalf."""

    step: int
    command: str
    exit_code: int | None = None
    summary: str = ""


class PlannedCheck(BaseModel):
    """One resolved verification check: what runs, and where it came from (ADR-0007).

    The unit of the per-session verification plan. `provenance` names the artifact
    the command was resolved from (`config:AVATAR_TEST_COMMAND`, `ci:.github/...`,
    `Makefile:test`, `llm:<cited path>`), so every run's rubric is auditable.
    """

    name: str
    command: str
    kind: Literal["test", "lint"]
    provenance: str


class CheckResult(BaseModel):
    """One verifier check with an explicit status (§12).

    A skipped check is not a passed check: ``skip_reason`` is required when
    ``status == "skip"`` so the gate can distinguish allowed skips from evasions.
    """

    name: str
    kind: Literal["required", "optional"]
    status: Literal["pass", "fail", "skip"]
    evidence: str
    skip_reason: str | None = None


class VerifierResult(BaseModel):
    """The verifier's verdict for one verification attempt (§12)."""

    passed: bool
    summary: str
    checks: list[CheckResult] = Field(default_factory=list)
    recommended_next_action: str | None = None


class TaskState(BaseModel):
    """The full, serializable state of one task — the harness's source of truth (§7).

    Carries the two independent axes (``phase`` = where the work is, ``outcome`` =
    how it ended), the bounding counters, and the accumulated evidence/decisions
    the context builder draws on. The model's message history is derived from this.
    """

    task_id: str = Field(default_factory=lambda: uuid4().hex)
    goal: str
    constraints: list[str] = Field(default_factory=list)
    task_kind: Literal["edit", "investigate", "test_only"] = "edit"

    # Two independent axes (§7): phase = WHERE the work is; outcome = HOW it ended.
    phase: Literal["investigating", "editing", "verifying"] = "investigating"
    outcome: Literal["success", "incomplete", "blocked", "failed"] | None = None

    iterations: int = 0
    consecutive_failures: int = 0  # tool/action errors in a row -> "incomplete" at cap (§5)
    repair_failures: int = 0  # verification rejections in a row -> "failed" at cap (§5)
    prompt_tokens: int = 0  # provider-reported usage totals (in-client retries included)
    completion_tokens: int = 0
    files_read: set[str] = Field(default_factory=set)
    files_modified: set[str] = Field(default_factory=set)
    commands_run: list[CommandRecord] = Field(default_factory=list)

    evidence: list[Evidence] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    verifier_results: list[VerifierResult] = Field(default_factory=list)

    # The per-session verification plan (ADR-0007). `None` = not yet resolved;
    # `[]` = resolved and nothing was discovered (the verifier fails legibly).
    # Frozen once via `freeze_verification_plan` — the rubric never moves mid-run.
    verification_plan: list[PlannedCheck] | None = None

    current_plan: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    latest_error: str | None = None
    final_answer: str | None = None

    @property
    def terminal(self) -> bool:
        """Whether the task has reached a terminal outcome (loop should stop)."""
        return self.outcome is not None

    def add_feedback(self, summary: str, *, detail: str | None = None, kind: str = "feedback") -> None:
        """Append evidence the next context build will surface (§5 repair loop).

        Args:
            summary: One-line evidence the next context build surfaces.
            detail: Optional verbatim detail kept out of the model's summary view.
            kind: Evidence category, e.g. `feedback` or `blocker`.
        """
        self.evidence.append(Evidence(step=self.iterations, kind=kind, summary=summary, detail=detail))

    def freeze_verification_plan(self, plan: list[PlannedCheck]) -> None:
        """Freeze the resolved verification plan — once, before editing begins (ADR-0007).

        The freeze is an authority transfer away from the model: after it, the
        rubric cannot move. A second freeze attempt is a harness bug, not a retry.

        Args:
            plan: The resolved checks (may be empty: "nothing discovered").

        Raises:
            RuntimeError: When a plan is already frozen onto this state.
        """
        if self.verification_plan is not None:
            raise RuntimeError("verification plan is already frozen")
        self.verification_plan = list(plan)

    def block(self, reason: str) -> None:
        """Terminal: the task needs human input (§5 ask_user in a non-interactive run).

        Args:
            reason: Why the task is blocked.
        """
        self.add_feedback(reason, kind="blocker")
        self.outcome = "blocked"
