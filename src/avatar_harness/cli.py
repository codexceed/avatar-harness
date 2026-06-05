"""CLI entry point and the Phase 0 echo skeleton.

Phase 0 has no model call and no tools: it wires config + state + the event
spine and proves the loop's ``agent_start … agent_end`` bracket. Later phases
replace ``run_echo`` with the real ``AgentRunner`` (§5) without changing this
shape — the CLI stays a thin shell over the loop.
"""

import argparse
from pathlib import Path

from pydantic import BaseModel

from avatar_harness.config import HarnessConfig
from avatar_harness.eventlog import EventLog
from avatar_harness.events import Emitter, Event
from avatar_harness.state import TaskState


class EchoResult(BaseModel):
    task_id: str
    answer: str
    outcome: str


def run_echo(task: str, *, emitter: Emitter, config: HarnessConfig | None = None) -> EchoResult:
    """Phase 0 skeleton: echo the task back, bracketed by lifecycle events."""
    config = config or HarnessConfig()
    state = TaskState(goal=task)

    emitter.emit("agent_start", goal=task, task_id=state.task_id)
    emitter.emit("turn_start", task_id=state.task_id)

    # No model, no tools yet — the "loop" is a single echo turn.
    state.final_answer = task
    state.outcome = "success"

    emitter.emit("turn_end", task_id=state.task_id)
    emitter.emit("agent_end", outcome=state.outcome, task_id=state.task_id)

    return EchoResult(task_id=state.task_id, answer=state.final_answer, outcome=state.outcome)


def _print_event(event: Event) -> None:
    rest = ", ".join(f"{k}={v}" for k, v in event.items() if k != "type")
    print(f"[{event['type']}] {rest}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="avatar-harness",
        description="A bounded, verifiable coding-agent harness.",
    )
    parser.add_argument("task", help="The natural-language task to run.")
    parser.add_argument("--log", default="events/session.jsonl", help="Path to the JSONL event log.")
    args = parser.parse_args(argv)

    emitter = Emitter()
    emitter.subscribe(EventLog(Path(args.log)))
    emitter.subscribe(_print_event)

    result = run_echo(args.task, emitter=emitter, config=HarnessConfig())
    print(f"\n{result.answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
