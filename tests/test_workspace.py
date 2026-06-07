import subprocess

import pytest

from avatar_harness.workspace import (
    DirtyWorkspaceError,
    PatchError,
    PathOutsideWorkspaceError,
    Workspace,
)


def _diff(path: str, old: str, new: str) -> str:
    """A minimal one-file unified diff replacing `old` with `new` at line 1."""
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


# A correct hunk for the two-line `calc.py` the git_repo fixture commits (the bug
# is on line 2, so it needs a context line — `_diff` only addresses line-1 files).
_CALC_FIX = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
)


def test_workspace_reads_inside_root(tmp_path):
    (tmp_path / "hello.txt").write_text("line1\nline2\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    assert ws.read("hello.txt") == "line1\nline2\n"


def test_workspace_refuses_path_outside_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    ws = Workspace(root)
    with pytest.raises(PathOutsideWorkspaceError):
        ws.read("../secret.txt")
    with pytest.raises(PathOutsideWorkspaceError):
        ws.read("/etc/passwd")


def test_workspace_refuses_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    (root / "link.txt").symlink_to(secret)
    ws = Workspace(root)
    with pytest.raises(PathOutsideWorkspaceError):
        ws.read("link.txt")


def test_workspace_read_respects_line_range(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    # 1-indexed, inclusive range.
    assert ws.read("f.txt", line_range=(2, 4)) == "b\nc\nd\n"


# --- patch application (§10) --------------------------------------------


def test_workspace_applies_multi_file_patch_atomically(git_repo):
    (git_repo / "b.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-q", "-m", "add b"], check=True, capture_output=True
    )
    ws = Workspace(git_repo)
    diff = _CALC_FIX + _diff("b.py", "x = 1", "x = 2")
    changed = ws.apply_patch(diff)
    assert set(changed) == {"calc.py", "b.py"}
    assert "a + b" in ws.read("calc.py")
    assert ws.read("b.py") == "x = 2\n"


def test_workspace_rejects_patch_touching_outside_root(git_repo):
    ws = Workspace(git_repo)
    escape = "--- a/../evil.py\n+++ b/../evil.py\n@@ -0,0 +1 @@\n+pwned = True\n"
    with pytest.raises(PathOutsideWorkspaceError):
        ws.apply_patch(escape)
    assert not (git_repo.parent / "evil.py").exists()


def test_workspace_stale_patch_applies_nothing(git_repo):
    ws = Workspace(git_repo)
    # Context that does not match the current file: a failed apply is model-correctable
    # and must leave the workspace byte-for-byte unchanged (all-or-nothing).
    before = ws.read("calc.py")
    stale = _diff("calc.py", "    return a * b", "    return a + b")
    with pytest.raises(PatchError):
        ws.apply_patch(stale)
    assert ws.read("calc.py") == before
    assert ws.diff() == ""


def test_workspace_patch_creates_and_deletes_only_when_explicit(git_repo):
    ws = Workspace(git_repo)
    create = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+created = True\n"
    changed = ws.apply_patch(create)
    assert "new.py" in changed
    assert ws.read("new.py") == "created = True\n"


def test_workspace_diff_reflects_applied_patch(git_repo):
    ws = Workspace(git_repo)
    assert ws.diff() == ""  # clean at open
    ws.apply_patch(_CALC_FIX)
    delta = ws.diff()
    assert "a + b" in delta
    assert "calc.py" in delta


# --- command execution (§15) --------------------------------------------


def test_workspace_run_captures_stdout_stderr_exit_code(tmp_path):
    ws = Workspace(tmp_path)
    out = ws.run("python -c \"import sys; print('hi'); sys.stderr.write('warn'); sys.exit(3)\"")
    assert out.exit_code == 3
    assert "hi" in out.stdout
    assert "warn" in out.stderr
    assert out.timed_out is False


def test_workspace_run_times_out(tmp_path):
    ws = Workspace(tmp_path)
    out = ws.run('python -c "import time; time.sleep(5)"', timeout=1)
    assert out.timed_out is True
    assert out.exit_code is None


# --- clean-start assertion (§15) ----------------------------------------


def test_workspace_open_accepts_clean_state_and_pins_head(git_repo):
    ws = Workspace(git_repo)  # clean checkout — must not raise
    assert ws.diff() == ""


def test_workspace_open_rejects_dirty_unless_allowed(git_repo):
    (git_repo / "calc.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
    with pytest.raises(DirtyWorkspaceError):
        Workspace(git_repo)
    # ...but an explicit acknowledgement is allowed.
    ws = Workspace(git_repo, allow_dirty=True)
    assert ws.diff() != ""
