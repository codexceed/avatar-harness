import os
import subprocess
import sys

from pydantic import BaseModel

from avatar.config import HarnessConfig
from avatar.deps import CancellationToken, RunDeps
from avatar.state import PlannedCheck
from avatar.tools import default_registry, filesystem
from avatar.tools.base import (
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    ToolRuntime,
    is_edit_intent,
    phase_admits_tool,
)
from avatar.tools.commands import run_linter, run_tests
from avatar.tools.edit import (
    DeleteFileInput,
    StrReplaceInput,
    delete_file,
    str_replace,
    write_file,
)
from avatar.tools.filesystem import list_files, read_file
from avatar.tools.search import search_repo
from avatar.workspace import Workspace


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


def test_search_excludes_harness_journal(tmp_path):
    # The harness's own journal must not surface in search results (Q1).
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "j.jsonl").write_text("needle in the journal\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("needle in the app\n", encoding="utf-8")
    ws = Workspace(tmp_path, log_path="events/j.jsonl")
    deps = RunDeps(workspace=ws, config=HarnessConfig(), cancellation=CancellationToken())
    result = ToolRuntime(_registry(), deps).execute("search_repo", {"query": "needle"})
    assert "app.py" in result.content
    assert "events/j.jsonl" not in result.content


def test_search_repo_no_matches_is_clean_success(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = _runtime(tmp_path).execute("search_repo", {"query": "zzz_absent"})
    assert result.success
    assert result.content == ""


def test_search_repo_searches_tree_when_stdin_is_a_pipe(tmp_path):
    # rg invoked with no explicit path falls back to searching STDIN whenever stdin
    # isn't a tty — so in any embedding with a piped stdin (CI, cron, a supervising
    # process) the search blocks on the silent pipe until the 30s timeout and never
    # sees the tree (found following the tutorial, 2026-06-10). Run the handler in a
    # child whose stdin is an open, never-written pipe: it must return tree matches
    # promptly, proving the search reads the workspace, not stdin.
    (tmp_path / "a.py").write_text("def login():\n    pass\n", encoding="utf-8")
    code = (
        "from avatar.config import HarnessConfig\n"
        "from avatar.deps import CancellationToken, RunDeps\n"
        "from avatar.tools.base import ToolRegistry, ToolRuntime\n"
        "from avatar.tools.search import search_repo\n"
        "from avatar.workspace import Workspace\n"
        f"ws = Workspace({str(tmp_path)!r})\n"
        "deps = RunDeps(workspace=ws, config=HarnessConfig(), cancellation=CancellationToken())\n"
        "reg = ToolRegistry()\n"
        "reg.register(search_repo)\n"
        "result = ToolRuntime(reg, deps).execute('search_repo', {'query': 'login'})\n"
        "print('TREE-MATCH' if result.success and 'a.py' in result.content else f'MISS: {result!r}')\n"
    )
    read_fd, write_fd = os.pipe()  # parent keeps write_fd open: the child's stdin never EOFs
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            stdin=read_fd,
            capture_output=True,
            text=True,
            timeout=10,  # far below the tool's own 30s rg timeout: a stdin-read hang fails here
            check=False,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert "TREE-MATCH" in proc.stdout, proc.stdout + proc.stderr


def test_search_repo_caps_large_output_with_marker(tmp_path):
    # A search matching a large volume must be capped with a loud truncation marker, so a
    # huge result can't balloon ToolEnd.content (and thus the journal) — the 875 MB blowup
    # where search_repo recursed over a growing journal.jsonl (2026-06-15).
    big = "\n".join(f"match_{i} " + "x" * 200 for i in range(2000))  # ~420 KB of matches
    (tmp_path / "big.py").write_text(big + "\n", encoding="utf-8")
    result = _runtime(tmp_path).execute("search_repo", {"query": "match_"})
    assert result.success
    assert "[truncated:" in result.content
    assert len(result.content) <= 60_000  # capped far below the unbounded ~420 KB
    assert "truncated" in result.summary


def test_search_repo_small_output_not_truncated(tmp_path):
    # A normal-sized result is returned verbatim — no marker, no "truncated" note.
    (tmp_path / "a.py").write_text("def login():\n    pass\n", encoding="utf-8")
    result = _runtime(tmp_path).execute("search_repo", {"query": "login"})
    assert result.success
    assert "[truncated:" not in result.content
    assert "truncated" not in result.summary


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


def test_list_files_wildcards_skip_hidden(tmp_path):
    # pathlib glob matches dot-prefixed entries, unlike rg: a venv or .git inside the
    # workspace turned `*`/`**/*` into thousands of junk paths (4k+ in a 5-file
    # workspace, found following the tutorial 2026-06-10). Discovery mirrors rg's
    # default: hidden is invisible to wildcards, readable when explicitly named.
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "x.py").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / ".cache").mkdir()
    (tmp_path / "src" / ".cache" / "junk.py").write_text("", encoding="utf-8")
    runtime = _runtime(tmp_path)
    top = runtime.execute("list_files", {"glob": "*"})
    assert top.success
    # `src` (non-hidden dir) still expands per Phase 2.5; `.venv` no longer does.
    assert top.content.splitlines() == ["a.py", "src/m.py"]
    recursive = runtime.execute("list_files", {"glob": "**/*"})
    assert recursive.success
    assert recursive.content.splitlines() == ["a.py", "src/m.py"]  # nested hidden also skipped


def test_list_files_dir_expansion_skips_hidden_children(tmp_path):
    # The Phase-2.5 directory expansion must not walk into hidden children either.
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "y.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / ".cache").mkdir()
    (tmp_path / "pkg" / ".cache" / "z.py").write_text("", encoding="utf-8")
    result = _runtime(tmp_path).execute("list_files", {"glob": "pkg"})
    assert result.success
    assert result.content == "pkg/y.py"


def test_list_files_explicit_hidden_pattern_still_lists(tmp_path):
    # The escape hatch: a glob that NAMES a dot-prefixed segment opts into hidden —
    # `.github/*` must keep working (mirrors `rg pattern .github/` with an explicit path).
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("", encoding="utf-8")
    result = _runtime(tmp_path).execute("list_files", {"glob": ".github/**/*"})
    assert result.success
    assert result.content == ".github/workflows/ci.yml"


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


def _array_schemas(node):
    """Yield every JSON-schema subobject describing an array (recursively)."""
    if isinstance(node, dict):
        if node.get("type") == "array":
            yield node
        for value in node.values():
            yield from _array_schemas(value)
    elif isinstance(node, list):
        for item in node:
            yield from _array_schemas(item)


def test_read_file_schema_arrays_are_provider_agnostic():
    # ADR-0019: a tool input schema must not emit a `prefixItems` array without `items`
    # (pydantic's tuple rendering), which Gemini's request validator rejects. Every array
    # branch declares `items` and no branch uses `prefixItems`.
    schema = read_file.input_model.model_json_schema()
    arrays = list(_array_schemas(schema))
    assert arrays, "expected at least the line_range array branch"
    for arr in arrays:
        assert "items" in arr, f"array schema missing 'items': {arr}"
        assert "prefixItems" not in arr, f"array schema uses prefixItems: {arr}"


def test_read_file_line_range_reads_slice(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\nd\n", encoding="utf-8")
    result = _runtime(tmp_path).execute("read_file", {"path": "f.txt", "line_range": [2, 3]})
    assert result.success is True
    assert result.content == "b\nc\n"


def test_read_file_line_range_must_be_a_pair(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    result = _runtime(tmp_path).execute("read_file", {"path": "f.txt", "line_range": [1, 2, 3]})
    assert result.success is False  # model-correctable validation feedback, not a crash


def test_registry_exposes_only_phase_tools():
    reg = _registry()
    reg.register(
        ToolDefinition(
            name="str_replace",
            description="edit-only",
            input_model=read_file.input_model,
            handler=read_file.handler,
            phases=frozenset({"editing"}),
        )
    )
    investigating = {t.name for t in reg.active_for_phase("investigating")}
    assert {"read_file", "search_repo", "list_files"} <= investigating
    assert "str_replace" not in investigating


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
    for tool in (read_file, str_replace, write_file, run_tests, run_linter):
        reg.register(tool)
    config = HarnessConfig(**config_kw)
    deps = RunDeps(workspace=Workspace(root), config=config, cancellation=CancellationToken())
    return ToolRuntime(reg, deps)


def test_str_replace_tool_reports_changed_files(git_repo):
    result = _edit_runtime(git_repo).execute(
        "str_replace", {"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}
    )
    assert result.success
    assert result.files_changed == ["calc.py"]


def test_str_replace_tool_stale_context_is_model_correctable(git_repo):
    result = _edit_runtime(git_repo).execute(
        "str_replace", {"path": "calc.py", "old_string": "return a * b", "new_string": "return a + b"}
    )
    assert result.success is False  # returned, never raised into the loop
    assert result.error
    assert "a + b" not in (Workspace(git_repo).read("calc.py"))  # nothing written


# --- write_file (ADR-0003 B): first-class file creation -----------------------------------
#
# New-file creation gets a plain-content transport — no diff costume (a new-file hunk has
# no anchor content, so the unified-diff format is pure fragility there, per the dogfood
# incident). Modification stays string-anchored: without overwrite=true an existing target
# is refused toward str_replace, preserving the exact-match staleness invariant.


def test_write_file_creates_and_stages_new_file(git_repo):
    result = _edit_runtime(git_repo).execute(
        "write_file", {"path": "tools/chat.py", "content": "print('hi')\n"}
    )
    assert result.success
    assert result.files_changed == ["tools/chat.py"]
    ws = Workspace(git_repo, allow_dirty=True)
    assert ws.read("tools/chat.py") == "print('hi')\n"
    assert "tools/chat.py" in ws.diff()  # staged → visible to diff/artifact/verifier


def test_write_file_refuses_existing_without_overwrite(git_repo):
    result = _edit_runtime(git_repo).execute("write_file", {"path": "calc.py", "content": "x = 1\n"})
    assert result.success is False  # model-correctable, never raised into the loop
    assert "str_replace" in (result.error or "")  # steered to the string-anchored editor (ADR-0015)
    assert "return a - b" in Workspace(git_repo).read("calc.py")  # nothing clobbered


def test_write_file_overwrite_replaces_content(git_repo):
    result = _edit_runtime(git_repo).execute(
        "write_file", {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n", "overwrite": True}
    )
    assert result.success
    assert Workspace(git_repo, allow_dirty=True).read("calc.py") == "def add(a, b):\n    return a + b\n"


def test_write_file_outside_root_refused(git_repo):
    # Defense in depth at the Workspace chokepoint (the gate also blocks via declared paths).
    result = _edit_runtime(git_repo).execute("write_file", {"path": "../escape.py", "content": "x"})
    assert result.success is False
    assert "outside" in (result.error or "")
    assert not (git_repo.parent / "escape.py").exists()


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


def _plan_runtime(root, plan, **config_kw) -> ToolRuntime:
    """An edit runtime whose RunDeps carry a frozen verification plan (ADR-0007)."""
    reg = ToolRegistry()
    for tool in (read_file, str_replace, write_file, run_tests, run_linter):
        reg.register(tool)
    config_kw.setdefault("test_command", "")
    config_kw.setdefault("lint_command", "")
    deps = RunDeps(
        workspace=Workspace(root),
        config=HarnessConfig(**config_kw),
        cancellation=CancellationToken(),
        verification_plan=[PlannedCheck(**c) for c in plan],
    )
    return ToolRuntime(reg, deps)


def test_run_tests_falls_back_to_frozen_plan_command(git_repo):
    # With no config override, run_tests rides the frozen plan's test command —
    # the model exercises the same rubric the verifier will grade (ADR-0007).
    rt = _plan_runtime(
        git_repo,
        [
            {
                "name": "tests",
                "command": "python -c \"print('plan tests ran')\"",
                "kind": "test",
                "provenance": "Makefile:test",
            }
        ],
    )
    result = rt.execute("run_tests", {})
    assert result.success
    assert "plan tests ran" in result.content


def test_run_tests_appends_target_to_pytest_style_command(git_repo):
    # A pytest-style invocation accepts positional file targets — the target scopes it.
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    rt = _edit_runtime(git_repo, test_command="python -m pytest -q")
    result = rt.execute("run_tests", {"target": "tests/test_ok.py"})
    assert result.success
    assert "1 passed" in result.content


def test_run_tests_target_on_untargetable_command_is_model_correctable(git_repo):
    # PR-#40 review: appending a file target to `make test` produces
    # `make test tests/x.py` — broken. The mismatch is fed back as a
    # model-correctable error naming the declared contract, never executed.
    rt = _plan_runtime(
        git_repo,
        [{"name": "tests", "command": "make test", "kind": "test", "provenance": "Makefile:test"}],
    )
    result = rt.execute("run_tests", {"target": "tests/test_ok.py"})
    assert result.success is False
    assert "make test" in (result.error or "")
    assert "target" in (result.error or "")
    assert not any("tests/test_ok.py" in c.command for c in rt.deps.workspace.command_log)


def test_run_tests_with_no_command_or_plan_fails_legibly(git_repo):
    rt = _edit_runtime(git_repo, test_command="", lint_command="")
    result = rt.execute("run_tests", {})
    assert result.success is False
    assert "AVATAR_TEST_COMMAND" in (result.error or "")


def test_run_linter_with_no_command_or_plan_fails_legibly(git_repo):
    rt = _edit_runtime(git_repo, test_command="", lint_command="")
    result = rt.execute("run_linter", {})
    assert result.success is False
    assert "AVATAR_LINT_COMMAND" in (result.error or "")


# --- Phase 2.6 Lane B: tool-failure isolation (the runtime never raises into the loop). ---


class _BoomInput(BaseModel):
    """Empty input for a tool whose handler unconditionally raises."""


def _boom_handler(_args: _BoomInput, _deps: RunDeps) -> ToolResult:
    raise RuntimeError("handler exploded")


_boom = ToolDefinition(
    name="boom",
    description="A third-party-style tool whose handler raises.",
    input_model=_BoomInput,
    handler=_boom_handler,
    phases=frozenset({"investigating"}),
)


def _boom_runtime(tmp_path) -> ToolRuntime:
    reg = _registry()
    reg.register(_boom)
    deps = RunDeps(workspace=Workspace(tmp_path), config=HarnessConfig(), cancellation=CancellationToken())
    return ToolRuntime(reg, deps)


def test_tool_handler_exception_becomes_failed_result(tmp_path):
    # A handler that raises must come back as a failed ToolResult, not a propagated exception.
    result = _boom_runtime(tmp_path).execute("boom", {})
    assert result.tool_name == "boom"
    assert result.success is False
    assert result.error
    assert "handler exploded" in result.error


def test_runtime_never_raises_into_loop(tmp_path):
    # The whole point of the runtime: dispatching a crashing tool returns, never raises.
    rt = _boom_runtime(tmp_path)
    try:
        result = rt.execute("boom", {})
    except Exception as exc:  # pragma: no cover - a raise here is the failure under test
        raise AssertionError(f"runtime raised into the loop: {exc!r}") from exc
    assert result.success is False


def test_system_failure_is_surfaced_not_retried(tmp_path):
    # A systemic handler crash is surfaced as a failed result carrying the exception type —
    # distinct from a silent retry (which would yield a success once the cause cleared).
    result = _boom_runtime(tmp_path).execute("boom", {})
    assert result.success is False
    assert result.error is not None
    assert "RuntimeError" in result.error


# --- ADR-0005: transient edits in investigate (tier-1 admission rides an explicit rule) ---


def test_phase_admits_tier1_in_investigate_kind():
    # ADR-0005: tier-1 mutation is legal in an investigate task — admitted from
    # `investigating` via the explicit transient-edit rule (the verifier's net-zero-diff
    # contract is the enforcement point, not the gate).
    assert phase_admits_tool("investigating", "investigate", str_replace) is True
    assert phase_admits_tool("investigating", "investigate", write_file) is True


def test_transient_edit_rule_is_not_edit_intent():
    # The edit-intent phase bootstrap stays edit-kinds-only: an investigate str_replace
    # is NOT an edit intent (no `investigating -> editing` advance rides on it), and the
    # transient rule covers only tier 1 — command tools do not leak into investigating.
    assert is_edit_intent("investigate", str_replace) is False
    assert phase_admits_tool("investigating", "investigate", run_tests) is False


def test_registry_admits_tier1_for_investigate():
    # The registry mirror of the predicate: what `admitted_for` returns is exactly what
    # the ContextBuilder advertises, so the model is told about str_replace/write_file
    # in an investigate task — and only the tier-1 tools, not the command tools.
    reg = _registry()
    for tool in (str_replace, write_file, run_tests):
        reg.register(tool)
    admitted = {t.name for t in reg.admitted_for("investigating", "investigate")}
    assert {"str_replace", "write_file"} <= admitted
    assert "run_tests" not in admitted


# --- str_replace tool (ADR-0015) -------------------------------------------


def _edit_deps(git_repo) -> RunDeps:
    return RunDeps(workspace=Workspace(git_repo), config=HarnessConfig(), cancellation=CancellationToken())


def test_str_replace_edits_unique_match(git_repo):
    deps = _edit_deps(git_repo)
    r = str_replace.handler(
        StrReplaceInput(path="calc.py", old_string="return a - b", new_string="return a + b"), deps
    )
    assert r.success and "calc.py" in r.files_changed
    assert (git_repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"


def test_str_replace_missing_anchor_is_correctable(git_repo):
    r = str_replace.handler(
        StrReplaceInput(path="calc.py", old_string="return a * b", new_string="x"), _edit_deps(git_repo)
    )
    assert r.success is False
    assert "not found" in (r.error or "") and "exactly" in (r.error or "")


def test_str_replace_ambiguous_anchor_reports_count_and_widen_hint(git_repo):
    r = str_replace.handler(
        StrReplaceInput(path="calc.py", old_string="a", new_string="Z"), _edit_deps(git_repo)
    )
    assert r.success is False
    assert "matches" in (r.error or "") and "uniquely identifies ONE" in (r.error or "")


def test_str_replace_replace_all_succeeds(git_repo):
    r = str_replace.handler(
        StrReplaceInput(path="calc.py", old_string="a", new_string="Z", replace_all=True),
        _edit_deps(git_repo),
    )
    assert r.success
    assert (git_repo / "calc.py").read_text(encoding="utf-8") == "def Zdd(Z, b):\n    return Z - b\n"


def test_str_replace_empty_and_identical_are_guarded(git_repo):
    deps = _edit_deps(git_repo)
    empty = str_replace.handler(StrReplaceInput(path="calc.py", old_string="", new_string="x"), deps)
    assert empty.success is False and "non-empty" in (empty.error or "")
    noop = str_replace.handler(
        StrReplaceInput(path="calc.py", old_string="return a - b", new_string="return a - b"), deps
    )
    assert noop.success is False and "identical" in (noop.error or "")


def test_str_replace_missing_file_points_to_write_file(git_repo):
    r = str_replace.handler(
        StrReplaceInput(path="ghost.py", old_string="x", new_string="y"), _edit_deps(git_repo)
    )
    assert r.success is False and "write_file" in (r.error or "")


def test_str_replace_registered_and_editing_phase_tier1():
    reg = default_registry()
    admitted = {t.name for t in reg.admitted_for("editing", "edit")}
    assert "str_replace" in admitted  # advertised in the editing phase
    # tier-1 → it is an edit-intent tool, so it advances the phase like write_file
    assert (
        next(t for t in reg.admitted_for("editing", "edit") if t.name == "str_replace").permission_tier == 1
    )


# --- delete_file tool (ADR-0015) -------------------------------------------


def test_delete_file_removes_and_reports(git_repo):
    r = delete_file.handler(DeleteFileInput(path="calc.py"), _edit_deps(git_repo))
    assert r.success and "calc.py" in r.files_changed
    assert not (git_repo / "calc.py").exists()


def test_delete_file_missing_is_model_correctable(git_repo):
    r = delete_file.handler(DeleteFileInput(path="ghost.py"), _edit_deps(git_repo))
    assert r.success is False and "does not exist" in (r.error or "")


def test_delete_file_registered_and_editing_phase_tier1():
    reg = default_registry()
    admitted = {t.name for t in reg.admitted_for("editing", "edit")}
    assert "delete_file" in admitted
    assert (
        next(t for t in reg.admitted_for("editing", "edit") if t.name == "delete_file").permission_tier == 1
    )
