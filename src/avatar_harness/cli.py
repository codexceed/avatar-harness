"""CLI entry point.

`run_echo` is the Phase 0 skeleton (kept for the event-spine tests); `main()`
now drives the real Phase 1 read-only loop via `run_agent`. The CLI stays a thin
shell over the loop — wiring components and event subscribers, nothing more.
"""

import argparse
from pathlib import Path

from pydantic import BaseModel

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.eventlog import EventLog
from avatar_harness.events import Emitter, Event
from avatar_harness.model_client import ModelClient, OpenAIModelClient
from avatar_harness.runner import AgentRunner
from avatar_harness.state import TaskState
from avatar_harness.tools import default_registry
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace

# Truncation width for event values rendered to the terminal.
_EVENT_VALUE_WIDTH = 160


class EchoResult(BaseModel):
    """Result of the Phase 0 echo skeleton: the task id, echoed answer, and outcome."""

    task_id: str
    answer: str
    outcome: str


def run_echo(task: str, *, emitter: Emitter, config: HarnessConfig | None = None) -> EchoResult:
    """Phase 0 skeleton: echo the task, bracketed by lifecycle events."""
    config = config or HarnessConfig()
    state = TaskState(goal=task)
    emitter.emit("agent_start", goal=task, task_id=state.task_id)
    emitter.emit("turn_start", task_id=state.task_id)
    state.final_answer = task
    state.outcome = "success"
    emitter.emit("turn_end", task_id=state.task_id)
    emitter.emit("agent_end", outcome=state.outcome, task_id=state.task_id)
    return EchoResult(task_id=state.task_id, answer=state.final_answer, outcome=state.outcome)


def run_agent(
    task: str,
    *,
    config: HarnessConfig,
    emitter: Emitter,
    model_client: ModelClient | None = None,
) -> TaskState:
    """Run the read-only investigate loop over `task` (Phase 1)."""
    deps = RunDeps(
        workspace=Workspace(config.workspace_root),
        config=config,
        cancellation=CancellationToken(),
    )
    runner = AgentRunner(
        model_client=model_client or OpenAIModelClient(config),
        registry=default_registry(),
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=emitter,
        config=config,
    )
    return runner.run(TaskState(goal=task, task_kind="investigate"))


def _print_event(event: Event) -> None:
    parts = []
    for key, value in event.items():
        if key == "type":
            continue
        text = str(value).replace("\n", " ")
        if len(text) > _EVENT_VALUE_WIDTH:
            text = text[:_EVENT_VALUE_WIDTH] + "…"
        parts.append(f"{key}={text}")
    print(f"[{event['type']}] " + ", ".join(parts))


def _render_result(state: TaskState) -> None:
    print(f"\nStatus: {state.outcome}")
    if state.final_answer:
        print(f"\n{state.final_answer}")
    if state.files_read:
        print("\nInspected: " + ", ".join(sorted(state.files_read)))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse args, wire the loop, run the task, render the result."""
    parser = argparse.ArgumentParser(
        prog="avatar-harness",
        description="A bounded, verifiable coding-agent harness.",
    )
    parser.add_argument("task", help="The natural-language task to run.")
    parser.add_argument("--log", default="events/session.jsonl", help="Path to the JSONL event log.")
    args = parser.parse_args(argv)

    config = HarnessConfig()
    emitter = Emitter()
    emitter.subscribe(EventLog(Path(args.log)))
    emitter.subscribe(_print_event)

    state = run_agent(args.task, config=config, emitter=emitter)
    _render_result(state)
    return 0 if state.outcome == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
