"""Phase 3.1 Lane 2b — the Textual cockpit shell (ADR-0002).

The full-screen app skeleton that *renders* a run: a transcript pane + a status bar
(mode · phase · outcome) + an input box, wired to a session's `events()` stream.
Approval/plan/diff modals are Lane 2c; this is the shell + event rendering.

Tested headlessly against a `ReplaySession` (a fixed event list, no model) via Textual's
`App.run_test()` + `Pilot` — deterministic, no snapshot dependency (assertions are on the
app's tracked render state and queried widget content).
"""

import pytest

pytest.importorskip("textual")  # the cockpit lives behind the optional [textual] extra

from textual.widgets import Input

from avatar.event_types import (
    AgentEnd,
    AgentStart,
    DecisionError,
    ModelDecisionEvent,
    ModelUpdate,
    PhaseChanged,
    ToolEnd,
    ToolStart,
    VerificationEnd,
)
from avatar.tui.app import CockpitApp
from avatar.tui.replay import ReplaySession


async def _settle(app, pilot) -> None:
    """Let the event-consuming worker drain and the UI flush."""
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_cockpit_renders_event_stream():
    events = [
        AgentStart(goal="explain x"),
        ToolStart(tool="read_file", input={"path": "app.py"}),
        ToolEnd(tool="read_file", success=True, summary="read 1 line"),
        AgentEnd(outcome="success"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    joined = "\n".join(app.rendered)
    assert "▶ you  explain x" in joined  # observe mode: AgentStart is the only user representation
    assert "read_file" in joined  # the tool activity
    assert "success" in joined  # the terminal outcome


async def test_status_bar_reflects_phase_and_mode():
    events = [AgentStart(goal="g"), PhaseChanged(old="investigating", new="editing")]
    app = CockpitApp(ReplaySession(events), mode="edit")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.phase == "editing" and app.mode == "edit"
        status = app._status_text()
        assert "editing" in status and "edit" in status  # the bar reflects both


async def test_tool_activity_renders():
    events = [
        AgentStart(goal="g"),
        ToolStart(tool="run_tests", input={}),
        ToolEnd(tool="run_tests", success=False, summary="tests exit=1"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    joined = "\n".join(app.rendered)
    assert "run_tests" in joined and "tests exit=1" in joined


async def test_model_update_deltas_stream_into_transcript():
    events = [AgentStart(goal="g"), ModelUpdate(delta="hello "), ModelUpdate(delta="world")]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    joined = "".join(app.rendered)
    assert "hello" in joined and "world" in joined


async def test_input_submit_invokes_callback():
    seen: list[str] = []
    app = CockpitApp(ReplaySession([]), on_submit=seen.append)
    async with app.run_test() as pilot:
        inp = app.query_one("#prompt", Input)
        inp.focus()
        inp.value = "explain the loop"
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#prompt", Input).value == ""  # input cleared after submit
    assert seen == ["explain the loop"]  # the prompt reached the submit callback once


async def test_ask_user_question_renders():
    # Regression: a model that asks a question used to show only `■ blocked` — `_format`
    # had no `ModelDecisionEvent` case, so the question itself was invisible
    # (dogfood `temp/events/416f924…jsonl`). The question must reach the transcript.
    events = [
        AgentStart(goal="g"),
        ModelDecisionEvent(
            action_type="ask_user",
            action="The workspace is empty. Would you like me to create a new script?",
        ),
        AgentEnd(outcome="blocked"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    joined = "\n".join(app.rendered)
    assert "create a new script" in joined  # the question, not just the blocked status
    assert "blocked" in joined


async def test_final_answer_renders():
    events = [
        AgentStart(goal="g"),
        ModelDecisionEvent(action_type="final_answer", action="The loop runs until verified."),
        AgentEnd(outcome="success"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    joined = "\n".join(app.rendered)
    assert "The loop runs until verified." in joined  # the model's answer is rendered


async def test_thought_renders_when_present():
    # The thought is the public display-channel summary (ADR-0001 D6); it renders for any
    # decision, including a `tool_call` whose call line comes from ToolStart/ToolEnd.
    events = [
        AgentStart(goal="g"),
        ModelDecisionEvent(thought="checking the dir", action_type="tool_call"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    joined = "\n".join(app.rendered)
    assert "checking the dir" in joined


async def test_user_and_model_turns_carry_distinct_markers():
    events = [
        AgentStart(goal="explain x"),
        ModelDecisionEvent(action_type="final_answer", action="here is x"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    joined = "\n".join(app.rendered)
    user_line = next(line for line in joined.splitlines() if "explain x" in line)
    model_line = next(line for line in joined.splitlines() if "here is x" in line)
    assert user_line.startswith("▶")  # user turn
    assert model_line.startswith("●")  # model turn — a different leading marker
    assert user_line[0] != model_line[0]


async def test_failed_verification_verdict_is_visible():
    # The 3.2d contract: in conversational mode the verifier is advisory and outcome may be
    # "success" — so the cockpit MUST surface the real verdict, or the human (the terminal
    # authority) is deciding blind. Regression: the dogfood run where a failed
    # `diff_present` check rendered only as `■ success`.
    events = [
        AgentStart(goal="write the script"),
        VerificationEnd(passed=False, summary="verification failed: ['diff_present']"),
        AgentEnd(outcome="success"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        status = app._status_text()
    joined = "\n".join(app.rendered)
    assert "⚠" in joined and "diff_present" in joined  # the verdict is in the transcript
    assert "⚠" in status  # and flagged in the status bar, next to the outcome


async def test_passed_verification_verdict_is_visible():
    events = [
        AgentStart(goal="g"),
        VerificationEnd(passed=True, summary="all checks passed"),
        AgentEnd(outcome="success"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    joined = "\n".join(app.rendered)
    assert "✓ verification" in joined


async def test_decision_errors_render_in_transcript():
    # A malformed-decision retry (recovered or not) must be visible live — a looping
    # run should *look* like it is struggling, not idle.
    events = [
        AgentStart(goal="g"),
        DecisionError(error="not valid JSON: ...", recovered=True),
        DecisionError(error="no valid decision after retries", recovered=False),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    joined = "\n".join(app.rendered)
    assert "not valid JSON" in joined and "no valid decision" in joined


async def test_agent_end_marks_run_complete():
    events = [AgentStart(goal="g"), AgentEnd(outcome="success")]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.query_one("#prompt", Input).disabled is False  # re-enabled for the next goal
        assert "success" in app._status_text()
    assert app.outcome == "success"


async def test_status_resets_between_goals():
    """A new goal's AgentStart resets ALL per-goal display state — phase included.

    Dogfood `events/04849a5a…jsonl`: goal 2 (incomplete, never verified) displayed goal
    1's `phase: verifying · verify: ✓` — `_handle` reset outcome/verdict on AgentStart
    but never phase, so the bar mixed two goals' states.
    """
    events = [
        AgentStart(goal="g1"),
        PhaseChanged(old="investigating", new="verifying"),
        VerificationEnd(passed=True, summary="ok"),
        AgentEnd(outcome="success"),
        AgentStart(goal="g2"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.phase == "investigating"  # not goal 1's "verifying"
        assert app.verdict is None  # not goal 1's ✓
        assert app.outcome is None  # goal 2 is live
        assert "verify" not in app._status_text()
