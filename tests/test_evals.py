"""Eval-0 — Slice 1 (walking skeleton): spec, fixtures, scoring, runner, metrics.

Offline by construction: the runner integration test drives a `ScriptedModel`, so
no network/API spend. The live multi-model baseline is a separate step.
See docs/eval-harness-design.md.
"""

import json
import math
import shutil
from pathlib import Path

import pytest
from conftest import ScriptedModel
from pydantic import ValidationError

from avatar_harness.config import HarnessConfig
from avatar_harness.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar_harness.workspace import Workspace
from evals.classify import classify, failure_histogram
from evals.metrics import pass_at_1, pass_caret_k
from evals.provision import provision
from evals.result import ResultRow, load_results
from evals.run import _cleanup_workspaces, _journal_events, _resolve_run_workspace, run_task
from evals.score import is_solved, run_probe
from evals.spec import TaskSpec, load_task_spec
from evals.stats import mcnemar, mean_ci

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
        # A probe, when declared, is the AUTHORITATIVE signal (option A): the verifier
        # verdict is ignored — a fresh creation can't satisfy the edit gate, so a working
        # probe-passing chatbot must still score solved.
        (True, 0, True),
        (False, 0, True),  # verifier failed (no test contract) but the probe passed -> solved
        (True, 1, False),
        (False, 1, False),
        # No probe -> the harness verifier decides (e.g. investigate's grounded-answer gate).
        (True, None, True),
        (False, None, False),
    ],
)
def test_probe_is_authoritative_when_present_else_verifier(verifier_passed, probe_exit, expected):
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


# --- E. per-task env (Q3): the user declares the program's runtime env ---------


def test_taskspec_carries_env(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('id = "x"\ngoal = "g"\n[env]\nOPENAI_API_KEY = "sk-eval-dummy"\n', encoding="utf-8")
    assert load_task_spec(p).env == {"OPENAI_API_KEY": "sk-eval-dummy"}


def test_taskspec_env_defaults_empty(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('id = "x"\ngoal = "g"\n', encoding="utf-8")
    assert load_task_spec(p).env == {}


def test_probe_respects_env_vars(tmp_path):
    cmd = "python -c \"import os,sys; sys.exit(0 if os.environ.get('EVAL_X')=='y' else 3)\""
    assert run_probe(cmd, tmp_path, env={"EVAL_X": "y"}) == 0
    assert run_probe(cmd, tmp_path) == 3  # absent without the task env


# --- F. run workspace in cwd + cleanup (#1, #4) -------------------------------


def test_resultrow_records_workspace():
    row = ResultRow(
        task="x", model="m", seed=0, solved=True, outcome="success", iterations=1, workspace="/tmp/eval_x"
    )
    assert row.workspace == "/tmp/eval_x"
    assert ResultRow.model_validate(json.loads(row.to_jsonl())).workspace == "/tmp/eval_x"


def test_run_task_provisions_under_workspace_root(tmp_path):
    run_dir = tmp_path / "eval_run_TS"
    run_dir.mkdir()
    spec = TaskSpec(id="create-chatbot", goal="g", task_kind="edit", fixture="empty")
    decisions = [
        ModelDecision(
            action=ToolCall(name="write_file", input={"path": "chatbot.py", "content": "import openai\n"})
        ),
        ModelDecision(action=FinalAnswer(answer="done")),
    ]
    row = run_task(
        spec, config=HarnessConfig(), model_client=ScriptedModel(decisions), seed=0, workspace_root=run_dir
    )
    assert row.workspace is not None
    assert Path(row.workspace).parent == run_dir  # provisioned UNDER the run workspace
    assert (Path(row.workspace) / "chatbot.py").exists()


def test_resolve_run_workspace_explicit_and_auto(tmp_path):
    existing = tmp_path / "given"
    existing.mkdir()
    path, preexisting = _resolve_run_workspace(str(existing), "TS")
    assert path == existing and preexisting is True

    newp = tmp_path / "newws"
    path, preexisting = _resolve_run_workspace(str(newp), "TS")
    assert path == newp and preexisting is False and newp.exists()

    path, preexisting = _resolve_run_workspace(None, "TS")  # auto -> cwd/eval_run_<stamp>
    try:
        assert path.name == "eval_run_TS" and path.parent == Path.cwd() and preexisting is False
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_cleanup_removes_created_run_workspace(tmp_path):
    run_dir = tmp_path / "eval_run_TS"
    (run_dir / "sub").mkdir(parents=True)
    _cleanup_workspaces([], run_dir, preexisting=False)
    assert not run_dir.exists()  # auto-created -> whole run dir removed


def test_cleanup_preexisting_removes_only_row_subdirs(tmp_path):
    run_dir = tmp_path / "given"
    run_dir.mkdir()
    keep = run_dir / "preexisting_stuff"
    keep.mkdir()
    scratch = run_dir / "scratch"
    scratch.mkdir()
    row = ResultRow(
        task="x", model="m", seed=0, solved=True, outcome="success", iterations=1, workspace=str(scratch)
    )
    _cleanup_workspaces([row], run_dir, preexisting=True)
    assert not scratch.exists()  # our scratch removed
    assert keep.exists() and run_dir.exists()  # the user's dir + content preserved


# --- G. results loader (#3 — cross-run reading) -------------------------------


def test_load_results_round_trips(tmp_path):
    rows = [
        ResultRow(task="a", model="m", seed=0, solved=True, outcome="success", iterations=2),
        ResultRow(task="b", model="m", seed=1, solved=False, outcome="failed", iterations=5),
    ]
    p = tmp_path / "r.jsonl"
    p.write_text("".join(r.to_jsonl() + "\n" for r in rows), encoding="utf-8")
    assert load_results(p) == rows


def test_load_results_skips_blank_lines(tmp_path):
    p = tmp_path / "r.jsonl"
    row = ResultRow(task="a", model="m", seed=0, solved=True, outcome="success", iterations=1)
    p.write_text(row.to_jsonl() + "\n\n", encoding="utf-8")  # trailing blank line
    assert load_results(p) == [row]


# --- H. failure-mode classifier (Slice 2) -------------------------------------


def _frow(outcome: str, solved: bool, probe_exit=None) -> ResultRow:
    return ResultRow(
        task="t", model="m", seed=0, solved=solved, outcome=outcome, iterations=1, probe_exit=probe_exit
    )


@pytest.mark.parametrize(
    ("outcome", "solved", "probe_exit", "bucket"),
    [
        ("success", True, 0, "solved"),
        ("failed", False, None, "verification_failed"),
        ("incomplete", False, None, "budget_exhausted"),
        ("blocked", False, None, "blocked"),
        ("error: BadRequestError: 400", False, None, "harness_error"),
        ("success", False, 1, "probe_failed"),  # declared done, but the code doesn't work
    ],
)
def test_classify_buckets(outcome, solved, probe_exit, bucket):
    assert classify(_frow(outcome, solved, probe_exit)) == bucket


def test_classify_loop_oscillation_from_events():
    row = _frow("incomplete", False)
    events = [{"type": "model_decision", "action": "read_file({'path': 'a.py'})"} for _ in range(4)]
    assert classify(row, events) == "loop_oscillation"


def test_classify_decision_error_from_events():
    row = _frow("incomplete", False)
    events = [{"type": "decision_error", "error": "bad"} for _ in range(3)]
    assert classify(row, events) == "decision_error"


def test_failure_histogram_counts_failures_only():
    rows = [_frow("success", True, 0), _frow("failed", False), _frow("incomplete", False)]
    assert failure_histogram(rows) == {"verification_failed": 1, "budget_exhausted": 1}


# --- I. statistics: clustered CI + paired McNemar (Slice 2) -------------------


def test_mean_ci_clusters_by_task():
    # task a: 2/2, task b: 0/2 -> grand mean 0.5; cluster-means SE = stdev([1,0])/sqrt(2) = 0.5
    rows = [_row("a", 0, True), _row("a", 1, True), _row("b", 0, False), _row("b", 1, False)]
    ci = mean_ci(rows)
    assert ci.mean == pytest.approx(0.5)
    assert ci.se == pytest.approx(0.5)
    assert ci.lo == 0.0 and ci.hi == 1.0  # capped to [0, 1]


def test_mean_ci_single_task_falls_back_to_binomial():
    rows = [_row("a", 0, True), _row("a", 1, True), _row("a", 2, False)]  # one cluster, 2/3
    ci = mean_ci(rows)
    assert ci.mean == pytest.approx(2 / 3)
    assert ci.se == pytest.approx(math.sqrt((2 / 3) * (1 / 3) / 3))  # can't cluster with 1 task


def test_mcnemar_no_change():
    base = [_row("a", 0, True), _row("a", 1, True)]
    cand = [_row("a", 0, True), _row("a", 1, True)]
    r = mcnemar(base, cand)
    assert r.regressions == 0 and r.improvements == 0 and r.p_value == 1.0


def test_mcnemar_detects_regression():
    base = [_row("a", i, True) for i in range(6)]
    cand = [_row("a", i, False) for i in range(6)]
    r = mcnemar(base, cand)
    assert r.regressions == 6 and r.improvements == 0 and r.n_pairs == 6
    assert r.p_value == pytest.approx(2 * 0.5**6)  # exact two-sided sign test = 0.03125


def test_mcnemar_pairs_only_shared_keys():
    base = [_row("a", 0, True), _row("b", 0, True)]
    cand = [_row("a", 0, False)]  # only task a, seed 0 is shared
    r = mcnemar(base, cand)
    assert r.n_pairs == 1 and r.regressions == 1


# --- J. the remaining single-turn tasks: specs load + probes work (Slice 2) ----

_PROBES = Path(__file__).resolve().parent.parent / "evals" / "probes"
_TASKS = Path(__file__).resolve().parent.parent / "evals" / "tasks"


def test_all_seed_specs_load():
    specs = [load_task_spec(p) for p in sorted(_TASKS.glob("*.toml"))]
    ids = {s.id for s in specs}
    assert {"create-chatbot", "modify-existing", "investigate-question", "secret-safety"} <= ids


def test_calc_fixed_probe(tmp_path):
    # Separate dirs: each eval run gets a fresh scratch repo, so don't share a __pycache__.
    good = tmp_path / "good"
    good.mkdir()
    (good / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert run_probe(f"python {_PROBES / 'calc_fixed.py'}", good) == 0  # fixed -> solved
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    assert run_probe(f"python {_PROBES / 'calc_fixed.py'}", bad) == 1  # still buggy


def test_no_secret_leak_probe(tmp_path):
    (tmp_path / "journal.jsonl").write_text('{"x":"sk-eval-SENTINEL-DONOTLEAK"}\n', encoding="utf-8")
    assert run_probe(f"python {_PROBES / 'no_secret_leak.py'}", tmp_path) == 1  # leaked
    (tmp_path / "journal.jsonl").write_text('{"x":"clean"}\n', encoding="utf-8")
    assert run_probe(f"python {_PROBES / 'no_secret_leak.py'}", tmp_path) == 0  # safe


# --- K. review follow-ups: live histogram path + probe-bearing scoring contract -----


def test_failure_histogram_uses_events_resolver():
    # DO3: with an events resolver, an incomplete run refines to loop_oscillation (unreachable
    # row-only). This is the live-path behavior run.py now exercises.
    row = _frow("incomplete", False)
    events = [{"type": "model_decision", "action": "read_file({})"} for _ in range(4)]
    assert failure_histogram([row], events_for=lambda _r: events) == {"loop_oscillation": 1}


def test_journal_events_reads_row_workspace(tmp_path):
    (tmp_path / "journal.jsonl").write_text('{"type":"model_decision","action":"x"}\n\n', encoding="utf-8")
    row = ResultRow(
        task="t", model="m", seed=0, solved=False, outcome="incomplete", iterations=1, workspace=str(tmp_path)
    )
    assert _journal_events(row) == [{"type": "model_decision", "action": "x"}]


def test_journal_events_missing_is_empty():
    row = ResultRow(task="t", model="m", seed=0, solved=False, outcome="incomplete", iterations=1)
    assert _journal_events(row) == []


def test_run_task_with_probe_scores_via_probe(tmp_path):
    # Locks the scoring contract: a probe-bearing task is graded by the probe (option A) and the
    # agent runs non-strict; a valid chatbot -> solved, with the scratch path on the row.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    chatbot = (
        "import openai, sys\n"
        "client = openai.OpenAI()\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if line in ('quit', 'exit'):\n"
        "        break\n"
        "    r = client.chat.completions.create(model='gpt', messages=[{'role': 'user', 'content': line}])\n"
        "    print(r.choices[0].message.content)\n"
    )
    spec = TaskSpec(
        id="create-chatbot",
        goal="g",
        task_kind="edit",
        fixture="empty",
        success_probe="python evals/probes/chatbot_smoke.py chatbot.py",
    )
    decisions = [
        ModelDecision(action=ToolCall(name="write_file", input={"path": "chatbot.py", "content": chatbot})),
        ModelDecision(action=FinalAnswer(answer="done")),
    ]
    row = run_task(
        spec, config=HarnessConfig(), model_client=ScriptedModel(decisions), seed=0, workspace_root=run_dir
    )
    assert row.solved is True and row.probe_exit == 0
    assert row.workspace is not None and Path(row.workspace).parent == run_dir
