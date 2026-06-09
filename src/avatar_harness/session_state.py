"""SessionState + ReplSession — the multi-turn scope above one task (§23, Phase 3.1 Lane 2a).

`TaskState` (§7) is per-goal and unchanged. `SessionState` is the scope *above* it: the
conversation `history`, the sequence of per-goal `tasks`, the session-scoped approval
`grants`, and the current `mode`. `ReplSession` is the thin driver: each goal becomes one
fresh `TaskState` run through the existing single-task `Session` (one code path — batch is
the degenerate one-`submit` case, §23.2), seeded with prior history and carrying grants
forward. The Textual cockpit (Lane 2b) renders this; here it stays pure logic.

Mode routing is a **visible heuristic default + explicit override** (ADR-0002 D3): a
lightweight rule seeds `task_kind`, and `set_mode` overrides it — never a hidden
per-prompt classifier. Local **meta commands** (`/help` `/quit` `/state` `/mode` `/diff`
`/permissions`) are handled by `run_meta` and never reach the model (§23.2).
"""

import re
from dataclasses import dataclass
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, Field

from avatar_harness.config import HarnessConfig
from avatar_harness.harness import Harness
from avatar_harness.session import ApprovalGrant, Session
from avatar_harness.state import TaskState
from avatar_harness.workspace import PathOutsideWorkspaceError, SensitivePathError, Workspace

TaskKind = Literal["edit", "investigate", "test_only"]
_TASK_KINDS: tuple[TaskKind, ...] = ("edit", "investigate", "test_only")

_AT_PATH = re.compile(r"@(\S+)")  # `@path/to/file` grounding references in a goal
_GROUND_BUDGET = 2000  # per-file content cap — grounding is a hint, not a dump

_META_HELP = "commands: /help · /quit · /state · /mode <edit|investigate|test_only> · /diff · /permissions"


@dataclass(frozen=True)
class MetaResult:
    """The outcome of a local meta command — the cockpit interprets `kind`, displays `text`.

    `kind`: `message` (show text) · `mode_set` (mode changed) · `state` (session summary) ·
    `diff` (text is a unified diff → the diff modal) · `quit` (end the session).
    """

    kind: Literal["message", "mode_set", "state", "diff", "quit"]
    text: str


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
        self._ground_paths(task, prompt)  # @path references seed the named files as context
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

    def is_meta(self, text: str) -> bool:
        """Whether `text` is a meta command (handled locally, never run as a goal).

        Args:
            text: The raw user input.

        Returns:
            True iff the input begins with `/`.
        """
        return text.lstrip().startswith("/")

    def run_meta(self, text: str) -> MetaResult:  # noqa: PLR0911 — a flat per-command switch
        """Handle a `/command` locally and return a result the cockpit renders/routes (§23.2).

        Args:
            text: The raw `/command [arg]` input.

        Returns:
            The `MetaResult` for the command (unknown commands are reported, never run).
        """
        parts = text.strip().lstrip("/").split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in {"help", ""}:
            return MetaResult(kind="message", text=_META_HELP)
        if cmd in {"quit", "exit"}:
            return MetaResult(kind="quit", text="ending session")
        if cmd == "mode":
            return self._meta_mode(arg)
        if cmd == "state":
            summary = (
                f"mode: {self.resolve_mode('')} · "
                f"tasks: {len(self.state.tasks)} · turns: {len(self.state.history)}"
            )
            return MetaResult(kind="state", text=summary)
        if cmd == "diff":
            return MetaResult(kind="diff", text=self._workspace_diff())
        if cmd == "permissions":
            return self._meta_permissions()
        return MetaResult(kind="message", text=f"unknown command: /{cmd} — {_META_HELP}")

    def _meta_mode(self, arg: str) -> MetaResult:
        """Set the mode from `/mode <arg>`, or report an invalid kind.

        Args:
            arg: The requested mode.

        Returns:
            A `mode_set` result on success, else a `message` error.
        """
        if arg in _TASK_KINDS:
            self.set_mode(cast(TaskKind, arg))
            return MetaResult(kind="mode_set", text=f"mode set to {arg}")
        return MetaResult(kind="message", text=f"unknown mode: {arg} (use edit | investigate | test_only)")

    def _meta_permissions(self) -> MetaResult:
        """List the session-scoped standing grants.

        Returns:
            A `message` result naming each granted tool/prefix, or noting there are none.
        """
        if not self.state.grants:
            return MetaResult(kind="message", text="no standing approvals this session")
        lines = "\n".join(f"{g.tool} {g.prefix} (tier {g.tier})" for g in self.state.grants)
        return MetaResult(kind="message", text=f"standing approvals:\n{lines}")

    def _workspace_diff(self) -> str:
        """The current uncommitted diff vs the pinned baseline (read-only; tolerates a dirty tree).

        Returns:
            The unified-diff text (empty when there are no changes).
        """
        ws = Workspace(
            self.harness.config.workspace_root,
            allow_dirty=True,  # /diff is a read-only inspection — never refuse on a dirty tree
            sensitive_path_globs=self.harness.config.sensitive_path_globs,
        )
        return ws.diff()

    def _seed_history(self, task: TaskState) -> None:
        """Seed prior conversation into `task` as initial `history` evidence (not transcript bleed).

        Args:
            task: The fresh per-goal `TaskState` to seed.
        """
        for turn in self.state.history:
            task.add_feedback(f"{turn.role}: {turn.text}", kind="history")

    def _ground_paths(self, task: TaskState, prompt: str) -> None:
        """Seed any `@path` references in `prompt` as `grounding` evidence on `task`.

        Files are read through the `Workspace`, so the sensitive-path denylist and
        confinement apply — a refused, missing, or out-of-root path becomes a short note
        rather than a crash or a leaked secret.

        Args:
            task: The fresh per-goal `TaskState` to seed.
            prompt: The user's goal, scanned for `@path` references.
        """
        refs = _AT_PATH.findall(prompt)
        if not refs:
            return
        ws = Workspace(
            self.harness.config.workspace_root,
            allow_dirty=True,  # grounding is a read-only inspection — tolerate a dirty tree
            sensitive_path_globs=self.harness.config.sensitive_path_globs,
        )
        for path in refs:
            try:
                content = ws.read(path)
            except (SensitivePathError, PathOutsideWorkspaceError, OSError) as exc:
                note = f"could not ground: {type(exc).__name__}"
                task.add_feedback(f"@{path}", detail=note, kind="grounding")
            else:
                task.add_feedback(f"@{path}", detail=content[:_GROUND_BUDGET], kind="grounding")
