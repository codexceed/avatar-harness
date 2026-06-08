from avatar_harness.config import HarnessConfig
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.tools import filesystem
from avatar_harness.tools.base import ToolDefinition, ToolRegistry, ToolRuntime
from avatar_harness.tools.commands import run_linter, run_tests
from avatar_harness.tools.edit import apply_patch
from avatar_harness.tools.filesystem import list_files, read_file
from avatar_harness.tools.search import search_repo
from avatar_harness.workspace import Workspace

_FIX = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
)


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in (read_file, list_files, search_repo):
        reg.register(tool)
    return reg


def _runtime(tmp_path) -> ToolRuntime:
    deps = RunDeps(workspace=Workspace(tmp_path), config=HarnessConfig(), cancellation=CancellationToken())
    return ToolRuntime(_registry(), deps)


def test_search_repo_finds_matches(tmp_path):
    (tmp_path / "a.py").write_text("def login():\n    pass\n", encoding="utf-8")
    result = _runtime(tmp_path).execute("search_repo", {"query": "login"})
    assert result.success
    assert "a.py" in result.content
    assert "login" in result.content


def test_search_repo_no_matches_is_clean_success(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = _runtime(tmp_path).execute("search_repo", {"query": "zzz_absent"})
    assert result.success
    assert result.content == ""


def test_list_files_matches_glob(tmp_path):
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    result = _runtime(tmp_path).execute("list_files", {"glob": "*.py"})
    assert result.success
    assert result.content == "a.py"


def test_list_files_dir_pattern_lists_contained_files(tmp_path):
    # A glob matching a directory expands to the files under it (the dogfood gap:
    # `rich*` matched a dir and was silently dropped, returning 0).
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sub").mkdir()
    (tmp_path / "pkg" / "sub" / "n.py").write_text("", encoding="utf-8")
    result = _runtime(tmp_path).execute("list_files", {"glob": "pkg"})
    assert result.success
    assert "pkg/m.py" in result.content
    assert "pkg/sub/n.py" in result.content


def test_list_files_result_is_capped_with_overflow_note(tmp_path, monkeypatch):
    # A directory match must not dump thousands of paths into context.
    monkeypatch.setattr(filesystem, "_LIST_CAP", 2)
    d = tmp_path / "many"
    d.mkdir()
    for i in range(5):
        (d / f"f{i}.py").write_text("", encoding="utf-8")
    result = _runtime(tmp_path).execute("list_files", {"glob": "many"})
    assert result.success
    lines = result.content.splitlines()
    assert len([line for line in lines if line.endswith(".py")]) == 2  # capped
    assert any("more" in line for line in lines)  # overflow noted
    assert "5 file" in result.summary  # full count preserved


def test_read_missing_file_is_model_correctable(tmp_path):
    result = _runtime(tmp_path).execute("read_file", {"path": "nope.txt"})
    assert result.success is False
    assert "not found" in (result.error or "")


def test_registry_exposes_only_phase_tools():
    reg = _registry()
    reg.register(
        ToolDefinition(
            name="apply_patch",
            description="edit-only",
            input_model=read_file.input_model,
            handler=read_file.handler,
            phases=frozenset({"editing"}),
        )
    )
    investigating = {t.name for t in reg.active_for_phase("investigating")}
    assert {"read_file", "search_repo", "list_files"} <= investigating
    assert "apply_patch" not in investigating


def test_unknown_tool_name_rejected(tmp_path):
    result = _runtime(tmp_path).execute("frobnicate", {})
    assert result.success is False
    assert "unknown tool" in (result.error or "")


def test_invalid_tool_input_fed_back(tmp_path):
    result = _runtime(tmp_path).execute("read_file", {})  # missing required 'path'
    assert result.success is False
    assert "invalid input" in (result.error or "")


# --- side-effecting tools (Phase 2) -------------------------------------


def _edit_runtime(root, **config_kw) -> ToolRuntime:
    reg = ToolRegistry()
    for tool in (read_file, apply_patch, run_tests, run_linter):
        reg.register(tool)
    config = HarnessConfig(**config_kw)
    deps = RunDeps(workspace=Workspace(root), config=config, cancellation=CancellationToken())
    return ToolRuntime(reg, deps)


def test_apply_patch_tool_reports_changed_files(git_repo):
    result = _edit_runtime(git_repo).execute("apply_patch", {"diff": _FIX})
    assert result.success
    assert result.files_changed == ["calc.py"]


def test_apply_patch_tool_stale_context_is_model_correctable(git_repo):
    stale = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-return a * b\n+return a + b\n"
    result = _edit_runtime(git_repo).execute("apply_patch", {"diff": stale})
    assert result.success is False  # returned, never raised into the loop
    assert result.error
    assert "a + b" not in (Workspace(git_repo).read("calc.py"))  # nothing written


def test_run_tests_passing_surfaces_evidence(git_repo):
    rt = _edit_runtime(git_repo, test_command="python -c \"print('5 passed')\"")
    result = rt.execute("run_tests", {})
    assert result.success
    assert "5 passed" in result.content


def test_run_tests_failure_is_not_a_tool_error(git_repo):
    # The command ran and reported failing tests — that is DATA, not a tool failure.
    rt = _edit_runtime(git_repo, test_command="python -c \"import sys; print('1 failed'); sys.exit(1)\"")
    result = rt.execute("run_tests", {})
    assert result.success is True
    assert "1 failed" in result.content


def test_run_tests_target_not_found_is_model_correctable(git_repo):
    # Usage error / target not found (pytest exit 4) is model-correctable, not a hard run.
    rt = _edit_runtime(git_repo, test_command='python -c "import sys; sys.exit(4)"')
    result = rt.execute("run_tests", {})
    assert result.success is False
    assert result.error


def test_run_linter_runs_configured_command(git_repo):
    rt = _edit_runtime(git_repo, lint_command="python -c \"print('All checks passed')\"")
    result = rt.execute("run_linter", {})
    assert result.success
    assert "All checks passed" in result.content
