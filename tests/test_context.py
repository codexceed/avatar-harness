from avatar_harness.context import ContextBuilder
from avatar_harness.state import DecisionRecord, TaskState
from avatar_harness.tools import default_registry
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


def test_context_packet_carries_task_kind(tmp_path, read_registry):
    # task_kind is threaded onto the packet so the model adapter can frame the prompt
    # per kind (edit vs investigate) — see model_client._KIND_FRAMING.
    state = TaskState(goal="fix the bug", task_kind="edit")
    packet = ContextBuilder().build(state, Workspace(tmp_path), read_registry)
    assert packet.task_kind == "edit"


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


def test_edit_task_advertises_apply_patch_in_investigating(tmp_path):
    # Contract: an edit task starts in `investigating`, and the runner's bootstrap admits
    # the edit-intent tool (apply_patch, tier 1) from there to avoid a pure-creation
    # deadlock (runner._phase_admits). The context MUST advertise exactly what the runner
    # admits — a live model, told "call only the tools listed below," would otherwise never
    # see apply_patch and would loop on reads/final_answer, never triggering the bootstrap.
    state = TaskState(goal="add a helper function", task_kind="edit")  # phase = investigating
    packet = ContextBuilder().build(state, Workspace(tmp_path), default_registry())
    assert state.phase == "investigating"
    names = {t.name for t in packet.allowed_tools}
    assert "apply_patch" in names  # the edit-intent tool is surfaced for discovery
    assert "run_tests" not in names  # but ONLY the edit-intent tier bootstraps, not all editing tools


def test_investigate_task_advertises_tier1_tools(tmp_path):
    # ADR-0005: transient edits are legal in investigate tasks, and advertised tools must
    # mirror admitted tools (the PR-#6 invariant — one shared predicate feeds the runner's
    # gate and this advertisement). So an investigate task in `investigating` is told about
    # apply_patch/write_file — and ONLY the tier-1 tools ride the rule, not command tools.
    state = TaskState(goal="explain the loop", task_kind="investigate")
    packet = ContextBuilder().build(state, Workspace(tmp_path), default_registry())
    assert state.phase == "investigating"
    names = {t.name for t in packet.allowed_tools}
    assert {"apply_patch", "write_file"} <= names
    assert "run_tests" not in names


# --- action ledger: the agent sees what it already did (Phase 2.5) -------------


def test_context_includes_prior_actions(tmp_path, read_registry):
    # The loop fix: the model's own prior tool calls are surfaced so it stops
    # re-issuing them (turns 9-13 replayed turns 1-5 in the dogfood).
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
    assert joined.count("list_files rich*") == 1  # collapsed, not repeated 3x
    assert "x3" in joined


def test_context_respects_char_budget(tmp_path, read_registry):
    state = TaskState(goal="x", task_kind="investigate")
    for i in range(4):
        state.add_feedback(f"sum{i}", detail="D" * 100)
    packet = ContextBuilder(detail_char_budget=120, max_detail_chars=100).build(
        state, Workspace(tmp_path), read_registry
    )
    blob = "\n".join(packet.recent_evidence)
    assert blob.count("D" * 100) <= 2  # only ~budget worth of detail is included verbatim


# --- truncation visibility + realistic budgets (dogfood `events/63bced3f…jsonl`) -----------


def test_truncated_detail_is_marked(tmp_path, read_registry):
    """A cut detail carries a visible, quantified marker — never a silent slice.

    Dogfood context: the model saw a 3,548-char file silently cut at 1,500 chars — it
    appeared to end mid-function — and burned 42 of 50 turns re-reading it. The wording
    is deliberately neutral: evidence detail also comes from run_tests/run_linter/
    search_repo (all `kind="tool_result"`), where a "re-read with line_range" hint would
    itself be a misleading signal (PR-#29 review).
    """
    state = TaskState(goal="x", task_kind="investigate")
    state.add_feedback("read big.py", detail="X" * 300)
    packet = ContextBuilder(max_detail_chars=100).build(state, Workspace(tmp_path), read_registry)
    blob = "\n".join(packet.recent_evidence)
    assert "[truncated" in blob  # the cut is visible...
    assert "100/300" in blob  # ...and quantified (shown/total)
    assert "line_range" not in blob  # neutral wording — no tool-specific advice that may not apply


def test_untruncated_detail_has_no_marker(tmp_path, read_registry):
    """Detail that fits whole renders unmarked — the marker means exactly one thing."""
    state = TaskState(goal="x", task_kind="investigate")
    state.add_feedback("read small.py", detail="Y" * 50)
    packet = ContextBuilder(max_detail_chars=100).build(state, Workspace(tmp_path), read_registry)
    assert "[truncated" not in "\n".join(packet.recent_evidence)


def test_pinned_verifier_detail_truncation_is_marked(tmp_path, read_registry):
    """The verifier pin shows detail past the spent budget — but a cut is still marked.

    The pin (§9) guarantees the latest verifier output detail survives compaction; if
    that detail itself exceeds the per-item cap, the cut must be as loud as anywhere else.
    """
    state = TaskState(goal="x", task_kind="investigate")
    state.add_feedback("filler", detail="F" * 200)
    state.add_feedback("verification failed: ['tests']", detail="V" * 300, kind="verification")
    packet = ContextBuilder(max_detail_chars=100, detail_char_budget=100).build(
        state, Workspace(tmp_path), read_registry
    )
    blob = "\n".join(packet.recent_evidence)
    assert "V" * 100 in blob  # the pin held: verifier detail shown despite the spent budget
    assert "100/300" in blob  # and its truncation is marked


def test_degraded_detail_is_marked_as_elided(tmp_path, read_registry):
    """An over-budget item degraded to summary-only says so — degrade is not silent either.

    Same failure mode as silent truncation, one tier up: a summary-only line that *had*
    detail must say the detail was elided (and how much), or the model can't tell "this
    tool returned only a summary" from "the harness aged the content out".
    """
    state = TaskState(goal="x", task_kind="investigate")
    state.add_feedback("OLD", detail="D" * 80)
    state.add_feedback("NEW", detail="E" * 80)
    packet = ContextBuilder(max_detail_chars=100, detail_char_budget=80).build(
        state, Workspace(tmp_path), read_registry
    )
    old_line = next(line for line in packet.recent_evidence if line.startswith("OLD"))
    assert "[detail elided: 80 chars]" in old_line  # degraded, and says so
    assert "D" * 80 not in old_line  # the detail itself is genuinely gone
    new_line = next(line for line in packet.recent_evidence if line.startswith("NEW"))
    assert "elided" not in new_line  # the in-budget item is unmarked


def test_summary_only_evidence_never_marked_elided(tmp_path, read_registry):
    """Evidence that never had detail renders bare — elision marks loss, not absence."""
    state = TaskState(goal="x", task_kind="investigate")
    state.add_feedback("plain note")
    packet = ContextBuilder(detail_char_budget=0).build(state, Workspace(tmp_path), read_registry)
    assert "elided" not in "\n".join(packet.recent_evidence)


def test_default_budgets_fit_an_ordinary_source_file(tmp_path, read_registry):
    """The defaults must let a whole source file survive into context unmarked.

    Modification structurally requires seeing the entire file at once (a unified diff's
    exact context lines, or a full `write_file(overwrite)` rewrite); the dogfood file was
    3,548 chars, so a ~4k detail is the floor the defaults must clear per item.
    """
    state = TaskState(goal="x", task_kind="investigate")
    content = "line\n" * 800  # 4000 chars
    state.add_feedback("read scripts/chatbot.py", detail=content)
    packet = ContextBuilder().build(state, Workspace(tmp_path), read_registry)
    blob = "\n".join(packet.recent_evidence)
    assert content in blob  # intact, no truncation
    assert "[truncated" not in blob


def test_last_two_verifier_outputs_pinned_verbatim(tmp_path, read_registry):
    # Repair loops need more than the LATEST verdict: "what did I try before and why did
    # it fail" must survive compaction too, or the model re-attempts the same fix
    # (loop-determinism hardening). The pin covers the last N (default 2) verifier items.
    state = TaskState(goal="x", task_kind="edit")
    state.add_feedback("verifier run 1", detail="FIRST_VERIFIER_DETAIL", kind="verification")
    state.add_feedback("noise-a", detail="N" * 300)
    state.add_feedback("verifier run 2", detail="SECOND_VERIFIER_DETAIL", kind="verification")
    state.add_feedback("noise-b", detail="M" * 300)
    packet = ContextBuilder(detail_char_budget=5).build(state, Workspace(tmp_path), read_registry)
    blob = "\n".join(packet.recent_evidence)
    assert "SECOND_VERIFIER_DETAIL" in blob  # the latest verdict, as before
    assert "FIRST_VERIFIER_DETAIL" in blob  # and the one before it — repair history survives
