"""Workspace — a tracked, path-confined handle to the repo (§8, §15).

Tools touch the filesystem and run commands *only* through this handle, so a
tool physically cannot reach outside the workspace root. It also owns the diff
baseline: at construction it pins the current git HEAD, and `diff()` compares
the working tree against that pinned baseline (not the git index), so the task's
delta is well-defined and the harness never needs to commit (§15).
"""

import subprocess
from pathlib import Path


class PathOutsideWorkspace(Exception):
    """Raised when a requested path resolves outside the workspace root."""


class Workspace:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self._baseline = self._capture_baseline()

    # --- path confinement ------------------------------------------------

    def _resolve(self, rel_path: str) -> Path:
        """Resolve `rel_path` against the root, refusing any escape.

        `.resolve()` follows symlinks, so a symlink pointing outside the root
        resolves to an outside path and is rejected — there is no traversal hole.
        """
        candidate = (self.root / rel_path).resolve()
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise PathOutsideWorkspace(rel_path)
        return candidate

    # --- reads (tier 0) --------------------------------------------------

    def read(self, path: str, line_range: tuple[int, int] | None = None) -> str:
        text = self._resolve(path).read_text(encoding="utf-8")
        if line_range is None:
            return text
        start, end = line_range  # 1-indexed, inclusive
        return "".join(text.splitlines(keepends=True)[start - 1 : end])

    def list_files(self, glob: str) -> list[str]:
        return sorted(
            str(p.relative_to(self.root)) for p in self.root.glob(glob) if p.is_file()
        )

    # --- diff against the pinned baseline (§15) --------------------------

    def _capture_baseline(self) -> str | None:
        """Pin the current git HEAD, or None when not a git repo."""
        result = self._git("rev-parse", "HEAD")
        return result.stdout.strip() if result and result.returncode == 0 else None

    def diff(self) -> str:
        """Working-tree delta vs. the pinned baseline (empty when no baseline)."""
        if self._baseline is None:
            return ""
        result = self._git("diff", self._baseline)
        return result.stdout if result else ""

    def _git(self, *args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
