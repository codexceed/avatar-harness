"""CLI entry point — the batch shell.

`main()` drives the real loop via `run_agent`. The CLI stays a thin shell over the
loop — wiring components and event subscribers, nothing more. It is deliberately
**TUI-free**: the harness is an independent core under many consumers, and the
interactive cockpit ships as a separate `jo-cli` package (the `jo` command)
so the import direction stays strictly consumer → core.
"""

import argparse
import sys
from datetime import datetime
from typing import Literal
from uuid import uuid4

from avatar.artifact import ArtifactManager
from avatar.config import HarnessConfig
from avatar.eventlog import EventLog
from avatar.events import Emitter, Event
from avatar.harness import Harness
from avatar.journal import resolve_log_path, update_latest_pointer
from avatar.model_client import ModelClient
from avatar.state import TaskState
from avatar.workspace import DirtyWorkspaceError, Workspace

# Truncation width for event values rendered to the terminal.
_EVENT_VALUE_WIDTH = 160


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
        prog="avatar",
        description="A bounded, verifiable coding-agent harness.",
    )
    parser.add_argument("task", nargs="?", default=None, help="The natural-language task to run.")
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
    if args.task is None:
        parser.error("a task is required (for the interactive cockpit, install `jo-cli` and run `jo`)")

    session_id = uuid4().hex
    log_path = resolve_log_path(args.log, session_id)
    config.log_path = str(log_path)  # hide the harness's own journal from the agent's file tools
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
        update_latest_pointer(log_path)

    print("\n" + _report(state, config))
    return 0 if state.outcome == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
