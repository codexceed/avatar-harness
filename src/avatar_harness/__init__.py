"""avatar-harness: a minimal, ground-up coding-agent harness.

A bounded, verifiable loop around an LLM: the model proposes actions, the harness
owns execution/state/permissions/logging, and the loop terminates on external
verification — never on a text reply. See `HARNESS_DESIGN.md` for the design.

The curated public surface (`__all__`) is the stable importable API. `Harness`
is the one-call entry point; the rest are the seams a downstream user composes
against (tools, model client, workspace, state, decisions) — all extensible at
the edges (Principle A).

    from avatar_harness import Harness
    state = Harness.from_env().run("explain the retry loop")
"""

from avatar_harness.config import HarnessConfig
from avatar_harness.deps import RunDeps
from avatar_harness.harness import Harness
from avatar_harness.model_client import (
    AskUser,
    FinalAnswer,
    ModelClient,
    ModelDecision,
    ToolCall,
)
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolDefinition, ToolRegistry, ToolResult
from avatar_harness.workspace import Workspace

__version__ = "0.0.0"

__all__ = [
    "AskUser",
    "FinalAnswer",
    "Harness",
    "HarnessConfig",
    "ModelClient",
    "ModelDecision",
    "RunDeps",
    "TaskState",
    "ToolCall",
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    "Workspace",
]
