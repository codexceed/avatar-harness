"""Fixture provisioning — a fresh, clean scratch git repo per eval run (hermetic).

Each run gets its own throwaway repo with a clean git baseline, so `Workspace` opens it
without `--allow-dirty` and the agent's diff is well-defined against the pinned HEAD.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path


def provision(fixture: Path | None, *, parent: Path | None = None, label: str = "eval_") -> Path:
    """Create a fresh scratch git repo seeded from `fixture`, committed clean.

    Args:
        fixture: A directory whose tree is copied into the scratch repo, or `None`
            for a bare repo (an empty initial commit — the creation-from-nothing case).
        parent: The directory to create the scratch repo under (the run workspace); `None`
            uses the system temp dir.
        label: Filename prefix for the scratch repo (e.g. `model__task__seedN__`) so a kept
            run workspace is inspectable.

    Returns:
        The path to the provisioned scratch repo (a clean git baseline).
    """
    repo = Path(tempfile.mkdtemp(prefix=label, dir=parent))
    if fixture is not None and Path(fixture).exists():
        shutil.copytree(fixture, repo, dirs_exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "eval@avatar-harness.local")
    _git(repo, "config", "user.name", "avatar-eval")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "baseline")
    return repo


def _git(repo: Path, *args: str) -> None:
    """Run a git command in `repo`, raising on failure.

    Args:
        repo: The repo directory.
        args: The git arguments.
    """
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
