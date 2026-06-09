"""Phase 3.1 Lane 1 — bounded per-subscriber fan-out + drop policy (ADR-0001).

The foundation `EventBus` fans out on unbounded queues; Lane 1 makes each subscriber
queue **bounded with a soft cap** that sheds only droppable `*_update` events under
pressure — lifecycle/control events are *never* dropped (they may exceed the cap). A
slow or broken subscriber can never stall publishing or its peers, and drops are
visible as `event_id` gaps, never silent reordering. The privileged journal (tested in
`test_journal.py`) stays lossless regardless of what any subscriber sheds.
"""

import asyncio

from avatar_harness.bus import EventBus
from avatar_harness.event_types import (
    AgentStart,
    ModelUpdate,
    PhaseChanged,
    load_events,
)
from avatar_harness.journal import JsonlEventJournal


def _drain(queue: asyncio.Queue) -> list:
    """Pull every buffered event from a subscriber queue (skipping the close sentinel)."""
    out: list = []
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item is not None:
            out.append(item)
    return out


def test_bounded_subscriber_sheds_updates_under_pressure():
    # An undrained subscriber with a small cap keeps the earliest `max_queue` updates
    # and sheds the rest (drop-newest) — the queue can never grow without bound.
    bus = EventBus("s1")
    queue = bus.subscribe(max_queue=3)
    for i in range(10):
        bus.publish_nowait(ModelUpdate(delta=f"d{i}"))
    received = _drain(queue)
    assert [e.delta for e in received] == ["d0", "d1", "d2"]  # kept first 3, shed 7


def test_lifecycle_events_never_dropped_even_when_full():
    # At the cap, a further update is shed — but a lifecycle/control event is always
    # enqueued, even over the cap (that is what "never drop lifecycle" means).
    bus = EventBus("s1")
    queue = bus.subscribe(max_queue=2)
    bus.publish_nowait(ModelUpdate(delta="a"))
    bus.publish_nowait(ModelUpdate(delta="b"))
    assert queue.qsize() == 2  # at the cap
    bus.publish_nowait(ModelUpdate(delta="c"))  # shed
    bus.publish_nowait(PhaseChanged(old="investigating", new="editing"))  # never shed
    types = [e.type for e in _drain(queue)]
    assert types.count("model_update") == 2  # the third update was dropped
    assert "phase_changed" in types  # the lifecycle event went through, over the cap


def test_dropped_updates_show_as_event_id_gaps():
    # Drops are visible as a non-contiguous event_id sequence at the subscriber; order
    # is preserved. (Global ids are contiguous — see the monotonic test.)
    bus = EventBus("s1")
    queue = bus.subscribe(max_queue=2)
    bus.publish_nowait(ModelUpdate(delta="0"))  # id 1, kept
    bus.publish_nowait(ModelUpdate(delta="1"))  # id 2, kept → full
    bus.publish_nowait(ModelUpdate(delta="2"))  # id 3, shed
    bus.publish_nowait(ModelUpdate(delta="3"))  # id 4, shed
    bus.publish_nowait(PhaseChanged(old="a", new="b"))  # id 5, lifecycle kept
    assert [e.event_id for e in _drain(queue)] == [1, 2, 5]  # gap at 3,4 is visible


def test_slow_subscriber_never_blocks_publish_or_peers():
    # A never-drained subscriber cannot block publishing or starve a healthy peer.
    bus = EventBus("s1")
    slow = bus.subscribe(max_queue=1)
    fast = bus.subscribe(max_queue=100)
    for i in range(5):
        bus.publish_nowait(ModelUpdate(delta=str(i)))  # must not raise or block
    assert [e.event_id for e in _drain(fast)] == [1, 2, 3, 4, 5]  # peer saw everything
    assert slow.qsize() <= 1  # the slow one shed without affecting anyone


def test_global_event_id_monotonic_across_drops():
    # The bus assigns a contiguous global order regardless of any subscriber dropping.
    bus = EventBus("s1")
    bus.subscribe(max_queue=1)  # will shed most updates
    ids = [bus.publish_nowait(ModelUpdate(delta=str(i))).event_id for i in range(6)]
    assert ids == [1, 2, 3, 4, 5, 6]
    assert bus.history[-1].event_id == 6  # in-memory lossless record stays contiguous


def test_journal_lossless_while_subscriber_drops(tmp_path):
    # The headline contract: a bounded subscriber sheds updates, but the privileged
    # journal records EVERY event in order — the lossless write-ahead substrate.
    journal = JsonlEventJournal(tmp_path / "j.jsonl")
    bus = EventBus("s1", journal=journal)
    queue = bus.subscribe(max_queue=2)
    for i in range(5):
        bus.publish_nowait(ModelUpdate(delta=str(i)))
    assert len(_drain(queue)) < 5  # the subscriber lost some
    journal.close()
    reloaded = load_events(journal.path)
    assert [e.event_id for e in reloaded] == [1, 2, 3, 4, 5]  # journal lost none
    assert all(e.type == "model_update" for e in reloaded)


def test_bus_without_journal_behaves_like_foundation():
    # The journal is optional; a journal-less bus fans out exactly as the 3.0 foundation,
    # and the generous default cap never drops a normal-size stream.
    bus = EventBus("s1")  # no journal
    queue = bus.subscribe()  # default cap
    first = bus.publish_nowait(AgentStart(goal="g"))
    assert first.event_id == 1 and first.session_id == "s1"
    for i in range(50):
        bus.publish_nowait(ModelUpdate(delta=str(i)))
    received = _drain(queue)
    assert len(received) == 51  # agent_start + 50 updates, nothing shed at the default cap
