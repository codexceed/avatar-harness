"""Sensitive-path denylist: secret reads/writes blocked at the gate (§11, Phase 2.5).

Prevention only — deterministic path-pattern matching, no content detection
(redaction is deliberately out, see PROGRESS Phase 2.5). The policy is central
(`PermissionPolicy` + `HarnessConfig`) and enforced over each tool's *declared*
paths, so it can't drift or be forgotten by a tool author.
"""

from typing import Literal

import pytest

from avatar_harness.config import HarnessConfig
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.permission import PermissionPolicy
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolRegistry, ToolRuntime
from avatar_harness.tools.edit import StrReplaceInput, str_replace, write_file
from avatar_harness.tools.filesystem import ListFilesInput, ReadFileInput, list_files, read_file
from avatar_harness.tools.search import search_repo
from avatar_harness.workspace import SensitivePathError, Workspace


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


def test_str_replace_denied_when_target_is_sensitive(git_repo):
    # The denylist spans every declared path, not just reads — an edit writing `.env` is blocked.
    raw = {"path": ".env", "old_string": "OLD=0", "new_string": "LEAK=1"}
    perm = PermissionPolicy().check(str_replace, raw, _state(), Workspace(git_repo))
    assert perm.blocked is True


def test_write_file_denied_when_target_is_sensitive(git_repo):
    # write_file (ADR-0003 B) declares its target path, so the central denylist covers it
    # exactly like apply_patch — a new mutating tool gets the policy for free (§11).
    raw = {"path": ".env", "content": "LEAK=1"}
    perm = PermissionPolicy().check(write_file, raw, _state(), Workspace(git_repo))
    assert perm.blocked is True


def test_sensitive_writes_still_denied_in_investigate(git_repo):
    # ADR-0005 admits tier-1 tools in investigate tasks, but the relaxation removes ONLY
    # the kind gate: the sensitive-path denylist (and workspace chokepoint behind it)
    # still refuses every secret-targeting write, whatever the task kind.
    state = _state("investigate")
    ws = Workspace(git_repo)
    write = PermissionPolicy().check(write_file, {"path": ".env", "content": "LEAK=1"}, state, ws)
    assert write.blocked is True
    raw = {"path": ".env", "old_string": "OLD=0", "new_string": "LEAK=1"}
    replace = PermissionPolicy().check(str_replace, raw, state, ws)
    assert replace.blocked is True


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


def test_str_replace_declares_its_targets():
    assert "x.py" in list(str_replace.paths(StrReplaceInput(path="x.py", old_string="a", new_string="b")))


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


def test_search_exclusion_uses_canonical_matcher(git_repo):
    # ripgrep's `-g` globs and `path_is_sensitive` diverge on slash patterns (fnmatch's
    # `*` crosses `/`, gitignore's does not). search_repo must defer to the canonical
    # matcher so its exclusions agree with the gate/workspace for the same config list.
    nested = git_repo / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "x.pem").write_text("S=sk-zzz\n", encoding="utf-8")
    (git_repo / "ok.py").write_text("S = 'sk-zzz'\n", encoding="utf-8")
    cfg = HarnessConfig(sensitive_path_globs=["a/*.pem"])
    deps = RunDeps(
        workspace=Workspace(git_repo, sensitive_path_globs=cfg.sensitive_path_globs),
        config=cfg,
        cancellation=CancellationToken(),
    )
    reg = ToolRegistry()
    reg.register(search_repo)
    result = ToolRuntime(reg, deps).execute("search_repo", {"query": "sk-zzz"})
    assert result.success
    assert "ok.py" in result.content
    assert "a/b/x.pem" not in result.content  # canonical matcher excludes it; rg's -g would not


# --- defense in depth: the workspace refuses sensitive RESOLVED paths ----------
# The gate alone is single-layer (bypassed by a non-gated caller) and checks the
# *requested* path, not the resolved one — so a symlink launders the secret. The
# Workspace chokepoint enforces on the resolved path, closing both holes.


def test_workspace_read_refuses_sensitive_file(tmp_path):
    (tmp_path / ".env").write_text("API_KEY=sk-SECRET\n", encoding="utf-8")
    with pytest.raises(SensitivePathError):
        Workspace(tmp_path).read(".env")


def test_workspace_read_refuses_symlink_to_sensitive(tmp_path):
    # The bypass: an innocuously-named symlink pointing at a denylisted file. The
    # check is on the RESOLVED path, so the symlink can't launder the secret.
    (tmp_path / ".env").write_text("API_KEY=sk-SECRET\n", encoding="utf-8")
    (tmp_path / "notes.txt").symlink_to(tmp_path / ".env")
    with pytest.raises(SensitivePathError):
        Workspace(tmp_path).read("notes.txt")


def test_read_file_tool_refuses_sensitive_without_gate(tmp_path):
    # Even a direct tool call (no permission gate) cannot read a secret.
    (tmp_path / ".env").write_text("API_KEY=sk-SECRET\n", encoding="utf-8")
    deps = RunDeps(workspace=Workspace(tmp_path), config=HarnessConfig(), cancellation=CancellationToken())
    reg = ToolRegistry()
    reg.register(read_file)
    result = ToolRuntime(reg, deps).execute("read_file", {"path": ".env"})
    assert result.success is False
    assert "sk-SECRET" not in result.content


def test_workspace_apply_patch_refuses_sensitive_target(git_repo):
    diff = "--- a/.env\n+++ b/.env\n@@ -0,0 +1 @@\n+LEAK=1\n"
    with pytest.raises(SensitivePathError):
        Workspace(git_repo).apply_patch(diff)
