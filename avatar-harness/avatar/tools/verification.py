"""Verification-contract tools: `declare_verification` (§10, ADR-0037).

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
    """One check in a model-declared verification contract (ADR-0037)."""

    command: str = Field(description="A shell command that runs the code and exits non-zero if broken.")
    kind: str = Field(default="test", description="'test' or 'lint' — the slot this check fills.")


class DeclareVerificationInput(BaseModel):
    """Input for `declare_verification`: the checks that define 'done' for this greenfield task."""

    checks: list[DeclaredCheckInput] = Field(description="One or more executing verification checks.")


def _declare_verification(args: DeclareVerificationInput, deps: RunDeps) -> ToolResult:
    if not args.checks:
        return ToolResult(
            tool_name="declare_verification",
            success=False,
            error="declare at least one verification check (an executing test/lint command)",
        )
    vacuous = [c.command for c in args.checks if vacuous_declared_check(c.command)]
    if vacuous:
        # Model-correctable (§10): a no-op check proves nothing; the model re-declares a real one.
        return ToolResult(
            tool_name="declare_verification",
            success=False,
            error=(
                f"these declared checks are vacuous (they don't run the code): {vacuous}. "
                "Declare commands that execute what you built and fail (non-zero exit) if it is broken."
            ),
        )
    checks = [
        PlannedCheck(
            name=f"declared_{i + 1}",
            command=c.command,
            kind="declared",
            provenance="model-declared",
        )
        for i, c in enumerate(args.checks)
    ]
    deps.declared_contract = checks
    rubric = "; ".join(f"`{c.command}`" for c in checks)
    return ToolResult(
        tool_name="declare_verification",
        success=True,
        content=f"declared {len(checks)} verification check(s): {rubric}",
        summary=f"declared {len(checks)} check(s)",
    )


declare_verification = ToolDefinition(
    name="declare_verification",
    description=(
        "Declare the verification contract for a from-scratch task: one or more commands that RUN "
        "what you build and exit non-zero if it is broken (e.g. `python -m pytest test_x.py`). The "
        "harness runs them itself and grades on the real exit code. Declare this before you finish."
    ),
    input_model=DeclareVerificationInput,
    handler=_declare_verification,
    phases=_DECLARE_PHASES,
    permission_tier=0,
)
