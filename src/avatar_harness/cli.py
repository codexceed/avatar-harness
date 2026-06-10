"""CLI entry point.

`main()` drives the real loop via `run_agent`. The CLI stays a thin shell over the
loop — wiring components and event subscribers, nothing more.
"""

import argparse
import contextlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from avatar_harness.artifact import ArtifactManager
from avatar_harness.config import HarnessConfig
from avatar_harness.eventlog import EventLog
from avatar_harness.events import Emitter, Event
from avatar_harness.harness import Harness
from avatar_harness.journal import JsonlEventJournal
from avatar_harness.model_client import ModelClient
from avatar_harness.session_state import ReplSession
from avatar_harness.state import TaskState
from avatar_harness.tui import load_cockpit
from avatar_harness.workspace import DirtyWorkspaceError, Workspace

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


def _launch_cockpit(
    *,
    config: HarnessConfig,
    model_client: ModelClient | None,
    auto: bool,
    log_arg: str | None,
    allow_dirty: bool = False,
) -> int:
    """Build a `ReplSession` over the cockpit and run it to exit (the `--interactive` path).

    The whole sitting is journaled to one write-ahead `events/<session_id>.jsonl` (or
    `--log`), so an interactive run is as replayable as a batch one — events are committed
    to disk *before* the TUI renders them, and a cockpit crash loses nothing journaled.

    Args:
        config: Harness config for the session.
        model_client: Model client; a default `OpenAIModelClient` if omitted.
        auto: `True` keeps the strict §12 gate; `False` (default) is conversational (§23.5).
        log_arg: The `--log` value, or `None` for the managed per-session layout.
        allow_dirty: Acknowledge a dirty tree at the start of the sitting (§15).

    Returns:
        Process exit code (`0` once the cockpit is dismissed).
    """
    session_id = uuid4().hex
    log_path = _resolve_log_path(log_arg, session_id)
    journal = JsonlEventJournal(log_path)
    harness = Harness(config=config, model=model_client)
    repl = ReplSession(harness, session_id=session_id, auto=auto, journal=journal, allow_dirty=allow_dirty)
    cockpit_cls = load_cockpit()  # guarded import — clear hint if the [textual] extra is absent
    try:
        cockpit_cls(repl=repl).run()
    finally:
        journal.close()
        if log_arg is None:  # only the managed per-session layout maintains the pointer
            _update_latest_pointer(log_path)
    return 0


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
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Launch the interactive Textual cockpit (a multi-turn REPL) instead of a batch run.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="In the cockpit, keep the strict §12 verification gate (default: conversational).",
    )
    args = parser.parse_args(argv)

    config = config or HarnessConfig()
    if args.interactive:
        return _launch_cockpit(
            config=config,
            model_client=model_client,
            auto=args.auto,
            log_arg=args.log,
            allow_dirty=args.allow_dirty,
        )
    if args.task is None:
        parser.error("a task is required (or pass --interactive for the cockpit)")

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
