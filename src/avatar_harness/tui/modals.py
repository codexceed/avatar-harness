"""Cockpit modals — approval / diff / plan (Phase 3.1 Lane 2c, ADR-0002 §6).

The interactive surfaces the cockpit pops over the shell. Each is a Textual `ModalScreen`
that `dismiss`es a small typed result the caller routes:

- `ApprovalModal` → `ApprovalChoice` — `[y]` allow once · `[a]` always (scoped grant) ·
  `[d]` deny · `[v]` toggle the command/diff detail. The cockpit feeds the choice to
  `session.resolve_approval(allow=…, remember=…)` — `[a]` sets `remember=True` (PR #10 grant).
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
    *blocking* prompt sitting above the transcript — not a transcript line. The keys
    `[y]/[a]/[d]/[v]` and the equivalent clickable buttons share one dismiss contract.

    Args:
        tool: The tool name awaiting approval.
        reason: The gate's reason, shown to the human.
        tool_input: The proposed call arguments (the command/diff detail, shown via `[v]`).
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

    def compose(self) -> Iterator[Widget]:
        """Render the bounded dialog: summary, the (toggleable) detail, buttons, key hints.

        Yields:
            The dialog container holding the prompt, detail, buttons, and hint widgets.
        """
        with VerticalScroll(id="approval_dialog"):
            yield Static(f"{self.tool} wants to run — {self.reason}", id="approval_prompt")
            yield Static(str(self.tool_input), id="approval_detail")
            with Horizontal(id="approval_buttons"):
                yield Button("Allow once", id="approve-once")
                yield Button("Always", id="always")
                yield Button("Deny", id="deny")
                yield Button("View", id="view")
            yield Static("[y] allow once   [a] always (scoped)   [d] deny   [v] view", id="approval_hints")

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
        """Allow and remember a scoped grant for matching calls this session."""
        self.dismiss(ApprovalChoice(allow=True, remember=True))

    def action_deny(self) -> None:
        """Deny this call."""
        self.dismiss(ApprovalChoice(allow=False, remember=False))

    def action_toggle_view(self) -> None:
        """Toggle the command/diff detail line."""
        self._show_detail = not self._show_detail
        self.query_one("#approval_detail", Static).display = self._show_detail


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
            yield Static(self.diff_text or "(no changes)")

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
