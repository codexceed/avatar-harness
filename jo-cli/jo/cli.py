"""jo — the interactive cockpit's own entry point (the `jo-cli` distribution).

The harness is an independent core under many consumers (TUIs, eval drivers,
autonomous wrappers); the cockpit is one of them, so it owns its launcher. The
import direction is strictly consumer → core: this module consumes the public
`Harness`/`ReplSession` surface, and nothing in the core imports it back.

The whole sitting is journaled to one write-ahead `events/<session_id>.jsonl`
(or `--log`), so an interactive run is as replayable as a batch one — events are
committed to disk *before* the TUI renders them, and a cockpit crash loses
nothing journaled.
"""

import argparse
from uuid import uuid4

from avatar import (
    Harness,
    HarnessConfig,
    JsonlEventJournal,
    ModelClient,
    ReplSession,
    resolve_log_path,
    update_latest_pointer,
)
from jo import load_cockpit


def main(
    argv: list[str] | None = None,
    *,
    config: HarnessConfig | None = None,
    model_client: ModelClient | None = None,
) -> int:
    """`jo` entry point: build a `ReplSession` over the cockpit and run it to exit.

    Args:
        argv: Argument vector; falls back to `sys.argv` when omitted.
        config: Harness config; constructed from the environment when omitted.
        model_client: Model client; a default `OpenAIModelClient` if omitted (injectable for tests).

    Returns:
        Process exit code (`0` once the cockpit is dismissed).
    """
    parser = argparse.ArgumentParser(
        prog="jo",
        description="jo — the interactive cockpit (a multi-turn TUI) over avatar-harness.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Keep the strict §12 verification gate (default: conversational — the human decides).",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Path to the sitting's JSONL journal (default: events/<session_id>.jsonl + a latest pointer).",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Start despite uncommitted tracked changes in the workspace (§15).",
    )
    args = parser.parse_args(argv)

    if config is None:
        config = HarnessConfig()
        # The cockpit is attended: Ctrl-C is an instant hard-cancel and `max_iterations` bounds a
        # runaway, so the per-run wall-clock guillotine is off by default here — it used to
        # terminate in-progress builds as `incomplete` mid-work. An explicit
        # AVATAR_MAX_WALL_CLOCK_SECONDS still wins for anyone who wants a cap — keyed on
        # `model_fields_set` (not os.environ) so a `.env`-sourced cap counts too (PR-#106 review).
        if "max_wall_clock_seconds" not in config.model_fields_set:
            config.max_wall_clock_seconds = None
    session_id = uuid4().hex
    log_path = resolve_log_path(args.log, session_id)
    config.log_path = str(log_path)  # hide the harness's own journal from the agent's file tools
    journal = JsonlEventJournal(log_path)
    harness = Harness(config=config, model=model_client)
    repl = ReplSession(
        harness, session_id=session_id, auto=args.auto, journal=journal, allow_dirty=args.allow_dirty
    )
    cockpit_cls = load_cockpit()  # guarded import — clear hint if the [textual] extra is absent
    try:
        cockpit_cls(repl=repl).run()
    finally:
        journal.close()
        if args.log is None:  # only the managed per-session layout maintains the pointer
            update_latest_pointer(log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
