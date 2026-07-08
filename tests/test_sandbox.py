"""The execution sandbox (ADR-0042): hermetic execution at the `Workspace.run` seam.

`hermetic-env` scrubs the child environment to a language-neutral allowlist, so an inherited
`PYTEST_ADDOPTS`/`PYTHONPATH` (Threat C) can no longer rig a verification command — while
`PATH`/`VIRTUAL_ENV` survive so `python -m pytest` still resolves. `none` reproduces today's
fully-inherited environment exactly. The seam is a pure transform: it seals the substrate
without moving execution off the `Workspace` chokepoint.
"""

import subprocess
import sys

import pytest

from avatar.config import HarnessConfig
from avatar.sandbox import (
    ExecSpec,
    HermeticEnv,
    NoSandbox,
    SandboxExec,
    hermetic_env,
    make_sandbox,
)
from avatar.workspace import Workspace

_HAS_GIT = subprocess.run(["git", "--version"], capture_output=True, check=False).returncode == 0


# --- the allowlist, in isolation -----------------------------------------------------------


def test_hermetic_env_drops_injection_vectors_keeps_interpreter():
    source = {
        "PATH": "/usr/bin",
        "VIRTUAL_ENV": "/venv",
        "LC_ALL": "en_US.UTF-8",
        "PYTEST_ADDOPTS": "-p no:randomly",  # the classic rig
        "PYTHONPATH": "/tmp/evil",
        "NODE_OPTIONS": "--require /tmp/evil.js",
        "RUBYOPT": "-r/tmp/evil",
        "LD_PRELOAD": "/tmp/evil.so",
        "SECRET_TOKEN": "hunter2",
    }
    scrubbed = hermetic_env(source)
    assert scrubbed == {"PATH": "/usr/bin", "VIRTUAL_ENV": "/venv", "LC_ALL": "en_US.UTF-8"}
    # everything injectable is gone by construction — no per-language blocklist
    for dropped in ("PYTEST_ADDOPTS", "PYTHONPATH", "NODE_OPTIONS", "RUBYOPT", "LD_PRELOAD", "SECRET_TOKEN"):
        assert dropped not in scrubbed


# --- the backends' prepare() contract ------------------------------------------------------


def test_no_sandbox_is_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:randomly")
    spec = NoSandbox().prepare(["echo", "hi"], tmp_path)
    assert spec.argv == ["echo", "hi"]  # argv unchanged
    assert spec.env.get("PYTEST_ADDOPTS") == "-p no:randomly"  # full env inherited (today's behavior)
    assert spec.preexec_fn is None


def test_hermetic_env_backend_scrubs_but_keeps_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:randomly")
    monkeypatch.setenv("PATH", "/usr/bin")
    spec = HermeticEnv().prepare(["python", "-m", "pytest"], tmp_path)
    assert spec.argv == ["python", "-m", "pytest"]  # identity argv
    assert "PYTEST_ADDOPTS" not in spec.env  # the substrate is sealed
    assert spec.env.get("PATH") == "/usr/bin"  # interpreter still resolvable
    assert spec.preexec_fn is None  # rlimits off (thread-safe; ADR-0042 Increment 1)


def test_sandbox_exec_wraps_argv_and_denies_network(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:randomly")
    spec = SandboxExec().prepare(["true"], tmp_path)
    assert spec.argv[0].endswith("sandbox-exec")  # argv is launcher-wrapped
    assert "-p" in spec.argv and spec.argv[-1] == "true"
    profile = spec.argv[spec.argv.index("-p") + 1]
    assert "deny network*" in profile
    assert "PYTEST_ADDOPTS" not in spec.env  # env scrubbed too


def test_sandbox_exec_allow_network_skips_the_wrap(tmp_path):
    spec = SandboxExec(allow_network=True).prepare(["true"], tmp_path)
    assert spec.argv == ["true"]  # no launcher when network is permitted


# --- the factory ---------------------------------------------------------------------------


def test_make_sandbox_routes_modes():
    assert isinstance(make_sandbox("none"), NoSandbox)
    assert isinstance(make_sandbox("hermetic-env"), HermeticEnv)
    assert isinstance(make_sandbox("sandbox-exec"), SandboxExec)


def test_make_sandbox_default_config_is_hermetic_env():
    # the shipped default flips the substrate closed for every harness run
    assert HarnessConfig().sandbox_mode == "hermetic-env"
    assert isinstance(make_sandbox(HarnessConfig().sandbox_mode), HermeticEnv)


def test_make_sandbox_rejects_unbuilt_and_unknown():
    with pytest.raises(NotImplementedError):
        make_sandbox("container")  # ADR-0042 Increment 2
    with pytest.raises(NotImplementedError):
        make_sandbox("bwrap")
    with pytest.raises(ValueError):
        make_sandbox("nonsense")


# --- end to end through the Workspace seam -------------------------------------------------


@pytest.mark.skipif(not _HAS_GIT, reason="Workspace pins a git baseline")
def test_planted_env_cannot_flip_a_verifier_command(monkeypatch, tmp_path):
    """The ADR's headline integration: an inherited env var that would decide a command's
    exit code is powerless once the seam is sealed."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # A command whose PASS is entirely at the mercy of an inherited env var — exactly the shape
    # of a rigged `pytest` run that a planted `PYTEST_ADDOPTS` would coerce green.
    rigged = f'{sys.executable} -c "import os,sys; sys.exit(0 if os.environ.get(\'RIGGED\') else 1)"'
    monkeypatch.setenv("RIGGED", "1")

    inherited = Workspace(tmp_path, allow_dirty=True, sandbox=NoSandbox())
    sealed = Workspace(tmp_path, allow_dirty=True, sandbox=HermeticEnv())

    assert inherited.run(rigged).exit_code == 0  # today: the rig wins
    assert sealed.run(rigged).exit_code == 1  # sealed: the var never reaches the child


@pytest.mark.skipif(not _HAS_GIT, reason="Workspace pins a git baseline")
def test_sealed_workspace_still_runs_ordinary_commands(tmp_path):
    # scrubbing must not break a legitimate command that needs only PATH
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    ws = Workspace(tmp_path, allow_dirty=True, sandbox=HermeticEnv())
    out = ws.run(f'{sys.executable} -c "print(2 + 2)"')
    assert out.exit_code == 0
    assert out.stdout.strip() == "4"


@pytest.mark.skipif(not _HAS_GIT, reason="Workspace pins a git baseline")
def test_default_workspace_is_unsealed_back_compat(monkeypatch, tmp_path):
    # a bare Workspace(root) (no sandbox arg) inherits the environment — nothing changes for
    # the read-only inspection sites that construct one directly.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("RIGGED", "1")
    ws = Workspace(tmp_path, allow_dirty=True)
    rigged = f'{sys.executable} -c "import os,sys; sys.exit(0 if os.environ.get(\'RIGGED\') else 1)"'
    assert ws.run(rigged).exit_code == 0


def test_exec_spec_is_frozen():
    spec = ExecSpec(argv=["true"], env={})
    with pytest.raises((AttributeError, TypeError)):
        spec.argv = ["false"]  # type: ignore[misc]  # frozen — a spec is a value
