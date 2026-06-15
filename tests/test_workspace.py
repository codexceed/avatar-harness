import subprocess

import pytest

from avatar_harness.workspace import (
    AmbiguousMatchError,
    DirtyWorkspaceError,
    EmptyAnchorError,
    MatchNotFoundError,
    PatchError,
    PathOutsideWorkspaceError,
    Workspace,
)

_CALC = "def add(a, b):\n    return a - b\n"  # the file the git_repo fixture commits


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


def test_workspace_hides_harness_journal_from_tools(tmp_path):
    # The harness writes its journal under the workspace; the file tools hide exactly the
    # active log + its `latest.jsonl` pointer (Q1) — never a whole `events/` dir a project owns.
    (tmp_path / "events").mkdir()
    for name in ("abc.jsonl", "latest.jsonl", "user_data.jsonl"):
        (tmp_path / "events" / name).write_text("{}\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    ws = Workspace(tmp_path, log_path="events/abc.jsonl")

    listed = ws.list_files("**/*")
    assert "app.py" in listed
    assert "events/abc.jsonl" not in listed  # the active journal — hidden
    assert "events/latest.jsonl" not in listed  # its pointer — hidden
    assert "events/user_data.jsonl" in listed  # a real project file — visible
    with pytest.raises(FileNotFoundError):
        ws.read("events/abc.jsonl")  # invisible to read, too


def test_workspace_hides_nothing_without_log_path(tmp_path):
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "abc.jsonl").write_text("{}\n", encoding="utf-8")
    ws = Workspace(tmp_path)  # no log_path → no journal to hide
    assert "events/abc.jsonl" in ws.list_files("**/*")


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


def test_workspace_diff_includes_created_files(git_repo):
    # A created file is part of the task's deliverable — it must appear in the diff,
    # or the secret scan and artifact are blind to brand-new files (apply via
    # `git apply --index` so the new file is tracked, not left untracked).
    ws = Workspace(git_repo)
    ws.apply_patch('--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+TOKEN = "x"\n')
    delta = ws.diff()
    assert "new.py" in delta
    assert "TOKEN" in delta


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


def test_workspace_run_missing_binary_is_failed_output_not_crash(tmp_path):
    # ADR-0007 robustness floor: a missing binary must surface as a failed
    # CommandOutput (shell convention exit 127), never raise into the loop.
    ws = Workspace(tmp_path)
    out = ws.run("definitely-not-a-real-binary-xyz --version")
    assert out.exit_code == 127
    assert out.timed_out is False
    assert "not found" in out.stderr
    assert ws.command_log[-1] is out  # still recorded at the chokepoint


def test_workspace_run_empty_command_is_failed_output(tmp_path):
    ws = Workspace(tmp_path)
    out = ws.run("")
    assert out.exit_code == 127
    assert out.stderr


def test_workspace_run_unparseable_command_is_failed_output(tmp_path):
    # shlex chokes on the unbalanced quote — that too is a legible failure, not a raise.
    ws = Workspace(tmp_path)
    out = ws.run("echo 'unclosed")
    assert out.exit_code == 127
    assert out.stderr


def test_workspace_records_command_log(tmp_path):
    # Every command run through the workspace is recorded at the single chokepoint,
    # so the runner can surface the full command ledger (§7 commands_run, §14 artifact).
    ws = Workspace(tmp_path)
    ws.run('python -c "pass"')
    ws.run('python -c "import sys; sys.exit(2)"')
    assert [c.command for c in ws.command_log] == [
        'python -c "pass"',
        'python -c "import sys; sys.exit(2)"',
    ]
    assert ws.command_log[1].exit_code == 2


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


def test_workspace_open_ignores_untracked_files(git_repo):
    # Untracked files never enter `git diff HEAD`, so they can't pollute the diff
    # baseline (§15) — the clean-start guard must not trip on them.
    (git_repo / "scratch.txt").write_text("not tracked\n", encoding="utf-8")
    ws = Workspace(git_repo)  # must not raise
    assert ws.diff() == ""


# --- string-anchored replace (ADR-0015) ------------------------------------


def test_workspace_replace_unique_match(git_repo):
    ws = Workspace(git_repo)
    rel = ws.replace("calc.py", "return a - b", "return a + b")
    assert rel == "calc.py"
    assert (git_repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert "+    return a + b" in ws.diff()  # the edit is a DERIVED diff vs the pinned baseline


def test_workspace_replace_missing_anchor_raises_and_leaves_file_unchanged(git_repo):
    ws = Workspace(git_repo)
    with pytest.raises(MatchNotFoundError):
        ws.replace("calc.py", "return a * b", "return a + b")  # not in the file
    assert (git_repo / "calc.py").read_text(encoding="utf-8") == _CALC  # byte-for-byte unchanged


def test_workspace_replace_ambiguous_anchor_raises_with_count(git_repo):
    ws = Workspace(git_repo)
    with pytest.raises(AmbiguousMatchError) as excinfo:
        ws.replace("calc.py", "a", "Z")  # 'a' occurs several times
    assert excinfo.value.count >= 2
    assert (git_repo / "calc.py").read_text(encoding="utf-8") == _CALC  # unchanged


def test_workspace_replace_all_changes_every_occurrence(git_repo):
    ws = Workspace(git_repo)
    ws.replace("calc.py", "a", "Z", replace_all=True)
    assert (git_repo / "calc.py").read_text(encoding="utf-8") == "def Zdd(Z, b):\n    return Z - b\n"


def test_workspace_replace_missing_file_raises(git_repo):
    ws = Workspace(git_repo)
    with pytest.raises(FileNotFoundError):
        ws.replace("nope.py", "x", "y")


def test_workspace_replace_confined_to_root(git_repo):
    ws = Workspace(git_repo)
    with pytest.raises(PathOutsideWorkspaceError):
        ws.replace("../escape.py", "x", "y")


def test_workspace_replace_empty_anchor_rejected_at_chokepoint(git_repo):
    # The empty-anchor guard lives at the chokepoint, so a DIRECT SDK caller can't corrupt a
    # file (`str.replace("", x)` would insert between every character). The tool layer is not
    # the only line of defense.
    ws = Workspace(git_repo)
    with pytest.raises(EmptyAnchorError):
        ws.replace("calc.py", "", "X", replace_all=True)
    assert (git_repo / "calc.py").read_text(encoding="utf-8") == _CALC  # byte-for-byte unchanged


def test_workspace_replace_empty_new_deletes_the_span(git_repo):
    # An empty `new` is the deliberate span-deletion path — the intentional asymmetry with
    # an empty `old` (rejected). Pinned so it's contract, not incidental behavior.
    ws = Workspace(git_repo)
    ws.replace("calc.py", "    return a - b\n", "")
    assert (git_repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n"


# --- file deletion (ADR-0015) ----------------------------------------------


def test_workspace_remove_tracked_file_shows_in_diff(git_repo):
    ws = Workspace(git_repo)
    rel = ws.remove("calc.py")
    assert rel == "calc.py"
    assert not (git_repo / "calc.py").exists()
    assert "calc.py" in ws.diff() and "-def add(a, b):" in ws.diff()  # deletion is in the diff


def test_workspace_remove_transient_file_nets_to_zero(git_repo):
    ws = Workspace(git_repo)
    ws.write_file("scratch.py", "probe = 1\n")  # created + staged
    assert "scratch.py" in ws.diff()
    ws.remove("scratch.py")
    assert not (git_repo / "scratch.py").exists()
    assert ws.diff() == ""  # create-then-delete nets back to the pinned baseline


def test_workspace_remove_missing_file_raises(git_repo):
    ws = Workspace(git_repo)
    with pytest.raises(FileNotFoundError):
        ws.remove("nope.py")


def test_workspace_remove_confined_to_root(git_repo):
    ws = Workspace(git_repo)
    with pytest.raises(PathOutsideWorkspaceError):
        ws.remove("../escape.py")
