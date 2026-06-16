"""Interactive journaling — the cockpit path persists its event stream (closing the gap).

Batch mode writes `events/<session_id>.jsonl` via the `EventLog` subscriber; the
`jo-cli` cockpit streamed events to the TUI transcript only — nothing reached
disk, so an interactive run was unreplayable after the fact. The fix threads the
already-built write-ahead `JsonlEventJournal` (Lane 1) through `ReplSession` into each
per-goal `Session` — **one journal per REPL sitting**, shared by reference the way
`grants` already are (each goal's `bus.close()` closes the handle; `append` reopens it).
`Harness.session()` gains the same `journal=` seam for SDK parity.
"""

from conftest import ScriptedModel

from avatar.config import HarnessConfig
from avatar.event_types import load_events
from avatar.harness import Harness
from avatar.journal import JsonlEventJournal
from avatar.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar.session_state import ReplSession
from avatar.tools.base import ToolRegistry
from avatar.tools.filesystem import read_file
from jo import cli as jo_cli
from jo.app import CockpitApp


def _read_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(read_file)
    return reg


def _harness(tmp_path, decisions) -> Harness:
    config = HarnessConfig(workspace_root=str(tmp_path))
    return Harness(config=config, model=ScriptedModel(decisions), tools=_read_registry())


def _investigate_decisions(n_goals: int = 1) -> list[ModelDecision]:
    return [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ] * n_goals


# --- ReplSession threads the journal ------------------------------------------------------


def test_repl_session_threads_journal_into_per_goal_sessions(tmp_path):
    journal = JsonlEventJournal(tmp_path / "events.jsonl")
    repl = ReplSession(_harness(tmp_path, []), journal=journal)
    session = repl.start("explain x in app.py")
    assert session.bus.journal is journal  # the per-goal bus commits to the shared journal


def test_repl_session_without_journal_stays_in_memory(tmp_path):
    repl = ReplSession(_harness(tmp_path, []))
    session = repl.start("explain x in app.py")
    assert session.bus.journal is None  # default unchanged: no disk writes unless wired


async def test_repl_goals_share_one_journal_file(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    journal = JsonlEventJournal(tmp_path / "events.jsonl")
    repl = ReplSession(_harness(tmp_path, _investigate_decisions(n_goals=2)), journal=journal)
    await repl.submit("explain x in app.py")
    await repl.submit("explain x again in app.py")  # second goal: append reopens the closed handle

    events = load_events(journal.path)
    assert sum(1 for e in events if e.type == "agent_end") == 2  # both goals journaled
    assert len({e.session_id for e in events}) == 2  # per-goal sessions distinguishable in one file


# --- Harness.session() SDK parity ---------------------------------------------------------


async def test_harness_session_accepts_journal(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    journal = JsonlEventJournal(tmp_path / "events.jsonl")
    session = _harness(tmp_path, _investigate_decisions()).session("explain x in app.py", journal=journal)
    assert session.bus.journal is journal
    await session.run()
    assert [e.type for e in load_events(journal.path)][-1] == "agent_end"


# --- CLI launch wiring --------------------------------------------------------------------


def _launch_interactive(git_repo, monkeypatch, argv: list[str]) -> ReplSession:
    """Run `jo_cli.main` with a stubbed (non-blocking) cockpit; return the launched ReplSession."""
    launched: dict = {}

    def _fake_run(self, *args, **kwargs):
        launched["repl"] = self.repl

    monkeypatch.setattr(CockpitApp, "run", _fake_run)
    config = HarnessConfig(workspace_root=str(git_repo))
    assert jo_cli.main(argv, config=config, model_client=ScriptedModel([])) == 0
    return launched["repl"]


def test_jo_cli_wires_journal_at_default_path(git_repo, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # the default events/ dir is cwd-relative
    repl = _launch_interactive(git_repo, monkeypatch, [])
    assert repl.journal is not None
    expected = tmp_path / "events" / f"{repl.state.session_id}.jsonl"
    assert repl.journal.path.resolve() == expected.resolve()  # cwd-relative, like batch mode
    assert repl.journal.path.exists()  # the flight recorder exists from launch


def test_jo_cli_respects_log_flag(git_repo, monkeypatch, tmp_path):
    log = tmp_path / "custom" / "run.jsonl"
    repl = _launch_interactive(git_repo, monkeypatch, ["--log", str(log)])
    assert repl.journal is not None
    assert repl.journal.path == log
