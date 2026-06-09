"""Phase 3.1 Lane 3 — `run_command`: a constrained, tier-3, approval-gated command tool.

A scoped revision of §2's "no run_shell in v1" (ADR-0002 D4): one tool that runs a
model-chosen command through `Workspace.run` (no shell metacharacters), default-blocked
in batch and approval-gated in the REPL. The human approval gate is the backstop; the
verifier still owns `outcome` (invariant #3).
"""

import asyncio

from conftest import ScriptedModel

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.events import Emitter
from avatar_harness.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar_harness.permission import PermissionPolicy
from avatar_harness.runner import AgentRunner
from avatar_harness.session import Session
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.commands import run_command
from avatar_harness.tools.filesystem import read_file
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


def _deps(tmp_path, **cfg) -> RunDeps:
    return RunDeps(
        workspace=Workspace(tmp_path), config=HarnessConfig(**cfg), cancellation=CancellationToken()
    )


def test_run_command_runs_argv_via_workspace(tmp_path):
    # The command RAN → success=True with output in content; a ran-but-failed command is
    # still success=True (data), exactly like run_tests. And it never self-certifies.
    result = run_command.handler(
        run_command.input_model(command="python -c \"print('hello')\""), _deps(tmp_path)
    )
    assert result.success
    assert "hello" in result.content
    assert result.terminate is False  # evidence, never a "ready for verification" signal (§12, invariant #3)


def test_run_command_is_tier_3():
    assert run_command.permission_tier == 3  # default-blocked in batch; approval-gated in the REPL


def test_run_command_is_editing_verifying_only():
    # ADR-0002: not advertised during read-only `investigating`, so an investigate task
    # can't reach it (and the command-ungrounded verifier dead-end it would cause).
    assert run_command.phases == frozenset({"editing", "verifying"})


def test_run_command_ran_but_failed_is_success(tmp_path):
    # A nonzero exit is DATA, not a tool failure — success=True, like run_tests.
    result = run_command.handler(
        run_command.input_model(command='python -c "import sys; sys.exit(1)"'), _deps(tmp_path)
    )
    assert result.success
    assert "exit=1" in result.summary


def test_run_command_empty_is_model_correctable(tmp_path):
    # shlex.split("") → [] → subprocess.run([]) would raise; guard it as model-correctable.
    result = run_command.handler(run_command.input_model(command="   "), _deps(tmp_path))
    assert not result.success
    assert result.error == "empty command"


def test_run_command_created_file_flows_into_diff(git_repo):
    # A command that creates a file must participate in the existing path: attributed in
    # files_changed AND staged so it shows in the pinned-HEAD diff (→ artifact, verifier).
    deps = _deps(git_repo)
    result = run_command.handler(
        run_command.input_model(command="python -c \"open('gen.py','w').write('X = 1\\n')\""), deps
    )
    assert result.success
    assert "gen.py" in result.files_changed  # the command's mutation is attributed
    assert "gen.py" in deps.workspace.diff()  # staged → visible in the diff/artifact/verifier


def test_run_command_blocked_in_batch(tmp_path):
    # tier-3 → the gate asks; with no approval controller (batch) that is a block.
    perm = PermissionPolicy().check(
        run_command, {"command": "echo hi"}, TaskState(goal="x", task_kind="edit"), Workspace(tmp_path)
    )
    assert perm.blocked and perm.ask


def test_run_command_no_shell_metacharacters(tmp_path):
    # shlex.split execs an argv — no shell. `&&` is passed as a literal arg, not an operator,
    # so a chained `echo pwned` never runs as its own command.
    cmd = 'python -c "import sys; print(sys.argv[1:])" && echo pwned'
    result = run_command.handler(run_command.input_model(command=cmd), _deps(tmp_path))
    assert "'&&'" in result.content  # '&&' arrived as a literal argv item, proving no shell split


def test_run_command_timeout_is_system_failure(tmp_path):
    # A timeout is a SYSTEM failure: surfaced as success=False, never auto-retried.
    deps = _deps(tmp_path, command_timeout_seconds=1)
    result = run_command.handler(
        run_command.input_model(command='python -c "import time; time.sleep(3)"'), deps
    )
    assert not result.success
    assert result.error and "timed out" in result.error


async def test_run_command_approved_executes(tmp_path):
    # Through a session, approving the tier-3 call lets the command run (writes a sentinel).
    reg = ToolRegistry()
    reg.register(run_command)
    reg.register(read_file)
    decisions = [
        ModelDecision(
            action=ToolCall(name="run_command", input={"command": "python -c \"open('RAN','w').write('1')\""})
        ),
        ModelDecision(action=FinalAnswer(answer="ran the command")),
    ]
    runner = AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=reg,
        deps=_deps(tmp_path),
        context_builder=ContextBuilder(),
        verifier=Verifier(HarnessConfig()),
        emitter=Emitter(),
        config=HarnessConfig(),
    )
    # edit task already in `editing` (run_command isn't advertised in `investigating`).
    session = Session(runner, TaskState(goal="run it", task_kind="edit", phase="editing"))
    run_task = asyncio.create_task(session.run())
    async for ev in session.events():
        if ev.type == "approval_requested":
            await session.resolve_approval(ev.approval_id, allow=True)
    await run_task
    assert (tmp_path / "RAN").exists()  # the gated command ran after approval
