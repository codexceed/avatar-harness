"""CLI entry point.

`run_echo` is the Phase 0 skeleton (kept for the event-spine tests); `main()`
now drives the real Phase 1 read-only loop via `run_agent`. The CLI stays a thin
shell over the loop — wiring components and event subscribers, nothing more.
"""

import argparse
import contextlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from avatar_harness.artifact import ArtifactManager
from avatar_harness.config import HarnessConfig
from avatar_harness.eventlog import EventLog
from avatar_harness.events import Emitter, Event
from avatar_harness.harness import Harness
from avatar_harness.model_client import ModelClient
from avatar_harness.state import TaskState
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
    harness = Harness(config=config, model=model_client, emitter=emitter)
    return harness.run(task, task_kind=task_kind, allow_dirty=allow_dirty)


def _print_event(event: Event) -> None:
    parts = []
    for key, value in event.items():
        if key in ("type", "ts", "session_id"):  # prefix/grouping keys, not inline noise
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


def _resolve_log_path(arg: str | None, session_id: str) -> Path:
    """Pick the event-log path: an explicit `--log`, else a per-session default.

    The default `events/<session_id>.jsonl` makes one session one file — grouping is
    physical and the filename is self-identifying — instead of appending every run to a
    shared static log that must be filtered apart.

    Args:
        arg: The `--log` value, or `None` to use the per-session default.
        session_id: This run's id, used to name the default log.

    Returns:
        The resolved log path.
    """
    if arg is not None:
        return Path(arg)
    return Path("events") / f"{session_id}.jsonl"


def _update_latest_pointer(log_path: Path) -> None:
    """Point `latest.jsonl` at this run's log so the newest session is always reachable.

    Best-effort: a platform without symlink support (or a permission error) just leaves
    the per-session log — the pointer is a convenience, not the source of truth.

    Args:
        log_path: This run's per-session log file.
    """
    pointer = log_path.parent / "latest.jsonl"
    with contextlib.suppress(OSError):
        if pointer.is_symlink() or pointer.exists():
            pointer.unlink()
        pointer.symlink_to(log_path.name)


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
    parser.add_argument(
        "--log",
        default=None,
        help="Path to the JSONL event log (default: events/<session_id>.jsonl + a latest.jsonl pointer).",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Run despite uncommitted tracked changes in the workspace (§15).",
    )
    args = parser.parse_args(argv)

    config = config or HarnessConfig()
    session_id = uuid4().hex
    log_path = _resolve_log_path(args.log, session_id)
    emitter = Emitter(session_id=session_id)
    emitter.subscribe(EventLog(log_path))
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

    # Swing the pointer only after the run has actually produced the per-session log:
    # a run that aborts before the first event (e.g. the dirty-workspace path) never
    # creates the file, so updating the pointer eagerly would leave latest.jsonl
    # dangling and lose the pointer to the last usable session log.
    if args.log is None:  # only the managed per-session layout maintains the pointer
        _update_latest_pointer(log_path)

    print("\n" + _report(state, config))
    return 0 if state.outcome == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
