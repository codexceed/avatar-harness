import json

import pytest

from avatar.cli import main, run_agent
from avatar.config import HarnessConfig
from avatar.events import Emitter
from avatar.model_client import FinalAnswer, ModelClient, ModelDecision, ToolCall
from avatar.workspace import DirtyWorkspaceError

_FIX = {"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}


class _OneShotModel(ModelClient):
    """A ModelClient that answers immediately — enough to exercise CLI wiring."""

    def decide(self, context: object) -> ModelDecision:
        return ModelDecision(action=FinalAnswer(answer="done"))


class _ScriptedModel(ModelClient):
    """Replays a fixed decision sequence; repeats the last when exhausted."""

    def __init__(self, decisions: list[ModelDecision]) -> None:
        self._decisions = decisions
        self._i = 0

    def decide(self, context: object) -> ModelDecision:
        decision = self._decisions[min(self._i, len(self._decisions) - 1)]
        self._i += 1
        return decision


def test_run_agent_rejects_dirty_workspace_by_default(git_repo):
    (git_repo / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    config = HarnessConfig(workspace_root=str(git_repo))
    with pytest.raises(DirtyWorkspaceError):
        run_agent("anything", config=config, emitter=Emitter(), model_client=_OneShotModel())


def test_run_agent_allow_dirty_opens_dirty_workspace(git_repo):
    # `--allow-dirty` threads through to Workspace(allow_dirty=True), so a tracked
    # modification no longer blocks the run (§15 acknowledged-dirty escape).
    (git_repo / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    config = HarnessConfig(workspace_root=str(git_repo))
    state = run_agent(
        "anything", config=config, emitter=Emitter(), model_client=_OneShotModel(), allow_dirty=True
    )
    assert state.iterations >= 1  # the loop ran instead of raising at open


def test_run_agent_edit_task_end_to_end(git_repo):
    # The whole product path for an edit task: patch -> verifier runs its command ->
    # success, with the command recorded. This is the integration the scripted unit
    # tests didn't exercise as one flow.
    test_cmd = 'python -c "import calc; assert calc.add(2, 3) == 5"'
    config = HarnessConfig(workspace_root=str(git_repo), test_command=test_cmd, lint_command="")
    model = _ScriptedModel(
        [
            ModelDecision(action=ToolCall(name="str_replace", input=_FIX)),
            ModelDecision(action=FinalAnswer(answer="fixed the sign error")),
        ]
    )
    state = run_agent("fix add()", config=config, emitter=Emitter(), model_client=model, task_kind="edit")
    assert state.outcome == "success"
    assert "calc.py" in state.files_modified
    assert any(test_cmd in c.command for c in state.commands_run)


def test_main_reports_via_artifact(git_repo, capsys):
    # main() renders its terminal output through ArtifactManager — one reporting
    # contract (status + files + verification), not a hand-rolled print.
    test_cmd = 'python -c "import calc; assert calc.add(2, 3) == 5"'
    model = _ScriptedModel(
        [
            ModelDecision(action=ToolCall(name="str_replace", input=_FIX)),
            ModelDecision(action=FinalAnswer(answer="fixed the sign error")),
        ]
    )
    config = HarnessConfig(workspace_root=str(git_repo), test_command=test_cmd, lint_command="")
    code = main(["fix add()"], config=config, model_client=model, task_kind="edit")
    out = capsys.readouterr().out
    assert code == 0
    assert "Status: success" in out
    assert "calc.py" in out


def test_main_task_kind_flag_selects_edit_contract(git_repo, capsys):
    """The batch CLI can select edit verification without an SDK-only call."""
    test_cmd = 'python -c "import calc; assert calc.add(2, 3) == 5"'
    config = HarnessConfig(workspace_root=str(git_repo), test_command=test_cmd, lint_command="")

    code = main(["fix add()", "--task-kind", "edit"], config=config, model_client=_edit_model())

    assert code == 0
    assert "Status: success" in capsys.readouterr().out


def test_main_friendly_error_on_dirty_workspace(git_repo, capsys, monkeypatch):
    # A dirty tree must produce a clear message + hint, not a raw traceback.
    (git_repo / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    monkeypatch.setenv("AVATAR_WORKSPACE_ROOT", str(git_repo))
    code = main(["explain something"])
    captured = capsys.readouterr()  # drain once — a second call returns empty
    assert code != 0
    assert "--allow-dirty" in captured.out + captured.err


def test_main_dirty_workspace_preserves_existing_latest_pointer(git_repo, tmp_path, monkeypatch):
    # A run that aborts before the first event (dirty workspace) must not swing the
    # latest.jsonl pointer: doing so eagerly would unlink the last usable pointer and
    # replace it with a symlink to a per-session log that is never created — a dangle.
    monkeypatch.chdir(tmp_path)
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    prior_log = events_dir / "deadbeef.jsonl"
    prior_log.write_text('{"type": "agent_start", "session_id": "deadbeef"}\n', encoding="utf-8")
    latest = events_dir / "latest.jsonl"
    latest.symlink_to(prior_log.name)

    (git_repo / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    monkeypatch.setenv("AVATAR_WORKSPACE_ROOT", str(git_repo))
    code = main(["explain something"])

    assert code == 2  # dirty-workspace exit
    assert latest.is_symlink()
    assert latest.resolve() == prior_log.resolve()  # pointer still points at the last usable log


def _edit_model() -> "_ScriptedModel":
    return _ScriptedModel(
        [
            ModelDecision(action=ToolCall(name="str_replace", input=_FIX)),
            ModelDecision(action=FinalAnswer(answer="fixed the sign error")),
        ]
    )


def test_main_default_log_is_per_session(git_repo, tmp_path, monkeypatch):
    # With no --log, the run writes to events/<session_id>.jsonl (not a shared static
    # file), every line carries that session_id, and the filename stem IS the id —
    # so grouping is intentional and the log is self-identifying.
    monkeypatch.chdir(tmp_path)
    test_cmd = 'python -c "import calc; assert calc.add(2, 3) == 5"'
    config = HarnessConfig(workspace_root=str(git_repo), test_command=test_cmd, lint_command="")
    code = main(["fix add()"], config=config, model_client=_edit_model(), task_kind="edit")
    assert code == 0

    events_dir = tmp_path / "events"
    session_logs = [p for p in events_dir.glob("*.jsonl") if p.name != "latest.jsonl"]
    assert len(session_logs) == 1  # one per-session file, not appended to a god-log
    log = session_logs[0]
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    session_ids = {e["session_id"] for e in events}
    assert session_ids == {log.stem}  # filename stem == the stamped session_id

    latest = events_dir / "latest.jsonl"
    assert latest.is_symlink()  # newest session is always reachable via a stable pointer
    assert latest.resolve() == log.resolve()


def test_main_respects_explicit_log_path(git_repo, tmp_path, monkeypatch):
    # An explicit --log opts out of the managed per-session layout: events land in the
    # given file and no latest.jsonl pointer is synthesized.
    monkeypatch.chdir(tmp_path)
    test_cmd = 'python -c "import calc; assert calc.add(2, 3) == 5"'
    config = HarnessConfig(workspace_root=str(git_repo), test_command=test_cmd, lint_command="")
    log_path = tmp_path / "custom" / "run.jsonl"
    code = main(
        ["fix add()", "--log", str(log_path)],
        config=config,
        model_client=_edit_model(),
        task_kind="edit",
    )
    assert code == 0
    assert log_path.exists()
    assert not (log_path.parent / "latest.jsonl").exists()


def test_batch_cli_does_not_own_interactive_flags(git_repo, capsys):
    # The cockpit launch was inverted out of the core CLI (the TUI ships its own
    # `jo-cli` entry point): the batch shell no longer knows --interactive/--auto.
    config = HarnessConfig(workspace_root=str(git_repo))
    for flag in ("--interactive", "--auto"):
        with pytest.raises(SystemExit) as excinfo:
            main(["task", flag], config=config, model_client=_OneShotModel())
        assert excinfo.value.code == 2  # argparse: unrecognized argument
        assert "unrecognized" in capsys.readouterr().err
