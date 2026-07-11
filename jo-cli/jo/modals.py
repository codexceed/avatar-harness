"""Cockpit modals — approval / diff / plan (Phase 3.1 Lane 2c, ADR-0002 §6).

The interactive surfaces the cockpit pops over the shell. Each is a Textual `ModalScreen`
that `dismiss`es a small typed result the caller routes:

- `ApprovalModal` → `ApprovalChoice` — `[y]` allow once · `[a]` always (scoped grant) ·
  `[d]` deny · `[v]` toggle the command/diff detail. The cockpit feeds the choice to
  `session.resolve_approval(allow=…, remember=…)` — `[a]` sets `remember=True` (PR #10 grant).
  A contract amendment (`alter_verification`) never offers `[a]`: each amendment is ratified
  by a human (ADR-0038/0039); the core refuses such grants too, this just keeps the UI honest.
- `DiffModal` → `None` — a read-only scrollable diff viewer; dismissed with escape/enter.
- `PlanModal` → `PlanChoice` — an editable plan with **approve** / **revise**; revise returns
  the edited text (the plan-mode *flow* that consumes it is the 3.2 tail).
"""

from collections.abc import Iterator
from dataclasses import dataclass

from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Static, TextArea

from avatar import UNGRANTABLE_TOOLS


@dataclass(frozen=True)
class ApprovalChoice:
    """The decision returned by `ApprovalModal`: allow/deny, and whether to remember it."""

    allow: bool
    remember: bool


@dataclass(frozen=True)
class PlanChoice:
    """The decision returned by `PlanModal`: approved vs revised, with the (possibly edited) text."""

    approved: bool
    text: str


class ApprovalModal(ModalScreen[ApprovalChoice]):
    """Render a gated call and collect a `[y]/[a]/[d]` decision (control plane, §13).

    A bounded, centered dialog with a solid background and border so it reads as a
    *blocking* prompt sitting above the transcript — not a transcript line. The exact
    command (or call arguments) is shown up front so the human knows precisely what they
    are approving; `[v]` toggles the full raw arguments. The keys `[y]/[a]/[d]/[v]` and
    the equivalent clickable buttons share one dismiss contract.

    Args:
        tool: The tool name awaiting approval.
        reason: The gate's reason, shown to the human.
        tool_input: The proposed call arguments; its `command` (or the whole dict) is
            shown up front, with the full raw arguments behind `[v]`.
    """

    BINDINGS = [  # noqa: RUF012 — Textual's binding-list contract
        ("y", "allow_once", "allow once"),
        ("a", "allow_always", "always (scoped)"),
        ("d", "deny", "deny"),
        ("v", "toggle_view", "view"),
    ]

    DEFAULT_CSS = """
    ApprovalModal {
        align: center middle;
    }
    ApprovalModal #approval_dialog {
        width: 80%;
        max-width: 90;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $warning;
    }
    ApprovalModal #approval_prompt {
        text-style: bold;
        margin-bottom: 1;
    }
    ApprovalModal #approval_command {
        background: $panel;
        color: $text;
        text-style: bold;
        padding: 0 1;
        margin-bottom: 1;
    }
    ApprovalModal #approval_detail {
        display: none;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
        margin-bottom: 1;
    }
    ApprovalModal #approval_buttons {
        height: auto;
        align-horizontal: center;
    }
    ApprovalModal #approval_buttons Button {
        width: auto;
        min-width: 0;
        margin: 0 1;
    }
    """

    def __init__(self, *, tool: str, reason: str, tool_input: dict) -> None:
        super().__init__()
        self.tool = tool
        self.reason = reason
        self.tool_input = tool_input
        self._show_detail = False
        # A contract amendment must be ratified per occurrence — a standing grant would
        # let the model re-move its own goalposts silently (ADR-0038/0039). The core's
        # Session refuses to store/match such grants; hiding [a] keeps the UI honest,
        # derived from the same core constant so the two seams cannot drift.
        self._grantable = tool not in UNGRANTABLE_TOOLS

    def compose(self) -> Iterator[Widget]:
        """Render the bounded dialog: summary, the (toggleable) detail, buttons, key hints.

        Yields:
            The dialog container holding the prompt, detail, buttons, and hint widgets.
        """
        with VerticalScroll(id="approval_dialog"):
            prompt = (
                f"{self.tool} — the model wants to AMEND its verification contract"
                if self.tool == "alter_verification"
                else f"{self.tool} wants to run — {self.reason}"
            )
            # markup=False: the prompt/command/args are model-authored and routinely contain Rich
            # markup metacharacters (`[` in `python -c '...[...]'`, `grep -E '[...]'`, list literals).
            # Parsing them as markup crashed the modal (`MarkupError`); show them verbatim instead.
            yield Static(prompt, id="approval_prompt", markup=False)
            yield Static(self._command_text(), id="approval_command", markup=False)  # exact command
            yield Static(str(self.tool_input), id="approval_detail", markup=False)
            with Horizontal(id="approval_buttons"):
                yield Button("Allow once", id="approve-once")
                if self._grantable:
                    yield Button("Always", id="always")
                yield Button("Deny", id="deny")
                yield Button("View", id="view")
            hints = (
                "[y] allow once   [a] always (scoped)   [d] deny   [v] view"
                if self._grantable
                else "[y] allow once   [d] deny   [v] view"
            )
            yield Static(hints, id="approval_hints", markup=False)  # the `[y]`/`[d]` keys are literal

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route a button to the same decision its key binding makes (view toggles, no dismiss).

        Args:
            event: The Textual `Button.Pressed` message identifying which action was chosen.
        """
        if event.button.id == "approve-once":
            self.action_allow_once()
        elif event.button.id == "always":
            self.action_allow_always()
        elif event.button.id == "deny":
            self.action_deny()
        elif event.button.id == "view":
            self.action_toggle_view()

    def action_allow_once(self) -> None:
        """Allow this call once (no grant)."""
        self.dismiss(ApprovalChoice(allow=True, remember=False))

    def action_allow_always(self) -> None:
        """Allow and remember a scoped grant for matching calls this session.

        Inert for an ungrantable call (a contract amendment): the `[a]` binding is
        class-level, so the guard lives here rather than in the (static) bindings list.
        """
        if not self._grantable:
            return
        self.dismiss(ApprovalChoice(allow=True, remember=True))

    def action_deny(self) -> None:
        """Deny this call."""
        self.dismiss(ApprovalChoice(allow=False, remember=False))

    def action_toggle_view(self) -> None:
        """Toggle the full raw-arguments line (the command itself is always shown)."""
        self._show_detail = not self._show_detail
        self.query_one("#approval_detail", Static).display = self._show_detail

    def _command_text(self) -> str:
        """The exact thing being approved, shown up front.

        A command tool's `command` is shown verbatim (`$ <command>`); an `alter_verification`
        amendment gets a legible rationale + proposed-checks rendering (ADR-0038/0039); any other
        tool's call shows its arguments dict, so the human always sees precisely what will run.

        Returns:
            The command line (or the arguments) to display.
        """
        if self.tool == "alter_verification":
            return self._amendment_text()
        command = self.tool_input.get("command")
        return f"$ {command}" if command is not None else str(self.tool_input)

    def _amendment_text(self) -> str:
        """Render a verification-contract amendment so the human ratifies, not rubber-stamps.

        Surfaces the model's obsolescence rationale and the proposed replacement checks — the
        signal a reviewer needs to tell a genuine design change from a real failure being papered
        over. The immutable floor is never amendable, so it is noted but not up for approval.

        Returns:
            The multi-line amendment summary to display.
        """
        rationale = self.tool_input.get("rationale", "")
        checks = self.tool_input.get("checks") or []
        lines = [f"Rationale: {rationale}", "", "Proposed contract:"]
        lines += [f"  $ {c.get('command', c) if isinstance(c, dict) else c}" for c in checks]
        lines.append("")
        lines.append("(The immutable floor beneath the contract cannot be amended away.)")
        return "\n".join(lines)


class DiffModal(ModalScreen[None]):
    """A read-only, scrollable diff viewer (dismissed with escape/enter).

    Args:
        diff_text: The unified-diff text to display.
    """

    BINDINGS = [  # noqa: RUF012 — Textual's binding-list contract
        ("escape", "close", "close"),
        ("enter", "close", "close"),
    ]

    def __init__(self, diff_text: str) -> None:
        super().__init__()
        self.diff_text = diff_text

    def compose(self) -> Iterator[Widget]:
        """Render the diff inside a scroll container.

        Yields:
            The scrollable diff body.
        """
        with VerticalScroll(id="diff_body"):
            # markup=False: a diff routinely contains `[` (list literals, regexes, array indexing),
            # which Rich would parse as markup and crash the viewer — show it verbatim.
            yield Static(self.diff_text or "(no changes)", markup=False)

    def action_close(self) -> None:
        """Dismiss the viewer (no decision)."""
        self.dismiss(None)


class PlanModal(ModalScreen[PlanChoice]):
    """An editable plan with **approve** / **revise** (the approved/edited text is returned).

    Args:
        plan: The proposed plan text (pre-filled, editable).
    """

    def __init__(self, plan: str) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> Iterator[Widget]:
        """Render the editable plan and the approve/revise buttons.

        Yields:
            The plan editor and the two action buttons.
        """
        yield TextArea(self.plan, id="plan_text")
        yield Button("Approve", id="approve")
        yield Button("Revise", id="revise")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Resolve the plan: approve or revise, carrying the current (editable) text.

        Args:
            event: The Textual `Button.Pressed` message identifying which action was chosen.
        """
        text = self.query_one("#plan_text", TextArea).text
        self.dismiss(PlanChoice(approved=event.button.id == "approve", text=text))
