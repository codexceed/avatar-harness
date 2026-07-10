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
from avatar.planner import CHANGE_KIND_COVERAGE
from avatar.state import PlannedCheck
from avatar.tools.base import ToolDefinition, ToolResult

# Declared checks are offered while the model still shapes the contract, before the plan freezes.
_DECLARE_PHASES = frozenset({"investigating", "editing"})

_CHANGE_KINDS_DESCRIPTION = (
    "The kinds of change this contract validates: 'code' (functional — behavior in executable "
    "artifacts) and/or 'content' (textual artifacts: docs, specs). List every kind the task "
    "touches; each needs a covering check. Defaults to ['code']."
)

# Per-kind steering appended to a coverage rejection, so the model recovers in one turn.
# The 'code' wording keeps the ADR-0038 vocabulary ("vacuous", "at least one") — it is the
# same rule, now scoped to the kind instead of the whole contract.
_KIND_STEER = {
    "code": (
        "every candidate is vacuous there — at least one check must RUN what you build "
        "and exit non-zero if it is broken"
    ),
    "content": (
        "at least one check must inspect the artifact (name the file, e.g. "
        "`grep -q '<required section>' FILE.md`) and be able to fail on a wrong one "
        "— no `|| true`-style fallback"
    ),
}


class DeclaredCheckInput(BaseModel):
    """One check in a model-declared verification contract (ADR-0038)."""

    command: str = Field(description="A shell command that runs the code and exits non-zero if broken.")
    kind: str = Field(default="test", description="'test' or 'lint' — the slot this check fills.")


class DeclareVerificationInput(BaseModel):
    """Input for `declare_verification`: the checks that define 'done' for this greenfield task."""

    checks: list[DeclaredCheckInput] = Field(description="One or more executing verification checks.")
    change_kinds: list[str] = Field(default_factory=lambda: ["code"], description=_CHANGE_KINDS_DESCRIPTION)


class AlterVerificationInput(BaseModel):
    """Input for `alter_verification`: the replacement checks plus why the old ones are obsolete."""

    checks: list[DeclaredCheckInput] = Field(description="The replacement executing verification checks.")
    rationale: str = Field(description="Why the current contract is obsolete given the code as built.")
    change_kinds: list[str] = Field(default_factory=lambda: ["code"], description=_CHANGE_KINDS_DESCRIPTION)


def _validate_checks(
    checks: list[DeclaredCheckInput], change_kinds: list[str]
) -> tuple[list[PlannedCheck], str]:
    """Validate declared/amended checks against the declared change kinds (ADR-0038/0044).

    Coverage is per kind, judged across the whole contract (PR-#110 review): **each**
    declared kind needs at least one check satisfying its rulebook — `code` requires an
    executing check, `content` an anchored+falsifiable one — and one check may count
    toward both. Companion checks satisfying no rulebook are tolerated once every kind
    is covered (they still run and must pass); per-check rejection recreated the
    burn-a-turn failure the per-segment fix removed, one level up.

    Args:
        checks: The model-supplied checks to validate.
        change_kinds: The declared kinds this contract must cover.

    Returns:
        `(planned, "")` on success, or `([], error)` with a model-correctable message.
    """
    if not checks:
        return [], "declare at least one verification check (an executing test/lint command)"
    if not change_kinds:
        return [], "declare at least one change kind: 'code' (functional) and/or 'content' (textual)"
    unknown = sorted(set(change_kinds) - CHANGE_KIND_COVERAGE.keys())
    if unknown:
        return [], (
            f"unknown change kind(s) {unknown}: valid kinds are "
            "'code' (functional) and 'content' (textual artifacts)"
        )
    uncovered = [k for k in change_kinds if not any(CHANGE_KIND_COVERAGE[k](c.command) for c in checks)]
    if uncovered:
        # Model-correctable (§10): a declared kind with no covering check proves nothing there.
        steers = "; ".join(f"'{k}': {_KIND_STEER[k]}" for k in uncovered)
        return [], (
            f"no declared check covers change kind(s) {uncovered}: "
            f"{[c.command for c in checks]}. For {steers}."
        )
    planned = [
        PlannedCheck(
            name=f"declared_{i + 1}", command=c.command, kind="declared", provenance="model-declared"
        )
        for i, c in enumerate(checks)
    ]
    return planned, ""


def _declare_verification(args: DeclareVerificationInput, deps: RunDeps) -> ToolResult:
    checks, error = _validate_checks(args.checks, args.change_kinds)
    if error:
        return ToolResult(tool_name="declare_verification", success=False, error=error)
    deps.declared_contract = checks
    deps.declared_change_kinds = list(args.change_kinds)
    rubric = "; ".join(f"`{c.command}`" for c in checks)
    kinds = "+".join(args.change_kinds)
    return ToolResult(
        tool_name="declare_verification",
        success=True,
        content=f"declared {len(checks)} verification check(s) covering {kinds}: {rubric}",
        summary=f"declared {len(checks)} check(s)",
    )


def _alter_verification(args: AlterVerificationInput, deps: RunDeps) -> ToolResult:
    # The permission gate has already disposed of the amendment (attended human / ADR-0039
    # auto-approve / auto-deny) before this handler runs; here we only validate and buffer the
    # replacement, which the runner folds into the frozen plan (floor preserved).
    checks, error = _validate_checks(args.checks, args.change_kinds)
    if error:
        return ToolResult(tool_name="alter_verification", success=False, error=error)
    deps.declared_contract = checks
    deps.declared_change_kinds = list(args.change_kinds)
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
        "Declare the verification contract for a from-scratch task: one or more commands that exit "
        "non-zero if the deliverable is broken, plus change_kinds — the kinds of change being made "
        "('code' and/or 'content'). For 'code', at least one check MUST exercise the actual "
        "deliverable end-to-end — the real entry point imports and launches (e.g. run the program, "
        "or import its main module), not only isolated unit tests. For 'content' (docs/specs), at "
        "least one check must inspect the artifact and fail if it is wrong (e.g. grep required "
        "sections) — no can't-fail fallbacks. Install any tooling your checks need first, and make "
        "the commands run in that environment. The harness runs them itself and grades on the real "
        "exit code, and at verification time the kinds of files actually changed must all have been "
        "declared. Declare this before you edit."
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
