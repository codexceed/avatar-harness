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
from collections.abc import Callable, Iterator

from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

from avatar import (
    AgentEnd,
    AgentStart,
    ApprovalRequested,
    DecisionError,
    DirtyWorkspaceError,
    HarnessEvent,
    ModelDecisionEvent,
    ModelUpdate,
    PhaseChanged,
    ReplSession,
    Session,
    TaskState,
    ToolEnd,
    ToolStart,
    VerificationEnd,
)
from jo.modals import ApprovalChoice, ApprovalModal, DiffModal, PlanModal


class HistoryInput(Input):
    """An `Input` that recalls previously submitted prompts with the ↑/↓ arrows.

    The cockpit calls `remember` on every submit to append the line (de-duplicated
    against the most recent entry). `↑` walks toward older entries, `↓` toward newer;
    stepping past the newest restores the draft that was in progress before browsing
    began. History is in-memory for the sitting — the journal stays the durable record.

    Up/down are unbound on Textual's single-line `Input`, so binding them here is
    additive (Textual merges `BINDINGS` across the MRO) and steals nothing.

    Args:
        placeholder: The greyed-out prompt shown while the field is empty.
        id: The widget id (the cockpit queries `#prompt`).
    """

    BINDINGS = [  # noqa: RUF012 — Textual's binding-list contract
        Binding("up", "history_prev", show=False),
        Binding("down", "history_next", show=False),
    ]

    def __init__(self, *, placeholder: str = "", id: str | None = None) -> None:
        super().__init__(placeholder=placeholder, id=id)
        self._history: list[str] = []
        self._cursor: int | None = None  # None ⇒ not browsing; else an index into _history
        self._draft = ""  # the in-progress line, stashed when browsing starts

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
        """↑ — recall the previous (older) submitted prompt, stashing the draft first."""
        if not self._history:
            return
        if self._cursor is None:  # entering history: remember what was being typed
            self._draft = self.value
            self._cursor = len(self._history)
        if self._cursor > 0:
            self._cursor -= 1
            self._recall(self._history[self._cursor])

    def action_history_next(self) -> None:
        """↓ — move toward newer prompts; past the newest restores the stashed draft."""
        if self._cursor is None:
            return
        self._cursor += 1
        if self._cursor >= len(self._history):
            self._cursor = None
            self._recall(self._draft)
        else:
            self._recall(self._history[self._cursor])

    def _recall(self, text: str) -> None:
        """Replace the field with `text` and park the cursor at its end.

        Args:
            text: The recalled line to show.
        """
        self.value = text
        self.cursor_position = len(text)


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
        self._run_task: asyncio.Task[TaskState] | None = None  # the in-flight per-goal run, for ctrl+c

    def compose(self) -> Iterator[Widget]:
        """Lay out the three regions.

        Yields:
            The status bar, transcript log, and input widgets.
        """
        yield Static(self._status_text(), id="status")
        yield RichLog(id="transcript", highlight=False, markup=False, wrap=True)
        yield HistoryInput(placeholder="Ask, or describe a change…", id="prompt")

    def on_mount(self) -> None:
        """Observe mode: drain the fixed stream; drive mode waits for input. Install signal handlers."""
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
        in cyan, model turns (`● agent`) in green, tool I/O dim — the label is
        model-agnostic (`you`/`agent`), since this harness runs non-Claude models too.

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
            return Text(f"→ {event.tool} {event.input}", style="dim")
        if isinstance(event, ToolEnd):
            mark = "✓" if event.success else "✗"
            return Text(f"{mark} {event.tool}: {event.summary or event.content}", style="dim")
        if isinstance(event, ModelUpdate):
            line = Text("● agent", style="bold green")
            line.append(f"  {event.delta}", style="")
            return line
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Route a submitted prompt: drive the REPL (drive mode) or the observe-mode callback.

        Args:
            event: The Textual `Input.Submitted` message carrying the prompt text.
        """
        text = event.value.strip()
        prompt = self.query_one("#prompt", HistoryInput)
        prompt.value = ""
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
            self.query_one("#prompt", Input).disabled = True
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
            self.query_one("#prompt", Input).disabled = False  # the REPL stays usable
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
