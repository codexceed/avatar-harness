"""Phase 3.1 Lane 2c — the cockpit modals: approval / diff / plan (ADR-0002 §6).

The interactive surfaces the shell pops. The approval modal turns an `ApprovalRequested`
into a `[y]/[a]/[d]` decision routed to the control plane (`resolve_approval`, with `[a]`
the scoped grant); the diff modal is a read-only viewer; the plan modal carries an
editable plan with approve/revise. Driven headlessly via a host `App` + `Pilot`; assertions
are on the modal *result contract*, not pixels.
"""

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.widgets import Static, TextArea

from avatar_harness.event_types import AgentStart, ApprovalRequested
from avatar_harness.tui.app import CockpitApp
from avatar_harness.tui.modals import (
    ApprovalChoice,
    ApprovalModal,
    DiffModal,
    PlanChoice,
    PlanModal,
)
from avatar_harness.tui.replay import ReplaySession


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
