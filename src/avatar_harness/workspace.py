"""Workspace — a tracked, path-confined handle to the repo (§8, §15).

Tools touch the filesystem and run commands *only* through this handle, so a
tool physically cannot reach outside the workspace root. It also owns the diff
baseline: at construction it pins the current git HEAD, and `diff()` compares
the working tree against that pinned baseline (not the git index), so the task's
delta is well-defined and the harness never needs to commit (§15).
"""

import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from avatar_harness.config import DEFAULT_SENSITIVE_PATH_GLOBS


def path_is_sensitive(rel_path: str, globs: Sequence[str]) -> bool:
    """Whether `rel_path` matches any denylist glob (§11, Phase 2.5).

    A pattern *without* a slash matches any single path component (gitignore-style
    "match anywhere" — so ``.env`` hits ``a/b/.env`` and ``.ssh`` hits ``.ssh/id_rsa``).
    A pattern *with* a slash is matched against the whole relative path.

    Args:
        rel_path: The workspace-relative path to test.
        globs: The denylist patterns.

    Returns:
        `True` if any pattern matches, else `False`.
    """
    parts = PurePosixPath(rel_path).parts
    for glob in globs:
        if "/" in glob:
            if fnmatch(rel_path, glob):
                return True
        elif any(fnmatch(part, glob) for part in parts):
            return True
    return False


class PathOutsideWorkspaceError(Exception):
    """Raised when a requested path resolves outside the workspace root."""


class SensitivePathError(Exception):
    """Raised when a *resolved* path matches the sensitive-path denylist (§11, Phase 2.5).

    Enforced at the workspace chokepoint (not just the permission gate), so the
    check sees the symlink-resolved target — an innocuously-named symlink cannot
    launder a secret — and a non-gated caller still cannot read/patch it.
    """


class PatchError(Exception):
    """A patch failed to apply cleanly (stale context) — model-correctable (§10).

    Application is all-or-nothing: when this is raised, nothing was written.
    """


class ReplaceError(ValueError):
    """A string-anchored replace could not be applied cleanly (ADR-0015) — model-correctable.

    All-or-nothing: when raised, the file is byte-for-byte unchanged.
    """


class MatchNotFoundError(ReplaceError):
    """The `old` anchor was absent from the file — a stale or mistyped anchor (§10)."""


class EmptyAnchorError(ReplaceError):
    """`old` was empty — it would match between every character and rewrite the file (ADR-0015).

    Rejected at the `Workspace.replace` chokepoint so a direct SDK caller can't corrupt a file,
    not only the `str_replace` tool layer.
    """


class AmbiguousMatchError(ReplaceError):
    """The `old` anchor matched more than once and `replace_all` was not set (ADR-0015).

    Args:
        path: The workspace-relative file the anchor matched in.
        count: How many times the anchor matched (>1); exposed as `.count` for the tool's message.
    """

    def __init__(self, path: str, count: int) -> None:
        self.count = count
        super().__init__(f"{path}: {count} matches")


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
        sensitive_path_globs: The denylist refused on read/patch (resolved-path check,
            §11). Defaults to the built-in set (secure by default); the runner threads
            `HarnessConfig.sensitive_path_globs` through to match the permission gate.
        log_path: The harness's own event-journal path (`HarnessConfig.log_path`), hidden
            from the agent's file tools so it can't list/read/search the harness's plumbing.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        allow_dirty: bool = False,
        sensitive_path_globs: Sequence[str] | None = None,
        log_path: Path | str | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self._sensitive = list(
            DEFAULT_SENSITIVE_PATH_GLOBS if sensitive_path_globs is None else sensitive_path_globs
        )
        # The harness's own journal lives under the workspace (default `events/<id>.jsonl`);
        # hide exactly that file + its `latest.jsonl` pointer from the file tools so the agent
        # never lists/reads/searches its own event log (never a whole `events/` dir — a real
        # project may own one).
        self._ignored_relpaths = _journal_ignores(self.root, log_path)
        # The ledger of every command run through this handle, in order — the runner
        # reads it into `state.commands_run` so the artifact/log reflect what ran (§7).
        self.command_log: list[CommandOutput] = []
        if not allow_dirty:
            self._assert_clean()
        self._baseline = self._capture_baseline()

    def _assert_not_sensitive(self, resolved: Path) -> None:
        """Refuse a resolved in-root path that matches the denylist (§11, Phase 2.5).

        Args:
            resolved: An already-confined absolute path (under the root).

        Raises:
            SensitivePathError: When the resolved path matches a denylist glob.
        """
        if resolved == self.root:
            return
        rel = str(resolved.relative_to(self.root))
        if path_is_sensitive(rel, self._sensitive):
            raise SensitivePathError(rel)

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

        Raises:
            FileNotFoundError: When the path is the harness's own (hidden) journal.
        """
        resolved = self._resolve(path)
        if self.is_ignored(resolved.relative_to(self.root).as_posix()):
            raise FileNotFoundError(f"{path}: no such file")  # the harness journal is invisible
        self._assert_not_sensitive(resolved)  # resolved-path check closes the symlink bypass
        text = resolved.read_text(encoding="utf-8")
        if line_range is None:
            return text
        start, end = line_range  # 1-indexed, inclusive
        return "".join(text.splitlines(keepends=True)[start - 1 : end])

    def list_files(self, glob: str) -> list[str]:
        """Return workspace-relative paths of files matching `glob`, sorted.

        A glob that matches a *directory* expands to the files under it (recursively),
        so `list_files("pkg")` lists `pkg/`'s contents rather than silently returning
        nothing — the dogfood gap where `rich*` matched a dir and was dropped.

        Hidden (dot-prefixed) entries are skipped by wildcards, mirroring ripgrep's
        default — pathlib's glob matches them, so a venv or `.git` inside the workspace
        otherwise turns `*`/`**/*` into thousands of junk paths. A pattern that *names*
        a dot-prefixed segment (e.g. `.github/**/*`) opts into hidden, the same way an
        explicit path does for rg; `read_file` on an explicit hidden path always works.

        Args:
            glob: The glob pattern to match against the root.

        Returns:
            Sorted workspace-relative paths of the matching files (dir matches expanded;
            hidden entries skipped unless the pattern names a dot-prefixed segment).
        """
        show_hidden = any(seg.startswith(".") for seg in glob.split("/"))
        found: set[Path] = set()
        for p in self.root.glob(glob):
            if p.is_file():
                found.add(p)
            elif p.is_dir():
                found.update(q for q in p.rglob("*") if q.is_file())
        rels = (p.relative_to(self.root) for p in found)
        if not show_hidden:
            rels = (r for r in rels if not any(part.startswith(".") for part in r.parts))
        return sorted(s for r in rels if not self.is_ignored(s := r.as_posix()))

    def is_ignored(self, relpath: str) -> bool:
        """Whether `relpath` (workspace-relative POSIX) is hidden harness plumbing.

        Args:
            relpath: A workspace-relative POSIX path.

        Returns:
            `True` for the harness's own journal file or its `latest.jsonl` pointer.
        """
        return relpath in self._ignored_relpaths

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
            # raises PathOutsideWorkspaceError on escape, SensitivePathError on a denylist hit
            self._assert_not_sensitive(self._resolve(path))

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

    def write_file(self, path: str, content: str, *, overwrite: bool = False) -> str:
        """Create a file with `content` and stage it; refuse an existing target by default.

        The plain-content twin of `apply_patch` for the no-anchor case (ADR-0003 B):
        creation needs no diff costume, while *modification* stays diff-anchored —
        without `overwrite`, an existing target raises `FileExistsError` so the
        clean-apply staleness invariant can't be bypassed casually. Confinement and
        the sensitive-path denylist apply at this chokepoint like every other access,
        and the new file is staged so it appears in `diff()` (matching `apply_patch
        --index` and the `run_command` mutation capture).

        Args:
            path: The workspace-relative file to create.
            content: The full file content to write.
            overwrite: `True` to deliberately replace an existing file.

        Returns:
            The workspace-relative path written.

        Raises:
            FileExistsError: When the target exists and `overwrite` is `False`.
        """
        resolved = self._resolve(path)  # raises PathOutsideWorkspaceError on escape
        self._assert_not_sensitive(resolved)
        rel = str(resolved.relative_to(self.root))
        if resolved.exists() and not overwrite:
            raise FileExistsError(rel)
        resolved.parent.mkdir(parents=True, exist_ok=True)  # parents are confined with the file
        resolved.write_text(content, encoding="utf-8")
        self.stage([rel])  # untracked output is invisible to `git diff <baseline>` until staged
        return rel

    def replace(self, path: str, old: str, new: str, *, replace_all: bool = False) -> str:
        """Swap an exact string in a file; the anchor proves non-staleness (§10, ADR-0015).

        The string-anchored modification primitive that supersedes `apply_patch`'s diff
        costume: `old` must match the *current* file text exactly — read-before-edit (§10)
        enforced by the anchor, not by line arithmetic. It must match once, unless
        `replace_all`. All-or-nothing: a rejected match leaves the file byte-for-byte
        unchanged. Confinement, the sensitive-path denylist, and staging apply at this
        chokepoint like every other write; the resulting diff is *derived* by `diff()`,
        never authored by the model (§5).

        Args:
            path: The workspace-relative file to edit.
            old: The exact existing text to find (the anchor); must be non-empty.
            new: The replacement text; an empty string deletes the matched span.
            replace_all: Replace every occurrence instead of requiring a unique match.

        Returns:
            The workspace-relative path edited.

        Raises:
            FileNotFoundError: When the target file does not exist (create with `write_file`).
            EmptyAnchorError: When `old` is empty (would rewrite the whole file).
            MatchNotFoundError: When `old` is absent (stale or mistyped anchor).
            AmbiguousMatchError: When `old` matches more than once and `replace_all` is unset.

        Note:
            The input contract lives HERE, at the chokepoint, not only in the `str_replace`
            tool — a direct SDK caller gets the same guarantees. Confinement and the
            sensitive-path denylist apply via `_resolve`/`_assert_not_sensitive`. An empty
            `new` is deliberately allowed (span deletion); an empty `old` is rejected.
        """
        if not old:
            # `str.replace("", x)` inserts between every character — corruption, not an edit.
            raise EmptyAnchorError(path)
        resolved = self._resolve(path)
        self._assert_not_sensitive(resolved)
        rel = str(resolved.relative_to(self.root))
        if not resolved.is_file():
            raise FileNotFoundError(rel)  # edits target existing files; creation is write_file
        text = resolved.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise MatchNotFoundError(rel)
        if count > 1 and not replace_all:
            raise AmbiguousMatchError(rel, count)
        resolved.write_text(text.replace(old, new), encoding="utf-8")
        self.stage([rel])
        return rel

    # --- command execution (§15) -----------------------------------------

    def run(self, command: str, timeout: int | None = None) -> CommandOutput:
        """Run `command` confined to the root, capturing output; bounded by `timeout`.

        Never raises into the loop (ADR-0007 robustness floor): a missing binary,
        an empty command, or an unparseable command line all come back as a failed
        `CommandOutput` (shell convention: exit 127 = command not found) so the
        verifier and tools see a legible failure, not a `FileNotFoundError`.

        Args:
            command: The shell-style command to run.
            timeout: Seconds before the command is killed, or `None` for no bound.

        Returns:
            The captured stdout, stderr, exit code, and timeout flag.
        """
        out = self._run_unlogged(command, timeout)
        self.command_log.append(out)
        return out

    def _run_unlogged(self, command: str, timeout: int | None) -> CommandOutput:
        """Execute `command` and classify every failure mode into a `CommandOutput`.

        Args:
            command: The shell-style command to run.
            timeout: Seconds before the command is killed, or `None` for no bound.

        Returns:
            The captured output; exit 127 for not-found/unrunnable, `None` on timeout.
        """
        try:
            argv = shlex.split(command)
        except ValueError as exc:  # unbalanced quotes etc. — legible, not a raise
            return CommandOutput(
                command=command, stdout="", stderr=f"unparseable command: {exc}", exit_code=127
            )
        if not argv:
            return CommandOutput(command=command, stdout="", stderr="empty command", exit_code=127)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandOutput(
                command=command,
                stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
                stderr=exc.stderr or "" if isinstance(exc.stderr, str) else "",
                exit_code=None,
                timed_out=True,
            )
        except FileNotFoundError:
            return CommandOutput(
                command=command, stdout="", stderr=f"command not found: {argv[0]}", exit_code=127
            )
        except OSError as exc:  # not executable, bad interpreter, ... — surface, don't crash
            return CommandOutput(
                command=command, stdout="", stderr=f"cannot run {argv[0]}: {exc}", exit_code=126
            )
        return CommandOutput(
            command=command, stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode
        )

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

    def status_paths(self) -> set[str]:
        """Workspace-relative paths git currently sees as changed *or untracked*.

        Used to attribute a command's side effects: snapshot before and after a
        `run` and the delta is what the command touched. Includes untracked files
        (unlike `diff`, which is `git diff <baseline>`), so command-created files
        are visible. Empty when not a git repo.

        Returns:
            The set of paths from `git status --porcelain` (rename → its new path).
        """
        status = self._git("status", "--porcelain")
        if status is None or status.returncode != 0:
            return set()
        paths: set[str] = set()
        for line in status.stdout.splitlines():
            entry = line[3:].strip()  # porcelain is "XY <path>"
            if " -> " in entry:  # rename/copy: "old -> new" — attribute the new path
                entry = entry.split(" -> ", 1)[1]
            if entry:
                paths.add(entry.strip('"'))
        return paths

    def stage(self, paths: list[str]) -> None:
        """`git add` the given paths so they enter the pinned-HEAD `diff` (§15).

        Mirrors `apply_patch`'s `git apply --index`: a command-created (untracked)
        file is invisible to `git diff <baseline>` until staged, so staging is what
        makes codegen/migration output show up in the diff, artifact, and verifier.
        No-op when not a git repo or given no paths.

        Args:
            paths: Workspace-relative paths to stage.
        """
        if paths:
            self._git("add", "--", *paths)

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


def _journal_ignores(root: Path, log_path: Path | str | None) -> set[str]:
    """Workspace-relative paths of the harness's own journal, to hide from the file tools.

    The harness writes its event journal under the workspace (default `events/<id>.jsonl`
    plus an `events/latest.jsonl` pointer). Those are harness plumbing, not the user's
    project — so the file tools skip exactly the active log file and its pointer. A whole
    `events/` directory is never hidden, since a real project may legitimately own one.

    Args:
        root: The resolved workspace root.
        log_path: The active journal path (root/cwd-relative or absolute), or `None`.

    Returns:
        Workspace-relative POSIX paths to hide; empty when the journal is outside the root.
    """
    if not log_path:
        return set()
    base = Path(log_path)
    base = base if base.is_absolute() else (root / base)
    out: set[str] = set()
    for candidate in (base, base.parent / "latest.jsonl"):
        resolved = candidate.resolve()
        if resolved.is_relative_to(root):
            out.add(resolved.relative_to(root).as_posix())
    return out
