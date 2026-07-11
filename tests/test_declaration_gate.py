"""The greenfield declaration gate (ADR-0038): a greenfield edit must declare a verification
contract before it may edit.

When a task is `edit`, greenfield (tiers 1-3 resolve nothing), and the model has not declared a
contract, the runner refuses each edit-intent call and nudges the model to `declare_verification`
— up to `max_declaration_nudges` times, then falls back to the smoke floor (ADR-0014) so a run is
never stranded. The gate lives at the `investigating→editing` boundary in `_arun_tool_call`; a
detected/configured contract, or a contract the model declares first, skips it entirely.

Tests are hermetic: a stub planner declines the smoke floor so nothing makes a live model call.
"""

from types import SimpleNamespace

from conftest import ScriptedModel

from avatar.config import HarnessConfig
from avatar.context import ContextBuilder, ContextPacket
from avatar.deps import CancellationToken, RunDeps
from avatar.event_types import DeclarationRequired
from avatar.events import Emitter
from avatar.model_client import FinalAnswer, ModelDecision, ToolCall, build_messages
from avatar.planner import VerificationPlanner
from avatar.runner import AgentRunner
from avatar.session import EventBus
from avatar.state import TaskState
from avatar.tools.base import ToolRegistry
from avatar.tools.edit import write_file
from avatar.tools.filesystem import read_file
from avatar.tools.verification import declare_verification
from avatar.verifier import Verifier
from avatar.workspace import Workspace

# A declared/config check that runs and exits 0 — non-vacuous and fast, so verification passes.
_PASS = 'python -c "import sys; sys.exit(0)"'


def _hermetic_planner(config: HarnessConfig) -> VerificationPlanner:
    """A planner (on the run's config, so the override tier still resolves) whose greenfield smoke
    floor declines — no live model call at verify time."""
    no_tool = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kw: no_tool)))
    return VerificationPlanner(config, client=client)


def _gate_runner(tmp_path, decisions, *, event_sink=None, **config_kw) -> AgentRunner:
    reg = ToolRegistry()
    for tool in (read_file, write_file, declare_verification):
        reg.register(tool)
    config = HarnessConfig(**config_kw)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    return AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=reg,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=Emitter(),
        config=config,
        event_sink=event_sink,
        planner=_hermetic_planner(config),
    )


def _declare(command: str) -> ModelDecision:
    call = ToolCall(name="declare_verification", input={"checks": [{"command": command}]})
    return ModelDecision(action=call)


def _write(path: str = "main.py") -> ModelDecision:
    return ModelDecision(action=ToolCall(name="write_file", input={"path": path, "content": "x = 1\n"}))


def test_greenfield_edit_refused_until_contract_declared(tmp_path):
    # First edit is refused (nudge 1); once the model declares, the same edit proceeds and the
    # declared contract is what freezes. A `DeclarationRequired` event is published on the bus.
    bus = EventBus(session_id="s")
    decisions = [_write(), _declare(_PASS), _write(), ModelDecision(action=FinalAnswer(answer="built it"))]
    runner = _gate_runner(tmp_path, decisions, event_sink=bus, max_declaration_nudges=3)

    state = runner.run(TaskState(goal="build a module", task_kind="edit"))

    assert (tmp_path / "main.py").exists()  # the edit eventually landed
    assert state.declaration_nudges == 1  # refused exactly once, then the model complied
    assert any(isinstance(e, DeclarationRequired) for e in bus.history)  # typed event, not just journal
    assert state.verification_plan and all(c.kind == "declared" for c in state.verification_plan)
    assert state.outcome == "success"  # the declared check ran and passed


def test_nudges_exhaust_then_edit_proceeds(tmp_path):
    # The model never declares: refused exactly `max_declaration_nudges` times, then the gate stops
    # refusing and the edit goes through (the smoke floor would cover it — ADR-0038 decline path).
    runner = _gate_runner(tmp_path, [_write()], max_declaration_nudges=2, max_iterations=6)

    state = runner.run(TaskState(goal="build a module", task_kind="edit"))

    assert state.declaration_nudges == 2  # refused up to the cap, no further
    assert (tmp_path / "main.py").exists()  # after exhaustion, editing was permitted
    assert state.phase != "investigating"  # advanced past the gate
    assert any(e.kind == "declare_fallback" for e in state.evidence)  # the fallback is observable


def test_detected_contract_skips_the_gate(tmp_path):
    # A configured (non-greenfield) contract means no forced declaration — the first edit proceeds.
    decisions = [_write(), ModelDecision(action=FinalAnswer(answer="done"))]
    runner = _gate_runner(tmp_path, decisions, test_command=_PASS)

    state = runner.run(TaskState(goal="build a module", task_kind="edit"))

    assert state.declaration_nudges == 0  # never gated — an explicit contract exists
    assert (tmp_path / "main.py").exists()
    assert state.outcome == "success"


def test_declare_first_then_edit_never_refused(tmp_path):
    # Declaring before the first edit sidesteps the gate entirely — zero nudges.
    decisions = [_declare(_PASS), _write(), ModelDecision(action=FinalAnswer(answer="done"))]
    runner = _gate_runner(tmp_path, decisions)

    state = runner.run(TaskState(goal="build a module", task_kind="edit"))

    assert state.declaration_nudges == 0
    assert (tmp_path / "main.py").exists()
    assert state.verification_plan and all(c.kind == "declared" for c in state.verification_plan)


def test_gate_disabled_when_nudges_zero(tmp_path):
    # `max_declaration_nudges == 0` disables the gate silently (the pre-ADR-0038 behavior), with no
    # spurious fallback feedback.
    decisions = [_write(), ModelDecision(action=FinalAnswer(answer="done"))]
    runner = _gate_runner(tmp_path, decisions, max_declaration_nudges=0)

    state = runner.run(TaskState(goal="build a module", task_kind="edit"))

    assert state.declaration_nudges == 0
    assert (tmp_path / "main.py").exists()  # edit proceeded immediately
    assert not any(e.kind == "declare_fallback" for e in state.evidence)  # silent, not "exhausted"


def test_edit_mission_prompts_declaration():
    # The prompt orients the model: the edit mission tells it to declare a contract for greenfield.
    packet = ContextPacket(goal="build a module", phase="investigating", task_kind="edit")
    system = build_messages(packet)[0]["content"]
    assert "declare_verification" in system


def test_claim_done_without_editing_is_forced_to_declare(tmp_path):
    # grok4: an edit task that reaches `final_answer` with NO edit and NO contract must be nudged to
    # declare — while still in `investigating`, where declare is reachable — instead of freezing an
    # empty plan, failing "no contract", and being forced through `alter_verification` in repair.
    bus = EventBus(session_id="s")
    decisions = [
        # claim done with the deliverable pasted inline — no file written, no contract declared
        ModelDecision(action=FinalAnswer(answer="# Design Spec\n(inline, nothing written)")),
        # after the nudge: declare a real contract, write the file, finalize
        _declare(_PASS),
        _write(),
        ModelDecision(action=FinalAnswer(answer="wrote main.py")),
    ]
    runner = _gate_runner(tmp_path, decisions, event_sink=bus, max_declaration_nudges=3)

    state = runner.run(TaskState(goal="provide a design spec in markdown", task_kind="edit"))

    # The first final_answer was refused with a declaration nudge (nothing edited before it, so the
    # nudge can only be the claim-done gate), NOT verified against an empty plan.
    assert any(isinstance(e, DeclarationRequired) for e in bus.history)
    assert state.declaration_nudges == 1  # refused once, then the model complied
    # It verified on the DECLARED contract — no empty-plan failure, no alter_verification thrash.
    assert state.outcome == "success"
    assert state.verification_plan and all(c.kind == "declared" for c in state.verification_plan)


def test_claim_done_gate_respects_nudge_budget(tmp_path):
    # A model that keeps claiming done without declaring is nudged up to the cap, then `final_answer`
    # proceeds to verification (empty plan → floor) — refused a bounded number of times, never forever.
    decisions = [ModelDecision(action=FinalAnswer(answer="done, trust me"))]  # ScriptedModel repeats it
    runner = _gate_runner(tmp_path, decisions, max_declaration_nudges=2, max_iterations=8)

    state = runner.run(TaskState(goal="build a module", task_kind="edit"))

    assert state.declaration_nudges == 2  # nudged up to the cap, then stopped refusing
    assert state.outcome is not None  # reached a terminal verdict — did not loop forever on the gate
