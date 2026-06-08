"""Workspace — a tracked, path-confined handle to the repo (§8, §15).

Tools touch the filesystem and run commands *only* through this handle, so a
tool physically cannot reach outside the workspace root. It also owns the diff
baseline: at construction it pins the current git HEAD, and `diff()` compares
the working tree against that pinned baseline (not the git index), so the task's
delta is well-defined and the harness never needs to commit (§15).
"""

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


class PathOutsideWorkspaceError(Exception):
    """Raised when a requested path resolves outside the workspace root."""


class PatchError(Exception):
    """A patch failed to apply cleanly (stale context) — model-correctable (§10).

    Application is all-or-nothing: when this is raised, nothing was written.
    """


class DirtyWorkspaceError(Exception):
    """The workspace has uncommitted changes at open and they were not acknowledged (§15)."""


@dataclass(frozen=True)
class CommandOutput:
    """The captured result of one command run through the workspace."""

    command: str
    stdout: str
    stderr: str
    exit_code: int | None  # None when the command timed out
    timed_out: bool = False


class Workspace:
    """A tracked, path-confined handle to the repo; tools reach the FS only here (§8, §15).

    Args:
        root: The workspace root all paths are confined to.
        allow_dirty: When `True`, skip the clean-tree check at open (§15).
    """

    def __init__(self, root: Path | str, *, allow_dirty: bool = False) -> None:
        self.root = Path(root).resolve()
        # The ledger of every command run through this handle, in order — the runner
        # reads it into `state.commands_run` so the artifact/log reflect what ran (§7).
        self.command_log: list[CommandOutput] = []
        if not allow_dirty:
            self._assert_clean()
        self._baseline = self._capture_baseline()

    # --- path confinement ------------------------------------------------

    def _resolve(self, rel_path: str) -> Path:
        """Resolve `rel_path` against the root, refusing any escape.

        `.resolve()` follows symlinks, so a symlink pointing outside the root
        resolves to an outside path and is rejected — there is no traversal hole.

        Args:
            rel_path: The path to resolve against the root.

        Returns:
            The resolved absolute path inside the root.

        Raises:
            PathOutsideWorkspaceError: When `rel_path` escapes the root.
        """
        candidate = (self.root / rel_path).resolve()
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise PathOutsideWorkspaceError(rel_path)
        return candidate

    def contains(self, rel_path: str) -> bool:
        """Whether `rel_path` resolves inside the root, without raising (for the gate).

        Args:
            rel_path: The path to test.

        Returns:
            `True` if `rel_path` resolves inside the root, else `False`.
        """
        try:
            self._resolve(rel_path)
        except PathOutsideWorkspaceError:
            return False
        return True

    # --- reads (tier 0) --------------------------------------------------

    def read(self, path: str, line_range: tuple[int, int] | None = None) -> str:
        """Read a workspace file, optionally a 1-indexed inclusive line range.

        Args:
            path: The file to read.
            line_range: A 1-indexed inclusive `(start, end)` range, or `None` for all.

        Returns:
            The file text, sliced to `line_range` when given.
        """
        text = self._resolve(path).read_text(encoding="utf-8")
        if line_range is None:
            return text
        start, end = line_range  # 1-indexed, inclusive
        return "".join(text.splitlines(keepends=True)[start - 1 : end])

    def list_files(self, glob: str) -> list[str]:
        """Return workspace-relative paths of files matching `glob`, sorted.

        Args:
            glob: The glob pattern to match against the root.

        Returns:
            Sorted workspace-relative paths of the matching files.
        """
        return sorted(str(p.relative_to(self.root)) for p in self.root.glob(glob) if p.is_file())

    # --- patch application (tier 1, §10) ---------------------------------

    def apply_patch(self, diff: str) -> list[str]:
        """Apply a (possibly multi-file) unified diff atomically; return changed paths.

        Confinement first: every target path must resolve inside the root, else
        `PathOutsideWorkspaceError` (nothing written). Then a `git apply --check`
        dry run gates the real apply, so a stale diff raises `PatchError` and the
        workspace is left byte-for-byte unchanged — all-or-nothing (§10).

        Args:
            diff: The unified diff to apply.

        Returns:
            Sorted workspace-relative paths the diff changed.

        Raises:
            PatchError: When the diff names no targets or fails to apply cleanly.
        """
        targets = _parse_patch_targets(diff)
        if not targets:
            raise PatchError("no file targets found in diff")
        for path in targets:
            self._resolve(path)  # raises PathOutsideWorkspaceError on escape

        # Apply to the index as well as the working tree (`--index`), so a *created*
        # file is tracked and therefore appears in `diff()` — otherwise new files are
        # untracked and invisible to the secret scan and the artifact (§14/§15).
        check = self._git("apply", "--index", "--check", "-", stdin=diff)
        if check is None or check.returncode != 0:
            raise PatchError(
                (check.stderr.strip() if check else "git apply unavailable") or "patch did not apply"
            )
        applied = self._git("apply", "--index", "-", stdin=diff)
        if applied is None or applied.returncode != 0:
            raise PatchError(
                (applied.stderr.strip() if applied else "git apply unavailable") or "patch did not apply"
            )
        return sorted(targets)

    # --- command execution (§15) -----------------------------------------

    def run(self, command: str, timeout: int | None = None) -> CommandOutput:
        """Run `command` confined to the root, capturing output; bounded by `timeout`.

        Args:
            command: The shell-style command to run.
            timeout: Seconds before the command is killed, or `None` for no bound.

        Returns:
            The captured stdout, stderr, exit code, and timeout flag.
        """
        try:
            proc = subprocess.run(
                shlex.split(command),
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out = CommandOutput(
                command=command,
                stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
                stderr=exc.stderr or "" if isinstance(exc.stderr, str) else "",
                exit_code=None,
                timed_out=True,
            )
        else:
            out = CommandOutput(
                command=command, stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode
            )
        self.command_log.append(out)
        return out

    # --- diff against the pinned baseline (§15) --------------------------

    def _assert_clean(self) -> None:
        """Refuse to open on a dirty git tree unless explicitly acknowledged (§15).

        Only **tracked** modifications count: untracked files never enter
        `git diff HEAD`, so they cannot pollute the diff baseline and must not
        block the run (`--untracked-files=no`).

        Raises:
            DirtyWorkspaceError: When the tree has uncommitted *tracked* changes.
        """
        status = self._git("status", "--porcelain", "--untracked-files=no")
        if status is not None and status.returncode == 0 and status.stdout.strip():
            raise DirtyWorkspaceError(self.root)

    def _capture_baseline(self) -> str | None:
        """Pin the current git HEAD, or None when not a git repo.

        Returns:
            The HEAD commit sha, or `None` when not a git repo.
        """
        result = self._git("rev-parse", "HEAD")
        return result.stdout.strip() if result and result.returncode == 0 else None

    def diff(self) -> str:
        """Working-tree delta vs. the pinned baseline (empty when no baseline).

        Returns:
            The unified diff against the pinned baseline, empty when none.
        """
        if self._baseline is None:
            return ""
        result = self._git("diff", self._baseline)
        return result.stdout if result else ""

    def _git(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None


def _parse_patch_targets(diff: str) -> set[str]:
    """Extract the workspace-relative file paths a unified diff touches.

    Reads the `--- a/<path>` / `+++ b/<path>` headers, strips the `a/`/`b/`
    prefix, and ignores `/dev/null` (the new-file / delete sentinel).

    Args:
        diff: The unified diff to scan.

    Returns:
        The workspace-relative paths the diff touches.
    """
    targets: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            raw = line[4:].strip()
            if raw == "/dev/null":
                continue
            # Strip a leading a/ or b/ path component (git-style prefixes).
            if raw.startswith(("a/", "b/")):
                raw = raw[2:]
            targets.add(raw)
    return targets
