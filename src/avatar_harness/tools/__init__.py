"""Typed, phase-gated tools the model acts through (§10)."""

from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.commands import run_command, run_linter, run_tests
from avatar_harness.tools.edit import apply_patch, str_replace, write_file
from avatar_harness.tools.filesystem import list_files, read_file
from avatar_harness.tools.search import search_repo


def default_registry() -> ToolRegistry:
    """A registry with the MVP tool surface registered, phase-gated by definition.

    Read tools are active in every phase; the edit tools (`str_replace` — the primary,
    string-anchored editor, ADR-0015 — plus `write_file`/`apply_patch`) only in `editing`; the
    command tools in `editing`/`verifying` (§10/§21 capability groups); `run_command`
    in every phase but tier-3 (default-blocked in batch, approval-gated in the REPL —
    ADR-0002 D4). The `ContextBuilder` exposes only the phase-active subset to the model.

    Returns:
        A `ToolRegistry` with the MVP tools registered.
    """
    registry = ToolRegistry()
    for tool in (
        read_file,
        list_files,
        search_repo,
        str_replace,
        apply_patch,
        write_file,
        run_tests,
        run_linter,
        run_command,
    ):
        registry.register(tool)
    return registry
