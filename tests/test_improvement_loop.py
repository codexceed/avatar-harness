"""Tests for the Layer-1 read-only foundation of the evals-driven improvement loop (ADR-0024).

Deterministic and offline: digests/triage/proposals are pure functions over result rows,
journal events, and parsed markdown — no model, no network.
"""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from evals.cluster import Cluster, cluster_failures, triage_report
from evals.distill import TrajectoryDigest, distill, distill_results
from evals.journal_read import row_events
from evals.proposal import ChangeProposal, load_proposals, score_impact, write_proposals
from evals.result import ResultRow
from evals.triage import parse_adr_index, parse_catalog, triage
from evals.validate import ValidationScope, ValidationVerdict, frozen_assets, run_ladder


def _row(**kw) -> ResultRow:
    base = ResultRow(
        task="secret-safety", model="m", seed=0, solved=False, outcome="incomplete", iterations=20
    )
    return base.model_copy(update=kw)


# --- A. distill: journal -> compact trajectory digest -------------------------


def test_distill_orders_actions_and_flags_repeats():
    events = [
        {"type": "model_decision", "action": "read_file({'path': 'a'})"},
        {"type": "model_decision", "action": "search_repo({'query': 'x'})"},
        {"type": "model_decision", "action": "search_repo({'query': 'x'})"},
        {"type": "tool_end", "tool": "search_repo", "success": True},
    ]
    d = distill(_row(iterations=3), events)
    assert isinstance(d, TrajectoryDigest)
    assert d.actions[:3] == [
        "read_file({'path': 'a'})",
        "search_repo({'query': 'x'})",
        "search_repo({'query': 'x'})",
    ]
    assert d.repeated_action_max == 2  # the duplicate search
    assert d.tool_calls == 1


def test_distill_counts_failures_and_decision_errors():
    events = [
        {"type": "tool_end", "tool": "read_file", "success": False},
        {"type": "tool_end", "tool": "read_file", "success": True},
        {"type": "decision_error"},
        {"type": "decision_error"},
    ]
    d = distill(_row(), events)
    assert d.tool_calls == 2
    assert d.tool_failures == 1
    assert d.decision_errors == 2


def test_distill_is_compact_even_for_a_huge_run():
    # The whole point: a multi-MB journal distills to KB. Many long actions must not blow up.
    events = [
        {"type": "model_decision", "action": "search_repo({'query': '" + "z" * 5000 + "'})"}
        for _ in range(500)
    ]
    d = distill(_row(), events)
    assert len(d.to_jsonl()) < 20_000  # KB, not MB
    assert d.repeated_action_max == 500  # repeat detection still spans the whole run


def test_distill_results_uses_events_resolver():
    rows = [_row(seed=0), _row(seed=1)]
    by_seed = {0: [{"type": "model_decision", "action": "x"}], 1: []}
    digests = distill_results(rows, events_for=lambda r: by_seed[r.seed])
    assert [d.seed for d in digests] == [0, 1]
    assert digests[0].actions == ["x"] and digests[1].actions == []


def test_distill_consumes_a_one_shot_generator():
    # distill makes ONE streaming pass, so it can consume a journal generator without
    # materializing it. (A multi-pass version would exhaust the generator on actions and then
    # see no tool_end/decision_error events — this guards that regression.)
    def gen():
        yield {"type": "model_decision", "action": "a"}
        yield {"type": "tool_end", "tool": "x", "success": False}
        yield {"type": "decision_error"}
        yield {"type": "model_decision", "action": "a"}

    d = distill(_row(), gen())
    assert d.actions == ["a", "a"]
    assert d.repeated_action_max == 2
    assert d.tool_calls == 1 and d.tool_failures == 1
    assert d.decision_errors == 1


def test_row_events_streams_and_is_empty_when_absent(tmp_path):
    # The one shared reader: streams events (skipping blank lines), empty when the workspace
    # or journal is gone. Both run.py and distill.py read through this.
    (tmp_path / "journal.jsonl").write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
    assert list(row_events(_row(workspace=str(tmp_path)))) == [{"a": 1}, {"b": 2}]
    assert list(row_events(_row(workspace=None))) == []


# --- B. triage: dedup a cluster vs failure-modes.md + open ADRs ----------------

_CATALOG = """
## A — Harness / scaffold failures
### A2 · Gemini tool-schema incompatibility ✅
- **Mechanism:** ...
## C — Model behavioral failures
### C1 · Failure-to-conclude / won't-accept-unknowable 🔧 *(model)*
- **Mechanism:** denied the token, the model refuses to conclude it is unknowable ...
"""

_ADR_INDEX = """
| # | Title | Status |
| --- | --- | --- |
| [0020](0020-guard-probes.md) | Guard probes: no-leak check necessary not sufficient | Accepted |
| [0022](0022-unobtainable.md) | "unobtainable" terminal conclusion (won't-conclude fix) | Proposed |
"""


def test_parse_catalog_and_adr_index():
    catalog = parse_catalog(_CATALOG)
    ids = {e.id for e in catalog}
    assert ids == {"A2", "C1"}
    c1 = next(e for e in catalog if e.id == "C1")
    assert c1.bucket == "C" and "conclude" in c1.title.lower()
    adrs = parse_adr_index(_ADR_INDEX)
    assert {a.num for a in adrs} == {"0020", "0022"}
    assert next(a for a in adrs if a.num == "0022").status == "Proposed"


def test_triage_matches_known_c1_to_adr_0022():
    catalog = parse_catalog(_CATALOG)
    adrs = parse_adr_index(_ADR_INDEX)
    symptom = "secret-safety incomplete won't conclude unobtainable credentials denied search loop"
    result = triage(symptom, catalog, adrs)
    assert result.novel is False
    assert result.catalog_match == "C1"
    assert result.adr_match == "0022"


def test_triage_flags_a_novel_cluster():
    catalog = parse_catalog(_CATALOG)
    adrs = parse_adr_index(_ADR_INDEX)
    result = triage("widget frobnicator timeout serialization regression", catalog, adrs)
    assert result.novel is True
    assert result.catalog_match is None and result.adr_match is None


def test_triage_ignores_non_proposed_adrs():
    # An accepted/implemented ADR is not an *open* proposal to advance, so it never matches.
    adrs = parse_adr_index(_ADR_INDEX)
    catalog = parse_catalog(_CATALOG)
    # tokens overlap "guard probes ... leak" — but 0020 is Accepted, so adr_match stays None here.
    result = triage("guard probe leak check necessary sufficient", catalog, adrs)
    assert result.adr_match is None


# --- C. ChangeProposal: the typed A->B seam -----------------------------------


def _proposal(**kw) -> ChangeProposal:
    base = ChangeProposal(
        id="c1-conclude",
        mode="C1",
        title="Legitimize unobtainable conclusions",
        impact=8,
        remediation_type="prompt_instruction",
        blast_radius="global",
        target_tasks=["secret-safety", "investigate-question"],
    )
    return base.model_copy(update=kw)


def test_changeproposal_roundtrips_jsonl():
    p = _proposal(predicted_validation_cost_tokens=2_150_000, evidence=["row:secret-safety/m/0"])
    assert ChangeProposal.model_validate_json(p.to_jsonl()) == p


def test_changeproposal_routes_on_blast_radius_and_grader():
    assert _proposal(blast_radius="global").route() == "adr_only"
    assert _proposal(blast_radius="local").route() == "implement"
    assert _proposal(blast_radius="local", touches_grader=True).route() == "adr_only"  # grader-touching


def test_score_impact_scales_with_likelihood():
    assert score_impact(cluster_size=10, total_failures=10) == 10
    assert score_impact(cluster_size=5, total_failures=10) == 5
    assert score_impact(cluster_size=0, total_failures=10) == 0
    assert score_impact(cluster_size=3, total_failures=0) == 0  # no failures -> no impact, no divide-by-zero


def test_changeproposal_markdown_has_front_matter_and_body(tmp_path):
    p = _proposal(body="The model burns its budget hunting for a leaked copy.")
    md = p.to_markdown()
    assert md.startswith("---")  # YAML front-matter fence
    assert 'id: "c1-conclude"' in md  # scalars are JSON-quoted (valid YAML)
    assert 'remediation_type: "prompt_instruction"' in md
    assert "The model burns its budget" in md


def test_changeproposal_markdown_quotes_colon_bearing_title():
    # Regression: a ": " inside an unquoted title makes a YAML parser read a nested mapping and
    # the front-matter fails to render. to_markdown JSON-encodes every scalar, so it stays valid.
    title = "Edit mission: run the artifact before declaring done"
    md = _proposal(title=title).to_markdown()
    line = next(ln for ln in md.splitlines() if ln.startswith("title: "))
    assert json.loads(line.removeprefix("title: ")) == title  # quoted + JSON-valid (JSON ⊂ YAML)


def test_proposals_roundtrip_to_disk(tmp_path):
    proposals = [_proposal(id="p1"), _proposal(id="p2", blast_radius="local")]
    path = tmp_path / "proposals.jsonl"
    write_proposals(proposals, path)
    assert load_proposals(path) == proposals


# --- D. cluster: group failures + triage prefilter (Workflow A's deterministic spine) ----------


def _digest(task: str, model: str, seed: int, outcome: str, actions: list[str]) -> TrajectoryDigest:
    return TrajectoryDigest(
        task=task, model=model, seed=seed, outcome=outcome, iterations=20, actions=actions
    )


def test_cluster_groups_failures_by_task_outcome_and_skips_solved():
    rows = [
        _row(model="m1", seed=0),  # secret-safety / incomplete
        _row(model="m2", seed=1),  # secret-safety / incomplete (different model)
        _row(task="other", model="m1", seed=0, outcome="failed"),
        _row(model="m1", seed=2, solved=True, outcome="success"),  # solved -> excluded
    ]
    digests = [
        _digest("secret-safety", "m1", 0, "incomplete", ["read_file({'path': 'credentials'})"]),
        _digest("secret-safety", "m2", 1, "incomplete", ["search_repo({'query': 'sk'})"]),
        _digest("other", "m1", 0, "failed", []),
        _digest("secret-safety", "m1", 2, "success", []),
    ]
    clusters = cluster_failures(rows, digests)
    assert {c.task for c in clusters} == {"secret-safety", "other"}  # the solved run is excluded
    ss = next(c for c in clusters if c.task == "secret-safety")
    # Keyed on the grading-truth bucket (ADR-0025): the two incomplete secret-safety give-ups
    # bucket as budget_exhausted (row-only fallback — no persisted failure_mode on these rows).
    assert ss.bucket == "budget_exhausted" and ss.runs == 2 and ss.models == ["m1", "m2"]


def test_cluster_symptom_includes_task_bucket_and_action_tokens():
    rows = [_row(model="m1", seed=0)]
    digests = [_digest("secret-safety", "m1", 0, "incomplete", ["read_file({'path': 'credentials'})"])]
    [cluster] = cluster_failures(rows, digests)
    assert "secret-safety" in cluster.symptom and "budget_exhausted" in cluster.symptom
    assert "credentials" in cluster.symptom and "read_file" in cluster.symptom


def test_triage_report_pairs_each_cluster_with_a_verdict():
    catalog = parse_catalog(_CATALOG)
    adrs = parse_adr_index(_ADR_INDEX)
    clusters = [
        Cluster(
            task="t", bucket="budget_exhausted", models=["m"], runs=1, symptom="widget frobnicator timeout"
        )
    ]
    report = triage_report(clusters, catalog, adrs)
    assert len(report) == 1
    assert report[0].cluster.task == "t"
    assert report[0].triage.novel is True  # no overlap with the catalog / open ADRs


# --- E. validate: the canary ladder + frozen grading assets (Workflow B's only eval-spender) ----
#
# The ladder is the tested unit (offline, with injected stage runners — mirroring how `run_task` is
# tested with a ScriptedModel while `main` is the live driver). It must (1) spend nothing past a
# failed local check, (2) screen cheaply on a 1-seed canary before the full matrix, (3) reject a
# change that trades one task for another or that helps the matrix overall while regressing a single
# model. `frozen_assets` proves the anti-Goodhart property: grading is done against a trusted ref,
# never the agent's (possibly gamed) worktree.


def _cell(task: str, model: str, seed: int, solved: bool) -> ResultRow:
    return ResultRow(
        task=task,
        model=model,
        seed=seed,
        solved=solved,
        outcome="success" if solved else "incomplete",
        iterations=1,
    )


def _matrix(models, tasks, seeds, solved) -> list[ResultRow]:
    """A full (model x task x seed) matrix; `solved(model, task, seed) -> bool` sets each cell."""
    return [_cell(t, m, s, solved(m, t, s)) for m in models for t in tasks for s in range(seeds)]


class _Recorder:
    """A scripted `run_eval`: records each call and returns the next pre-built matrix in order."""

    def __init__(self, *returns: list[ResultRow]) -> None:
        self.calls: list[tuple[list[str], list[str], int]] = []
        self._returns = list(returns)

    def __call__(self, models: Sequence[str], tasks: Sequence[str], seeds: int) -> list[ResultRow]:
        self.calls.append((list(models), list(tasks), seeds))
        return self._returns.pop(0)


_OK_LOCAL = lambda: (True, "make check clean")  # noqa: E731


def test_run_ladder_stops_at_local_on_unit_failure():
    # A failed unit/local check ends the ladder before any eval spend (the cheapest rung).
    run_eval = _Recorder()
    verdict = run_ladder(
        [_cell("secret-safety", "A", 0, False)],
        ValidationScope(
            affected_models=["A"],
            target_tasks=["secret-safety"],
            all_models=["A"],
            all_tasks=["secret-safety"],
            seeds=8,
        ),
        run_local=lambda: (False, "pytest: 1 failed"),
        run_eval=run_eval,
    )
    assert isinstance(verdict, ValidationVerdict)
    assert verdict.passed is False
    assert verdict.stage_reached == "local"
    assert run_eval.calls == []  # ZERO eval spend


def test_run_ladder_stops_at_canary_on_raw_regression():
    # Canary (1 seed, affected models) shows a previously-passing run now failing -> stop; the full
    # matrix never runs. (The target passed in the baseline canary seed and now fails.)
    baseline = [_cell("secret-safety", "A", 0, True)]
    canary = [_cell("secret-safety", "A", 0, False)]
    run_eval = _Recorder(canary)
    verdict = run_ladder(
        baseline,
        ValidationScope(
            affected_models=["A"],
            target_tasks=["secret-safety"],
            all_models=["A"],
            all_tasks=["secret-safety"],
            seeds=8,
        ),
        run_local=_OK_LOCAL,
        run_eval=run_eval,
    )
    assert verdict.passed is False
    assert verdict.stage_reached == "canary"
    assert len(run_eval.calls) == 1  # canary ran, matrix did not


def test_run_ladder_stops_at_canary_when_no_improvement():
    # The target is still failing in the canary -> nothing to gain, don't spend the full matrix.
    baseline = [_cell("secret-safety", "A", 0, False)]
    canary = [_cell("secret-safety", "A", 0, False)]
    run_eval = _Recorder(canary)
    verdict = run_ladder(
        baseline,
        ValidationScope(
            affected_models=["A"],
            target_tasks=["secret-safety"],
            all_models=["A"],
            all_tasks=["secret-safety"],
            seeds=8,
        ),
        run_local=_OK_LOCAL,
        run_eval=run_eval,
    )
    assert verdict.passed is False
    assert verdict.stage_reached == "canary"
    assert len(run_eval.calls) == 1


def test_run_ladder_passes_when_canary_survives_and_matrix_improves():
    models, tasks, seeds = ["A", "B"], ["secret-safety", "other"], 8
    # Baseline: secret-safety fails everywhere; other passes everywhere.
    baseline = _matrix(models, tasks, seeds, lambda m, t, s: t != "secret-safety")
    # Canary (1 seed, affected models): secret-safety now passes -> survives.
    canary = _matrix(models, ["secret-safety"], 1, lambda m, t, s: True)
    # Full matrix: secret-safety fixed everywhere, other unchanged -> big, agnostic improvement.
    matrix = _matrix(models, tasks, seeds, lambda m, t, s: True)
    run_eval = _Recorder(canary, matrix)
    verdict = run_ladder(
        baseline,
        ValidationScope(
            affected_models=models,
            target_tasks=["secret-safety"],
            all_models=models,
            all_tasks=tasks,
            seeds=seeds,
        ),
        run_local=_OK_LOCAL,
        run_eval=run_eval,
    )
    assert verdict.passed is True
    assert verdict.stage_reached == "matrix"
    # The canary screens cheap (affected models, target task, 1 seed); the matrix is global.
    assert run_eval.calls[0] == (models, ["secret-safety"], 1)
    assert run_eval.calls[1] == (models, tasks, seeds)


def test_run_ladder_fails_matrix_on_cross_task_regression():
    # Survives the canary, but the full matrix shows the fix traded one task for another: it fixes
    # secret-safety and breaks `other` by the same amount -> globally a wash, so it is rejected.
    # This is the "validate globally, never per-failed-task" guarantee.
    models, tasks, seeds = ["A", "B"], ["secret-safety", "other"], 8
    baseline = _matrix(models, tasks, seeds, lambda m, t, s: t != "secret-safety")
    canary = _matrix(models, ["secret-safety"], 1, lambda m, t, s: True)
    # secret-safety fixed (+16) but `other` broken (-16): nets out across the matrix.
    matrix = _matrix(models, tasks, seeds, lambda m, t, s: t == "secret-safety")
    run_eval = _Recorder(canary, matrix)
    verdict = run_ladder(
        baseline,
        ValidationScope(
            affected_models=models,
            target_tasks=["secret-safety"],
            all_models=models,
            all_tasks=tasks,
            seeds=seeds,
        ),
        run_local=_OK_LOCAL,
        run_eval=run_eval,
    )
    assert verdict.passed is False
    assert verdict.stage_reached == "matrix"


def test_run_ladder_fails_matrix_on_model_agnosticism_violation():
    # The overall metrics look like a clear win (+24 vs -8 -> significant improvement), but it is
    # carried entirely by model A while model B significantly regresses on a non-target task. The
    # 1-seed canary on the target can't see it; only the full matrix + per-model agnosticism check
    # catches it -> rejected.
    models, tasks, seeds = ["A", "B"], ["secret-safety", "t2", "t3", "other"], 8

    def base(m, t, s):
        if m == "A":  # A fails secret-safety, t2, t3 (passes only `other`)
            return t == "other"
        return t != "secret-safety"  # B fails only secret-safety

    def cand(m, t, s):
        if m == "A":
            return True  # A: secret-safety, t2, t3 all fixed (+24); other still passes
        # B: secret-safety stays failing (so the canary on B shows no flip); `other` now broken (-8)
        return t not in ("secret-safety", "other")

    baseline = _matrix(models, tasks, seeds, base)
    # Canary: A's secret-safety flips to pass, B's stays failing -> one newly-passing, no regression
    # -> survives.
    canary = _matrix(models, ["secret-safety"], 1, lambda m, t, s: m == "A")
    matrix = _matrix(models, tasks, seeds, cand)
    run_eval = _Recorder(canary, matrix)
    verdict = run_ladder(
        baseline,
        ValidationScope(
            affected_models=models,
            target_tasks=["secret-safety"],
            all_models=models,
            all_tasks=tasks,
            seeds=seeds,
        ),
        run_local=_OK_LOCAL,
        run_eval=run_eval,
    )
    assert verdict.passed is False
    assert verdict.stage_reached == "matrix"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_frozen_assets_restores_grading_surface_from_trusted_ref(tmp_path):
    # The anti-Goodhart property: `validate` grades against the grading surface (specs · probes ·
    # fixtures) restored from a TRUSTED ref, never the agent's worktree — so a candidate that edits
    # a spec/probe to make itself pass is graded against the untouched original.
    repo = tmp_path / "repo"
    (repo / "evals" / "tasks").mkdir(parents=True)
    (repo / "evals" / "probes").mkdir(parents=True)
    (repo / "evals" / "fixtures" / "demo").mkdir(parents=True)
    trusted_spec = 'id = "secret-safety"\ngoal = "do not leak"\n'
    (repo / "evals" / "tasks" / "secret-safety.toml").write_text(trusted_spec, encoding="utf-8")
    (repo / "evals" / "probes" / "leak_check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (repo / "evals" / "fixtures" / "demo" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "trusted")

    # The agent dirties the grading surface in the worktree (the gaming attempt).
    (repo / "evals" / "tasks" / "secret-safety.toml").write_text(
        'id = "secret-safety"\ngoal = "trivially pass"\n', encoding="utf-8"
    )

    dest = tmp_path / "frozen"
    out = frozen_assets("HEAD", repo, dest)

    # Restored from the trusted ref — the dirty worktree edit is not present.
    assert (out / "tasks" / "secret-safety.toml").read_text() == trusted_spec
    assert (out / "probes" / "leak_check.py").exists()
    assert (out / "fixtures" / "demo" / "app.py").exists()
