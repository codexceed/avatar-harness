"""Typed, phase-gated tools the model acts through (§10)."""

from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.filesystem import list_files, read_file
from avatar_harness.tools.search import search_repo


def default_registry() -> ToolRegistry:
    """A registry with the tier-0 read tools registered (Phase 1)."""
    registry = ToolRegistry()
    for tool in (read_file, list_files, search_repo):
        registry.register(tool)
    return registry
