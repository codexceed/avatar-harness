from avatar_harness.context import ContextBuilder
from avatar_harness.state import DecisionRecord, TaskState
from avatar_harness.tools.base import ToolDefinition
from avatar_harness.tools.filesystem import read_file
from avatar_harness.workspace import Workspace


def test_context_contains_goal_phase_and_recent_evidence(tmp_path, read_registry):
    state = TaskState(goal="explain the loop", task_kind="investigate")
    state.add_feedback("found the loop in runner.py")
    packet = ContextBuilder().build(state, Workspace(tmp_path), read_registry)
    assert packet.goal == "explain the loop"
    assert packet.phase == "investigating"
    assert any("runner.py" in line for line in packet.recent_evidence)


def test_context_surfaces_evidence_detail(tmp_path, read_registry):
    # Tool CONTENT (stored as evidence detail) must reach the packet, not just summaries —
    # otherwise the model is blind to what its tools found and loops forever.
    state = TaskState(goal="x", task_kind="investigate")
    state.add_feedback("search done", detail="src/app.py:12: def handler()")
    packet = ContextBuilder().build(state, Workspace(tmp_path), read_registry)
    assert any("def handler()" in line for line in packet.recent_evidence)


def test_context_omits_out_of_phase_tools(tmp_path, read_registry):
    read_registry.register(
        ToolDefinition(
            name="apply_patch",
            description="edit-only",
            input_model=read_file.input_model,
            handler=read_file.handler,
            phases=frozenset({"editing"}),
        )
    )
    state = TaskState(goal="x", task_kind="investigate")  # phase = investigating
    packet = ContextBuilder().build(state, Workspace(tmp_path), read_registry)
    names = {t.name for t in packet.allowed_tools}
    assert "read_file" in names
    assert "apply_patch" not in names


# --- action ledger: the agent sees what it already did (Phase 2.5) -------------


def test_context_includes_prior_actions(tmp_path, read_registry):
    # The loop fix: the model's own prior tool calls are surfaced so it stops
    # re-issuing them (turns 9–13 replayed turns 1–5 in the dogfood).
    state = TaskState(goal="x", task_kind="investigate")
    state.decisions.append(
        DecisionRecord(step=1, rationale="look", chosen="list_files({'glob': '**/*'})", outcome="5065 files")
    )
    packet = ContextBuilder().build(state, Workspace(tmp_path), read_registry)
    assert any("list_files" in line for line in packet.prior_actions)


# --- less-lossy compaction: degrade, don't drop (Phase 2.5) -------------------


def test_old_evidence_degrades_to_summary_not_dropped(tmp_path, read_registry):
    state = TaskState(goal="x", task_kind="investigate")
    state.add_feedback("OLDSUM", detail="OLDDETAIL")
    for i in range(3):
        state.add_feedback(f"NEWSUM{i}", detail=f"NEWDETAIL{i}")
    packet = ContextBuilder(detail_char_budget=12).build(state, Workspace(tmp_path), read_registry)
    blob = "\n".join(packet.recent_evidence)
    assert "OLDSUM" in blob  # the old item is still present (not dropped)
    assert "OLDDETAIL" not in blob  # but its detail has degraded away
    assert "NEWDETAIL2" in blob  # the most-recent item keeps full detail


def test_recent_verifier_output_pinned_verbatim(tmp_path, read_registry):
    state = TaskState(goal="x", task_kind="investigate")
    state.add_feedback("verifier says", detail="REQUIRED_CHECK_FAILED", kind="verification")
    for i in range(4):
        state.add_feedback(f"noise{i}", detail=f"NOISEDETAIL{i}")
    packet = ContextBuilder(detail_char_budget=5).build(state, Workspace(tmp_path), read_registry)
    blob = "\n".join(packet.recent_evidence)
    assert "REQUIRED_CHECK_FAILED" in blob  # pinned verbatim despite tiny budget + newer noise


def test_duplicate_evidence_collapsed(tmp_path, read_registry):
    state = TaskState(goal="x", task_kind="investigate")
    for _ in range(3):
        state.add_feedback("list_files rich* → 0 files")
    packet = ContextBuilder().build(state, Workspace(tmp_path), read_registry)
    joined = "\n".join(packet.recent_evidence)
    assert joined.count("list_files rich*") == 1  # collapsed, not repeated 3×
    assert "×3" in joined


def test_context_respects_char_budget(tmp_path, read_registry):
    state = TaskState(goal="x", task_kind="investigate")
    for i in range(4):
        state.add_feedback(f"sum{i}", detail="D" * 100)
    packet = ContextBuilder(detail_char_budget=120, max_detail_chars=100).build(
        state, Workspace(tmp_path), read_registry
    )
    blob = "\n".join(packet.recent_evidence)
    assert blob.count("D" * 100) <= 2  # only ~budget worth of detail is included verbatim
