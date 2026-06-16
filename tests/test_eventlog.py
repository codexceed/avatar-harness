import json
from datetime import datetime

from avatar.eventlog import EventLog
from avatar.events import Emitter


def test_emitter_is_fire_and_forget():
    emitter = Emitter()
    seen = []

    def faulty(_event):
        raise RuntimeError("subscriber blew up")

    emitter.subscribe(faulty)
    emitter.subscribe(seen.append)

    emitter.emit("agent_start", goal="x")  # must not raise

    assert len(seen) == 1
    assert seen[0]["type"] == "agent_start"
    assert seen[0]["goal"] == "x"


def test_emitter_stamps_ts_at_emit_time():
    # The timestamp is a property of the event itself (stamped once, at emission),
    # so every subscriber — console and log — sees the same wall-clock value.
    emitter = Emitter()
    seen = []
    emitter.subscribe(seen.append)
    emitter.emit("turn_start", iteration=1)
    ts = seen[0]["ts"]
    assert isinstance(ts, str)
    assert datetime.fromisoformat(ts)  # parses as an ISO-8601 timestamp


def test_emitter_stamps_session_id_on_every_event():
    # session_id is a property of the run, stamped once at emission like ts, so every
    # event — and thus every log line — is intentionally grouped by session.
    emitter = Emitter(session_id="sess-abc")
    seen = []
    emitter.subscribe(seen.append)
    emitter.emit("agent_start", goal="x")
    emitter.emit("agent_end", outcome="success")
    assert [e["session_id"] for e in seen] == ["sess-abc", "sess-abc"]
    # type, ts, session_id lead the event (grouping keys before payload).
    assert list(seen[0])[:3] == ["type", "ts", "session_id"]


def test_emitter_omits_session_id_when_unset():
    # A session-less emitter (tests, ad-hoc construction) carries no session_id key —
    # grouping stays intentional, never a synthetic default.
    emitter = Emitter()
    seen = []
    emitter.subscribe(seen.append)
    emitter.emit("turn_start", iteration=1)
    assert "session_id" not in seen[0]


def test_subscriber_cannot_alter_control():
    emitter = Emitter()
    calls = []
    emitter.subscribe(calls.append)
    # emit yields no value a caller could branch on — control stays out of the emitter.
    assert emitter.emit("turn_start") is None
    assert len(calls) == 1  # the subscriber ran, but could not redirect
    assert calls[0]["type"] == "turn_start"


def test_eventlog_writes_valid_jsonl(tmp_path):
    log_path = tmp_path / "nested" / "session.jsonl"
    emitter = Emitter()
    seen = []
    emitter.subscribe(seen.append)
    emitter.subscribe(EventLog(log_path))

    emitter.emit("agent_start", goal="fix bug")
    emitter.emit("tool_call", tool="search_repo")
    emitter.emit("agent_end", outcome="success")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["type"] for e in events] == ["agent_start", "tool_call", "agent_end"]
    assert events[0]["goal"] == "fix bug"
    # The log persists the EMITTED timestamp verbatim — it does not re-stamp at write time.
    assert events[0]["ts"] == seen[0]["ts"]
