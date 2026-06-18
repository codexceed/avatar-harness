"""Tests for the Layer-1 read-only foundation of the evals-driven improvement loop (ADR-0024).

Deterministic and offline: digests/triage/proposals are pure functions over result rows,
journal events, and parsed markdown — no model, no network.
"""

from evals.cluster import Cluster, cluster_failures, triage_report
from evals.distill import TrajectoryDigest, distill, distill_results
from evals.journal_read import row_events
from evals.proposal import ChangeProposal, load_proposals, score_impact, write_proposals
from evals.result import ResultRow
from evals.triage import parse_adr_index, parse_catalog, triage


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
    assert "id: c1-conclude" in md
    assert "remediation_type: prompt_instruction" in md
    assert "The model burns its budget" in md


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
