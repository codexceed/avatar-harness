"""Phase 3.1 — prefix-scoped `ApprovalGrant`: a session-scoped standing approval.

`[a] always` on a tier-3 `run_command` stores a grant `(tool, program-prefix, tier)`
so later commands sharing that program auto-allow *without re-prompting a human*;
non-matching commands still prompt. Never global (empty prefix matches nothing),
never tier-4 (destructive stays human-gated).

The grant is a **`Session` (control-plane) affordance**, not part of `PermissionPolicy`:
the harness gate still returns `ask` for every tier-3 call (invariant #4 — the gate is
harness-owned); the Session may *answer* from a remembered grant. The runner's
`request_approval` path is unchanged. Auto-allows stay observable via
`ApprovalResolved(via="grant")` (invariant #5); a granted call emits **no**
`ApprovalRequested` (that event means "a human must decide", and a grant skips the human).
"""

import asyncio

from conftest import ScriptedModel

from avatar.config import HarnessConfig
from avatar.context import ContextBuilder
from avatar.deps import CancellationToken, RunDeps
from avatar.events import Emitter
from avatar.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar.runner import AgentRunner
from avatar.session import ApprovalGrant, Session
from avatar.state import TaskState
from avatar.tools.base import ToolRegistry
from avatar.tools.commands import run_command
from avatar.tools.filesystem import read_file
from avatar.tools.verification import alter_verification
from avatar.verifier import Verifier
from avatar.workspace import Workspace

# Two commands sharing the program `python`; a third with a different program `git`.
_PY_A = "python -c \"open('a','w').write('1')\""
_PY_B = "python -c \"open('b','w').write('1')\""
_GIT = "git --version"


def _deps(tmp_path) -> RunDeps:
    return RunDeps(workspace=Workspace(tmp_path), config=HarnessConfig(), cancellation=CancellationToken())


def _session(tmp_path, commands: list[str]) -> Session:
    """A session whose scripted model issues each command, then finalizes."""
    reg = ToolRegistry()
    reg.register(run_command)
    reg.register(read_file)
    decisions: list[ModelDecision] = [
        ModelDecision(action=ToolCall(name="run_command", input={"command": c})) for c in commands
    ]
    decisions.append(ModelDecision(action=FinalAnswer(answer="done")))
    runner = AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=reg,
        deps=_deps(tmp_path),
        context_builder=ContextBuilder(),
        verifier=Verifier(HarnessConfig()),
        emitter=Emitter(),
        config=HarnessConfig(),
    )
    # run_command isn't advertised in `investigating`; start an edit task already in `editing`.
    return Session(runner, TaskState(goal="run them", task_kind="edit", phase="editing"))


async def _drive(session: Session, *, allow: bool = True, remember_first: bool = False):
    """Run `session`, resolving every approval (the first optionally with `remember=True`).

    Returns (requested_events, resolved_events) so a test can assert how many human
    prompts fired and how each was decided.
    """
    requested: list = []
    resolved: list = []
    stream = session.events()  # subscribe eagerly, before the run task starts
    run_task = asyncio.create_task(session.run())
    async for ev in stream:
        if ev.type == "approval_requested":
            first = not requested
            requested.append(ev)
            await session.resolve_approval(ev.approval_id, allow=allow, remember=(remember_first and first))
        elif ev.type == "approval_resolved":
            resolved.append(ev)
    await run_task
    return requested, resolved


# --- behavioral, through a live Session -------------------------------------------------


async def test_grant_auto_allows_matching_prefix(tmp_path):
    # Approve `python …` with remember=True; a second `python …` (different args) auto-allows
    # with NO second human prompt.
    session = _session(tmp_path, [_PY_A, _PY_B])
    requested, _ = await _drive(session, remember_first=True)
    assert len(requested) == 1  # only the first call prompted a human
    assert (tmp_path / "a").exists() and (tmp_path / "b").exists()  # both commands ran


async def test_grant_does_not_cover_different_program(tmp_path):
    # After granting `python`, a `git …` call is a different program → still prompts.
    session = _session(tmp_path, [_PY_A, _GIT])
    requested, _ = await _drive(session, remember_first=True)
    assert len(requested) == 2  # the non-matching program re-prompted


async def test_remember_false_does_not_persist(tmp_path):
    # Approving without `remember` stores no grant; the next `python …` prompts again.
    session = _session(tmp_path, [_PY_A, _PY_B])
    requested, _ = await _drive(session, remember_first=False)
    assert len(requested) == 2  # yes-once never became always


async def test_grant_is_per_session_not_global(tmp_path):
    # Granting `python` in session A must not auto-allow `python` in a fresh session B.
    session_a = _session(tmp_path, [_PY_A])
    await _drive(session_a, remember_first=True)
    assert session_a._grants  # sanity: A did record the grant

    session_b = _session(tmp_path, [_PY_B])
    assert session_b._grants == []  # no process-global / cross-session leak
    requested, _ = await _drive(session_b, remember_first=False)
    assert len(requested) == 1  # B still prompted (grant did not transfer)


async def test_grant_auto_allow_is_observable(tmp_path):
    # The auto-allowed call is recorded as an ApprovalResolved(via="grant"); the human
    # prompt was resolved via="human". Observability holds without a second prompt (inv #5).
    session = _session(tmp_path, [_PY_A, _PY_B])
    _, resolved = await _drive(session, remember_first=True)
    vias = [ev.via for ev in resolved]
    assert vias == ["human", "grant"]
    assert all(ev.allowed for ev in resolved)


async def test_resolve_approval_remember_records_grant(tmp_path):
    # Resolving the prompt with allow+remember appends exactly one grant with the
    # program prefix and tier derived from the pending request.
    session = _session(tmp_path, [_PY_A])
    await _drive(session, remember_first=True)
    assert session._grants == [ApprovalGrant(tool="run_command", prefix="python", tier=3)]


async def test_deny_with_remember_stores_nothing(tmp_path):
    # `remember` only applies to an allow — there is no "always deny".
    session = _session(tmp_path, [_PY_A])
    await _drive(session, allow=False, remember_first=True)
    assert session._grants == []


# --- pure unit on the match predicate ---------------------------------------------------


def test_grant_never_covers_higher_tier():
    grant = ApprovalGrant(tool="run_command", prefix="python", tier=3)
    assert grant.matches("run_command", "python", 3)  # exact match auto-allows
    assert not grant.matches("run_command", "python", 4)  # never tier-4 (destructive)
    assert not grant.matches("run_command", "git", 3)  # different program
    assert not grant.matches("other_tool", "python", 3)  # different tool
    assert not ApprovalGrant(tool="run_command", prefix="", tier=3).matches(
        "run_command", "", 3
    )  # empty prefix is never a global grant


# --- contract amendments are never grantable (ADR-0038/0039) -----------------------------


def _amendment_session(tmp_path, grants: list[ApprovalGrant]) -> Session:
    """A session with `alter_verification` registered (tier 3) and an injected grant list."""
    reg = ToolRegistry()
    reg.register(run_command)
    reg.register(alter_verification)
    runner = AgentRunner(
        model_client=ScriptedModel([ModelDecision(action=FinalAnswer(answer="done"))]),
        registry=reg,
        deps=_deps(tmp_path),
        context_builder=ContextBuilder(),
        verifier=Verifier(HarnessConfig()),
        emitter=Emitter(),
        config=HarnessConfig(),
    )
    return Session(runner, TaskState(goal="amend", task_kind="edit", phase="editing"), grants=grants)


async def test_remember_on_an_amendment_stores_no_grant(tmp_path):
    # `[a] always` on an alter_verification approval must degrade to allow-once: a standing
    # grant would let the model re-move its own goalposts silently for the rest of the
    # session. The amendment itself is allowed; no grant lands in the session list.
    grants: list[ApprovalGrant] = []
    session = _amendment_session(tmp_path, grants)
    decision = asyncio.create_task(
        session.request_approval(
            "ap-1",
            "alter_verification",
            "amend the contract",
            {"checks": [{"command": "python -m pytest test_y.py"}], "rationale": "r"},
        )
    )
    await asyncio.sleep(0.01)  # let the request register as pending
    await session.resolve_approval("ap-1", allow=True, remember=True)
    assert await decision is True  # this one amendment is allowed...
    assert grants == []  # ...but nothing standing was stored


async def test_amendment_grant_never_matches_even_if_present():
    # Belt-and-braces beneath the storage guard: even a hand-built (or replayed/persisted)
    # amendment grant must never auto-allow — every amendment is ratified by a human.
    grant = ApprovalGrant(tool="alter_verification", prefix="alter_verification", tier=3)
    assert not grant.matches("alter_verification", "alter_verification", 3)
