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

from avatar.event_types import (
    AgentEnd,
    AgentStart,
    DecisionError,
    DeclarationRequired,
    ModelDecisionEvent,
    ModelUpdate,
    PhaseChanged,
    TaskEscalated,
    ToolEnd,
    ToolStart,
    VerificationEnd,
)
from jo.app import CockpitApp, HistoryInput
from jo.replay import ReplaySession


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


async def test_status_bar_follows_mid_run_escalation():
    # A consented `switch_to_editing` (ADR-0048) flips the kind mid-run. The bar's shown
    # classification must follow it — dogfood `tetris_grok4` escalated investigate→edit but
    # the bar kept reading "investigate" while the run edited.
    events = [
        AgentStart(goal="implement it"),
        TaskEscalated(from_kind="investigate", to_kind="edit", trigger="model"),
        PhaseChanged(old="investigating", new="editing"),
    ]
    app = CockpitApp(ReplaySession(events), mode="investigate")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.mode == "edit"  # the escalation updated the tracked kind, not just the phase
        assert "edit" in app._status_text()  # and the bar shows the new classification


async def test_status_bar_sits_in_the_footer_by_the_input():
    # The mode·phase indicator must be by the text box, not stranded at the screen top —
    # so the live task kind and phase are in view where the human is typing.
    app = CockpitApp(ReplaySession([AgentStart(goal="g")]))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        footer = app.query_one("#footer")
        assert app.query_one("#status") in footer.walk_children()  # co-located with the input


async def test_declaration_required_renders_as_transcript_line():
    # The greenfield declaration gate (ADR-0038) surfaces as an informational transcript line —
    # no modal (the model complies, not the human): observe-only.
    events = [
        AgentStart(goal="build tetris"),
        DeclarationRequired(nudge=1, max_nudges=3),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    joined = "\n".join(app.rendered)
    assert "declare a verification contract before editing" in joined


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
        inp = app.query_one("#prompt", HistoryInput)
        inp.focus()
        inp.text = "explain the loop"
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#prompt", HistoryInput).text == ""  # input cleared after submit
    assert seen == ["explain the loop"]  # the prompt reached the submit callback once


async def test_up_arrow_recalls_prompt_history():
    # ↑ walks back through submitted prompts (newest first), ↓ walks forward, and stepping
    # past the newest restores the half-typed draft. History accrues across submits.
    seen: list[str] = []
    app = CockpitApp(ReplaySession([]), on_submit=seen.append)
    async with app.run_test() as pilot:
        inp = app.query_one("#prompt", HistoryInput)
        inp.focus()
        for line in ("first goal", "second goal"):
            inp.text = line
            await pilot.press("enter")
            await pilot.pause()
        inp.text = "draft"  # a fresh, unsubmitted line
        await pilot.press("up")
        assert inp.text == "second goal"  # newest first
        await pilot.press("up")
        assert inp.text == "first goal"  # then older
        await pilot.press("up")
        assert inp.text == "first goal"  # clamped at the oldest entry
        await pilot.press("down")
        assert inp.text == "second goal"
        await pilot.press("down")
        assert inp.text == "draft"  # past the newest → the stashed draft returns
    assert seen == ["first goal", "second goal"]


async def test_history_skips_consecutive_duplicates():
    # Resubmitting the same line shouldn't stack identical history entries.
    app = CockpitApp(ReplaySession([]))
    async with app.run_test() as pilot:
        inp = app.query_one("#prompt", HistoryInput)
        inp.focus()
        for _ in range(3):
            inp.text = "same goal"
            await pilot.press("enter")
            await pilot.pause()
        await pilot.press("up")
        assert inp.text == "same goal"
        await pilot.press("up")
        assert inp.text == "same goal"  # only one entry — no duplicate stack to walk
    assert inp._history == ["same goal"]


@pytest.mark.parametrize("newline_key", ["ctrl+j", "shift+enter", "alt+enter"])
async def test_newline_key_inserts_newline_and_enter_submits_multiline(newline_key):
    # Enter submits; each of Ctrl+J / Shift+Enter / Alt+Enter inserts a newline (they share one
    # handler branch). A goal composed across two lines reaches the callback intact (with its
    # embedded newline), and the field clears afterward.
    seen: list[str] = []
    app = CockpitApp(ReplaySession([]), on_submit=seen.append)
    async with app.run_test() as pilot:
        inp = app.query_one("#prompt", HistoryInput)
        inp.focus()
        await pilot.press("a")
        await pilot.press(newline_key)  # newline, not submit
        await pilot.press("b")
        await pilot.pause()
        assert inp.text == "a\nb"  # the buffer spans two lines
        await pilot.press("enter")  # now submit the whole block
        await pilot.pause()
        assert inp.text == ""  # cleared after submit
    assert seen == ["a\nb"]  # the multi-line goal reached the callback intact


async def test_arrows_move_cursor_midbuffer_and_recall_only_at_edges():
    # In a multi-line draft, ↑ moves the cursor between lines; only when the cursor is already
    # on the first line does ↑ recall history instead (so multi-line editing isn't hijacked).
    app = CockpitApp(ReplaySession([]), on_submit=lambda _t: None)
    async with app.run_test() as pilot:
        inp = app.query_one("#prompt", HistoryInput)
        inp.focus()
        inp.text = "old goal"  # seed one history entry
        await pilot.press("enter")
        await pilot.pause()
        inp.text = "l1\nl2"  # a fresh two-line draft
        inp.move_cursor(inp.document.end)  # cursor on the last line
        await pilot.press("up")  # not the first line → move the cursor, don't recall
        assert inp.text == "l1\nl2"  # draft untouched
        assert inp.cursor_at_first_line  # cursor climbed to line 1
        await pilot.press("up")  # now on the first line → recall the older prompt
        assert inp.text == "old goal"


async def test_down_arrow_moves_cursor_midbuffer_and_recalls_only_at_last_line():
    # Symmetric to the ↑ test: in a multi-line draft, ↓ moves the cursor between lines; only when
    # the cursor is already on the last line does ↓ step forward through history.
    app = CockpitApp(ReplaySession([]), on_submit=lambda _t: None)
    async with app.run_test() as pilot:
        inp = app.query_one("#prompt", HistoryInput)
        inp.focus()
        for line in ("first goal", "second goal"):  # seed two history entries
            inp.text = line
            await pilot.press("enter")
            await pilot.pause()
        await pilot.press("up")  # browse back into history…
        await pilot.press("up")  # …to the oldest entry
        assert inp.text == "first goal"
        inp.text = "l1\nl2"  # compose a fresh two-line draft while browsing
        inp.move_cursor(inp.document.start)  # cursor on the first line
        await pilot.press("down")  # not the last line → move the cursor, don't advance history
        assert inp.text == "l1\nl2"  # draft untouched
        assert inp.cursor_at_last_line  # cursor descended to line 2
        await pilot.press("down")  # now on the last line → step forward to the newer prompt
        assert inp.text == "second goal"


async def test_whitespace_only_submit_is_a_noop():
    # A buffer that strips to empty (blank lines / spaces) must not launch a goal.
    seen: list[str] = []
    app = CockpitApp(ReplaySession([]), on_submit=seen.append)
    async with app.run_test() as pilot:
        inp = app.query_one("#prompt", HistoryInput)
        inp.focus()
        inp.text = "  \n  "
        await pilot.press("enter")
        await pilot.pause()
        assert inp.text == ""  # field still clears
    assert seen == []  # ...but nothing was submitted


async def test_ctrl_c_copies_active_selection_instead_of_quitting():
    # ctrl+c is bound (priority) to the app's cancel/quit, which shadows Textual's own
    # copy-selection action. With text selected, ctrl+c must copy it rather than quit.
    app = CockpitApp(ReplaySession([]))
    async with app.run_test() as pilot:
        copied: list[str] = []
        exited: list[bool] = []
        app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
        app.exit = lambda *a, **k: exited.append(True)  # type: ignore[method-assign,assignment]
        app.screen.get_selected_text = lambda: "selected text"  # type: ignore[method-assign]
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert copied == ["selected text"]  # the selection was copied...
        assert not exited  # ...and the app did not quit


async def test_ctrl_c_quits_when_nothing_selected_and_idle():
    # No selection, no live run (observe mode) → ctrl+c quits, as before.
    app = CockpitApp(ReplaySession([]))
    async with app.run_test() as pilot:
        exited: list[bool] = []
        app.exit = lambda *a, **k: exited.append(True)  # type: ignore[method-assign,assignment]
        app.screen.get_selected_text = lambda: None  # type: ignore[method-assign]
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert exited  # fell through to quit


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
        assert app.query_one("#prompt", HistoryInput).disabled is False  # re-enabled for the next goal
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
