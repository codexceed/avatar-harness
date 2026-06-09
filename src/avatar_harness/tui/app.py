"""CockpitApp — the full-screen Textual cockpit shell (Phase 3.1 Lane 2b, ADR-0002).

Renders a run from a session's `events()` stream into three regions: a **status bar**
(mode · phase · outcome), a scrollable **transcript** of lifecycle events, and an **input**
box for the next goal. It is purely an observation subscriber + an input source — it never
sits inside the loop (§13). Approval/plan/diff modals are Lane 2c.

The app tracks its rendered transcript lines and status fields as plain attributes
(`rendered`, `phase`, `mode`, `outcome`) so behavior is assertable headlessly via
`App.run_test()` without snapshotting the rendered screen.
"""

import asyncio
from collections.abc import Callable, Iterator

from textual.app import App
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

from avatar_harness.event_types import (
    AgentEnd,
    AgentStart,
    ApprovalRequested,
    DecisionError,
    HarnessEvent,
    ModelUpdate,
    PhaseChanged,
    ToolEnd,
    ToolStart,
    VerificationEnd,
)
from avatar_harness.session import Session
from avatar_harness.session_state import ReplSession
from avatar_harness.state import TaskState
from avatar_harness.tui.modals import ApprovalChoice, ApprovalModal, DiffModal, PlanModal
from avatar_harness.workspace import DirtyWorkspaceError


class CockpitApp(App):
    """The cockpit: status bar + transcript + input, over a session's event stream (§13, §23).

    Two construction modes (exactly one of `session`/`repl`):

    - **observe** (`session=`): drains a single fixed stream — a `ReplaySession` for tests or a
      future `--replay <journal>` viewer.
    - **drive** (`repl=`): the live multi-turn REPL. Input routes through `ReplSession` — meta
      commands handled locally, goals run as observable per-goal `Session`s (streamed here),
      plan mode runs plan → `PlanModal` → build. The cockpit stays a pure observer + control
      caller (§13): it renders `events()` and acts only via the modals → `resolve_approval`.

    Args:
        session: A fixed event source (`ReplaySession`/`Session`) for observe mode, or `None`.
        repl: The live `ReplSession` to drive; mutually exclusive with `session`.
        mode: The visible mode shown in the status bar (defaults to the repl's resolved mode).
        on_submit: Observe-mode submit callback (unused in drive mode); defaults to a no-op.
    """

    BINDINGS = [Binding("ctrl+c", "cancel", "cancel / quit", priority=True)]  # noqa: RUF012 — Textual contract

    CSS = """
    #status { dock: top; height: 1; background: $boost; color: $text; }
    #prompt { dock: bottom; }
    #transcript { height: 1fr; }
    """

    def __init__(
        self,
        session: object | None = None,
        *,
        repl: ReplSession | None = None,
        mode: str = "investigate",
        on_submit: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.repl = repl
        self._session = session  # the currently-observed stream (a per-goal Session in drive mode)
        self.mode = (repl.mode or mode) if repl is not None else mode
        self.phase = "investigating"
        self.outcome: str | None = None
        self.verdict: bool | None = None  # the verifier's real verdict (advisory in chat mode)
        self._on_submit = on_submit or (lambda _prompt: None)
        self.rendered: list[str] = []  # mirror of transcript lines, for headless assertions

    def compose(self) -> Iterator[Widget]:
        """Lay out the three regions.

        Yields:
            The status bar, transcript log, and input widgets.
        """
        yield Static(self._status_text(), id="status")
        yield RichLog(id="transcript", highlight=False, markup=False, wrap=True)
        yield Input(placeholder="Ask, or describe a change…", id="prompt")

    def on_mount(self) -> None:
        """Observe mode: start the worker draining the fixed stream. Drive mode waits for input."""
        if self.repl is None and self._session is not None:
            self.run_worker(self._consume(self._session), exclusive=False)

    async def _consume(self, session: object) -> None:
        """Render each event from `session`'s stream (observation only; never blocks the run).

        Args:
            session: The session whose `events()` to drain (a fixed source or a per-goal run).
        """
        async for event in session.events():  # type: ignore[attr-defined]
            self._handle(event)

    def _handle(self, event: HarnessEvent) -> None:
        """Update tracked state + the widgets for one event.

        Args:
            event: The lifecycle event to render.
        """
        if isinstance(event, AgentStart):
            self.outcome = None
            self.verdict = None
            self.query_one("#prompt", Input).disabled = True  # a run is active
        elif isinstance(event, PhaseChanged):
            self.phase = event.new
        elif isinstance(event, VerificationEnd):
            self.verdict = event.passed
        elif isinstance(event, ApprovalRequested):
            self._prompt_approval(event)  # announce → modal → resolve_approval (control plane)
        elif isinstance(event, AgentEnd):
            self.outcome = event.outcome
            self.query_one("#prompt", Input).disabled = False  # ready for the next goal
        self.query_one("#status", Static).update(self._status_text())
        self._write(self._format(event))

    def _write(self, line: str | None) -> None:
        """Append `line` to the transcript (and the `rendered` mirror); `None` is skipped.

        Args:
            line: The text to render, or `None` for events shown only in the status bar.
        """
        if line is not None:
            self.rendered.append(line)
            self.query_one("#transcript", RichLog).write(line)

    def _prompt_approval(self, event: ApprovalRequested) -> None:
        """Pop the approval modal for a gated call and route the choice to the control plane.

        The event only *announces* the need; the decision returns through
        `session.resolve_approval`, never the event stream (§13).

        Args:
            event: The `ApprovalRequested` event to surface.
        """
        modal = ApprovalModal(tool=event.tool, reason=event.reason, tool_input=event.input)

        def _resolve(choice: ApprovalChoice | None) -> None:
            if choice is None:
                return
            self.run_worker(
                self._session.resolve_approval(  # type: ignore[attr-defined]
                    event.approval_id, allow=choice.allow, remember=choice.remember
                )
            )

        self.push_screen(modal, _resolve)

    def _format(self, event: HarnessEvent) -> str | None:  # noqa: PLR0911 — a flat per-event switch
        """The transcript line for an event, or `None` for events shown only in the status bar.

        Args:
            event: The lifecycle event.

        Returns:
            A one-line rendering, or `None` to skip the transcript.
        """
        if isinstance(event, AgentStart):
            return f"▶ {event.goal}"
        if isinstance(event, ToolStart):
            return f"→ {event.tool} {event.input}"
        if isinstance(event, ToolEnd):
            return f"{'✓' if event.success else '✗'} {event.tool}: {event.summary or event.content}"
        if isinstance(event, ModelUpdate):
            return event.delta
        if isinstance(event, ApprovalRequested):
            return f"⏸ approval needed: {event.tool}"
        if isinstance(event, DecisionError):
            kind = "retried" if event.recovered else "turn lost"
            return f"↩ malformed decision ({kind}): {event.error}"
        if isinstance(event, VerificationEnd):
            # The real verdict, always — in conversational mode the outcome alone would
            # read "success" even when verification failed (§23.5: the human decides).
            mark = "✓" if event.passed else "⚠"
            verb = "passed" if event.passed else "failed"
            return (
                f"{mark} verification {verb}: {event.summary}"
                if event.summary
                else f"{mark} verification {verb}"
            )
        if isinstance(event, AgentEnd):
            return f"■ {event.outcome}"
        return None

    def _status_text(self) -> str:
        """The status-bar line: mode · phase · outcome (· the verifier's verdict, once known).

        Returns:
            The formatted status string.
        """
        line = f"mode: {self.mode} · phase: {self.phase} · outcome: {self.outcome or 'running'}"
        if self.verdict is not None:
            line += f" · verify: {'✓' if self.verdict else '⚠ failed'}"
        return line

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Route a submitted prompt: drive the REPL (drive mode) or the observe-mode callback.

        Args:
            event: The Textual `Input.Submitted` message carrying the prompt text.
        """
        text = event.value.strip()
        self.query_one("#prompt", Input).value = ""
        if not text:
            return
        if self.repl is not None:
            self._drive_input(text)
        else:
            self._on_submit(text)

    # --- drive mode: route input through the ReplSession (§23.2) --------------------------

    def _drive_input(self, text: str) -> None:
        """Handle one line of REPL input: meta locally, otherwise run it as a goal.

        Args:
            text: The submitted line.
        """
        if self.repl is None:
            return
        if self.repl.is_meta(text):
            self._handle_meta(text)
        else:
            self.run_worker(self._run_goal(text), exclusive=False)

    def _handle_meta(self, text: str) -> None:
        """Run a `/command` locally and route its `MetaResult` (quit/diff/mode/message).

        Args:
            text: The raw `/command` line.
        """
        if self.repl is None:
            return
        result = self.repl.run_meta(text)
        if result.kind == "quit":
            self.exit()
            return
        if result.kind == "diff":
            self.push_screen(DiffModal(result.text))
            return
        if result.kind == "mode_set":
            self.mode = self.repl.mode or self.mode
        self._write(result.text)
        self.query_one("#status", Static).update(self._status_text())

    async def _run_goal(self, text: str) -> None:
        """Run one non-meta goal: plan mode routes through the plan flow, else a direct run.

        A failed goal renders as a transcript line and leaves the REPL alive — an
        exception escaping a Textual worker would tear down the whole app (the dogfood
        crash: a `DirtyWorkspaceError` on a follow-up goal killed the TUI mid-session).

        Args:
            text: The user's goal.
        """
        if self.repl is None:
            return
        try:
            if self.repl.resolve_mode(text) == "plan":
                await self._run_plan_goal(text)
            else:
                session = self.repl.start(text)
                state = await self._observe(session)
                self.repl.record(state)
        except DirtyWorkspaceError as exc:
            self._write(
                f"✗ DirtyWorkspaceError: the tree at {exc} has uncommitted tracked changes — "
                "commit/stash them, or relaunch with --allow-dirty to acknowledge them"
            )
        except Exception as exc:
            self._write(f"✗ goal failed — {type(exc).__name__}: {exc}")
        finally:
            self.query_one("#prompt", Input).disabled = False  # the REPL stays usable
            self.query_one("#status", Static).update(self._status_text())

    async def _run_plan_goal(self, text: str) -> None:
        """Plan mode: stream the read-only plan → `PlanModal` → (on approve) stream the build.

        On revise the plan is re-run with the revision; an empty/abnormal plan or a declined
        revision is surfaced without building. The goal's turn is recorded once via `record_goal`.

        Args:
            text: The user's goal.
        """
        if self.repl is None:
            return
        revision: str | None = None
        while True:
            plan_state = await self._observe(self.repl.start_plan(text, revision=revision))
            if not self.repl.plan_is_approvable(plan_state):
                self.repl.record_goal(text, plan_state)  # nothing approvable — surface it
                return
            choice = await self.push_screen_wait(PlanModal(self.repl.extract_plan(plan_state)))
            if choice.approved:
                approved_plan = choice.text or self.repl.extract_plan(plan_state)
                break
            revision = choice.text  # revise → re-run the plan
        build_state = await self._observe(self.repl.start_build(text, approved_plan))
        self.repl.record_goal(text, build_state)

    async def _observe(self, session: Session) -> TaskState:
        """Run `session` while streaming its events into the transcript; return its terminal state.

        Sets `session` as the current one so an approval modal routes its decision back to it.

        Args:
            session: The per-goal `Session` to run and render.

        Returns:
            The terminal `TaskState`.
        """
        self._session = session
        run = asyncio.create_task(session.run())
        await self._consume(session)  # drains until the bus closes on agent_end
        return await run

    def action_cancel(self) -> None:
        """Ctrl-C: cancel the in-flight run if one is active (it refeeds as history), else quit."""
        session = self._session
        if self.repl is not None and isinstance(session, Session) and not session.state.terminal:
            self.run_worker(session.cancel("cancelled by user"))
        else:
            self.exit()
