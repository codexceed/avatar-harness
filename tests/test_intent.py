"""Mode routing — LLM classification, visible + correctable (revises ADR-0002 D3).

Dogfood `events/04849a5a…jsonl`: the first-word heuristic routed "Now make the UI
richer…" to `investigate` — editing structurally impossible — and the run burned 50
turns on reads. ADR-0002 D3's objection was *hiddenness*, not LLM-ness: routing now
uses a one-shot, schema-constrained classification (`AVATAR_CLASSIFIER_MODEL`) whose
verdict is displayed and overridable, with the hardened heuristic as the fallback.
"""

import json
from types import SimpleNamespace

from conftest import ScriptedModel

from avatar.config import HarnessConfig
from avatar.harness import Harness
from avatar.intent import ModeClassifier
from avatar.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar.session_state import ReplSession, default_mode
from avatar.tools.base import ToolRegistry
from avatar.tools.filesystem import read_file


def _classifier_transport(kind: str | None, captured: list[dict] | None = None, boom: bool = False):
    """An OpenAI-shaped transport whose reply is one `set_task_mode` call (or junk/raise)."""

    def create(**kwargs):
        if boom:
            raise RuntimeError("classifier endpoint down")
        if captured is not None:
            captured.append(kwargs)
        if kind is None:
            message = SimpleNamespace(content="no idea", tool_calls=None)
        else:
            call = SimpleNamespace(
                id="c1",
                type="function",
                function=SimpleNamespace(name="set_task_mode", arguments=json.dumps({"kind": kind})),
            )
            message = SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _repl(tmp_path, decisions=(), *, classifier=None, **cfg) -> ReplSession:
    config = HarnessConfig(workspace_root=str(tmp_path), **cfg)
    reg = ToolRegistry()
    reg.register(read_file)
    harness = Harness(config=config, model=ScriptedModel(list(decisions)), tools=reg)
    return ReplSession(harness, classifier=classifier)


# --- hardened heuristic (the fallback path) ------------------------------------------------


def test_heuristic_strips_leading_filler():
    """Leading conversational filler must not hide the edit verb (the dogfood misroute)."""
    assert default_mode("Now make it shiny") == "edit"
    assert default_mode("please add tests for the loop") == "edit"
    assert default_mode("Can you create a script?") == "edit"


def test_question_words_route_investigate():
    """Question-shaped prompts affirmatively route investigate, filler or not."""
    assert default_mode("now explain the loop") == "investigate"
    assert default_mode("how does add work?") == "investigate"
    assert default_mode("why is it slow") == "investigate"


# --- the classifier -------------------------------------------------------------------------


def test_classifier_routes_followup_with_history():
    """The classifier sees the conversation, so a follow-up classifies from context."""
    captured: list[dict] = []
    clf = ModeClassifier(
        HarnessConfig(classifier_model="tiny"), client=_classifier_transport("edit", captured)
    )
    kind = clf.classify(
        "Now make the UI richer with colors.",
        history=["user: Write a python script for a chatbot", "agent: created scripts/chatbot.py"],
    )
    assert kind == "edit"
    assert captured[0]["model"] == "tiny"  # the cheap classifier model, not the main runner
    blob = " ".join(str(m.get("content")) for m in captured[0]["messages"])
    assert "scripts/chatbot.py" in blob  # the conversation reached the classifier


def test_classifier_junk_reply_returns_none():
    """An unusable reply yields None — the caller falls back, never crashes."""
    clf = ModeClassifier(HarnessConfig(classifier_model="tiny"), client=_classifier_transport(None))
    assert clf.classify("anything") is None
    clf = ModeClassifier(HarnessConfig(classifier_model="tiny"), client=_classifier_transport("bogus_kind"))
    assert clf.classify("anything") is None


# --- ReplSession routing precedence ---------------------------------------------------------


def test_repl_routes_via_classifier_and_memoizes(tmp_path):
    """Override → classifier → heuristic; the verdict is memoized per prompt.

    `start()` re-resolves internally, so without the memo a goal would pay (and could
    flip) a second classification.
    """
    captured: list[dict] = []
    clf = ModeClassifier(
        HarnessConfig(classifier_model="tiny"), client=_classifier_transport("edit", captured)
    )
    repl = _repl(tmp_path, classifier=clf)
    assert repl.resolve_mode("Now enrich the UI") == "edit"  # classifier verdict, not heuristic's investigate
    assert repl.last_mode_source == "classifier"
    repl.resolve_mode("Now enrich the UI")
    assert len(captured) == 1  # memoized — one network call per prompt


def test_explicit_mode_overrides_classifier(tmp_path):
    """`/mode` always wins; the classifier isn't even consulted."""
    captured: list[dict] = []
    clf = ModeClassifier(
        HarnessConfig(classifier_model="tiny"), client=_classifier_transport("edit", captured)
    )
    repl = _repl(tmp_path, classifier=clf)
    repl.set_mode("investigate")
    assert repl.resolve_mode("add a retry to the client") == "investigate"
    assert repl.last_mode_source == "override"
    assert captured == []


def test_classifier_failure_falls_back_to_heuristic(tmp_path):
    """A dead classifier endpoint degrades to the heuristic — never blocks a goal."""
    clf = ModeClassifier(
        HarnessConfig(classifier_model="tiny"), client=_classifier_transport(None, boom=True)
    )
    repl = _repl(tmp_path, classifier=clf)
    assert repl.resolve_mode("Now make it colorful") == "edit"  # hardened heuristic catches it
    assert repl.last_mode_source == "heuristic"


def test_repl_without_classifier_model_runs_heuristic_only(tmp_path):
    """`AVATAR_CLASSIFIER_MODEL` unset/empty → no classifier construction, no surprise calls."""
    repl = _repl(tmp_path, classifier_model=None)
    assert repl.classifier is None
    assert repl.resolve_mode("explain the loop") == "investigate"
    assert repl.last_mode_source == "heuristic"


async def test_classified_goal_runs_end_to_end(tmp_path):
    """The classified kind actually drives the task (not just the display)."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ]
    clf = ModeClassifier(HarnessConfig(classifier_model="tiny"), client=_classifier_transport("investigate"))
    repl = _repl(tmp_path, decisions, classifier=clf)
    state = await repl.submit("Now walk me through app.py")
    assert state.task_kind == "investigate"
    assert state.outcome == "success"


def test_meta_state_never_classifies(tmp_path):
    """`/state` is strictly local — no classifier call, no token spend (§23.2)."""
    captured: list[dict] = []
    clf = ModeClassifier(
        HarnessConfig(classifier_model="tiny"), client=_classifier_transport("edit", captured)
    )
    repl = _repl(tmp_path, classifier=clf)
    result = repl.run_meta("/state")
    assert "mode: auto" in result.text
    assert captured == []  # the meta command never reached the model
    repl.set_mode("edit")
    assert "mode: edit" in repl.run_meta("/state").text  # an override displays verbatim


async def test_route_memo_cleared_per_goal(tmp_path):
    """The same prompt text on a LATER turn re-classifies in the new conversation.

    The memo exists only so `start()` doesn't re-pay the call within one goal;
    carrying it across goals would freeze "continue"-style follow-ups to a stale
    verdict (PR-#32 review).
    """
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ] * 2
    captured: list[dict] = []
    clf = ModeClassifier(
        HarnessConfig(classifier_model="tiny"), client=_classifier_transport("investigate", captured)
    )
    repl = _repl(tmp_path, decisions, classifier=clf)
    await repl.submit("walk me through app.py")
    await repl.submit("walk me through app.py")  # same text, new turn
    assert len(captured) == 2  # re-classified with the grown conversation
