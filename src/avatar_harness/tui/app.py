"""CockpitApp — the full-screen Textual cockpit shell (Phase 3.1 Lane 2b, ADR-0002).

Renders a run from a session's `events()` stream into three regions: a **status bar**
(mode · phase · outcome), a scrollable **transcript** of lifecycle events, and an **input**
box for the next goal. It is purely an observation subscriber + an input source — it never
sits inside the loop (§13). Approval/plan/diff modals are Lane 2c.

The app tracks its rendered transcript lines and status fields as plain attributes
(`rendered`, `phase`, `mode`, `outcome`) so behavior is assertable headlessly via
`App.run_test()` without snapshotting the rendered screen.
"""

from collections.abc import Callable, Iterator

from textual.app import App
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

from avatar_harness.event_types import (
    AgentEnd,
    AgentStart,
    ApprovalRequested,
    HarnessEvent,
    ModelUpdate,
    PhaseChanged,
    ToolEnd,
    ToolStart,
)


class CockpitApp(App):
    """The cockpit shell: status bar + transcript + input, fed by `session.events()`.

    Args:
        session: Any object exposing `events()` (and `resolve_approval`/`cancel`) — a live
            `Session` or a `ReplaySession`. The app subscribes to its event stream on mount.
        mode: The current visible mode shown in the status bar (the resolved `task_kind`).
        on_submit: Called with the prompt text when the user submits the input box; defaults
            to a no-op (the live `ReplSession.submit` wiring lands with the CLI entry point).
    """

    CSS = """
    #status { dock: top; height: 1; background: $boost; color: $text; }
    #prompt { dock: bottom; }
    #transcript { height: 1fr; }
    """

    def __init__(
        self,
        session: object,
        *,
        mode: str = "investigate",
        on_submit: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._session = session
        self.mode = mode
        self.phase = "investigating"
        self.outcome: str | None = None
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
        """Start the worker that drains the session's event stream into the UI."""
        self.run_worker(self._consume(), exclusive=False)

    async def _consume(self) -> None:
        """Render each event from the session stream (observation only; never blocks the run)."""
        async for event in self._session.events():  # type: ignore[attr-defined]
            self._handle(event)

    def _handle(self, event: HarnessEvent) -> None:
        """Update tracked state + the widgets for one event.

        Args:
            event: The lifecycle event to render.
        """
        if isinstance(event, AgentStart):
            self.outcome = None
            self.query_one("#prompt", Input).disabled = True  # a run is active
        elif isinstance(event, PhaseChanged):
            self.phase = event.new
        elif isinstance(event, AgentEnd):
            self.outcome = event.outcome
            self.query_one("#prompt", Input).disabled = False  # ready for the next goal
        self.query_one("#status", Static).update(self._status_text())
        line = self._format(event)
        if line is not None:
            self.rendered.append(line)
            self.query_one("#transcript", RichLog).write(line)

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
        if isinstance(event, AgentEnd):
            return f"■ {event.outcome}"
        return None

    def _status_text(self) -> str:
        """The status-bar line: mode · phase · outcome.

        Returns:
            The formatted status string.
        """
        return f"mode: {self.mode} · phase: {self.phase} · outcome: {self.outcome or 'running'}"

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Hand a submitted prompt to the submit callback and clear the input.

        Args:
            event: The Textual `Input.Submitted` message carrying the prompt text.
        """
        text = event.value.strip()
        if text:
            self._on_submit(text)
        self.query_one("#prompt", Input).value = ""
