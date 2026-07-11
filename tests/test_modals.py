"""Phase 3.1 Lane 2c — the cockpit modals: approval / diff / plan (ADR-0002 §6).

The interactive surfaces the shell pops. The approval modal turns an `ApprovalRequested`
into a `[y]/[a]/[d]` decision routed to the control plane (`resolve_approval`, with `[a]`
the scoped grant); the diff modal is a read-only viewer; the plan modal carries an
editable plan with approve/revise. Driven headlessly via a host `App` + `Pilot`; assertions
are on the modal *result contract*, not pixels.
"""

from typing import cast

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.widgets import Static, TextArea

from avatar.event_types import AgentStart, ApprovalRequested
from jo.app import CockpitApp
from jo.modals import (
    ApprovalChoice,
    ApprovalModal,
    DiffModal,
    PlanChoice,
    PlanModal,
)
from jo.replay import ReplaySession


class _Host(App):
    """A minimal host that pushes one modal and captures its dismiss result."""

    def __init__(self, modal) -> None:
        super().__init__()
        self._modal = modal
        self.result: object = "UNSET"

    def on_mount(self) -> None:
        self.run_worker(self._show())

    async def _show(self) -> None:
        self.result = await self.push_screen_wait(self._modal)


async def test_approval_modal_allow_once():
    host = _Host(ApprovalModal(tool="run_command", reason="tier 3", tool_input={"command": "pytest"}))
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
    assert host.result == ApprovalChoice(allow=True, remember=False)


async def test_approval_modal_always_scoped():
    host = _Host(ApprovalModal(tool="run_command", reason="tier 3", tool_input={"command": "pytest"}))
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
    assert host.result == ApprovalChoice(allow=True, remember=True)  # [a] → scoped grant


async def test_approval_modal_shows_exact_command_up_front():
    # The human must see the precise command WITHOUT pressing View (the dogfood gap:
    # the prompt named the tool/tier but hid `chmod +x chatbot.py` behind [v]).
    host = _Host(
        ApprovalModal(tool="run_command", reason="tier 3", tool_input={"command": "pip install openai"})
    )
    async with host.run_test() as pilot:
        await pilot.pause()
        modal = cast(ApprovalModal, host.screen)
        assert "pip install openai" in modal._command_text()  # the exact command is in the prompt
        assert modal.query_one("#approval_command", Static).display is not False  # shown, not toggled


async def test_approval_modal_renders_amendment_legibly():
    # An alter_verification amendment (ADR-0038/0039) shows the rationale + proposed checks — not a
    # raw args dict — so a human can tell a genuine design change from a real failure papered over.
    host = _Host(
        ApprovalModal(
            tool="alter_verification",
            reason="tier 3",
            tool_input={
                "checks": [{"command": "python -m pytest test_y.py", "kind": "test"}],
                "rationale": "the row-collapse behavior changed by design",
            },
        )
    )
    async with host.run_test() as pilot:
        await pilot.pause()
        modal = cast(ApprovalModal, host.screen)
        text = modal._command_text()
        assert "row-collapse behavior changed by design" in text  # the obsolescence rationale
        assert "python -m pytest test_y.py" in text  # the proposed replacement check
        assert "immutable floor" in text  # the un-amendable anchor is surfaced


async def test_amendment_modal_offers_no_always_option():
    # A contract amendment is ratified per occurrence: a standing `[a]` grant would let the
    # model re-move its own goalposts silently for the rest of the session (ADR-0038/0039).
    # The Always button and its `[a]` hint are absent, and the key itself is inert.
    host = _Host(
        ApprovalModal(
            tool="alter_verification",
            reason="tier 3",
            tool_input={"checks": [{"command": "python -m pytest test_y.py"}], "rationale": "r"},
        )
    )
    async with host.run_test() as pilot:
        await pilot.pause()
        assert not host.screen.query("#always")  # no Always button offered
        hints = str(host.screen.query_one("#approval_hints", Static).render())
        assert "always" not in hints  # no [a] hint either
        await pilot.press("a")  # the key must be inert — not a hidden grant path
        await pilot.pause()
        assert host.result == "UNSET"  # nothing dismissed; the modal still awaits a decision
        await pilot.press("y")
        await pilot.pause()
    assert host.result == ApprovalChoice(allow=True, remember=False)  # allow-once still works


async def test_approval_modal_deny():
    host = _Host(ApprovalModal(tool="run_command", reason="tier 3", tool_input={"command": "pytest"}))
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
    assert host.result == ApprovalChoice(allow=False, remember=False)


async def test_approval_modal_allow_once_button():
    host = _Host(ApprovalModal(tool="run_command", reason="tier 3", tool_input={"command": "pytest"}))
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#approve-once")
        await pilot.pause()
    assert host.result == ApprovalChoice(allow=True, remember=False)


async def test_approval_modal_always_button():
    host = _Host(ApprovalModal(tool="run_command", reason="tier 3", tool_input={"command": "pytest"}))
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#always")
        await pilot.pause()
    assert host.result == ApprovalChoice(allow=True, remember=True)  # always → scoped grant


async def test_approval_modal_deny_button():
    host = _Host(ApprovalModal(tool="run_command", reason="tier 3", tool_input={"command": "pytest"}))
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#deny")
        await pilot.pause()
    assert host.result == ApprovalChoice(allow=False, remember=False)


async def test_approval_modal_view_button_toggles_without_dismiss():
    host = _Host(ApprovalModal(tool="run_command", reason="tier 3", tool_input={"command": "pytest"}))
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#view")  # reveals the detail, does NOT dismiss
        await pilot.pause()
        assert host.screen.query_one("#approval_detail", Static).display is True
        assert host.result == "UNSET"  # view never resolves the modal
        await pilot.press("d")
        await pilot.pause()
    assert host.result == ApprovalChoice(allow=False, remember=False)


async def test_cockpit_pops_approval_modal_on_event():
    ev = ApprovalRequested(approval_id="a1", tool="run_command", input={"command": "pytest -q"})
    app = CockpitApp(ReplaySession([AgentStart(goal="g"), ev]))
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, ApprovalModal)  # auto-popped on the event
        assert app.screen.tool == "run_command"


async def test_diff_modal_shows_diff_text():
    diff = "diff --git a/x b/x\n+added line\n"
    host = _Host(DiffModal(diff))
    async with host.run_test() as pilot:
        await pilot.pause()
        assert isinstance(host.screen, DiffModal)
        assert host.screen.diff_text == diff  # read-only viewer carries the diff
        await pilot.press("escape")
        await pilot.pause()
    assert host.result is None  # dismissed without a decision


async def test_plan_modal_approve_returns_plan():
    host = _Host(PlanModal("1. read\n2. edit"))
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#approve")
        await pilot.pause()
    assert host.result == PlanChoice(approved=True, text="1. read\n2. edit")


async def test_plan_modal_revise_returns_edit():
    host = _Host(PlanModal("draft plan"))
    async with host.run_test() as pilot:
        await pilot.pause()
        host.screen.query_one(TextArea).text = "revised plan"
        await pilot.click("#revise")
        await pilot.pause()
    assert host.result == PlanChoice(approved=False, text="revised plan")  # revise returns the edit


async def test_approval_modal_survives_markup_metacharacters_in_command():
    # Regression (tetris_grok4): a model-authored command with Textual markup metacharacters
    # (`[a=1 b=]` — multiple key= pairs, trailing empty value) crashed the modal with a
    # `MarkupError` during layout, tearing down the whole cockpit. It must render verbatim.
    host = _Host(
        ApprovalModal(tool="run_command", reason="tier 3", tool_input={"command": "grep -E '[a=1 b=]' f"})
    )
    async with host.run_test() as pilot:
        await pilot.pause()  # layout runs here — pre-fix this raised MarkupError
        assert "[a=1 b=]" in cast(ApprovalModal, host.screen)._command_text()  # shown verbatim
        await pilot.press("y")
        await pilot.pause()
    assert host.result == ApprovalChoice(allow=True, remember=False)  # mounted, rendered, routed


async def test_diff_modal_survives_markup_metacharacters():
    # A diff routinely contains `[` (list literals, regex classes); the viewer must not parse it.
    host = _Host(DiffModal(diff_text="+ pattern = grep -E '[a=1 b=]'  # markup metacharacters"))
    async with host.run_test() as pilot:
        await pilot.pause()  # layout runs here — pre-fix this raised MarkupError
        await pilot.press("escape")
        await pilot.pause()
    assert host.result is None  # rendered + dismissed cleanly, no MarkupError
