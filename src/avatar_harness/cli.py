"""CLI entry point.

`run_echo` is the Phase 0 skeleton (kept for the event-spine tests); `main()`
now drives the real Phase 1 read-only loop via `run_agent`. The CLI stays a thin
shell over the loop — wiring components and event subscribers, nothing more.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from avatar_harness.artifact import ArtifactManager
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
from avatar_harness.workspace import DirtyWorkspaceError, Workspace

# Truncation width for event values rendered to the terminal.
_EVENT_VALUE_WIDTH = 160


class EchoResult(BaseModel):
    """Result of the Phase 0 echo skeleton: the task id, echoed answer, and outcome."""

    task_id: str
    answer: str
    outcome: str


def run_echo(task: str, *, emitter: Emitter, config: HarnessConfig | None = None) -> EchoResult:
    """Phase 0 skeleton: echo the task, bracketed by lifecycle events.

    Args:
        task: The natural-language task to echo back.
        emitter: Sink for the lifecycle events.
        config: Harness config; a default `HarnessConfig` if omitted.

    Returns:
        The task id, echoed answer, and `success` outcome.
    """
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
    allow_dirty: bool = False,
    task_kind: Literal["edit", "investigate", "test_only"] = "investigate",
) -> TaskState:
    """Run the agent loop over `task`.

    Args:
        task: The natural-language task to run.
        config: Harness config wiring the loop.
        emitter: Sink for observation events.
        model_client: Model client; a default `OpenAIModelClient` if omitted.
        allow_dirty: When `True`, open the workspace despite uncommitted tracked changes (§15).
        task_kind: The verification contract to apply (`investigate` / `edit` / `test_only`).

    Returns:
        The terminal `TaskState` after the loop settles.
    """
    deps = RunDeps(
        workspace=Workspace(config.workspace_root, allow_dirty=allow_dirty),
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
    return runner.run(TaskState(goal=task, task_kind=task_kind))


def _print_event(event: Event) -> None:
    parts = []
    for key, value in event.items():
        if key in ("type", "ts"):  # rendered as the line prefix, not inline
            continue
        text = str(value).replace("\n", " ")
        if len(text) > _EVENT_VALUE_WIDTH:
            text = text[:_EVENT_VALUE_WIDTH] + "…"
        parts.append(f"{key}={text}")
    print(f"{_clock(event.get('ts'))}[{event['type']}] " + ", ".join(parts))


def _clock(ts: object) -> str:
    """Render an ISO timestamp as a compact `HH:MM:SS ` prefix; empty if unparseable.

    Args:
        ts: The candidate ISO timestamp.

    Returns:
        The `HH:MM:SS ` prefix, or `""` if `ts` is not a parseable string.
    """
    if not isinstance(ts, str):
        return ""
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S ")
    except ValueError:
        return ""


def _report(state: TaskState, config: HarnessConfig) -> str:
    """Render the terminal artifact (§14) — the single reporting contract.

    Re-opens the workspace read-only (the agent may have edited it) solely to read
    the deliverable diff; status, files, and commands come from `state`.

    Args:
        state: The terminal task state to report from.
        config: Harness config, for the workspace root.

    Returns:
        The rendered plain-text artifact block.
    """
    ws = Workspace(config.workspace_root, allow_dirty=True)
    manager = ArtifactManager()
    return manager.render(manager.build(state, ws))


def main(
    argv: list[str] | None = None,
    *,
    config: HarnessConfig | None = None,
    model_client: ModelClient | None = None,
    task_kind: Literal["edit", "investigate", "test_only"] = "investigate",
) -> int:
    """CLI entry point: parse args, wire the loop, run the task, render the artifact.

    Args:
        argv: Argument vector; falls back to `sys.argv` when omitted.
        config: Harness config; constructed from the environment when omitted.
        model_client: Model client; a default `OpenAIModelClient` if omitted (injectable for tests).
        task_kind: The verification contract to apply (`investigate` / `edit` / `test_only`).

    Returns:
        Process exit code: `0` on `success`, `2` on a dirty workspace, `1` otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="avatar-harness",
        description="A bounded, verifiable coding-agent harness.",
    )
    parser.add_argument("task", help="The natural-language task to run.")
    parser.add_argument("--log", default="events/session.jsonl", help="Path to the JSONL event log.")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Run despite uncommitted tracked changes in the workspace (§15).",
    )
    args = parser.parse_args(argv)

    config = config or HarnessConfig()
    emitter = Emitter()
    emitter.subscribe(EventLog(Path(args.log)))
    emitter.subscribe(_print_event)

    try:
        state = run_agent(
            args.task,
            config=config,
            emitter=emitter,
            model_client=model_client,
            allow_dirty=args.allow_dirty,
            task_kind=task_kind,
        )
    except DirtyWorkspaceError as exc:
        print(
            f"\nworkspace has uncommitted tracked changes ({exc}).\n"
            "Commit or stash them, pass --allow-dirty to run anyway, or set "
            "AVATAR_WORKSPACE_ROOT to a clean checkout.",
            file=sys.stderr,
        )
        return 2

    print("\n" + _report(state, config))
    return 0 if state.outcome == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
