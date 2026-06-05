import json

from avatar_harness.eventlog import EventLog
from avatar_harness.events import Emitter


def test_emitter_is_fire_and_forget():
    emitter = Emitter()
    seen = []

    def faulty(_event):
        raise RuntimeError("subscriber blew up")

    emitter.subscribe(faulty)
    emitter.subscribe(seen.append)

    emitter.emit("agent_start", goal="x")  # must not raise

    assert seen == [{"type": "agent_start", "goal": "x"}]


def test_subscriber_cannot_alter_control():
    emitter = Emitter()
    calls = []
    emitter.subscribe(calls.append)
    # emit yields no value a caller could branch on — control stays out of the emitter.
    assert emitter.emit("turn_start") is None
    assert calls == [{"type": "turn_start"}]  # the subscriber ran, but could not redirect


def test_eventlog_writes_valid_jsonl(tmp_path):
    log_path = tmp_path / "nested" / "session.jsonl"
    emitter = Emitter()
    emitter.subscribe(EventLog(log_path))

    emitter.emit("agent_start", goal="fix bug")
    emitter.emit("tool_call", tool="search_repo")
    emitter.emit("agent_end", outcome="success")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["type"] for e in events] == ["agent_start", "tool_call", "agent_end"]
    assert events[0]["goal"] == "fix bug"
