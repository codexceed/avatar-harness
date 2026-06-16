import subprocess
from pathlib import Path

import pytest

from avatar.model_client import ModelClient, ModelDecision
from avatar.tools.base import ToolRegistry
from avatar.tools.filesystem import list_files, read_file
from avatar.tools.search import search_repo


class ScriptedModel(ModelClient):
    """A `ModelClient` stub that replays pre-built decisions; repeats the last when exhausted.

    The shared scripted-model factory for the whole suite: construct one with the turn-by-turn
    decisions a test needs (`ScriptedModel([tool_call, ..., final_answer])`). Imported from
    `conftest` rather than redefined per module.
    """

    def __init__(self, decisions: list[ModelDecision]) -> None:
        self._decisions = decisions
        self._i = 0

    def decide(self, context: object) -> ModelDecision:
        decision = self._decisions[min(self._i, len(self._decisions) - 1)]
        self._i += 1
        return decision


class CyclingModel(ModelClient):
    """A `ModelClient` stub that replays a fixed cycle of decisions forever.

    Useful when a test runs an unknown number of identical tasks (e.g. one full cycle per
    plan task) and must keep producing fresh decisions rather than repeating the last.
    """

    def __init__(self, cycle: list[ModelDecision]) -> None:
        self._cycle = cycle
        self._i = 0

    def decide(self, context: object) -> ModelDecision:
        decision = self._cycle[self._i % len(self._cycle)]
        self._i += 1
        return decision


@pytest.fixture
def read_registry() -> ToolRegistry:
    """A registry with the Phase 1 tier-0 read tools registered."""
    reg = ToolRegistry()
    for tool in (read_file, list_files, search_repo):
        reg.register(tool)
    return reg


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """An initialized git repo with one committed file, on a pinned HEAD.

    Patch/diff and clean-start tests need a real repo: `Workspace` pins HEAD at
    open and `apply_patch` shells out to `git apply`.
    """
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


@pytest.fixture(autouse=True)
def _offline_mode_routing(monkeypatch):
    """Unit tests never classify over the network.

    `classifier_model` defaults ON (the product choice), so a bare `ReplSession` would
    construct a real `ModeClassifier` and call the configured endpoint from any test
    that resolves a mode. Clearing the env var disables it suite-wide; tests that want
    classification inject a fake-transport `ModeClassifier` or pass `classifier_model=`
    explicitly (kwargs beat env).
    """
    monkeypatch.setenv("AVATAR_CLASSIFIER_MODEL", "")
