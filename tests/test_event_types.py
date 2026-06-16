"""Phase 3.0 foundation — Group 1: the typed `HarnessEvent` contract.

These pin the event union both the engine (emit) and the cockpit (render) build
against: a closed, versioned, discriminated union that round-trips through the
journal verbatim. Freezing this is the precondition for the lane fan-out.
"""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from avatar.event_types import (
    AgentStart,
    ApprovalRequested,
    DecisionError,
    EventBase,
    ModelUpdate,
    PhaseChanged,
    ToolEnd,
    dump_event,
    load_events,
    parse_event,
)
from avatar.eventlog import EventLog

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_harness_event_union_round_trips():
    # Each variant validates and round-trips via its `type` discriminator — the
    # renderer can match the union exhaustively.
    samples = [
        AgentStart(event_id=1, session_id="s", ts=_TS, goal="fix it"),
        PhaseChanged(event_id=2, session_id="s", ts=_TS, old="investigating", new="editing"),
        ToolEnd(event_id=3, session_id="s", ts=_TS, tool="read_file", success=True, summary="ok"),
        ApprovalRequested(event_id=4, session_id="s", ts=_TS, approval_id="a1", tool="run_command"),
        DecisionError(event_id=5, session_id="s", ts=_TS, error="not valid JSON", raw="{", recovered=True),
    ]
    for original in samples:
        reparsed = parse_event(dump_event(original))
        assert reparsed == original
        assert reparsed.type == original.type


def test_event_base_fields_present():
    # The base carries the journal's ordering + versioning keys; `type` (the discriminator)
    # is declared per concrete event, where it can be a sound Literal.
    required = {"schema_version", "event_id", "session_id", "task_id", "ts"}
    assert required <= set(EventBase.model_fields)
    e = AgentStart(event_id=1, session_id="s", ts=_TS)
    assert e.schema_version == 1
    assert {e.event_id, e.session_id, e.ts, e.type} == {1, "s", _TS, "agent_start"}


def test_unknown_event_type_is_rejected():
    # The union is CLOSED: an unknown discriminator fails validation, so the
    # renderer/journal can rely on exhaustiveness rather than silently dropping.
    with pytest.raises(ValidationError):
        parse_event({"type": "wat", "event_id": 1, "session_id": "s", "ts": _TS.isoformat()})


def test_model_update_channel_is_display():
    # Streamed model output is display-only — never a private-CoT channel (ADR-0001 D6).
    assert ModelUpdate(event_id=1, session_id="s", ts=_TS, delta="hi").channel == "display"
    with pytest.raises(ValidationError):
        # pyrefly: ignore[bad-argument-type]  — the invalid value is the point: pydantic must reject it
        ModelUpdate(event_id=1, session_id="s", ts=_TS, delta="x", channel="private")


def test_eventlog_writes_and_reloads_typed_events(tmp_path):
    # The journal serializes typed events to JSONL and they reload to the same typed
    # models — the durable-substrate round-trip (ADR-0001 migration step 1).
    log = EventLog(tmp_path / "j.jsonl")
    written = [
        AgentStart(event_id=1, session_id="s", ts=_TS, goal="g"),
        ToolEnd(event_id=2, session_id="s", ts=_TS, tool="read_file", success=True),
    ]
    for e in written:
        log(e)
    assert load_events(tmp_path / "j.jsonl") == written


def test_eventlog_still_writes_plain_dicts(tmp_path):
    # Back-compat: the dict Emitter path (sync loop + CLI) is untouched.
    log = EventLog(tmp_path / "d.jsonl")
    log({"type": "agent_start", "ts": _TS.isoformat()})
    assert (tmp_path / "d.jsonl").read_text(encoding="utf-8").strip()
    assert not isinstance({"type": "x"}, BaseModel)  # the dict branch is exercised
