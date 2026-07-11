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
import signal
import zlib
from collections.abc import Callable, Iterator

from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import RichLog, Static, TextArea

from avatar import (
    AgentEnd,
    AgentStart,
    ApprovalRequested,
    DecisionError,
    DeclarationRequired,
    DirtyWorkspaceError,
    HarnessEvent,
    ModelDecisionEvent,
    ModelUpdate,
    PhaseChanged,
    ReplSession,
    Session,
    TaskEscalated,
    TaskState,
    ToolEnd,
    ToolStart,
    VerificationEnd,
    VerificationStart,
)
from jo.modals import ApprovalChoice, ApprovalModal, DiffModal, PlanModal

# One stable color per tool *family*, so a transcript scans by color: blue = inspect,
# magenta = mutate, yellow = execute, cyan = the verification contract.
TOOL_STYLES: dict[str, str] = {
    "read_file": "blue",
    "list_files": "blue",
    "search_repo": "blue",
    "write_file": "magenta",
    "str_replace": "magenta",
    "delete_file": "magenta",
    "run_tests": "yellow",
    "run_linter": "yellow",
    "run_command": "yellow",
    "declare_verification": "cyan",
    "alter_verification": "cyan",
}

# Tools the map doesn't know (plugins, future registrations) hash onto this palette.
_TOOL_PALETTE: tuple[str, ...] = ("blue", "magenta", "yellow", "cyan", "bright_blue", "bright_magenta")

# The pending-model-inference color: green, matching the `● agent` turn marker.
THINKING_STYLE = "green"

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def tool_style(tool: str) -> str:
    """The stable Rich style for `tool` — family-mapped, or hashed onto the palette.

    Hashing uses `crc32`, not the per-process-salted builtin `hash`, so an unknown
    tool keeps the same color across sessions.

    Args:
        tool: The tool name as it appears in `ToolStart`/`ToolEnd`.

    Returns:
        A Rich style string, identical for every call with the same name.
    """
    known = TOOL_STYLES.get(tool)
    if known is not None:
        return known
    return _TOOL_PALETTE[zlib.crc32(tool.encode("utf-8")) % len(_TOOL_PALETTE)]


class HistoryInput(TextArea):
    """A multi-line prompt that submits on Enter and recalls history at its edges.

    Multi-line by subclassing Textual's `TextArea`: Enter submits the whole buffer (posting
    a `HistoryInput.Submitted` message), while `Ctrl+J` / `Shift+Enter` / `Alt+Enter` insert a
    newline. `Ctrl+J` is literally LF and is distinct from Enter on every terminal; the shift/alt
    variants only reach us under the enhanced (kitty) keyboard protocol, so they're conveniences
    layered on the universal `Ctrl+J` path.

    History recall is edge-gated so it doesn't fight cursor movement in a multi-line draft: `↑`
    recalls an older prompt only when the cursor is on the first line, `↓` a newer one only on the
    last line; anywhere in between the arrows move the cursor. The cockpit calls `remember` on every
    submit to append the line (de-duplicated against the most recent entry). Stepping past the newest
    entry restores the draft that was in progress before browsing began. History is in-memory for the
    sitting — the journal stays the durable record.

    Args:
        placeholder: The greyed-out prompt shown while the field is empty.
        id: The widget id (the cockpit queries `#prompt`).
    """

    class Submitted(Message):
        """Posted on Enter to hand the buffer to the cockpit (a `TextArea` has no `Submitted`).

        Carries `.value` so the cockpit's submit handler reads it exactly like the old
        `Input.Submitted.value`; `.text_area` is the sender (Textual `Message` convention).

        Args:
            text_area: The `HistoryInput` that produced the submission.
            value: The full (possibly multi-line) buffer text.
        """

        def __init__(self, text_area: "HistoryInput", value: str) -> None:
            self.value = value
            self.text_area = text_area
            super().__init__()

    def __init__(self, *, placeholder: str = "", id: str | None = None) -> None:
        super().__init__(text="", placeholder=placeholder, id=id, show_line_numbers=False, soft_wrap=True)
        self._history: list[str] = []
        self._cursor: int | None = None  # None ⇒ not browsing; else an index into _history
        self._draft = ""  # the in-progress line, stashed when browsing starts

    async def _on_key(self, event: Key) -> None:
        """Route keys: Enter submits, Ctrl+J/Shift+Enter/Alt+Enter newline, edge-gated ↑/↓ history.

        Any other key falls through to `TextArea`'s default editing/cursor handling.

        Args:
            event: The Textual `Key` event for the pressed key.
        """
        key = event.key
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self, self.text))
            return
        if key in ("ctrl+j", "shift+enter", "alt+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key == "up" and self._history and self.cursor_at_first_line:
            event.stop()
            event.prevent_default()
            self.action_history_prev()
            return
        if key == "down" and self._history and self.cursor_at_last_line:
            event.stop()
            event.prevent_default()
            self.action_history_next()
            return
        await super()._on_key(event)

    def remember(self, text: str) -> None:
        """Append a submitted prompt to history and reset the browse cursor.

        Args:
            text: The submitted line (consecutive duplicates are not re-stored).
        """
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._cursor = None
        self._draft = ""

    def action_history_prev(self) -> None:
        """↑ at the top edge — recall the previous (older) prompt, stashing the draft first."""
        if not self._history:
            return
        if self._cursor is None:  # entering history: remember what was being typed
            self._draft = self.text
            self._cursor = len(self._history)
        if self._cursor > 0:
            self._cursor -= 1
            self._recall(self._history[self._cursor])

    def action_history_next(self) -> None:
        """↓ at the bottom edge — move toward newer prompts; past the newest restores the draft."""
        if self._cursor is None:
            return
        self._cursor += 1
        if self._cursor >= len(self._history):
            self._cursor = None
            self._recall(self._draft)
        else:
            self._recall(self._history[self._cursor])

    def _recall(self, text: str) -> None:
        """Replace the buffer with `text` and park the cursor at its end.

        Args:
            text: The recalled line to show.
        """
        self.text = text
        self.move_cursor(self.document.end)


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
    #status { height: 1; background: $boost; color: $text; }
    #footer { dock: bottom; height: auto; }
    #prompt { height: auto; max-height: 8; border: none; padding: 0; }
    #activity { height: 1; }
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
        self.verdict: bool | None = None  # the verifier's real verdict (it steers the turn, §23.5)
        self._on_submit = on_submit or (lambda _prompt: None)
        self.rendered: list[str] = []  # mirror of transcript lines, for headless assertions
        self._run_task: asyncio.Task[TaskState] | None = None  # the in-flight per-goal run, for ctrl+c
        self.activity: str | None = None  # the spinner label ("thinking…"/"running x…"), None when idle
        self.activity_style: str = ""  # the spinner's color — headless-assertable like `rendered`
        self._spinner_frame = 0
        self._spinner_timer: Timer | None = None

    def compose(self) -> Iterator[Widget]:
        """Lay out the three regions.

        Yields:
            The status bar, transcript log, and input widgets.
        """
        yield RichLog(id="transcript", highlight=False, markup=False, wrap=True)
        # One bottom-docked footer (same-edge docks would overlap, not stack). The
        # mode·phase status sits at the footer's top — directly by the input the human is
        # typing into, not stranded at the screen top — so the live task kind and phase are
        # always in view where a decision is being made; the activity (spinner) line follows.
        yield Vertical(
            Static(self._status_text(), id="status"),
            Static("", id="activity"),
            HistoryInput(
                placeholder="Ask, or describe a change… (Enter to send · Shift+Enter for newline)",
                id="prompt",
            ),
            id="footer",
        )

    def on_mount(self) -> None:
        """Observe mode: drain the fixed stream; drive mode waits for input. Install signal handlers."""
        self._spinner_timer = self.set_interval(1 / 8, self._spin, pause=True)
        if self.repl is None and self._session is not None:
            self.run_worker(self._consume(self._session), exclusive=False)
        self._set_signal_handlers(install=True)

    def on_unmount(self) -> None:
        """Remove the SIGINT/SIGTERM handlers installed in `on_mount` (restore prior disposition)."""
        self._set_signal_handlers(install=False)

    def _set_signal_handlers(self, *, install: bool) -> None:
        """Install or remove graceful SIGINT/SIGTERM handlers (ADR-0030).

        Textual's full-screen driver does not claim `SIGINT`/`SIGTERM`, and in the TUI a
        ctrl+c arrives as a *key* (→ `action_cancel`), not a signal — so these handlers fire
        only for an *external* terminate (`kill`, a parent/CI SIGTERM), turning it into a
        graceful shutdown instead of a default-handler crash. Skipped under headless test
        runs (drive via `Pilot`, never touch process-wide signal state) and on platforms /
        loops without signal support (e.g. Windows).

        Args:
            install: Add the handlers when `True`, remove them when `False`.
        """
        if self.is_headless:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                if install:
                    loop.add_signal_handler(sig, self._on_terminate_signal)
                else:
                    loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # unsupported platform/loop — Textual still tears the app down on exit

    def _on_terminate_signal(self) -> None:
        """An external SIGINT/SIGTERM: cancel any in-flight run, then quit gracefully (ADR-0030)."""
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        self.exit()

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
            # Reset ALL per-goal display state: a follow-up goal must not wear the
            # previous goal's phase/verdict (dogfood `events/04849a5a…jsonl` showed
            # `phase: verifying · verify: ✓` on an unverified, incomplete goal 2).
            self.outcome = None
            self.verdict = None
            self.phase = "investigating"
            self.query_one("#prompt", HistoryInput).disabled = True  # a run is active
            self._set_activity("thinking…", THINKING_STYLE)  # first model inference is pending
        elif isinstance(event, PhaseChanged):
            self.phase = event.new
        elif isinstance(event, TaskEscalated):
            # A consented `switch_to_editing` flipped the kind mid-run (ADR-0048); the
            # classification shown by the bar must follow it, or it lies "investigate"
            # while the run edits (dogfood `tetris_grok4`: escalated goals stayed labeled
            # investigate). PhaseChanged carries the editing advance; this carries the kind.
            self.mode = event.to_kind
        elif isinstance(event, ToolStart):
            self._set_activity(f"running {event.tool}…", tool_style(event.tool))
        elif isinstance(event, ToolEnd):
            self._set_activity("thinking…", THINKING_STYLE)  # the next model turn is pending
        elif isinstance(event, VerificationStart):
            self._set_activity("verifying…", "yellow")  # harness-owned checks are running
        elif isinstance(event, VerificationEnd):
            self.verdict = event.passed
            self._set_activity("thinking…", THINKING_STYLE)  # end or a repair turn follows
        elif isinstance(event, ApprovalRequested):
            self._set_activity("waiting for approval…", "bold yellow")  # blocked on the human
            self._prompt_approval(event)  # announce → modal → resolve_approval (control plane)
        elif isinstance(event, AgentEnd):
            self.outcome = event.outcome
            self.query_one("#prompt", HistoryInput).disabled = False  # ready for the next goal
            self._set_activity(None)
        self.query_one("#status", Static).update(self._status_text())
        self._write(self._format(event))

    def _set_activity(self, label: str | None, style: str = "") -> None:
        """Set (or clear, with `None`) the color-coded spinner line above the input.

        Tracks `activity`/`activity_style` as plain attributes for headless assertions,
        renders the current frame immediately, and runs the animation timer only while
        something is pending.

        Args:
            label: What the run is waiting on ("thinking…", "running x…"), or `None` for idle.
            style: The Rich style coding the wait — the tool's color for a running tool,
                `THINKING_STYLE` for pending inference.
        """
        self.activity = label
        self.activity_style = style if label is not None else ""
        if self._spinner_timer is not None:
            if label is not None:
                self._spinner_timer.resume()
            else:
                self._spinner_timer.pause()
        self._render_activity()

    def _spin(self) -> None:
        """One animation tick — advance the spinner frame and re-render the activity line."""
        self._spinner_frame = (self._spinner_frame + 1) % len(_SPINNER_FRAMES)
        self._render_activity()

    def _render_activity(self) -> None:
        """Paint the activity line: `⠋ label` in its color, or blank when idle."""
        widget = self.query_one("#activity", Static)
        if self.activity is None:
            widget.update("")
        else:
            frame = _SPINNER_FRAMES[self._spinner_frame]
            widget.update(Text(f"{frame} {self.activity}", style=self.activity_style))

    def _write(self, line: str | Text | None) -> None:
        """Append `line` to the transcript (and the plain-string `rendered` mirror); `None` is skipped.

        A `Text` keeps its styling when written to the `RichLog` (which honors `Text`
        styles regardless of `markup=False` — that flag only governs *string* markup
        parsing), while `self.rendered` stays `list[str]` for headless substring asserts.

        Args:
            line: The text to render (styled `Text` or plain `str`), or `None` for events
                shown only in the status bar.
        """
        if line is not None:
            self.rendered.append(line.plain if isinstance(line, Text) else line)
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

    def _format(self, event: HarnessEvent) -> str | Text | None:  # noqa: PLR0911, C901 — a flat per-event switch
        """The transcript line for an event, or `None` for events shown only in the status bar.

        Lines are styled `Text` so the transcript reads as a chat: the user goal (`▶ you`)
        in cyan, model turns (`● agent`) in green, tool names in their stable per-family
        color (`tool_style`) with args/summaries dim — the label is model-agnostic
        (`you`/`agent`), since this harness runs non-Claude models too.

        Args:
            event: The lifecycle event.

        Returns:
            A one-line rendering (styled or plain), or `None` to skip the transcript.
        """
        if isinstance(event, AgentStart):
            # Drive mode already echoed the user's turn on submit (and AgentStart fires
            # twice in plan mode); suppress it here. Observe mode (no repl — replay) has
            # no submit echo, so AgentStart is the only user representation: keep it.
            if self.repl is not None:
                return None
            line = Text("▶ you", style="bold cyan")
            line.append(f"  {event.goal}", style="")
            return line
        if isinstance(event, ModelDecisionEvent):
            return self._format_decision(event)
        if isinstance(event, ToolStart):
            # Only the tool name is color-coded; the rest of the line stays dim as before.
            line = Text("→ ", style="dim")
            line.append(event.tool, style=tool_style(event.tool))
            line.append(f" {event.input}", style="dim")
            return line
        if isinstance(event, ToolEnd):
            line = Text(f"{'✓' if event.success else '✗'} ", style="dim")
            line.append(event.tool, style=tool_style(event.tool))
            line.append(f": {event.summary or event.content}", style="dim")
            return line
        if isinstance(event, ModelUpdate):
            line = Text("● agent", style="bold green")
            line.append(f"  {event.delta}", style="")
            return line
        if isinstance(event, ApprovalRequested):
            return f"⏸ approval needed: {event.tool}"
        if isinstance(event, DeclarationRequired):
            # Greenfield edit refused pending a declared contract (ADR-0038) — informational, no
            # modal (the model complies, not the human): observe-only, §13.
            return Text("✍ declare a verification contract before editing", style="yellow")
        if isinstance(event, DecisionError):
            kind = "retried" if event.recovered else "turn lost"
            return f"↩ malformed decision ({kind}): {event.error}"
        if isinstance(event, VerificationEnd):
            # The real verdict, always — a mid-repair verdict or an advisory (eval) run can show
            # `outcome: success` while a check failed, so render the verdict itself (§23.5).
            mark = "✓" if event.passed else "⚠"
            verb = "passed" if event.passed else "failed"
            line = Text(mark, style="green" if event.passed else "yellow")
            line.append(f" verification {verb}", style="")
            if event.summary:
                line.append(f": {event.summary}", style="")
            return line
        if isinstance(event, AgentEnd):
            return f"■ {event.outcome}"
        return None

    def _format_decision(self, event: ModelDecisionEvent) -> Text | None:
        """The model's turn for a `ModelDecisionEvent` — thought line + a spoken-message line.

        `final_answer`/`ask_user` render `● agent  {action}` (the answer / the question);
        a `tool_call` shows only its thought, since the call itself is rendered by
        `ToolStart`/`ToolEnd` (no duplicate). The thought, when present, is a dim/italic
        line above — the public display-channel summary, not private chain-of-thought
        (ADR-0001 D6), so showing it keeps execution legible.

        Args:
            event: The model-decision event.

        Returns:
            The composed model turn, or `None` for a thoughtless `tool_call`.
        """
        lines: list[Text] = []
        if event.thought:
            lines.append(Text(event.thought, style="dim italic"))
        if event.action_type in ("final_answer", "ask_user") and event.action:
            line = Text("● agent", style="bold green")
            line.append(f"  {event.action}", style="")
            lines.append(line)
        if not lines:
            return None
        return Text("\n").join(lines)

    def _status_text(self) -> str:
        """The status-bar line: mode · phase · outcome (· the verifier's verdict, once known).

        Returns:
            The formatted status string.
        """
        line = f"mode: {self.mode} · phase: {self.phase} · outcome: {self.outcome or 'running'}"
        if self.verdict is not None:
            line += f" · verify: {'✓' if self.verdict else '⚠ failed'}"
        return line

    def on_history_input_submitted(self, event: HistoryInput.Submitted) -> None:
        """Route a submitted prompt: drive the REPL (drive mode) or the observe-mode callback.

        Args:
            event: The `HistoryInput.Submitted` message carrying the (possibly multi-line) text.
        """
        text = event.value.strip()
        prompt = self.query_one("#prompt", HistoryInput)
        prompt.text = ""
        if not text:
            return
        prompt.remember(text)  # ↑/↓ recall — record the submitted line for this sitting
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
            # Echo the user's turn immediately, before the run starts: in drive mode the
            # AgentStart goal line is suppressed (it would otherwise double here, and fire
            # twice in plan mode), so this is the sole rendering of what the human typed.
            line = Text("▶ you", style="bold cyan")
            line.append(f"  {text}", style="")
            self._write(line)
            # Disable input synchronously: classification runs before AgentStart (whose
            # handler used to be the only disabler), and that window let a second goal
            # start and race the first (PR-#32 review). Re-enabled in _run_goal's finally.
            self.query_one("#prompt", HistoryInput).disabled = True
            # Spin from submit: mode classification (a network call) precedes AgentStart.
            self._set_activity("thinking…", THINKING_STYLE)
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
        # Belt-and-braces: clear per-goal display state before the stream starts, so the
        # bar can never wear a previous goal's verdict even if an early event were lost.
        self.outcome = None
        self.verdict = None
        self.phase = "investigating"
        try:
            # Resolve off-loop (classification is a network call) and announce the
            # verdict + its source before running — visible, correctable routing (D3).
            resolved = await asyncio.to_thread(self.repl.resolve_mode, text)
            self.mode = resolved
            self._write(f"▶ mode: {resolved} ({self.repl.last_mode_source}) — /mode to change")
            self.query_one("#status", Static).update(self._status_text())
            if resolved == "plan":
                await self._run_plan_goal(text)
            else:
                session = self.repl.start(text)  # memoized — no second classification
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
            # The run is over — drop the per-goal session. A mid-run failure (e.g. a missing
            # API key surfaced from the model client) leaves its Session non-terminal, and a
            # lingering reference made ctrl+c keep trying to cancel a dead run instead of
            # quitting; clearing it lets action_cancel fall through to exit.
            self._session = None
            self._set_activity(None)  # nothing pending — a crash/cancel must not leave it spinning
            self.query_one("#prompt", HistoryInput).disabled = False  # the REPL stays usable
            self.query_one("#status", Static).update(self._status_text())

    async def _run_plan_goal(self, text: str) -> None:
        """Plan mode: stream the no-net-change plan → `PlanModal` → (on approve) stream the build.

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

        Sets `session` as the current one so an approval modal routes its decision back to it,
        and exposes the run task (`_run_task`) so `action_cancel` / a terminate signal can
        hard-cancel it. A hard cancel injects `CancelledError` at the in-flight `await` —
        which, with the async model client (ADR-0030), aborts the request at the socket — so
        the cockpit frees immediately instead of waiting on a busy agent. The cancelled run's
        state is marked `incomplete` so it records cleanly as history.

        Args:
            session: The per-goal `Session` to run and render.

        Returns:
            The terminal `TaskState` (marked `incomplete` if we cancelled the run).

        Raises:
            asyncio.CancelledError: If this worker itself is cancelled (not the run task) —
                re-raised untouched so cancellation is never swallowed.
        """
        self._session = session
        run = asyncio.create_task(session.run())
        self._run_task = run
        try:
            await self._consume(session)  # drains until the bus closes (on agent_end or cancel)
            return await run
        except asyncio.CancelledError:
            if not run.cancelled():
                raise  # our own worker was cancelled — propagate; don't swallow it
            if not session.state.terminal:  # we cancelled the run — give it a clean terminal record
                session.state.add_feedback("cancelled by user", kind="cancelled")
                session.state.outcome = "incomplete"
            return session.state
        finally:
            self._run_task = None

    def action_cancel(self) -> None:
        """Ctrl-C: copy a selection, else interrupt a live run, else quit.

        Copy comes first: the priority binding (needed so ctrl+c reaches the app past the
        focused input) otherwise shadows Textual's own `screen.copy_text`, so a
        select-then-ctrl+c gesture would never copy.

        With nothing selected, a *live* run is **hard-cancelled** — `_run_task.cancel()`
        injects `CancelledError` at the in-flight `await`, which (with the async model
        client, ADR-0030) aborts the request at the socket, so the cockpit frees instantly
        instead of waiting on a busy agent. The run records as cancelled history (`_observe`),
        leaving no live run — so the next ctrl+c quits.
        """
        selection = self.screen.get_selected_text()
        if selection:
            self.copy_to_clipboard(selection)
            return
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()  # instant interrupt; _observe marks it cancelled + frees the UI
            self._write("⏸ cancelled")
        else:
            self.exit()
