"""Verification authority across modes (§23.5, ADR-0046 — supersedes ADR-0002 Decision 7).

The verifier **always runs, always reports, and always steers**: a failing verdict feeds the
repair loop in *every* mode, so the model repairs (or proposes a gated `alter_verification`)
toward functional correctness. A failed verdict is NEVER short-circuited to `success`. What
shifts with who is in the loop is only *who is deferred to at the terminal boundary*:

- **strict / `--auto`** (default for batch `Harness.run`/`arun` + the runner's default): the
  §12 gate owns `outcome`; repair exhaustion is `failed`.
- **conversational** (default for the interactive `ReplSession`): identical steering, but repair
  exhaustion is a first-class hand-off — the turn `blocks` (the last reply + failing verdict are
  on the state, the block reason is an `open_question`) rather than being pronounced `failed`.
  The human is terminal authority, but only *after* the verifier has steered to exhaustion.
"""

from conftest import ScriptedModel

from avatar.config import HarnessConfig
from avatar.context import ContextBuilder
from avatar.deps import CancellationToken, RunDeps
from avatar.events import Emitter
from avatar.harness import Harness
from avatar.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar.runner import AgentRunner
from avatar.session_state import ReplSession
from avatar.state import TaskState
from avatar.tools.base import ToolRegistry
from avatar.tools.filesystem import read_file
from avatar.verifier import Verifier
from avatar.workspace import Workspace


def _runner(
    tmp_path, registry, decisions, *, conversational=False, advisory=False, emitter=None, **cfg
) -> AgentRunner:
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
        advisory=advisory,
    )


def _read_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(read_file)
    return reg


# A failing edit: the model claims done without a diff, so `diff_present` fails the §12 gate.
_NO_DIFF = [ModelDecision(action=FinalAnswer(answer="done"))]
_PASS_CMDS = {"test_command": "true", "lint_command": "true"}


# --- conversational: steer, then defer to the human at exhaustion --------------------------


def test_conversational_steers_then_blocks_on_persistent_failure(git_repo):
    state = TaskState(goal="fix the add bug", task_kind="edit")
    runner = _runner(git_repo, _read_registry(), _NO_DIFF, conversational=True, **_PASS_CMDS)
    result = runner.run(state)

    # The verifier steers (the model actually enters the repair loop)...
    assert result.repair_failures >= 3
    # ...and at exhaustion the turn defers to the human — never a fake success on a failed verdict.
    assert result.outcome == "blocked"
    assert result.outcome != "success"
    assert result.final_answer == "done"  # the last reply is still delivered for the human
    assert result.verifier_results[-1].passed is False
    assert result.open_questions  # a first-class ask, not a silent failure


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


# --- advisory: external grading (ADR-0040 option A) — report, never steer ------------------


def test_advisory_mode_delivers_without_steering(git_repo):
    # The eval/option-A path: the verifier runs + reports, but a held-out probe grades, so a
    # failed verdict is delivered as success WITHOUT a repair loop (a fresh creation isn't
    # thrashed toward a gate the probe, not the harness, will judge). Distinct from conversational.
    state = TaskState(goal="fix the add bug", task_kind="edit")
    runner = _runner(git_repo, _read_registry(), _NO_DIFF, advisory=True, **_PASS_CMDS)
    result = runner.run(state)

    assert result.outcome == "success"  # delivered for the external grader
    assert result.repair_failures == 0  # advisory never steers
    assert result.verifier_results[-1].passed is False  # the real verdict is still recorded
    assert not result.open_questions  # not a hand-off; the probe decides


# --- strict / --auto: the §12 gate still owns the outcome (failed at exhaustion) ------------


def test_auto_mode_exhaustion_is_failed(git_repo):
    state = TaskState(goal="fix the add bug", task_kind="edit")
    runner = _runner(git_repo, _read_registry(), _NO_DIFF, conversational=False, **_PASS_CMDS)
    result = runner.run(state)

    assert result.outcome == "failed"  # strict gate: repair budget exhausted on an unverifiable edit
    assert result.repair_failures >= 1  # it actually entered the repair loop
    assert not result.open_questions  # autonomy pronounces failure, it does not defer to a human


# --- the REPL default is conversational; --auto restores strict ---------------------------


async def test_replsession_default_is_conversational_and_auto_restores_strict(git_repo):
    def _repl(*, auto):
        config = HarnessConfig(workspace_root=str(git_repo), **_PASS_CMDS)
        harness = Harness(config=config, model=ScriptedModel(_NO_DIFF), tools=_read_registry())
        return ReplSession(harness, auto=auto)

    # Default (conversational): the verifier steers to exhaustion, then defers to the human.
    conversational = await _repl(auto=False).submit("fix the add bug")
    assert conversational.task_kind == "edit"
    assert conversational.outcome == "blocked"
    assert conversational.repair_failures >= 3

    strict = await _repl(auto=True).submit("fix the add bug")  # --auto: strict §12 gate
    assert strict.outcome == "failed"
