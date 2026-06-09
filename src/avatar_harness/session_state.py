"""SessionState + ReplSession — the multi-turn scope above one task (§23, Phase 3.1 Lane 2a).

`TaskState` (§7) is per-goal and unchanged. `SessionState` is the scope *above* it: the
conversation `history`, the sequence of per-goal `tasks`, the session-scoped approval
`grants`, and the current `mode`. `ReplSession` is the thin driver: each goal becomes one
fresh `TaskState` run through the existing single-task `Session` (one code path — batch is
the degenerate one-`submit` case, §23.2), seeded with prior history and carrying grants
forward. The Textual cockpit (Lane 2b) renders this; here it stays pure logic.

Mode routing is a **visible heuristic default + explicit override** (ADR-0002 D3): a
lightweight rule seeds `task_kind`, and `set_mode` overrides it — never a hidden
per-prompt classifier. The `/mode` meta-command that drives `set_mode` is the 3.2 tail.
"""

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from avatar_harness.config import HarnessConfig
from avatar_harness.harness import Harness
from avatar_harness.session import ApprovalGrant, Session
from avatar_harness.state import TaskState

TaskKind = Literal["edit", "investigate", "test_only"]

# First-word imperatives that signal an edit goal; everything else defaults to investigate.
_EDIT_VERBS = frozenset(
    {
        "fix",
        "add",
        "implement",
        "refactor",
        "update",
        "remove",
        "delete",
        "rename",
        "create",
        "write",
        "change",
        "make",
        "build",
        "wire",
        "migrate",
    }
)


def default_mode(prompt: str) -> TaskKind:
    """Heuristically route a free-form prompt to an initial mode (`task_kind`).

    A first-word imperative (`fix …`, `add …`) reads as an edit goal; anything else
    (questions, "explain …", "why …") defaults to read-only investigation. This is the
    *visible* default the status bar shows and the user can override — not a classifier.

    Args:
        prompt: The user's natural-language goal.

    Returns:
        `"edit"` for an edit-shaped prompt, otherwise `"investigate"`.
    """
    words = prompt.strip().split()
    first = words[0].lower() if words else ""
    return "edit" if first in _EDIT_VERBS else "investigate"


class Turn(BaseModel):
    """One conversational turn — a user prompt or an agent reply (§23.1)."""

    role: Literal["user", "agent"]
    text: str
    task_id: str | None = None  # agent turns link to the TaskState they ran


class SessionState(BaseModel):
    """The scope above `TaskState`: what the user and agent have discussed/done (§23.1).

    Args:
        session_id: Stable id for the whole conversation.
        workspace_root: The repo the session operates on.
        config: The harness config in effect for the session.
        history: Conversational turns, carried across goals as context.
        tasks: One terminal `TaskState` per goal run so far.
        grants: Session-scoped standing approvals (`[a] always`); never cross-session.
        mode: The explicit mode override, or `None` to use the per-prompt heuristic.
    """

    session_id: str
    workspace_root: str
    config: HarnessConfig
    history: list[Turn] = Field(default_factory=list)
    tasks: list[TaskState] = Field(default_factory=list)
    grants: list[ApprovalGrant] = Field(default_factory=list)
    mode: TaskKind | None = None


class ReplSession:
    """Drives a multi-turn conversation over the unchanged single-task engine (§23.2).

    Each goal runs as one fresh `TaskState` through a per-goal `Session`; history seeds the
    next task and grants persist across tasks. `submit` is the simple run-to-completion
    path; `start`/`record` are the lower-level pair the cockpit uses so it can observe the
    per-task event stream and answer approvals between them.

    Args:
        harness: The configured `Harness`; supplies the per-goal run wiring.
        session_id: Stable conversation id; generated if omitted.
    """

    def __init__(self, harness: Harness, *, session_id: str | None = None) -> None:
        self.harness = harness
        self.state = SessionState(
            session_id=session_id or uuid4().hex,
            workspace_root=str(harness.config.workspace_root),
            config=harness.config,
        )

    @property
    def mode(self) -> TaskKind | None:
        """The explicit mode override, or `None` when the heuristic decides."""
        return self.state.mode

    def set_mode(self, mode: TaskKind) -> None:
        """Pin the mode for subsequent goals (the `/mode` override; overrides the heuristic).

        Args:
            mode: The `task_kind` to force on later goals until changed.
        """
        self.state.mode = mode

    def resolve_mode(self, prompt: str) -> TaskKind:
        """The mode for `prompt`: the explicit override if set, else the heuristic.

        Args:
            prompt: The user's goal.

        Returns:
            The resolved `task_kind`.
        """
        return self.state.mode or default_mode(prompt)

    def start(self, prompt: str) -> Session:
        """Build (but don't run) a per-goal `Session`: resolve mode, seed history, record the turn.

        The returned session is wired with the session-scoped grant list (shared by
        reference, so a `[a] always` persists across goals); the caller runs it and observes
        `events()`, then calls `record`.

        Args:
            prompt: The user's goal.

        Returns:
            A not-yet-started `Session` for this goal.
        """
        task = TaskState(goal=prompt, task_kind=self.resolve_mode(prompt))
        self._seed_history(task)  # prior turns become initial evidence (before this turn is added)
        self.state.history.append(Turn(role="user", text=prompt))
        runner = self.harness._build_runner(allow_dirty=False)
        return Session(runner, task, grants=self.state.grants)

    def record(self, state: TaskState) -> None:
        """Record a finished goal: append the terminal task and the agent's reply turn.

        Args:
            state: The terminal `TaskState` returned by `session.run()`.
        """
        self.state.tasks.append(state)
        reply = state.final_answer or (state.outcome or "done")
        self.state.history.append(Turn(role="agent", text=reply, task_id=state.task_id))

    async def submit(self, prompt: str) -> TaskState:
        """Run one goal to completion and record it — the simple (batch-shaped) path.

        Args:
            prompt: The user's goal.

        Returns:
            The terminal `TaskState`.
        """
        session = self.start(prompt)
        state = await session.run()
        self.record(state)
        return state

    def _seed_history(self, task: TaskState) -> None:
        """Seed prior conversation into `task` as initial `history` evidence (not transcript bleed).

        Args:
            task: The fresh per-goal `TaskState` to seed.
        """
        for turn in self.state.history:
            task.add_feedback(f"{turn.role}: {turn.text}", kind="history")
