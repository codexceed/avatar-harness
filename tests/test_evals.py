"""Eval-0 — Slice 1 (walking skeleton): spec, fixtures, scoring, runner, metrics.

Offline by construction: the runner integration test drives a `ScriptedModel`, so
no network/API spend. The live multi-model baseline is a separate step.
See docs/eval-harness-design.md.
"""

import json

import pytest
from conftest import ScriptedModel
from pydantic import ValidationError

from avatar_harness.config import HarnessConfig
from avatar_harness.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar_harness.workspace import Workspace
from evals.metrics import pass_at_1, pass_caret_k
from evals.provision import provision
from evals.result import ResultRow
from evals.run import run_task
from evals.score import is_solved, run_probe
from evals.spec import TaskSpec, load_task_spec

# --- A. task spec + fixtures ---------------------------------------------------


def test_taskspec_loads_and_validates(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text(
        'id = "create-chatbot"\n'
        'goal = "Write a python script for an OpenAI API compatible chatbot"\n'
        'task_kind = "edit"\n'
        'success_probe = "python evals/probes/chatbot_smoke.py"\n'
        "[budgets]\nmax_iterations = 30\n",
        encoding="utf-8",
    )
    spec = load_task_spec(p)
    assert isinstance(spec, TaskSpec)
    assert spec.id == "create-chatbot"
    assert spec.task_kind == "edit"
    assert spec.success_probe is not None
    assert spec.success_probe.endswith("chatbot_smoke.py")
    assert spec.budgets["max_iterations"] == 30
    assert spec.fixture == "empty"  # default


def test_taskspec_rejects_missing_required_fields(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text('id = "x"\n', encoding="utf-8")  # no goal
    with pytest.raises(ValidationError):
        load_task_spec(p)


def test_fixture_provisions_clean_git_repo(tmp_path):
    fixture = tmp_path / "fix"
    fixture.mkdir()
    (fixture / "app.py").write_text("x = 1\n", encoding="utf-8")
    repo = provision(fixture)
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    # A clean baseline: Workspace opens without DirtyWorkspaceError and sees no diff.
    ws = Workspace(repo)
    assert ws.diff() == ""


def test_empty_fixture_yields_bare_repo():
    repo = provision(None)
    ws = Workspace(repo)  # opens clean (empty initial commit)
    assert ws.diff() == ""


# --- B. scoring + probe --------------------------------------------------------


@pytest.mark.parametrize(
    ("verifier_passed", "probe_exit", "expected"),
    [
        (True, 0, True),
        (True, 1, False),
        (False, 0, False),
        (False, 1, False),
        (True, None, True),  # no probe declared -> verifier alone decides
        (False, None, False),
    ],
)
def test_score_requires_verifier_pass_and_probe_zero(verifier_passed, probe_exit, expected):
    assert is_solved(verifier_passed, probe_exit) is expected


def test_probe_runs_in_scratch_repo_and_sees_agent_output(tmp_path):
    (tmp_path / "out.txt").write_text("ok", encoding="utf-8")
    assert run_probe("test -f out.txt", tmp_path) == 0
    assert run_probe("test -f missing.txt", tmp_path) != 0


def test_probe_nonzero_marks_unsolved_even_if_verifier_passed():
    # The verifier-leakage guard: a passing verifier cannot rescue a failing probe.
    assert is_solved(True, 1) is False


# --- C. runner integration (ScriptedModel, no network) ------------------------


def test_eval_run_produces_result_row():
    spec = TaskSpec(
        id="create-chatbot",
        goal="Write a python script for an OpenAI API compatible chatbot",
        task_kind="edit",
        fixture="empty",
    )
    decisions = [
        ModelDecision(
            action=ToolCall(
                name="write_file",
                input={"path": "chat_bot.py", "content": "import openai\n\ndef chat():\n    pass\n"},
            )
        ),
        ModelDecision(action=FinalAnswer(answer="created chat_bot.py")),
    ]
    row = run_task(spec, config=HarnessConfig(), model_client=ScriptedModel(decisions), seed=0)
    assert isinstance(row, ResultRow)
    assert row.task == "create-chatbot"
    assert row.seed == 0
    assert row.outcome is not None
    assert row.iterations >= 1
    assert isinstance(row.solved, bool)


def test_result_row_is_jsonl_roundtrippable():
    row = ResultRow(
        task="x",
        model="m",
        seed=1,
        solved=True,
        outcome="success",
        iterations=3,
        prompt_tokens=10,
        completion_tokens=2,
        probe_exit=0,
    )
    line = row.to_jsonl()
    assert "\n" not in line  # one JSONL row
    back = ResultRow.model_validate(json.loads(line))
    assert back == row


# --- D. metrics ----------------------------------------------------------------


def _row(task: str, seed: int, solved: bool) -> ResultRow:
    return ResultRow(
        task=task,
        model="m",
        seed=seed,
        solved=solved,
        outcome="success" if solved else "failed",
        iterations=1,
    )


def test_pass_at_1():
    rows = [_row("a", 0, True), _row("a", 1, False), _row("b", 0, True), _row("b", 1, True)]
    assert pass_at_1(rows) == pytest.approx(0.75)


def test_pass_caret_k():
    # pass^k: a task counts only if ALL its seeds passed (reliability, not capability).
    rows = [_row("a", 0, True), _row("a", 1, True), _row("b", 0, True), _row("b", 1, False)]
    assert pass_caret_k(rows) == pytest.approx(0.5)  # a all-pass, b not
