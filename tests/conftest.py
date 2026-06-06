import pytest

from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.filesystem import list_files, read_file
from avatar_harness.tools.search import search_repo


@pytest.fixture
def read_registry() -> ToolRegistry:
    """A registry with the Phase 1 tier-0 read tools registered."""
    reg = ToolRegistry()
    for tool in (read_file, list_files, search_repo):
        reg.register(tool)
    return reg
