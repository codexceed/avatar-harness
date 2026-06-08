"""Typed, phase-gated tools the model acts through (§10)."""

from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.commands import run_linter, run_tests
from avatar_harness.tools.edit import apply_patch
from avatar_harness.tools.filesystem import list_files, read_file
from avatar_harness.tools.search import search_repo


def default_registry() -> ToolRegistry:
    """A registry with the MVP tool surface registered, phase-gated by definition.

    Read tools are active in every phase; `apply_patch` only in `editing`; the
    command tools in `editing`/`verifying` (§10/§21 capability groups). The
    `ContextBuilder` exposes only the phase-active subset to the model.

    Returns:
        A `ToolRegistry` with the MVP tools registered.
    """
    registry = ToolRegistry()
    for tool in (read_file, list_files, search_repo, apply_patch, run_tests, run_linter):
        registry.register(tool)
    return registry
