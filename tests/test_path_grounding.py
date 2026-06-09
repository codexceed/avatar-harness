"""Phase 3.1 · 3.2b — `@path` grounding (§23.4, ADR-0002 J).

When a goal mentions `@path/to/file`, `ReplSession.start` reads that file *through the
Workspace* (so the sensitive-path denylist + confinement apply) and seeds it as initial
`kind="grounding"` evidence on the fresh `TaskState` — explicit context, like history
seeding. A refused, missing, or out-of-root path becomes a short note, never a crash and
never a leaked secret.
"""

from conftest import ScriptedModel

from avatar_harness.config import HarnessConfig
from avatar_harness.harness import Harness
from avatar_harness.session_state import ReplSession
from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.filesystem import read_file


def _repl(root) -> ReplSession:
    reg = ToolRegistry()
    reg.register(read_file)
    config = HarnessConfig(workspace_root=str(root))
    return ReplSession(Harness(config=config, model=ScriptedModel([]), tools=reg))


def _grounding(session) -> list:
    return [e for e in session.state.evidence if e.kind == "grounding"]


def test_at_path_seeds_file_as_grounding(git_repo):
    session = _repl(git_repo).start("explain @calc.py")
    grounded = _grounding(session)
    assert any("calc.py" in e.summary for e in grounded)
    assert any("def add" in (e.detail or "") for e in grounded)  # the file content is seeded


def test_grounding_respects_denylist(tmp_path):
    (tmp_path / ".env").write_text("API_KEY=SECRET123\n", encoding="utf-8")
    session = _repl(tmp_path).start("use the config in @.env")
    blob = "".join(f"{e.summary}{e.detail or ''}" for e in session.state.evidence)
    assert "SECRET123" not in blob  # the denylisted secret never enters state
    assert any(e.kind == "grounding" and ".env" in e.summary for e in _grounding(session))  # noted as refused


def test_missing_at_path_is_noted_not_fatal(tmp_path):
    session = _repl(tmp_path).start("see @nope.py")  # must not raise
    assert any("nope.py" in e.summary for e in _grounding(session))


def test_no_at_path_no_grounding(tmp_path):
    session = _repl(tmp_path).start("explain the loop")
    assert _grounding(session) == []


def test_multiple_at_paths_each_grounded(git_repo):
    (git_repo / "b.py").write_text("y = 2\n", encoding="utf-8")
    session = _repl(git_repo).start("compare @calc.py and @b.py")
    summaries = " ".join(e.summary for e in _grounding(session))
    assert "calc.py" in summaries and "b.py" in summaries
