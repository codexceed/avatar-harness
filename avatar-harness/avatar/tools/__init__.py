"""Typed, phase-gated tools the model acts through (§10)."""

from avatar.tools.base import ToolRegistry
from avatar.tools.commands import run_command, run_linter, run_tests
from avatar.tools.edit import delete_file, str_replace, write_file
from avatar.tools.filesystem import list_files, read_file
from avatar.tools.search import search_repo
from avatar.tools.verification import alter_verification, declare_verification, switch_to_editing


def default_registry() -> ToolRegistry:
    """A registry with the MVP tool surface registered, phase-gated by definition.

    Read tools are active in every phase; the edit tools (`str_replace` (the string-anchored
    editor), `write_file`, `delete_file` — ADR-0015) only in `editing`; the command tools
    all in `editing`/`verifying` (§10/§21 capability groups) — `run_tests`/`run_linter` at
    tier-2 (allowed), `run_command` at tier-3 (default-blocked in batch, approval-gated in
    the REPL — ADR-0002 D4). The `ContextBuilder` exposes only the phase-active subset to the model.

    Returns:
        A `ToolRegistry` with the MVP tools registered.
    """
    registry = ToolRegistry()
    for tool in (
        read_file,
        list_files,
        search_repo,
        str_replace,
        write_file,
        delete_file,
        run_tests,
        run_linter,
        run_command,
        declare_verification,
        alter_verification,
        switch_to_editing,
    ):
        registry.register(tool)
    return registry
