"""SessionState + ReplSession — the multi-turn scope above one task (§23, Phase 3.1 Lane 2a).

`TaskState` (§7) is per-goal and unchanged. `SessionState` is the scope *above* it: the
conversation `history`, the sequence of per-goal `tasks`, the session-scoped approval
`grants`, and the current `mode`. `ReplSession` is the thin driver: each goal becomes one
fresh `TaskState` run through the existing single-task `Session` (one code path — batch is
the degenerate one-`submit` case, §23.2), seeded with prior history and carrying grants
forward. The Textual cockpit (Lane 2b) renders this; here it stays pure logic.

Mode routing (revised ADR-0002 D3) is **visible and correctable, never hidden**: an
explicit `set_mode` override wins; else a one-shot LLM classification (`ModeClassifier`,
when configured) seeds `task_kind` from the prompt + conversation; else the hardened
word heuristic. The verdict is announced before the run and `/mode` re-routes.
**Plan mode** (ADR-0002 D5) is the one mode that isn't a `task_kind`:
a no-net-change plan task (`investigate` kind: the tree must net to zero diff at
verification, ADR-0005) → human approve/revise → the approved plan seeds the edit task as a
constraint; `submit_plan` drives that flow. Local **meta commands** (`/help` `/quit` `/state`
`/mode` `/plan` `/diff` `/permissions`) are handled by `run_meta` and never reach the
model (§23.2).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, Field

from avatar.config import HarnessConfig
from avatar.harness import Harness
from avatar.intent import ModeClassifier
from avatar.journal import JsonlEventJournal
from avatar.session import ApprovalGrant, Session
from avatar.state import ConversationTurn, TaskState
from avatar.workspace import PathOutsideWorkspaceError, SensitivePathError, Workspace

TaskKind = Literal["edit", "investigate", "test_only"]
_TASK_KINDS: tuple[TaskKind, ...] = ("edit", "investigate", "test_only")

# A session interaction mode: a `task_kind`, or `plan` — the plan→approve→build
# flow (ADR-0002 Decision 5). `plan` is not a `task_kind`; it routes through `submit_plan`.
Mode = Literal["edit", "investigate", "test_only", "plan"]
_MODES: tuple[Mode, ...] = (*_TASK_KINDS, "plan")

# Seeded as a constraint on the plan task so the model proposes a plan, not an answer
# (a directive, not the gate: ADR-0005 admits tier-1 writes in investigate tasks).
_PLAN_DIRECTIVE = "Plan mode: using only read tools, propose a concise step-by-step plan. Do not edit."

# Plan-run outcomes that are NOT approvable: the run never produced a usable plan (blocked on
# input, or exhausted a general budget). A *verifier-rejected* plan (`failed`) is still shown
# to the human — the loop is the human's authority, not the structural gate's (toward 3.2d).
_UNAPPROVABLE_PLAN_OUTCOMES = frozenset({"blocked", "incomplete"})

# Budget discipline (mirrors the harness's other loops): a programmatic `decide` that never
# approves cannot spin forever — each revision is a real model run.
_MAX_PLAN_REVISIONS = 10

_AT_PATH = re.compile(r"@(\S+)")  # `@path/to/file` grounding references in a goal
_GROUND_BUDGET = 2000  # per-file content cap — grounding is a hint, not a dump

_META_HELP = (
    "commands: /help · /quit · /state · /mode <edit|investigate|test_only|plan> · "
    "/plan · /diff · /permissions"
)


@dataclass(frozen=True)
class MetaResult:
    """The outcome of a local meta command — the cockpit interprets `kind`, displays `text`.

    `kind`: `message` (show text) · `mode_set` (mode changed) · `state` (session summary) ·
    `diff` (text is a unified diff → the diff modal) · `quit` (end the session).
    """

    kind: Literal["message", "mode_set", "state", "diff", "quit"]
    text: str


@dataclass(frozen=True)
class PlanDecision:
    """The human's verdict on a proposed plan — the `PlanModal` choice, decoupled from Textual.

    `approved`: build with this plan (transition into editing). `text`: the (possibly edited)
    plan — on approval an empty `text` keeps the proposed plan; on revise it is the revision
    request fed back into the re-planning turn.
    """

    approved: bool
    text: str


# Leading conversational filler skipped before the first significant word is judged —
# "Now make…" must not hide the edit verb (dogfood `events/04849a5a…jsonl`).
_FILLER_WORDS = frozenset(
    {"now", "please", "also", "then", "next", "and", "ok", "okay", "just", "can", "could", "you", "we"}
)

# Question openers that affirmatively signal an investigative goal.
_QUESTION_WORDS = frozenset(
    {
        "why",
        "how",
        "what",
        "when",
        "where",
        "who",
        "which",
        "explain",
        "describe",
        "show",
        "is",
        "are",
        "does",
        "do",
    }
)

# First significant word imperatives that signal an edit goal; question words signal
# investigate; anything else defaults to investigate.
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
    (questions, "explain …", "why …") defaults to investigation (grounded answer,
    net-zero diff at verification). This is the
    *visible* default the status bar shows and the user can override — not a classifier.

    Args:
        prompt: The user's natural-language goal.

    Returns:
        `"edit"` for an edit-shaped prompt, otherwise `"investigate"`.
    """
    for word in prompt.strip().split():
        token = word.lower().strip(",.!?:;")
        if token in _FILLER_WORDS:
            continue  # skip conversational filler — judge the first significant word
        if token in _EDIT_VERBS:
            return "edit"
        return "investigate"  # question words and everything else route to investigate
    return "investigate"


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
        mode: The explicit mode override (incl. `plan`), or `None` for the per-prompt heuristic.
    """

    session_id: str
    workspace_root: str
    config: HarnessConfig
    history: list[Turn] = Field(default_factory=list)
    tasks: list[TaskState] = Field(default_factory=list)
    grants: list[ApprovalGrant] = Field(default_factory=list)
    mode: Mode | None = None


class ReplSession:
    """Drives a multi-turn conversation over the unchanged single-task engine (§23.2).

    Each goal runs as one fresh `TaskState` through a per-goal `Session`; history seeds the
    next task and grants persist across tasks. `submit` is the simple run-to-completion
    path; `start`/`record` are the lower-level pair the cockpit uses so it can observe the
    per-task event stream and answer approvals between them.

    Args:
        harness: The configured `Harness`; supplies the per-goal run wiring.
        session_id: Stable conversation id; generated if omitted.
        auto: Terminal-boundary authority (§23.5, ADR-0046). The verifier steers (repair
            loop) either way; this only sets the disposition at repair exhaustion. The
            default (`False`) is *conversational* — the model repairs or proposes a gated
            amendment, and at exhaustion the turn defers to the human (`blocked` + an open
            question). `auto=True` is the strict §12 gate (repair exhaustion → `failed`; the
            `--auto` flag, wired by the CLI in 3.2e). A failed verdict is never advisory.
        journal: One write-ahead `JsonlEventJournal` for the whole sitting, threaded into
            every per-goal `Session` (shared by reference, like `grants`) so the multi-turn
            conversation lands in one durable file. Each goal's `bus.close()` closes the
            handle; `append` reopens it for the next goal. `None` (default) keeps the
            interactive stream in memory only.
        classifier: The LLM mode router (revised ADR-0002 D3); if omitted, built from
            `config.classifier_model` when set (`None`/empty → heuristic-only routing).
            Its verdict is displayed and `/mode`-overridable — visible, never silent.
        allow_dirty: `True` acknowledges a dirty tree at the *start* of the sitting
            (the `--allow-dirty` flag). Regardless of this, **session-owned dirt is
            always tolerated**: the §15 clean-start check applies to the sitting's first
            goal only — after that, uncommitted changes are the session's own work
            product, and a follow-up goal must not be refused because the previous one
            succeeded.
    """

    def __init__(
        self,
        harness: Harness,
        *,
        session_id: str | None = None,
        auto: bool = False,
        journal: JsonlEventJournal | None = None,
        classifier: ModeClassifier | None = None,
        allow_dirty: bool = False,
    ) -> None:
        self.harness = harness
        self.auto = auto
        self.journal = journal
        self.allow_dirty = allow_dirty
        if classifier is None and harness.config.classifier_model:
            classifier = ModeClassifier(harness.config)
        self.classifier = classifier
        self.last_mode_source: str = "heuristic"  # how the latest goal's mode was decided
        self._route_memo: tuple[str, Mode] | None = None  # one classification per prompt
        # Flipped once the first per-goal workspace opens successfully: from then on the
        # sitting owns its tree, and later goals open `allow_dirty` (multi-turn §15).
        self._tree_claimed = False
        self.state = SessionState(
            session_id=session_id or uuid4().hex,
            workspace_root=str(harness.config.workspace_root),
            config=harness.config,
        )

    @property
    def mode(self) -> Mode | None:
        """The explicit mode override (incl. `plan`), or `None` when the heuristic decides."""
        return self.state.mode

    def set_mode(self, mode: Mode) -> None:
        """Pin the mode for subsequent goals (the `/mode` override; overrides the heuristic).

        Args:
            mode: The mode to force on later goals until changed (a `task_kind`, or `plan`).
        """
        self.state.mode = mode

    def resolve_mode(self, prompt: str) -> Mode:
        """The mode for `prompt`: explicit override → classifier → heuristic.

        The classifier verdict is memoized per prompt (`start()` re-resolves
        internally; a goal pays at most one classification call) and any classifier
        failure degrades to the heuristic — routing can lose quality, never block.
        `last_mode_source` records how the verdict was reached, for display.

        Args:
            prompt: The user's goal.

        Returns:
            The resolved mode. Classifier/heuristic only ever yield a `task_kind`;
            `plan` is opt-in (set explicitly), never inferred.
        """
        if self.state.mode is not None:
            self.last_mode_source = "override"
            return self.state.mode
        if self._route_memo is not None and self._route_memo[0] == prompt:
            return self._route_memo[1]
        kind: str | None = None
        if self.classifier is not None:
            lines = [f"{t.role}: {t.text}" for t in self.state.history]
            kind = self.classifier.classify(prompt, history=lines)
        if kind in _TASK_KINDS:
            self.last_mode_source = "classifier"
        else:
            kind = default_mode(prompt)
            self.last_mode_source = "heuristic"
        resolved = cast(Mode, kind)
        self._route_memo = (prompt, resolved)
        return resolved

    def start(self, prompt: str) -> Session:
        """Build (but don't run) the next per-goal `Session`: resolve mode, seed history + the turn.

        In `plan` mode this is the **plan task** (`task_kind="investigate"` with the
        planning directive) — the first step of the plan flow the cockpit drives; otherwise it
        is a direct run of the resolved `task_kind`. The returned session is wired with the
        session-scoped grant list (shared by reference), so a `[a] always` persists across goals.

        Args:
            prompt: The user's goal.

        Returns:
            A not-yet-started `Session` for this goal.
        """
        resolved = self.resolve_mode(prompt)
        if resolved == "plan":
            return self.start_plan(prompt)
        return self._make_session(prompt, cast(TaskKind, resolved))

    def start_plan(self, prompt: str, *, revision: str | None = None) -> Session:
        """Build the plan `Session` for `prompt` (observable; the cockpit streams it).

        An `investigate` task (net-zero-diff contract, ADR-0005) seeded with the planning
        directive (and, on a revise,
        the revision note so the model refines). No user turn is appended — the plan flow
        records the goal's turn once via `record_goal`, so plan and build don't double it.

        Args:
            prompt: The user's goal.
            revision: A prior revision request to fold in, or `None` for the first plan.

        Returns:
            A not-yet-started plan `Session`.
        """
        constraints = [_PLAN_DIRECTIVE]
        if revision:
            constraints.append(f"Revision requested: {revision}")
        return self._make_session(prompt, "investigate", extra_constraints=constraints, append_turn=False)

    def start_build(self, prompt: str, plan: str) -> Session:
        """Build the edit task for an approved plan: the plan rides as a `constraint` (§12, D5).

        The build is a normal `edit` task — it rides the `investigating → editing` gate; the
        approved plan is surfaced to the model as a constraint. No user turn is appended: the
        goal's turn was recorded when planning began (this is the same goal, continued).

        Args:
            prompt: The user's goal.
            plan: The approved plan text to seed as a constraint.

        Returns:
            A not-yet-started edit `Session`.
        """
        return self._make_session(prompt, "edit", extra_constraints=[plan], append_turn=False)

    def _make_session(
        self,
        prompt: str,
        kind: TaskKind,
        *,
        extra_constraints: list[str] | None = None,
        append_turn: bool = True,
    ) -> Session:
        """Build a per-goal `Session` for `kind`, seeding prior history + `@path` grounding.

        Args:
            prompt: The user's goal.
            kind: The `task_kind` for the fresh `TaskState`.
            extra_constraints: Constraints to seed (a planning directive, an approved plan).
            append_turn: Whether to record the user turn now (off for the build step, whose
                turn was already recorded at planning time).

        Returns:
            A not-yet-started `Session` wired with the session-scoped grants.
        """
        task = TaskState(
            goal=prompt,
            task_kind=kind,
            constraints=list(extra_constraints or ()),
            mode_source=self.last_mode_source,  # how resolve_mode decided this sitting's kind
        )
        self._seed_history(task)  # prior turns become the task's conversation (before this turn is added)
        self._ground_paths(task, prompt)  # @path references seed the named files as context
        # The REPL is conversational by default (§23.5); `--auto` (self.auto) restores the strict gate.
        # Strict clean-start applies to the sitting's FIRST goal only: once a workspace has
        # opened, later dirt is the session's own work product (or deliberate user edits
        # with the human in the loop) — never grounds to refuse a follow-up.
        runner = self.harness._build_runner(
            allow_dirty=self.allow_dirty or self._tree_claimed, conversational=not self.auto
        )
        self._tree_claimed = True  # only reached when the workspace opened (no DirtyWorkspaceError)
        # Record the user turn ONLY after the workspace opens: a goal that fails to start
        # (e.g. DirtyWorkspaceError on the first goal) must not leave a phantom prompt that
        # `_seed_history` would replay as a real user turn on the next goal (ADR-0017).
        if append_turn:
            self.state.history.append(Turn(role="user", text=prompt))
        return Session(runner, task, grants=self.state.grants, journal=self.journal)

    def record(self, state: TaskState) -> None:
        """Record a finished goal: append the terminal task and the agent's reply turn.

        A goal that ended by asking the user records the **question** as its agent turn,
        not the bare outcome. Otherwise the next user message — which is the *answer* —
        seeds history as `agent: blocked` and reads to the model as a fresh, contextless
        goal, so it re-asks and the conversation goes in circles (dogfood
        `events/f0957ed4…jsonl`). Preference: the final answer, then the open question,
        then the outcome.

        Args:
            state: The terminal `TaskState` returned by `session.run()`.
        """
        self.state.tasks.append(state)
        # The open question stands in for the reply only when the goal actually blocked on
        # it. Guarding on `blocked` pins today's "an ask always blocks" assumption: once the
        # runner's anticipated interactive-answer path lets a goal answer an ask inline and
        # still succeed, a leftover open question must not shadow the real answer/outcome.
        asked = state.outcome == "blocked" and bool(state.open_questions)
        reply = state.final_answer or (state.open_questions[-1] if asked else None) or state.outcome or "done"
        self.state.history.append(Turn(role="agent", text=reply, task_id=state.task_id))
        # The routing memo is intra-goal only (start() re-resolves the same prompt):
        # a finished goal changes the conversation, so the same text next turn must
        # re-classify in the new context (PR-#32 review — "continue", "keep going").
        self._route_memo = None

    async def submit(self, prompt: str) -> TaskState:
        """Run one goal to completion and record it — the simple (batch-shaped) path.

        Args:
            prompt: The user's goal.

        Returns:
            The terminal `TaskState`.

        Raises:
            ValueError: In `plan` mode — planning is interactive (approve/revise), so it has
                no run-to-completion path; use `submit_plan`, or `set_mode` to switch modes.
        """
        if self.resolve_mode(prompt) == "plan":
            raise ValueError("plan mode is interactive — use submit_plan(prompt, decide), or set_mode(...)")
        session = self.start(prompt)
        state = await session.run()
        self.record(state)
        return state

    async def submit_plan(self, prompt: str, decide: Callable[[str], PlanDecision]) -> TaskState:
        """Drive the plan flow: plan → approve/revise → build (ADR-0002 D5, §23).

        Proposes a plan with a no-net-change `investigate` task, then calls `decide` (the `PlanModal` in the
        cockpit; an injected callback in tests). On revise it re-runs the plan task with the
        revision fed back so the model refines it; on approval it runs the edit task seeded
        with the approved plan as a constraint and returns its terminal state.

        A plan run that produced nothing usable — empty, or terminated as
        `blocked`/`incomplete` — is never offered for approval: its terminal planning state is
        recorded and returned instead (you can't approve `""`). A non-empty verifier-rejected
        plan *is* shown to the human (the human is the authority, not the structural gate). A
        `decide` that never approves stops at the revision budget and returns `incomplete`.

        Args:
            prompt: The user's goal.
            decide: Called with each proposed plan; returns the human's `PlanDecision`.

        Returns:
            The terminal `TaskState` — the build (edit) task on approval, else the terminal
            planning state when there was nothing approvable or the revision budget was hit.
        """
        plan_state = await self.start_plan(prompt).run()
        revisions = 0
        while True:
            if not self.plan_is_approvable(plan_state):
                return self.record_goal(prompt, plan_state)  # nothing approvable — surface it
            decision = decide(self.extract_plan(plan_state))
            if decision.approved:
                approved_plan = decision.text or self.extract_plan(plan_state)
                break
            revisions += 1
            if revisions >= _MAX_PLAN_REVISIONS:
                plan_state.add_feedback("plan revision budget exhausted; no build run", kind="blocker")
                plan_state.outcome = "incomplete"
                return self.record_goal(prompt, plan_state)
            plan_state = await self.start_plan(prompt, revision=decision.text).run()
        return self.record_goal(prompt, await self.start_build(prompt, approved_plan).run())

    def record_goal(self, prompt: str, state: TaskState) -> TaskState:
        """Record one plan-flow goal: append its user turn (once) and the terminal task.

        Used by the plan flow (`submit_plan` and the cockpit), whose plan/build sessions are
        built with `append_turn=False` — the goal's user turn is recorded here, *after* they
        run, so neither echoes the current goal into its own history evidence (matching `start`).

        Args:
            prompt: The user's goal.
            state: The terminal task to record (a build, or a surfaced planning state).

        Returns:
            The recorded terminal `TaskState`.
        """
        self.state.history.append(Turn(role="user", text=prompt))
        self.record(state)
        return state

    @staticmethod
    def extract_plan(state: TaskState) -> str:
        """The proposed plan text from a plan task: its `final_answer`, else its `current_plan`.

        Args:
            state: A terminal plan `TaskState`.

        Returns:
            The plan text (possibly empty when the run produced nothing).
        """
        return state.final_answer or "\n".join(state.current_plan)

    @staticmethod
    def plan_is_approvable(state: TaskState) -> bool:
        """Whether a finished plan run may be offered to the human for approval (§23.5).

        False for an empty plan (you can't approve `""`) or one that terminated abnormally
        (`blocked`/`incomplete`); a non-empty verifier-rejected (`failed`) plan is approvable
        — the human, not the structural gate, is the authority.

        Args:
            state: A terminal plan `TaskState`.

        Returns:
            True iff the plan is non-empty and did not terminate abnormally.
        """
        return (
            bool(ReplSession.extract_plan(state).strip()) and state.outcome not in _UNAPPROVABLE_PLAN_OUTCOMES
        )

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
        if cmd == "plan":
            self.set_mode("plan")
            return MetaResult(kind="mode_set", text="mode set to plan")
        if cmd == "state":
            # Strictly local: never resolve (the classifier is a network call, and meta
            # commands must not reach a model or spend tokens — §23.2, PR-#32 review).
            summary = (
                f"mode: {self.state.mode or 'auto'} · "
                f"tasks: {len(self.state.tasks)} · turns: {len(self.state.history)}"
            )
            return MetaResult(kind="state", text=summary)
        if cmd == "diff":
            return MetaResult(kind="diff", text=self._workspace_diff())
        if cmd == "permissions":
            return self._meta_permissions()
        return MetaResult(kind="message", text=f"unknown command: /{cmd} — {_META_HELP}")

    def _meta_mode(self, arg: str) -> MetaResult:
        """Set the mode from `/mode <arg>`, or report an invalid mode.

        Args:
            arg: The requested mode.

        Returns:
            A `mode_set` result on success, else a `message` error.
        """
        if arg in _MODES:
            self.set_mode(cast(Mode, arg))
            return MetaResult(kind="mode_set", text=f"mode set to {arg}")
        return MetaResult(
            kind="message", text=f"unknown mode: {arg} (use edit | investigate | test_only | plan)"
        )

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
            log_path=self.harness.config.log_path,
        )
        return ws.diff()

    def _seed_history(self, task: TaskState) -> None:
        """Seed prior conversation onto `task` as real chat turns (ADR-0017, not evidence bullets).

        Prior goals/replies become `ConversationTurn`s the model client replays as genuine
        `role="user"`/`role="assistant"` messages ahead of the working packet — the model
        under-weighted them as flattened "Recent evidence" and re-asked answered questions.
        An agent turn maps to `"assistant"`; a user turn to `"user"`.

        Args:
            task: The fresh per-goal `TaskState` to seed.
        """
        for turn in self.state.history:
            role: Literal["user", "assistant"] = "assistant" if turn.role == "agent" else "user"
            task.conversation.append(ConversationTurn(role=role, content=turn.text))

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
            log_path=self.harness.config.log_path,
        )
        for path in refs:
            try:
                content = ws.read(path)
            except (SensitivePathError, PathOutsideWorkspaceError, OSError) as exc:
                note = f"could not ground: {type(exc).__name__}"
                task.add_feedback(f"@{path}", detail=note, kind="grounding")
            else:
                task.add_feedback(f"@{path}", detail=content[:_GROUND_BUDGET], kind="grounding")
