"""Shell syntax is rejected, not interpreted, at command boundaries (ADR-0045).

Every command string executes via `shlex.split` + no-shell `subprocess.run`
(`Workspace.run`), so shell operators are never operators — they arrive as literal
argv items of the FIRST program. The motivating dogfood journal
(`tetris_glm/events/be46ea273029486fbc62ac5360a6c82f.jsonl`) shows both failure modes:

- **Silent false pass:** a declared `grep -q A f.md && grep -q B f.md && …` chain ran
  as ONE grep whose later patterns became unopenable *filenames*; `-q` exits 0 on the
  first match despite those errors, so verification "passed" checking 1 of 10 sections.
- **Hang:** a declared heredoc (`python3 - <<'EOF' …`) blocked forever on stdin no
  shell would ever feed, timed out, and the unamendable frozen contract fed a
  finalization spiral that burned the run to `incomplete`.

The boundary rule pinned here: `&&` normalizes to check *conjunction* (one
`PlannedCheck` per segment — execution finally matches the planner's per-segment
classification); every other operator (`;`, `|`, `||`, redirects, heredocs) is a
model-correctable rejection with a steer. `run_command` rejects all of them,
chains included — silently mis-executing is worse than an error.
"""

import shlex

import pytest

from avatar.config import HarnessConfig
from avatar.deps import CancellationToken, RunDeps
from avatar.state import TaskState
from avatar.tools.commands import run_command
from avatar.tools.verification import (
    AlterVerificationInput,
    DeclaredCheckInput,
    DeclareVerificationInput,
    alter_verification,
    declare_verification,
)
from avatar.verifier import Verifier
from avatar.workspace import Workspace


def _deps(tmp_path, **cfg) -> RunDeps:
    return RunDeps(
        workspace=Workspace(tmp_path), config=HarnessConfig(**cfg), cancellation=CancellationToken()
    )


def _declare(deps: RunDeps, commands: list[str], **kwargs):
    args = DeclareVerificationInput(checks=[DeclaredCheckInput(command=c) for c in commands], **kwargs)
    return declare_verification.handler(args, deps)


# --- declaration time: `&&` splits into conjunction ----------------------------------------


def test_declared_conjunction_splits_into_checks(tmp_path):
    # One chained declaration becomes N frozen checks — each a clean single argv, so the
    # verifier runs (and can fail) EVERY segment instead of one mangled grep.
    deps = _deps(tmp_path)
    result = _declare(
        deps,
        ["grep -q '^# ASCII Tetris' DESIGN.md && grep -q '^## Overview' DESIGN.md"],
        change_kinds=["content"],
    )
    assert result.success, result.error
    assert deps.declared_contract is not None
    commands = [c.command for c in deps.declared_contract]
    assert len(commands) == 2
    assert not any("&&" in c for c in commands)
    assert deps.declared_contract[0].name == "declared_1"
    assert deps.declared_contract[1].name == "declared_2"


def test_declared_conjunction_split_is_quote_aware(tmp_path):
    # `&&` inside quotes is data, not an operator — the regex-blind planner split is for
    # classification; the execution-side split must respect quoting.
    deps = _deps(tmp_path)
    result = _declare(deps, ['python -c "print(1 and 2)" && pytest'], change_kinds=["code"])
    assert result.success, result.error
    assert deps.declared_contract is not None
    commands = [c.command for c in deps.declared_contract]
    assert len(commands) == 2
    assert shlex.split(commands[0]) == ["python", "-c", "print(1 and 2)"]
    assert shlex.split(commands[1]) == ["pytest"]


def test_declared_heredoc_rejected_with_steer(tmp_path):
    # The journal's `declared_2`: frozen, then hung at verification-shaped execution.
    # Now it dies at declaration with a steer to the portable form.
    deps = _deps(tmp_path)
    result = _declare(deps, ["python3 - <<'EOF'\nimport tetromino\nEOF"], change_kinds=["code"])
    assert not result.success
    assert "without a shell" in (result.error or "")
    assert "script file" in (result.error or "")


@pytest.mark.parametrize(
    "command",
    [
        "pytest | tee out.log",
        "pytest; ruff check .",
        "pytest || true",
        "pytest > out.txt",
        "pytest test_x.py 2>&1",
    ],
)
def test_declared_shell_operators_rejected(tmp_path, command):
    # `|`, `;`, `||`, redirects: no no-shell equivalent exists (unlike `&&` → conjunction),
    # so they reject as model-correctable rather than freezing an unrunnable contract.
    deps = _deps(tmp_path)
    result = _declare(deps, [command], change_kinds=["code"])
    assert not result.success, command
    assert "without a shell" in (result.error or "")


def test_alter_verification_enforces_the_same_boundary(tmp_path):
    # Session 1's escape hatch: the AMENDED contract is where the false-pass chain
    # actually entered. Same seam, same rule.
    deps = _deps(tmp_path)
    rejected = alter_verification.handler(
        AlterVerificationInput(
            checks=[DeclaredCheckInput(command="pytest | tee out.log")],
            rationale="obsolete",
            change_kinds=["code"],
        ),
        deps,
    )
    assert not rejected.success
    assert "without a shell" in (rejected.error or "")

    split = alter_verification.handler(
        AlterVerificationInput(
            checks=[DeclaredCheckInput(command="python -m pytest && python -c 'import game'")],
            rationale="obsolete",
            change_kinds=["code"],
        ),
        deps,
    )
    assert split.success, split.error
    assert deps.declared_contract is not None and len(deps.declared_contract) == 2


def test_quoted_operator_argument_rejected_legibly(tmp_path):
    # PR #112 review: posix lexing strips quotes, so `grep -q '&&' f` is indistinguishable
    # from an operator at token level and silently mis-split into two segments — the exact
    # defect class ADR-0045 exists to eliminate. Reject it legibly instead.
    deps = _deps(tmp_path)
    result = _declare(deps, ["grep -q '&&' README.md"], change_kinds=["content"])
    assert not result.success
    assert "quoted" in (result.error or "")


def test_declared_chain_segments_share_a_chain_id(tmp_path):
    # Split segments keep their `&&` lineage so the verifier can preserve short-circuit
    # semantics; independent checks carry no chain.
    deps = _deps(tmp_path)
    result = _declare(
        deps,
        ["python -m pytest && python -c 'import game'", "python -c 'import extra'"],
        change_kinds=["code"],
    )
    assert result.success, result.error
    assert deps.declared_contract is not None
    first, second, lone = deps.declared_contract
    assert first.chain is not None and first.chain == second.chain
    assert lone.chain is None


def test_verifier_short_circuits_a_failed_chain(git_repo):
    # PR #112 review P1: `a && b` split into independent checks ran `b` even when `a`
    # failed — unlike shell, a failing check no longer guards a later mutating command.
    # The chain must stop at the first failure; the skipped segment reports legibly.
    ws = Workspace(git_repo)
    (git_repo / "game.py").write_text("X = 1\n", encoding="utf-8")
    ws.stage(["game.py"])
    deps = RunDeps(workspace=ws, config=HarnessConfig(), cancellation=CancellationToken())
    chain = 'python -c \'import sys; sys.exit(1)\' && python -c \'open("mutated.txt", "w").write("x")\''
    declared = _declare(deps, [chain], change_kinds=["code"])
    assert declared.success, declared.error
    assert deps.declared_contract is not None

    state = TaskState(goal="g", task_kind="edit", files_modified={"game.py"}, declared_change_kinds=["code"])
    state.freeze_verification_plan(deps.declared_contract)
    report = Verifier(HarnessConfig(lint_command="")).verify(state, ws)

    assert report.passed is False
    assert not (git_repo / "mutated.txt").exists()  # the mutation never ran
    skipped = [c for c in report.checks if "not run" in c.evidence]
    assert skipped and skipped[0].status == "fail"  # skipped-after-fail is never a pass


# --- verification time: the journal's false pass must fail ---------------------------------


def test_verifier_fails_partial_content_after_split(git_repo):
    # The session-1 replay: DESIGN.md has the FIRST declared section only. Under the old
    # mangling the chain ran as one `grep -q` that exit-0'd on the first match (later
    # patterns became unopenable filenames) → verification passed vacuously. Split into
    # conjunction, the second check genuinely runs and fails.
    ws = Workspace(git_repo)  # open on the clean pinned HEAD, then edit like a run would
    (git_repo / "DESIGN.md").write_text("# ASCII Tetris\n", encoding="utf-8")
    ws.stage(["DESIGN.md"])
    deps = RunDeps(workspace=ws, config=HarnessConfig(), cancellation=CancellationToken())
    declared = _declare(
        deps,
        ["grep -q '^# ASCII Tetris' DESIGN.md && grep -q '^## Scoring' DESIGN.md"],
        change_kinds=["content"],
    )
    assert declared.success, declared.error
    assert deps.declared_contract is not None

    state = TaskState(
        goal="design spec",
        task_kind="edit",
        files_modified={"DESIGN.md"},
        declared_change_kinds=["content"],
    )
    state.freeze_verification_plan(deps.declared_contract)
    report = Verifier(HarnessConfig(lint_command="")).verify(state, ws)

    declared_checks = [c for c in report.checks if c.name.startswith("declared_")]
    assert len(declared_checks) == 2  # both segments are real, separately-graded checks
    assert any(c.status == "fail" for c in declared_checks)  # the missing section is CAUGHT
    assert report.passed is False


# --- run_command: reject, don't silently mis-execute ---------------------------------------


def test_run_command_rejects_chained_commands(tmp_path):
    # Session 2's fake evidence: `python3 test_logic.py && python3 -c "import …"` ran ONLY
    # the first program (the rest became its sys.argv) yet reported exit=0. Reject instead.
    result = run_command.handler(
        run_command.input_model(command="python3 test_logic.py && python3 -c 'import game'"),
        _deps(tmp_path),
    )
    assert not result.success
    assert "without a shell" in (result.error or "")
    assert "own call" in (result.error or "")


def test_run_command_rejects_heredoc_instead_of_hanging(tmp_path):
    # The journal's event 83: `python3 - <<'EOF'` blocked on stdin until the timeout.
    # Now it is refused instantly, with the steer to a script file.
    result = run_command.handler(
        run_command.input_model(command="python3 - <<'EOF'\nprint('x')\nEOF"),
        _deps(tmp_path, command_timeout_seconds=1),
    )
    assert not result.success
    assert "without a shell" in (result.error or "")
    assert "script file" in (result.error or "")
