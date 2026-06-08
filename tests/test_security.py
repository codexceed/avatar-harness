"""Sensitive-path denylist: secret reads/writes blocked at the gate (§11, Phase 2.5).

Prevention only — deterministic path-pattern matching, no content detection
(redaction is deliberately out, see PROGRESS Phase 2.5). The policy is central
(`PermissionPolicy` + `HarnessConfig`) and enforced over each tool's *declared*
paths, so it can't drift or be forgotten by a tool author.
"""

from typing import Literal

from avatar_harness.config import HarnessConfig
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.permission import PermissionPolicy
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolRegistry, ToolRuntime
from avatar_harness.tools.edit import ApplyPatchInput, apply_patch
from avatar_harness.tools.filesystem import ListFilesInput, ReadFileInput, list_files, read_file
from avatar_harness.tools.search import search_repo
from avatar_harness.workspace import Workspace


def _state(kind: Literal["edit", "investigate", "test_only"] = "edit") -> TaskState:
    return TaskState(goal="x", task_kind=kind)


# --- the gate blocks sensitive reads/writes -----------------------------------


def test_read_file_denied_for_sensitive_path(git_repo):
    # The exact failure from the dogfood: reading `.env` must be refused at the gate.
    perm = PermissionPolicy().check(read_file, {"path": ".env"}, _state(), Workspace(git_repo))
    assert perm.blocked is True
    assert perm.reason


def test_read_file_denied_for_pem_anywhere(git_repo):
    # A bare pattern (no slash) matches by path component at any depth.
    perm = PermissionPolicy().check(read_file, {"path": "certs/server.pem"}, _state(), Workspace(git_repo))
    assert perm.blocked is True


def test_read_file_denied_for_secret_inside_ssh_dir(git_repo):
    perm = PermissionPolicy().check(read_file, {"path": ".ssh/id_rsa"}, _state(), Workspace(git_repo))
    assert perm.blocked is True


def test_non_sensitive_path_still_allowed(git_repo):
    perm = PermissionPolicy().check(read_file, {"path": "calc.py"}, _state(), Workspace(git_repo))
    assert perm.blocked is False


def test_apply_patch_denied_when_target_is_sensitive(git_repo):
    # The denylist spans every declared path, not just reads — a patch writing `.env` is blocked.
    diff = "--- a/.env\n+++ b/.env\n@@ -0,0 +1 @@\n+LEAK=1\n"
    perm = PermissionPolicy().check(apply_patch, {"diff": diff}, _state(), Workspace(git_repo))
    assert perm.blocked is True


# --- configurable via HarnessConfig -------------------------------------------


def test_harness_config_has_default_denylist():
    assert any(".env" in g for g in HarnessConfig().sensitive_path_globs)


def test_denylist_configured_via_harness_config(git_repo):
    # An override list REPLACES the default set: a custom pattern blocks, others pass.
    policy = PermissionPolicy(HarnessConfig(sensitive_path_globs=["*.secret"]).sensitive_path_globs)
    assert policy.check(read_file, {"path": "data.secret"}, _state(), Workspace(git_repo)).blocked is True
    assert policy.check(read_file, {"path": "calc.py"}, _state(), Workspace(git_repo)).blocked is False


# --- tools self-declare their path-bearing inputs -----------------------------


def test_read_file_declares_its_path():
    assert list(read_file.paths(ReadFileInput(path="a/b.py"))) == ["a/b.py"]


def test_apply_patch_declares_its_targets():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert "x.py" in list(apply_patch.paths(ApplyPatchInput(diff=diff)))


def test_pathless_tool_declares_no_paths():
    # The default extractor is a pass-through: list_files (a glob, not a path) declares none.
    assert list(list_files.paths(ListFilesInput())) == []


# --- search must not become a denylist bypass ---------------------------------


def test_search_repo_excludes_sensitive_files(git_repo):
    # `search_repo("<secret>")` would otherwise grep a secret file and surface the
    # secret — exclude denylisted files from results (path exclude, not detection).
    # Uses a non-hidden sensitive file (*.pem); rg skips dotfiles on its own.
    (git_repo / "server.pem").write_text("PRIVATE_KEY=sk-or-secret-123\n", encoding="utf-8")
    (git_repo / "app.py").write_text("KEY = 'sk-or-secret-123'\n", encoding="utf-8")
    deps = RunDeps(workspace=Workspace(git_repo), config=HarnessConfig(), cancellation=CancellationToken())
    reg = ToolRegistry()
    reg.register(search_repo)
    result = ToolRuntime(reg, deps).execute("search_repo", {"query": "sk-or-secret-123"})
    assert result.success
    assert "app.py" in result.content  # an ordinary file is still searchable
    assert "server.pem" not in result.content  # the secret file is excluded
