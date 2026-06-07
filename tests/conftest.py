import subprocess
from pathlib import Path

import pytest

from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.filesystem import list_files, read_file
from avatar_harness.tools.search import search_repo


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
