"""Declared `change_kinds` select per-kind vacuity rulebooks (ADR-0044, amends ADR-0038).

The dogfood run `tetris_glm/events/8216e26b…jsonl` (a markdown design spec) showed the
"must execute the code" vacuity rule is a category error for textual deliverables: a
legitimate anchored `grep` contract was rejected, then the same assertions laundered
through `python3 -c "…"` were accepted — two burned turns, obfuscation taught, no
integrity gained. ADR-0044's cure, pinned here across its three seams:

- **Declaration:** `declare_verification` takes `change_kinds: list` (`"code"` /
  `"content"`, default `["code"]`); each declared kind needs ≥1 covering check —
  `code` keeps the executing rule, `content` gets **anchored + falsifiable** (an
  assertive inspector with an operand or a real executor, and no `|| true`-style
  neutralization).
- **Analyzer:** `check_covers_content` classifies checks mechanically (no self-tags);
  `classify_change_paths` maps changed paths to kinds, murky → `code`.
- **Audit:** at verification time `kinds(diff) ⊆ declared change_kinds` — a required
  `change_kind_coverage` check fails legibly on under-declaration; over-declaration
  is tolerated. No declaration (tiers 1-3) → no reconciliation.
"""

from typing import get_args

from avatar.config import HarnessConfig
from avatar.deps import CancellationToken, RunDeps
from avatar.planner import (
    CHANGE_KIND_COVERAGE,
    ChangeKind,
    check_covers_content,
    classify_change_paths,
)
from avatar.state import TaskState
from avatar.tools.base import ToolRegistry, ToolRuntime
from avatar.tools.verification import (
    DeclaredCheckInput,
    DeclareVerificationInput,
    declare_verification,
)
from avatar.verifier import Verifier
from avatar.workspace import Workspace

# The exact check the dogfood run declared and had rejected — the motivating artifact.
_DOC_CHECK = (
    "test -f DESIGN.md && grep -q '^# ASCII Tetris' DESIGN.md "
    "&& grep -q 'Acceptance Criteria' DESIGN.md && grep -q 'Game Loop' DESIGN.md "
    "&& echo 'DESIGN.md OK'"
)
_CODE_CHECK = "python -m pytest test_game.py"
_PASS = 'python -c "import sys; sys.exit(0)"'


# --- the content rulebook: anchored + falsifiable (planner analyzer) -----------------------


def test_content_coverage_accepts_anchored_inspection():
    # Inspectors with operands are first-class for content: for text, grep IS the check.
    assert check_covers_content(_DOC_CHECK)
    assert check_covers_content("test -f DESIGN.md")
    assert check_covers_content("grep -q '^# Title' README.md")


def test_content_coverage_accepts_a_real_executor():
    # An executing check covers content too (one check may count toward both kinds).
    assert check_covers_content("python3 -c \"open('DESIGN.md').read()\"")


def test_content_coverage_rejects_pure_emitters():
    # A line that inspects nothing can't distinguish done from not-done.
    assert not check_covers_content("")
    assert not check_covers_content("true")
    assert not check_covers_content("echo ok")
    assert not check_covers_content("printf done")
    assert not check_covers_content("echo a && echo b")


def test_content_coverage_rejects_neutralized_lines():
    # Falsifiable means the line can exit non-zero on a wrong artifact; a can't-fail
    # `||` alternative guarantees exit 0 regardless.
    assert not check_covers_content("grep -q '^# Title' DESIGN.md || true")
    assert not check_covers_content("test -f DESIGN.md || echo fine")


# --- path classification: the diff's kinds (murky → code) ----------------------------------


def test_classify_change_paths_content_extensions():
    assert classify_change_paths(["DESIGN.md"]) == {"content"}
    assert classify_change_paths(["docs/spec.rst", "NOTES.txt"]) == {"content"}


def test_classify_change_paths_code_and_mixed():
    assert classify_change_paths(["game.py"]) == {"code"}
    assert classify_change_paths(["game.py", "README.md"]) == {"code", "content"}


def test_classify_change_paths_murky_config_is_code():
    # Behavior-bearing config fails toward strictness.
    assert classify_change_paths(["pyproject.toml"]) == {"code"}
    assert classify_change_paths([".github/workflows/ci.yml"]) == {"code"}


# --- declaration time: per-kind coverage (tool seam) ----------------------------------------


def _deps(tmp_path) -> RunDeps:
    return RunDeps(workspace=Workspace(tmp_path), config=HarnessConfig(), cancellation=CancellationToken())


def _declare(tmp_path, commands: list[str], **kwargs):
    args = DeclareVerificationInput(checks=[DeclaredCheckInput(command=c) for c in commands], **kwargs)
    return declare_verification.handler(args, _deps(tmp_path))


def test_change_kinds_is_a_declared_field():
    # Guard against pydantic silently ignoring an unknown kwarg: the field must exist.
    assert "change_kinds" in DeclareVerificationInput.model_fields


def test_content_contract_accepted_for_content_change(tmp_path):
    # The journal's rejected turn, replayed under ADR-0044: accepted on turn one.
    result = _declare(tmp_path, [_DOC_CHECK], change_kinds=["content"])
    assert result.success, result.error


def test_content_kind_rejects_unanchored_contract(tmp_path):
    result = _declare(tmp_path, ["echo ok"], change_kinds=["content"])
    assert not result.success
    assert "content" in (result.error or "")  # the rejection names the uncovered kind


def test_mixed_kinds_require_per_kind_coverage(tmp_path):
    # A pytest check alone covers `code` but leaves `content` uncovered — rejected,
    # naming the gap; adding the anchored doc check satisfies both.
    result = _declare(tmp_path, [_CODE_CHECK], change_kinds=["code", "content"])
    assert not result.success
    assert "content" in (result.error or "")
    result = _declare(tmp_path, [_CODE_CHECK, _DOC_CHECK], change_kinds=["code", "content"])
    assert result.success, result.error


def test_default_change_kinds_is_code(tmp_path):
    # Backward compatible: omitted → ["code"], so an inspection-only contract is still
    # rejected exactly as before ADR-0044.
    result = _declare(tmp_path, [_DOC_CHECK])
    assert not result.success
    result = _declare(tmp_path, [_CODE_CHECK])
    assert result.success, result.error


def test_empty_change_kinds_rejected(tmp_path):
    result = _declare(tmp_path, [_CODE_CHECK], change_kinds=[])
    assert not result.success


def test_change_kind_registry_is_exhaustive():
    # The `ChangeKind` Literal and the rulebook registry share one definition; static
    # typing catches typos but not a *missing* registry entry (dict keys may be a
    # subset of the annotated type), so exhaustiveness is pinned here.
    assert set(get_args(ChangeKind)) == set(CHANGE_KIND_COVERAGE)


def test_unknown_change_kind_rejected_at_input_validation(tmp_path):
    # `change_kinds` is `list[ChangeKind]`, so an invalid value never reaches the handler:
    # `ToolRuntime` rejects it at input validation and the failed ToolResult carries
    # pydantic's permitted-values error back to the model (§10, model-correctable).
    registry = ToolRegistry()
    registry.register(declare_verification)
    runtime = ToolRuntime(registry, _deps(tmp_path))
    result = runtime.execute(
        "declare_verification",
        {"checks": [{"command": _CODE_CHECK}], "change_kinds": ["docs"]},
    )
    assert not result.success
    error = result.error or ""
    assert "code" in error and "content" in error  # the permitted values are steered


def test_companion_checks_tolerated_once_kinds_covered(tmp_path):
    # Judge-contracts-whole (PR-#110 review) survives: an `echo` companion rides along
    # once every declared kind has a covering check.
    result = _declare(tmp_path, [_CODE_CHECK, "echo built"], change_kinds=["code"])
    assert result.success, result.error


# --- verification time: the diff audits the declaration -------------------------------------


def _edit_state(files: set[str], kinds: list[str] | None) -> TaskState:
    return TaskState(
        goal="build it",
        task_kind="edit",
        files_modified=files,
        declared_change_kinds=kinds,
    )


def test_verifier_fails_on_undeclared_code_change(git_repo):
    # Declared `content`, shipped code: the audit fails a required check, legibly.
    state = _edit_state({"tetris.py", "DESIGN.md"}, ["content"])
    report = Verifier(HarnessConfig(test_command=_PASS, lint_command="")).verify(state, Workspace(git_repo))
    assert report.passed is False
    audit = next(c for c in report.checks if c.name == "change_kind_coverage")
    assert audit.status == "fail"
    assert "code" in audit.evidence  # names the undeclared kind
    assert "tetris.py" in audit.evidence  # and the offending path


def test_verifier_tolerates_over_declaration(git_repo):
    # Declared both kinds, shipped docs only: over-declaring is self-inflicted
    # strictness, not an integrity violation.
    state = _edit_state({"DESIGN.md"}, ["code", "content"])
    report = Verifier(HarnessConfig(test_command=_PASS, lint_command="")).verify(state, Workspace(git_repo))
    audit = next(c for c in report.checks if c.name == "change_kind_coverage")
    assert audit.status == "pass"
    assert report.passed


def test_verifier_skips_audit_without_a_declaration(git_repo):
    # Tiers 1-3 contracts declare nothing — no reconciliation runs.
    state = _edit_state({"tetris.py"}, None)
    report = Verifier(HarnessConfig(test_command=_PASS, lint_command="")).verify(state, Workspace(git_repo))
    assert not any(c.name == "change_kind_coverage" for c in report.checks)
