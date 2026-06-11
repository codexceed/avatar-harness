"""Command tools: run_tests, run_linter (§10, tier 2).

These let the model *investigate* by running the configured test/lint commands.
They are complementary to — never a substitute for — the harness-owned verifier,
which runs its own command to set the success outcome (§5). A command that runs
and reports failures is DATA (`success=True`, failures in `content`); only a
command that could not run (timeout, target not found) is a failed `ToolResult`.
"""

from pydantic import BaseModel

from avatar_harness.deps import RunDeps
from avatar_harness.tools.base import ToolDefinition, ToolResult

# Verification tools load in the editing and verifying phases (§21).
_VERIFY_PHASES = frozenset({"editing", "verifying"})

_USAGE_ERROR_EXIT = 4  # pytest convention: usage error / target not found (model-correctable).
_CONTENT_BUDGET = 2000


def _excerpt(out: object) -> str:
    text = f"{getattr(out, 'stdout', '')}{getattr(out, 'stderr', '')}".strip()
    return text[:_CONTENT_BUDGET]


class RunTestsInput(BaseModel):
    """Input for `run_tests`: an optional target appended to the configured command."""

    target: str | None = None


def _run_tests(args: RunTestsInput, deps: RunDeps) -> ToolResult:
    command = deps.config.test_command
    if args.target:
        command = f"{command} {args.target}"
    out = deps.workspace.run(command, timeout=deps.config.command_timeout_seconds)
    if out.timed_out:
        # A timeout is a SYSTEM failure: surface it, never auto-retry (§16).
        return ToolResult(tool_name="run_tests", success=False, error=f"tests timed out: {command!r}")
    if out.exit_code == _USAGE_ERROR_EXIT:
        # Target not found is model-correctable (§10): the model fixes the target and retries.
        return ToolResult(tool_name="run_tests", success=False, error=f"test target not found: {command!r}")
    return ToolResult(
        tool_name="run_tests",
        success=True,  # the command RAN; pass/fail is in the content, not the tool's success flag
        content=_excerpt(out),
        summary=f"tests exit={out.exit_code}",
    )


run_tests = ToolDefinition(
    name="run_tests",
    description="Run the configured test command (optionally scoped to a target).",
    input_model=RunTestsInput,
    handler=_run_tests,
    phases=_VERIFY_PHASES,
    permission_tier=2,
)


class RunLinterInput(BaseModel):
    """Input for `run_linter`: none — it runs the configured lint command."""


def _run_linter(args: RunLinterInput, deps: RunDeps) -> ToolResult:  # noqa: ARG001 — ToolHandler shape; run_linter takes no input
    command = deps.config.lint_command
    out = deps.workspace.run(command, timeout=deps.config.command_timeout_seconds)
    if out.timed_out:
        return ToolResult(tool_name="run_linter", success=False, error=f"lint timed out: {command!r}")
    return ToolResult(
        tool_name="run_linter",
        success=True,
        content=_excerpt(out),
        summary=f"lint exit={out.exit_code}",
    )


run_linter = ToolDefinition(
    name="run_linter",
    description="Run the configured lint / type checks over the workspace.",
    input_model=RunLinterInput,
    handler=_run_linter,
    phases=_VERIFY_PHASES,
    permission_tier=2,
)


class RunCommandInput(BaseModel):
    """Input for `run_command`: one project command (run as an argv, no shell metacharacters)."""

    command: str


def _run_command(args: RunCommandInput, deps: RunDeps) -> ToolResult:
    # Empty input would shlex.split to [] → subprocess.run([]) raises; treat as
    # model-correctable rather than a system error surfaced from the runtime.
    if not args.command.strip():
        return ToolResult(tool_name="run_command", success=False, error="empty command")
    ws = deps.workspace
    # Attribute the command's side effects: the paths git sees as changed/untracked
    # AFTER minus those already changed BEFORE (§8/§15). This is what makes codegen,
    # migrations, and formatters participate in the diff/artifact/verifier path.
    before = ws.status_paths()
    out = ws.run(args.command, timeout=deps.config.command_timeout_seconds)
    if out.timed_out:
        # A timeout is a SYSTEM failure: surface it, never auto-retry (§16).
        return ToolResult(
            tool_name="run_command", success=False, error=f"command timed out: {args.command!r}"
        )
    changed = sorted(ws.status_paths() - before)
    ws.stage(changed)  # untracked output is invisible to `git diff <baseline>` until staged
    return ToolResult(
        tool_name="run_command",
        success=True,  # the command RAN; pass/fail lives in content/exit, not the flag — evidence (§12)
        content=_excerpt(out),
        summary=f"`{args.command}` exit={out.exit_code}",
        files_changed=changed,  # flows into state.files_modified → diff → artifact → verifier
    )


run_command = ToolDefinition(
    name="run_command",
    description=(
        "Run a project command (build, codegen, migration, a specific test target, ...) as an argv "
        "(no shell metacharacters). Approval-gated: default-blocked in batch, asks in the REPL."
    ),
    input_model=RunCommandInput,
    handler=_run_command,
    # editing/verifying only (ADR-0002): phase governs the *workflow contract* even though
    # tier-3 is the security boundary. Deliberately NOT admitted in investigate tasks:
    # ADR-0005 relaxes tier-1 writes only, so no command tool runs from `investigating` —
    # the recorded ADR-0005 limitation (a true instrument→run→observe→revert loop needs a
    # follow-up decision). A pure-execution task is a later, explicit mode, not this tool.
    phases=_VERIFY_PHASES,
    permission_tier=3,
)
