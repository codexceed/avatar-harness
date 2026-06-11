"""Repository text search via ripgrep (§10, tier 0)."""

import subprocess

from pydantic import BaseModel

from avatar_harness.deps import RunDeps
from avatar_harness.tools.base import ToolDefinition, ToolResult
from avatar_harness.workspace import path_is_sensitive

_READ_PHASES = frozenset({"investigating", "editing", "verifying"})


class SearchRepoInput(BaseModel):
    """Input for `search_repo`: the ripgrep query pattern."""

    query: str


def _search_repo(args: SearchRepoInput, deps: RunDeps) -> ToolResult:
    try:
        proc = subprocess.run(
            # The explicit "." path is load-bearing: without one, rg searches STDIN
            # whenever stdin isn't a tty, so any embedding with a piped stdin (CI, cron,
            # a supervising process) blocks on the silent pipe until the timeout and never
            # sees the tree. stdin=DEVNULL is belt-and-braces for the same trap, and the
            # "--" keeps a query that starts with "-" a pattern rather than an rg flag.
            ["rg", "--line-number", "--no-heading", "--color=never", "--", args.query, "."],
            cwd=str(deps.workspace.root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolResult(tool_name="search_repo", success=False, error=f"search failed: {exc}")

    # rg exit codes: 0 = matches, 1 = no matches (a clean result), 2 = error.
    if proc.returncode not in (0, 1):
        return ToolResult(
            tool_name="search_repo", success=False, error=proc.stderr.strip() or "ripgrep error"
        )

    # Drop hits in denylisted files so search can't exfiltrate a secret — using the SAME
    # `path_is_sensitive` matcher as the gate/workspace, so "what counts as sensitive" is
    # one source of truth everywhere (not ripgrep's divergent glob engine; §11, Phase 2.5).
    # The "./" the explicit search path prefixes onto every hit is stripped first, so the
    # model-visible paths and the denylist matcher see the same relative form as before.
    globs = deps.config.sensitive_path_globs
    lines = (line.removeprefix("./") for line in proc.stdout.splitlines())
    kept = [line for line in lines if not path_is_sensitive(line.split(":", 1)[0], globs)]
    return ToolResult(
        tool_name="search_repo",
        success=True,
        content="\n".join(kept),
        summary=f"{len(kept)} match(es) for {args.query!r}",
    )


search_repo = ToolDefinition(
    name="search_repo",
    description="Search repository text with ripgrep; returns file:line matches.",
    input_model=SearchRepoInput,
    handler=_search_repo,
    phases=_READ_PHASES,
)
