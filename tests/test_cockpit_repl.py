"""Phase 3.2e — the cockpit wired to a live `ReplSession` (§23, ADR-0002).

3.2b/2c built the cockpit *shell* + modals against a fixed `ReplaySession`; this wires the
real multi-turn driver: `CockpitApp(repl=...)` routes input (meta handled locally, goals run
as observable per-goal `Session`s, plan mode runs plan → `PlanModal` → build), and the TUI's
own `jo-cli` entry point launches it (`--auto` restores the strict §12 gate) — the core CLI
never imports the TUI. Tested headlessly with Textual's `Pilot` over a real `ReplSession`
(ScriptedModel + a tmp git repo).
"""

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")  # the cockpit lives behind the optional [textual] extra


from conftest import ScriptedModel
from pydantic import BaseModel

from avatar_harness.config import HarnessConfig
from avatar_harness.harness import Harness
from avatar_harness.intent import ModeClassifier
from avatar_harness.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar_harness.session_state import ReplSession
from avatar_harness.tools.base import ToolDefinition, ToolRegistry, ToolResult
from avatar_harness.tools.edit import str_replace
from avatar_harness.tools.filesystem import read_file
from avatar_harness.tui import cli as jo_cli
from avatar_harness.tui.app import CockpitApp
from avatar_harness.tui.modals import ApprovalModal, DiffModal, PlanModal


class _Empty(BaseModel):
    pass


def _read_registry(*, edit: bool = False) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(read_file)
    if edit:
        reg.register(str_replace)
    return reg


def _gated_registry() -> ToolRegistry:
    """read_file + a tier-3 `risky` tool reachable while investigating (no file mutation)."""

    def _risky(args, deps) -> ToolResult:
        return ToolResult(tool_name="risky", success=True, summary="did the thing")

    risky = ToolDefinition(
        name="risky",
        description="needs approval",
        input_model=_Empty,
        handler=_risky,
        phases=frozenset({"investigating"}),
        permission_tier=3,
    )
    reg = _read_registry()
    reg.register(risky)
    return reg


def _repl(root, decisions, *, registry=None, auto=False, **cfg) -> ReplSession:
    config = HarnessConfig(workspace_root=str(root), **cfg)
    harness = Harness(config=config, model=ScriptedModel(decisions), tools=registry or _read_registry())
    return ReplSession(harness, auto=auto)


async def _type_and_send(pilot, app, text: str) -> None:
    """Type `text` into the prompt and submit it."""
    inp = app.query_one("#prompt")
    inp.focus()
    inp.value = text
    await pilot.press("enter")
    await pilot.pause()


async def _settle(app, pilot) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _wait_for_screen(app, pilot, screen_cls, *, tries: int = 40):
    """Pump the UI until a `screen_cls` modal is on top (bounded, so a miss fails fast)."""
    for _ in range(tries):
        if isinstance(app.screen, screen_cls):
            return
        await pilot.pause()
    raise AssertionError(f"{screen_cls.__name__} never appeared (top screen: {type(app.screen).__name__})")


# --- the multi-turn run loop --------------------------------------------------------------


async def test_submit_goal_runs_and_renders(git_repo):
    (git_repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ]
    app = CockpitApp(repl=_repl(git_repo, decisions))
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "explain where x is set in app.py")
        await _settle(app, pilot)
        joined = "\n".join(app.rendered)
        assert "explain where x is set" in joined  # the goal
        assert "read_file" in joined  # streamed tool activity
        assert "success" in joined  # terminal outcome
        assert app.query_one("#prompt").disabled is False  # ready for the next goal


async def test_multi_turn_records_each_goal(git_repo):
    (git_repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ] * 2
    repl = _repl(git_repo, decisions)
    app = CockpitApp(repl=repl)
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "explain x in app.py")
        await _settle(app, pilot)
        await _type_and_send(pilot, app, "explain x again in app.py")
        await _settle(app, pilot)
    assert len(repl.state.tasks) == 2  # one recorded TaskState per goal


# --- meta commands handled locally --------------------------------------------------------


async def test_meta_command_renders_without_running_model(git_repo):
    repl = _repl(git_repo, [])  # no decisions: a goal would crash; a meta command must not run one
    app = CockpitApp(repl=repl)
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "/help")
        await _settle(app, pilot)
        assert any("/diff" in line for line in app.rendered)  # the help text rendered
    assert repl.state.tasks == []  # no goal task was spun


async def test_quit_meta_exits_app(git_repo):
    app = CockpitApp(repl=_repl(git_repo, []))
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "/quit")
        await pilot.pause()
        assert app.is_running is False  # /quit ended the cockpit


async def test_diff_meta_pops_diff_modal(git_repo):
    app = CockpitApp(repl=_repl(git_repo, []))
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "/diff")
        await _wait_for_screen(app, pilot, DiffModal)  # /diff opens the read-only diff viewer


# --- approval routing through the modal ---------------------------------------------------


async def test_approval_gated_goal_pops_modal_and_routes(git_repo):
    decisions = [
        ModelDecision(action=ToolCall(name="risky", input={})),
        ModelDecision(action=FinalAnswer(answer="did it")),
    ]
    repl = _repl(git_repo, decisions, registry=_gated_registry())
    app = CockpitApp(repl=repl)
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "run the risky thing")
        await _wait_for_screen(app, pilot, ApprovalModal)  # the gated call announced → modal
        await pilot.press("y")  # allow once
        await _settle(app, pilot)
    assert any("risky" in line for line in app.rendered)  # the approved tool ran + rendered
    assert len(repl.state.tasks) == 1


# --- plan mode end to end -----------------------------------------------------------------


async def test_plan_mode_runs_plan_then_modal_then_build(git_repo):
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="PLAN: in calc.py, change `-` to `+`")),
        ModelDecision(
            action=ToolCall(
                name="str_replace",
                input={"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"},
            )
        ),
        ModelDecision(action=FinalAnswer(answer="fixed add()")),
    ]
    repl = _repl(
        git_repo,
        decisions,
        registry=_read_registry(edit=True),
        auto=True,
        test_command="true",
        lint_command="true",
    )
    repl.set_mode("plan")
    app = CockpitApp(repl=repl)
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "fix the add bug")
        await _wait_for_screen(app, pilot, PlanModal)  # the read-only plan is presented for approval
        await pilot.click("#approve")
        await _settle(app, pilot)
    build = repl.state.tasks[-1]
    assert build.task_kind == "edit"  # approval transitioned plan → build (edit task)
    assert build.outcome == "success"


# --- CLI launch ---------------------------------------------------------------------------


async def test_goal_error_renders_instead_of_crashing(git_repo):
    # The dogfood crash: a follow-up goal raised DirtyWorkspaceError inside the Textual
    # worker and tore down the whole TUI. A per-goal failure must render as a transcript
    # line and leave the REPL alive for the next input.
    (git_repo / "calc.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
    repl = _repl(git_repo, [])  # pre-existing dirt → the first goal's clean check trips
    app = CockpitApp(repl=repl)
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "explain calc.py")
        await _settle(app, pilot)
        joined = "\n".join(app.rendered)
        assert "DirtyWorkspaceError" in joined  # surfaced to the human...
        assert not app.query_one("#prompt").disabled  # ...and the REPL is still usable
    # reaching here at all means the app survived the failed goal


def test_jo_cli_threads_allow_dirty(git_repo, monkeypatch):
    launched: dict = {}

    def _fake_run(self, *args, **kwargs):
        launched["repl"] = self.repl

    monkeypatch.setattr(CockpitApp, "run", _fake_run)
    config = HarnessConfig(workspace_root=str(git_repo))
    code = jo_cli.main(["--allow-dirty"], config=config, model_client=ScriptedModel([]))
    assert code == 0
    assert launched["repl"].allow_dirty is True  # the §15 acknowledgement reaches the REPL


def test_jo_cli_launches_cockpit(git_repo, monkeypatch):
    launched: dict = {}

    def _fake_run(self, *args, **kwargs):  # stub replacing the blocking CockpitApp.run
        launched["repl"] = self.repl

    monkeypatch.setattr(CockpitApp, "run", _fake_run)
    config = HarnessConfig(workspace_root=str(git_repo))
    code = jo_cli.main(["--auto"], config=config, model_client=ScriptedModel([]))
    assert code == 0
    assert isinstance(launched["repl"], ReplSession)
    assert launched["repl"].auto is True  # --auto threaded into the REPL (strict gate)


async def test_observe_renders_leading_events(git_repo):
    """The per-goal stream is subscribed BEFORE the run starts — no missed AgentStart.

    `arun` publishes AgentStart synchronously before its first await; `_observe` used to
    create the run task before `session.events()` subscribed, so the cockpit missed each
    goal's leading events — including the status-reset that AgentStart triggers (the
    stale `verify: ✓` in dogfood `events/04849a5a…jsonl`).
    """
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="calc.py defines add")),
    ]
    repl = _repl(git_repo, decisions)
    app = CockpitApp(repl=repl)
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "explain calc.py")
        await _settle(app, pilot)
    assert any(line.startswith("▶") for line in app.rendered)  # the AgentStart line rendered


async def test_resolved_mode_displayed_before_run(git_repo):
    """The routing verdict is visible and correctable — never silent control (D3).

    The transcript announces the resolved mode + its source + the `/mode` override
    before the goal runs, so a misclassification is seeable the moment it happens.
    """
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="calc.py defines add")),
    ]
    repl = _repl(git_repo, decisions)  # no classifier wired → heuristic source
    app = CockpitApp(repl=repl)
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "explain calc.py")
        await _settle(app, pilot)
    mode_lines = [line for line in app.rendered if "mode:" in line and "/mode" in line]
    assert mode_lines and "investigate" in mode_lines[0]
    assert "heuristic" in mode_lines[0]  # the source is named


async def test_input_disabled_during_classification(git_repo):
    """No second goal can start while the first is still classifying (PR-#32 review).

    Classification runs before `AgentStart` (whose handler used to be the only
    disabler), so `_drive_input` must close the window synchronously.
    """

    def _slow_create(**kwargs):
        time.sleep(0.3)
        raise RuntimeError("never classifies")  # falls back to the heuristic

    slow_transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_slow_create)))

    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="calc.py defines add")),
    ]
    repl = _repl(git_repo, decisions)
    repl.classifier = ModeClassifier(HarnessConfig(classifier_model="tiny"), client=slow_transport)
    app = CockpitApp(repl=repl)
    async with app.run_test() as pilot:
        await _type_and_send(pilot, app, "explain calc.py")
        assert app.query_one("#prompt").disabled  # closed immediately, mid-classification
        await _settle(app, pilot)
        assert not app.query_one("#prompt").disabled  # reopened once the goal finished
