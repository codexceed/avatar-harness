from avatar_harness.cli import run_echo
from avatar_harness.events import Emitter


def test_run_emits_start_and_end():
    emitter = Emitter()
    events = []
    emitter.subscribe(events.append)

    run_echo("hello", emitter=emitter)

    types = [e["type"] for e in events]
    assert types[0] == "agent_start"
    assert types[-1] == "agent_end"


def test_echo_roundtrip():
    emitter = Emitter()
    result = run_echo("do the thing", emitter=emitter)
    assert result.answer == "do the thing"
    assert result.outcome == "success"
