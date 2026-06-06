import pytest

from avatar_harness.workspace import PathOutsideWorkspace, Workspace


def test_workspace_reads_inside_root(tmp_path):
    (tmp_path / "hello.txt").write_text("line1\nline2\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    assert ws.read("hello.txt") == "line1\nline2\n"


def test_workspace_refuses_path_outside_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    ws = Workspace(root)
    with pytest.raises(PathOutsideWorkspace):
        ws.read("../secret.txt")
    with pytest.raises(PathOutsideWorkspace):
        ws.read("/etc/passwd")


def test_workspace_refuses_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    (root / "link.txt").symlink_to(secret)
    ws = Workspace(root)
    with pytest.raises(PathOutsideWorkspace):
        ws.read("link.txt")


def test_workspace_read_respects_line_range(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    # 1-indexed, inclusive range.
    assert ws.read("f.txt", line_range=(2, 4)) == "b\nc\nd\n"
