"""Cockpit transcript legibility — per-tool color coding + the activity spinner.

Two additions to the shell (ADR-0002):

- **Consistent tool colors:** every `→ tool` / `✓ tool` transcript line styles the tool
  name with one stable, family-coded color (`tool_style`): inspect = blue, mutate =
  magenta, execute = yellow, contract = cyan; unknown tools hash deterministically onto
  the palette so a name keeps its color across runs.
- **The activity spinner:** a color-coded line above the input showing what the run is
  waiting on — "thinking" (pending model inference, green), "running <tool>" (in that
  tool's color), "verifying" (yellow), "waiting for approval" — cleared when the run ends.

Tested headlessly per the package invariant: assertions are on `tool_style`, the styled
`Text` `_format` returns, and the tracked `activity`/`activity_style` fields — never a
screen snapshot.
"""

import pytest

pytest.importorskip("textual")  # the cockpit lives behind the optional [textual] extra

from rich.text import Text

from avatar.event_types import (
    AgentEnd,
    AgentStart,
    ToolEnd,
    ToolStart,
    VerificationEnd,
    VerificationStart,
)
from jo.app import CockpitApp, tool_style
from jo.replay import ReplaySession


async def _settle(app, pilot) -> None:
    """Let the event-consuming worker drain and the UI flush."""
    await app.workers.wait_for_complete()
    await pilot.pause()


def _styles(line: Text) -> set[str]:
    """Every style carried by `line` — its base style plus all span styles."""
    return {str(line.style), *(str(span.style) for span in line.spans)}


# --- consistent per-tool color coding ------------------------------------------------------


def test_tool_style_is_stable_per_tool():
    assert tool_style("read_file") == tool_style("read_file")
    assert tool_style("run_tests") == tool_style("run_tests")


def test_tool_style_groups_families_and_separates_them():
    # One color per family, and the families are visually distinct.
    assert tool_style("read_file") == tool_style("search_repo") == tool_style("list_files")
    assert tool_style("write_file") == tool_style("str_replace") == tool_style("delete_file")
    assert tool_style("run_tests") == tool_style("run_linter") == tool_style("run_command")
    assert tool_style("declare_verification") == tool_style("alter_verification")
    families = {
        tool_style("read_file"),
        tool_style("write_file"),
        tool_style("run_tests"),
        tool_style("declare_verification"),
    }
    assert len(families) == 4  # no two families share a color


def test_tool_style_is_deterministic_for_unknown_tools():
    # A tool the map doesn't know still gets one stable color (hashing must not use the
    # per-process-salted builtin `hash`).
    assert tool_style("some_plugin_tool") == tool_style("some_plugin_tool")
    assert tool_style("some_plugin_tool")  # non-empty — always a usable style


def test_tool_start_and_end_lines_share_the_tool_color():
    # The "consistent" contract: the same tool wears the same color on its start and end
    # lines — and ONLY the tool name is colored; every other span stays dim as before.
    app = CockpitApp(ReplaySession([]))
    start = app._format(ToolStart(tool="read_file", input={"path": "x"}))
    ok = app._format(ToolEnd(tool="read_file", success=True, summary="1 line"))
    bad = app._format(ToolEnd(tool="run_tests", success=False, summary="exit=1"))
    assert isinstance(start, Text) and isinstance(ok, Text) and isinstance(bad, Text)
    assert tool_style("read_file") in _styles(start)
    assert tool_style("read_file") in _styles(ok)
    assert tool_style("run_tests") in _styles(bad)
    for line, tool in ((start, "read_file"), (ok, "read_file"), (bad, "run_tests")):
        assert _styles(line) <= {tool_style(tool), "dim"}  # nothing but the name is colored


# --- the activity spinner -------------------------------------------------------------------


async def test_spinner_shows_thinking_while_model_inference_is_pending():
    events = [AgentStart(goal="g")]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.activity is not None and "thinking" in app.activity
        assert app.activity_style  # color-coded, not unstyled


async def test_spinner_shows_running_tool_in_the_tool_color():
    events = [AgentStart(goal="g"), ToolStart(tool="run_tests", input={})]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.activity is not None and "running run_tests" in app.activity
        assert app.activity_style == tool_style("run_tests")  # ties into the tool coding


async def test_spinner_returns_to_thinking_after_a_tool_ends():
    events = [
        AgentStart(goal="g"),
        ToolStart(tool="read_file", input={}),
        ToolEnd(tool="read_file", success=True, summary="ok"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.activity is not None and "thinking" in app.activity


async def test_spinner_shows_verifying_during_verification():
    events = [AgentStart(goal="g"), VerificationStart()]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.activity is not None and "verifying" in app.activity


async def test_spinner_clears_when_the_run_ends():
    events = [
        AgentStart(goal="g"),
        VerificationStart(),
        VerificationEnd(passed=True, summary="ok"),
        AgentEnd(outcome="success"),
    ]
    app = CockpitApp(ReplaySession(events))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.activity is None  # idle — nothing pending
