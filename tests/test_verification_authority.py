"""Phase 3.2d — conversational verification authority (§23.5, ADR-0002 Decision 7).

The verifier **always runs and always reports** (events + `verifier_results`); *who decides
on the result* shifts with who is in the loop:

- **strict / `--auto`** (default for batch `Harness.run`/`arun` + the runner's default): the
  §12 gate sets `outcome` and drives the repair loop — unchanged.
- **conversational** (default for the interactive `ReplSession`): the verifier runs + reports
  as *advisory*; a `FinalAnswer` is delivered and terminates the turn immediately — no repair
  loop. The turn `outcome` is `success` (a reply was produced + reported); the real verdict
  lives in `verifier_results[-1]` for the cockpit to render. The human is terminal authority.
"""

from conftest import ScriptedModel

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.events import Emitter
from avatar_harness.harness import Harness
from avatar_harness.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar_harness.runner import AgentRunner
from avatar_harness.session_state import ReplSession
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.filesystem import read_file
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


def _runner(tmp_path, registry, decisions, *, conversational=False, emitter=None, **cfg) -> AgentRunner:
    config = HarnessConfig(**cfg)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    return AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=registry,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=emitter or Emitter(),
        config=config,
        conversational=conversational,
    )


def _read_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(read_file)
    return reg


# A failing edit: the model claims done without a diff, so `diff_present` fails the §12 gate.
_NO_DIFF = [ModelDecision(action=FinalAnswer(answer="done"))]
_PASS_CMDS = {"test_command": "true", "lint_command": "true"}


# --- conversational: deliver + report, never gate -----------------------------------------


def test_conversational_delivers_reply_despite_failed_verification(git_repo):
    state = TaskState(goal="fix the add bug", task_kind="edit")
    runner = _runner(git_repo, _read_registry(), _NO_DIFF, conversational=True, **_PASS_CMDS)
    result = runner.run(state)

    assert result.outcome == "success"  # the reply was delivered (not blocked by the gate)
    assert result.final_answer == "done"
    assert result.repair_failures == 0  # conversational mode never enters the repair loop
    assert result.verifier_results[-1].passed is False  # but the verdict is recorded (advisory)


def test_conversational_verifier_always_runs_and_reports(git_repo):
    events: list = []
    emitter = Emitter()
    emitter.subscribe(events.append)
    state = TaskState(goal="fix the add bug", task_kind="edit")
    runner = _runner(git_repo, _read_registry(), _NO_DIFF, conversational=True, emitter=emitter, **_PASS_CMDS)
    runner.run(state)

    assert any(e["type"] == "verification_end" for e in events)  # the verifier ran + reported
    assert state.verifier_results  # its verdict is on the state for rendering


def test_conversational_passing_verification_is_success(git_repo):
    (git_repo / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="the handler is in app.py")),
    ]
    state = TaskState(goal="where is the handler?", task_kind="investigate")
    result = _runner(git_repo, _read_registry(), decisions, conversational=True).run(state)

    assert result.outcome == "success"
    assert result.verifier_results[-1].passed is True  # a clean pass is reported as such


# --- strict / --auto: the §12 gate still owns the outcome ---------------------------------


def test_auto_mode_preserves_strict_gate(git_repo):
    state = TaskState(goal="fix the add bug", task_kind="edit")
    runner = _runner(git_repo, _read_registry(), _NO_DIFF, conversational=False, **_PASS_CMDS)
    result = runner.run(state)

    assert result.outcome == "failed"  # strict gate: repair budget exhausted on an unverifiable edit
    assert result.repair_failures >= 1  # it actually entered the repair loop


# --- the REPL default is conversational; --auto restores strict ---------------------------


async def test_replsession_default_is_conversational_and_auto_restores_strict(git_repo):
    def _repl(*, auto):
        config = HarnessConfig(workspace_root=str(git_repo), **_PASS_CMDS)
        harness = Harness(config=config, model=ScriptedModel(_NO_DIFF), tools=_read_registry())
        return ReplSession(harness, auto=auto)

    conversational = await _repl(auto=False).submit("fix the add bug")  # default: deliver, no repair
    assert conversational.task_kind == "edit"
    assert conversational.outcome == "success"
    assert conversational.repair_failures == 0

    strict = await _repl(auto=True).submit("fix the add bug")  # --auto: strict §12 gate
    assert strict.outcome == "failed"
