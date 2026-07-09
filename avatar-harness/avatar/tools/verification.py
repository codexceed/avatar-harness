"""Verification-contract tools: `declare_verification` (§10, ADR-0038).

Greenfield edit tasks declare nothing the planner can detect or cite, so the model authors
its own **real** verification contract — executing checks the harness still runs itself and
grades on the real exit code (never self-certification, §5). The tool only *buffers* the
declared checks onto `RunDeps`; the runner drains them into the frozen plan at the
investigating→editing boundary (tools never mutate `TaskState`, §8). A non-vacuity guard
rejects no-op commands so the declared contract can't be a bar the model trivially clears.
"""

from pydantic import BaseModel, Field

from avatar.deps import RunDeps
from avatar.planner import vacuous_declared_check
from avatar.state import PlannedCheck
from avatar.tools.base import ToolDefinition, ToolResult

# Declared checks are offered while the model still shapes the contract, before the plan freezes.
_DECLARE_PHASES = frozenset({"investigating", "editing"})


class DeclaredCheckInput(BaseModel):
    """One check in a model-declared verification contract (ADR-0038)."""

    command: str = Field(description="A shell command that runs the code and exits non-zero if broken.")
    kind: str = Field(default="test", description="'test' or 'lint' — the slot this check fills.")


class DeclareVerificationInput(BaseModel):
    """Input for `declare_verification`: the checks that define 'done' for this greenfield task."""

    checks: list[DeclaredCheckInput] = Field(description="One or more executing verification checks.")


class AlterVerificationInput(BaseModel):
    """Input for `alter_verification`: the replacement checks plus why the old ones are obsolete."""

    checks: list[DeclaredCheckInput] = Field(description="The replacement executing verification checks.")
    rationale: str = Field(description="Why the current contract is obsolete given the code as built.")


def _validate_checks(checks: list[DeclaredCheckInput]) -> tuple[list[PlannedCheck], str]:
    """Validate declared/amended checks and build their `PlannedCheck`s (ADR-0038).

    Vacuity is judged across the whole contract (PR-#110 review): one executing check
    redeems it, so an artifact-inspection check *alongside* a real one is legal — it
    still runs and must pass. Only a contract in which no check executes anything is
    rejected; per-check rejection recreated the burn-a-turn failure the per-segment
    fix removed, one level up.

    Args:
        checks: The model-supplied checks to validate.

    Returns:
        `(planned, "")` on success, or `([], error)` with a model-correctable message.
    """
    if not checks:
        return [], "declare at least one verification check (an executing test/lint command)"
    if all(vacuous_declared_check(c.command) for c in checks):
        # Model-correctable (§10): an inspection-only contract proves nothing about the code.
        return [], (
            f"every declared check is vacuous (none executes the code): "
            f"{[c.command for c in checks]}. At least one check must RUN what you build "
            "and fail (non-zero exit) if it is broken."
        )
    planned = [
        PlannedCheck(
            name=f"declared_{i + 1}", command=c.command, kind="declared", provenance="model-declared"
        )
        for i, c in enumerate(checks)
    ]
    return planned, ""


def _declare_verification(args: DeclareVerificationInput, deps: RunDeps) -> ToolResult:
    checks, error = _validate_checks(args.checks)
    if error:
        return ToolResult(tool_name="declare_verification", success=False, error=error)
    deps.declared_contract = checks
    rubric = "; ".join(f"`{c.command}`" for c in checks)
    return ToolResult(
        tool_name="declare_verification",
        success=True,
        content=f"declared {len(checks)} verification check(s): {rubric}",
        summary=f"declared {len(checks)} check(s)",
    )


def _alter_verification(args: AlterVerificationInput, deps: RunDeps) -> ToolResult:
    # The permission gate has already disposed of the amendment (attended human / ADR-0039
    # auto-approve / auto-deny) before this handler runs; here we only validate and buffer the
    # replacement, which the runner folds into the frozen plan (floor preserved).
    checks, error = _validate_checks(args.checks)
    if error:
        return ToolResult(tool_name="alter_verification", success=False, error=error)
    deps.declared_contract = checks
    rubric = "; ".join(f"`{c.command}`" for c in checks)
    return ToolResult(
        tool_name="alter_verification",
        success=True,
        content=f"amended contract to {len(checks)} check(s): {rubric} — {args.rationale}",
        summary=f"amended to {len(checks)} check(s)",
    )


declare_verification = ToolDefinition(
    name="declare_verification",
    description=(
        "Declare the verification contract for a from-scratch task: one or more commands that RUN "
        "what you build and exit non-zero if it is broken. At least one check MUST exercise the "
        "actual deliverable end-to-end — the real entry point imports and launches (e.g. run the "
        "program, or import its main module), not only isolated unit tests. Install any tooling your "
        "checks need first, and make the commands run in that environment. The harness runs them "
        "itself and grades on the real exit code. Declare this before you edit."
    ),
    input_model=DeclareVerificationInput,
    handler=_declare_verification,
    phases=_DECLARE_PHASES,
    permission_tier=0,
)


alter_verification = ToolDefinition(
    name="alter_verification",
    description=(
        "Amend the verification contract you declared, when a check has become obsolete as the "
        "design evolved (NOT to dodge a real failure). Supply the replacement checks and a rationale. "
        "The replacement must still exercise the deliverable end-to-end — you may not narrow the "
        "contract to skip running the real entry point. This is gated: a human approves it, or an "
        "autonomous run applies its configured policy. The immutable floor beneath your contract "
        "cannot be amended away."
    ),
    input_model=AlterVerificationInput,
    handler=_alter_verification,
    phases=_DECLARE_PHASES,
    permission_tier=3,
)
