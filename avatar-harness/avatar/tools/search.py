"""Repository text search via ripgrep (§10, tier 0)."""

import subprocess

from pydantic import BaseModel

from avatar.deps import RunDeps
from avatar.tools.base import ToolDefinition, ToolResult
from avatar.workspace import path_is_sensitive

_READ_PHASES = frozenset({"investigating", "editing", "verifying"})

# Cap the model-visible (and journaled) search output so a large match set can't balloon
# `ToolEnd.content` — the 875 MB journal blowup where `search_repo` recursed over a growing
# `journal.jsonl` (ADR-0023, increment 0). Mirrors the `model_client._excerpt` marker.
_MAX_SEARCH_OUTPUT_CHARS = 50_000


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
    ws = deps.workspace
    lines = (line.removeprefix("./") for line in proc.stdout.splitlines())
    kept = [
        line
        for line in lines
        if not path_is_sensitive((p := line.split(":", 1)[0]), globs) and not ws.is_ignored(p)
    ]
    content = "\n".join(kept)
    truncated = len(content) > _MAX_SEARCH_OUTPUT_CHARS
    if truncated:
        full = len(content)
        content = (
            content[:_MAX_SEARCH_OUTPUT_CHARS]
            + f"\n… [truncated: {_MAX_SEARCH_OUTPUT_CHARS}/{full} chars shown]"
        )
    return ToolResult(
        tool_name="search_repo",
        success=True,
        content=content,
        summary=f"{len(kept)} match(es) for {args.query!r}" + (" (truncated)" if truncated else ""),
    )


search_repo = ToolDefinition(
    name="search_repo",
    description="Search repository text with ripgrep; returns file:line matches.",
    input_model=SearchRepoInput,
    handler=_search_repo,
    phases=_READ_PHASES,
)
