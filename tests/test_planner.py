"""ADR-0007 — dynamic, no-dependency verification-plan resolution.

The `VerificationPlanner` resolves the per-session rubric in strict tiers:
config override (always wins) → deterministic detection over repo artifacts
(CI > manifests > Makefile, no LLM, no Python assumption) → an LLM fallback
that only *proposes* with a validated citation. The resolved plan freezes onto
`TaskState` once and is journaled as a typed event.
"""

import json
from types import SimpleNamespace

import pytest

from avatar.config import HarnessConfig
from avatar.event_types import VerificationPlanFrozen, dump_event, parse_event
from avatar.planner import VerificationPlanner, vacuous_declared_check
from avatar.state import PlannedCheck, TaskState
from avatar.workspace import Workspace


def _config(**kw) -> HarnessConfig:
    kw.setdefault("test_command", "")
    kw.setdefault("lint_command", "")
    # Hermetic: ignore the developer's `.env`. Otherwise `AVATAR_PLANNER_MODEL` / `AVATAR_API_KEY`
    # leak in and opt these deterministic-tier tests into the live LLM fallback — a real network
    # call that proposes a command and flips `_resolve(...) == []` assertions (and is slow/flaky).
    return HarnessConfig(_env_file=None, **kw)


def _resolve(tmp_path, config: HarnessConfig | None = None, client=None) -> list[PlannedCheck]:
    planner = VerificationPlanner(config or _config(), client=client)
    return planner.resolve(Workspace(tmp_path))


def _by_kind(plan: list[PlannedCheck]) -> dict[str, PlannedCheck]:
    return {c.kind: c for c in plan}


class _CountingClient:
    """An OpenAI-compatible stub that records calls and replays canned tool-call args."""

    def __init__(self, arguments: list[dict] | None = None) -> None:
        self.calls = 0
        tool_calls = [
            SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(args)))
            for args in (arguments or [])
        ] or None
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls))])

        def create(**_kw):
            self.calls += 1
            return response

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


# --- tier 1: config override always wins ---------------------------------


def test_planner_config_override_wins(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\techo make-test\n", encoding="utf-8")
    plan = _resolve(tmp_path, _config(test_command="pytest -q tests/", lint_command="ruff check src"))
    by_kind = _by_kind(plan)
    assert by_kind["test"].command == "pytest -q tests/"
    assert by_kind["test"].provenance == "config:AVATAR_TEST_COMMAND"
    assert by_kind["lint"].command == "ruff check src"
    assert by_kind["lint"].provenance == "config:AVATAR_LINT_COMMAND"


def test_planner_config_override_is_per_slot(tmp_path):
    # Only the test slot is declared; detection still fills the lint slot.
    (tmp_path / "Makefile").write_text("lint:\n\techo make-lint\n", encoding="utf-8")
    plan = _resolve(tmp_path, _config(test_command="go test ./..."))
    by_kind = _by_kind(plan)
    assert by_kind["test"].provenance == "config:AVATAR_TEST_COMMAND"
    assert by_kind["lint"].command == "make lint"


# --- tier 2: deterministic detection --------------------------------------


def test_planner_detects_makefile_targets(tmp_path):
    (tmp_path / "Makefile").write_text(
        "install:\n\techo i\n\ntest:\n\techo t\n\nlint:\n\techo l\n", encoding="utf-8"
    )
    by_kind = _by_kind(_resolve(tmp_path))
    assert by_kind["test"].command == "make test"
    assert by_kind["test"].provenance == "Makefile:test"
    assert by_kind["lint"].command == "make lint"
    assert by_kind["lint"].provenance == "Makefile:lint"


def test_planner_detects_package_json_scripts(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "lint": "eslint ."}}), encoding="utf-8"
    )
    by_kind = _by_kind(_resolve(tmp_path))
    assert by_kind["test"].command == "npm test"
    assert by_kind["test"].provenance == "package.json:scripts.test"
    assert by_kind["lint"].command == "npm run lint"
    assert by_kind["lint"].provenance == "package.json:scripts.lint"


def test_planner_skips_npm_placeholder_test_script(tmp_path):
    # `npm init`'s default script is a declared *absence*, not a contract.
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}), encoding="utf-8"
    )
    assert _resolve(tmp_path) == []


def test_planner_detects_pyproject_pytest_and_ruff_via_python_m(tmp_path):
    # Python-ecosystem tools are invoked `python -m <tool>` so an installed-but-not-
    # on-PATH tool still resolves (the ADR's robustness floor).
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n\n[tool.ruff]\nline-length = 110\n",
        encoding="utf-8",
    )
    by_kind = _by_kind(_resolve(tmp_path))
    assert by_kind["test"].command == "python -m pytest"
    assert by_kind["test"].provenance == "pyproject.toml:pytest"
    assert by_kind["lint"].command == "python -m ruff check"
    assert by_kind["lint"].provenance == "pyproject.toml:ruff"


def test_planner_detects_go_module(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.22\n", encoding="utf-8")
    by_kind = _by_kind(_resolve(tmp_path))
    assert by_kind["test"].command == "go test ./..."
    assert by_kind["lint"].command == "go vet ./..."


def test_planner_detects_cargo_manifest(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "m"\n', encoding="utf-8")
    assert _by_kind(_resolve(tmp_path))["test"].command == "cargo test"


def test_planner_detects_pre_commit_config_as_lint(tmp_path):
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    by_kind = _by_kind(_resolve(tmp_path))
    assert by_kind["lint"].command == "python -m pre_commit run --all-files"
    assert "test" not in by_kind


def test_planner_ci_workflow_ranks_above_makefile(tmp_path):
    # CI is the gate the project actually trusts — least gameable (ADR-0007).
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: uv run pytest -q\n      - run: uv run ruff check .\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text("test:\n\techo t\nlint:\n\techo l\n", encoding="utf-8")
    by_kind = _by_kind(_resolve(tmp_path))
    assert by_kind["test"].command == "uv run pytest -q"
    assert by_kind["test"].provenance == "ci:.github/workflows/ci.yml"
    assert by_kind["lint"].command == "uv run ruff check ."


def test_planner_reads_ci_block_scalar_run_steps(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yaml").write_text(
        "jobs:\n  t:\n    steps:\n      - run: |\n          npm ci\n          npm test\n",
        encoding="utf-8",
    )
    assert _by_kind(_resolve(tmp_path))["test"].command == "npm test"


def test_planner_ci_install_lines_are_not_test_commands(tmp_path):
    # PR-#40 review (HIGH): `pip install pytest ruff` token-matched as the test
    # command and won by rank — a vacuous always-passing check. Dependency/setup
    # lines must be skipped; classification keys on the program position.
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - run: sudo apt-get update && sudo apt-get install -y ripgrep\n"
        "      - run: pip install pytest ruff\n"
        "      - run: uv sync --frozen\n"
        "      - run: npm install eslint\n"
        "      - run: cd subdir\n"
        "      - run: export FOO=pytest\n"
        "      - run: echo pytest done\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text("test:\n\techo t\n", encoding="utf-8")
    by_kind = _by_kind(_resolve(tmp_path))
    # No CI candidate survives; detection falls through to the next tier.
    assert by_kind["test"].command == "make test"
    assert by_kind["test"].provenance == "Makefile:test"
    assert "lint" not in by_kind


def test_planner_ci_classifies_on_program_position(tmp_path):
    # The invocation segment after setup steps (env prefix, && chaining) is the
    # declared command; the install half of the line never is.
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - run: pip install -e . && CI=1 pytest -q tests/\n"
        "      - run: npx eslint src\n",
        encoding="utf-8",
    )
    by_kind = _by_kind(_resolve(tmp_path))
    assert by_kind["test"].command == "CI=1 pytest -q tests/"
    assert by_kind["lint"].command == "npx eslint src"


def test_planner_malformed_package_json_never_raises(tmp_path):
    # PR-#40 review: resolve() runs inside the loop and must never crash on a
    # malformed repo artifact — the exact failure class ADR-0007 exists to remove.
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": {"cmd": "x"}, "lint": None}}), encoding="utf-8"
    )
    assert _resolve(tmp_path) == []
    (tmp_path / "package.json").write_text('{"scripts": "not-a-table"}', encoding="utf-8")
    assert _resolve(tmp_path) == []
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    assert _resolve(tmp_path) == []


def test_planner_malformed_pyproject_never_raises(tmp_path):
    # Non-table values where tables are expected must be skipped, not raised on —
    # and a string `tool`/`project` must not substring-match into a detection.
    (tmp_path / "pyproject.toml").write_text('project = "pytest"\ntool = "pytest"\n', encoding="utf-8")
    assert _resolve(tmp_path) == []
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = "pytest"\n\n[project.optional-dependencies]\nx = "pytest"\n',
        encoding="utf-8",
    )
    assert _resolve(tmp_path) == []
    (tmp_path / "pyproject.toml").write_text("not toml [", encoding="utf-8")
    assert _resolve(tmp_path) == []


def test_planner_empty_repo_resolves_empty_plan(tmp_path):
    # No contract discovered → an EMPTY plan (the verifier fails legibly), never an
    # invented Python default (the old `pytest -q`/`ruff check` assumption).
    (tmp_path / "main.lua").write_text("print('hi')\n", encoding="utf-8")
    assert _resolve(tmp_path) == []


# --- tier 3: LLM fallback (evidence-grounded only) -------------------------


def test_planner_llm_disabled_without_model_degrades_to_detection(tmp_path):
    # No planner model configured → the fallback is never consulted (offline-safe).
    client = _CountingClient([{"kind": "test", "command": "busted .", "source_path": "justfile"}])
    plan = _resolve(tmp_path, _config(planner_model=None), client=client)
    assert plan == []
    assert client.calls == 0


def test_planner_llm_not_consulted_when_detection_resolves(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\techo t\nlint:\n\techo l\n", encoding="utf-8")
    client = _CountingClient()
    plan = _resolve(tmp_path, _config(planner_model="cheap-model"), client=client)
    assert _by_kind(plan)["test"].command == "make test"
    assert client.calls == 0


def test_planner_llm_proposal_accepted_with_valid_citation(tmp_path):
    # The model only ever PROPOSES; the harness validates the citation (the script/
    # target actually exists in the cited artifact) before accepting.
    (tmp_path / "justfile").write_text("test:\n\tbusted .\n", encoding="utf-8")
    client = _CountingClient([{"kind": "test", "command": "busted .", "source_path": "justfile"}])
    plan = _resolve(tmp_path, _config(planner_model="cheap-model"), client=client)
    by_kind = _by_kind(plan)
    assert client.calls == 1
    assert by_kind["test"].command == "busted ."
    assert by_kind["test"].provenance == "llm:justfile"


def test_planner_llm_proposal_rejected_without_provenance(tmp_path):
    # A proposal citing a missing artifact has no provenance → rejected outright.
    (tmp_path / "justfile").write_text("test:\n\tbusted .\n", encoding="utf-8")
    client = _CountingClient([{"kind": "test", "command": "busted .", "source_path": "no-such-file"}])
    assert _resolve(tmp_path, _config(planner_model="cheap-model"), client=client) == []


def test_planner_llm_proposal_rejected_when_command_not_in_citation(tmp_path):
    # The cited artifact exists but does not declare the proposed command → forged.
    (tmp_path / "justfile").write_text("test:\n\tbusted .\n", encoding="utf-8")
    client = _CountingClient([{"kind": "test", "command": "pytest -q", "source_path": "justfile"}])
    assert _resolve(tmp_path, _config(planner_model="cheap-model"), client=client) == []


# --- freeze semantics + the typed journal event ----------------------------


def test_taskstate_verification_plan_freezes_once():
    state = TaskState(goal="fix it", task_kind="edit")
    assert state.verification_plan is None
    plan = [PlannedCheck(name="tests", command="make test", kind="test", provenance="Makefile:test")]
    state.freeze_verification_plan(plan)
    assert state.verification_plan == plan
    with pytest.raises(RuntimeError):
        state.freeze_verification_plan([])
    # The frozen plan round-trips with the rest of the state (source of truth, §7).
    assert TaskState.model_validate_json(state.model_dump_json()).verification_plan == plan


def test_verification_plan_frozen_event_round_trips():
    event = VerificationPlanFrozen(
        event_id=1,
        session_id="s",
        checks=[PlannedCheck(name="tests", command="make test", kind="test", provenance="Makefile:test")],
    )
    reparsed = parse_event(dump_event(event))
    assert reparsed == event
    assert reparsed.type == "verification_plan_frozen"


# --- tier 4: greenfield smoke floor (ADR-0014) -----------------------------


def _propose_smoke(tmp_path, client, files=("main.py",), config=None):
    planner = VerificationPlanner(config or _config(), client=client)
    return planner.propose_smoke_check(Workspace(tmp_path), list(files))


def test_smoke_floor_authors_model_check(tmp_path):
    # No declared contract; the model authors one executable check the harness will run.
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    client = _CountingClient([{"command": "python -m py_compile main.py", "rationale": "compiles"}])
    check = _propose_smoke(tmp_path, client)
    assert check == PlannedCheck(
        name="smoke", command="python -m py_compile main.py", kind="smoke", provenance="model-smoke"
    )


def test_smoke_floor_accepts_allowlisted_checkers(tmp_path):
    # Non-executing checkers across stacks are accepted (after python -m / npx unwrapping).
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    for cmd in (
        "python -m py_compile main.py",
        "ruff check",
        "node --check main.py",
        "npx tsc --noEmit",
        "go vet ./...",
        "php -l main.py",
    ):
        check = _propose_smoke(tmp_path, _CountingClient([{"command": cmd}]))
        assert check is not None and check.command == cmd, cmd


def test_smoke_floor_rejects_executing_or_unlisted_commands(tmp_path):
    # The allowlist (not a denylist) is what bounds unattended execution (PR #50, ADR-0014).
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    for cmd in (
        "true",  # vacuous, not a checker
        "echo ok",
        "python -c 'import main'",  # arbitrary code execution as a single argv
        "node -e 'require(\"./main\")'",
        "node main.py",  # bare node executes (no --check)
        "bash -c true",  # shell wrapper
        "pytest",  # runs project code
        "go run main.go",  # runs the program
        "cargo test",  # build.rs / proc-macros execute at check time
        "rm -rf /",
        "ruff check /etc",  # allowlisted program, but escapes the workspace
    ):
        assert _propose_smoke(tmp_path, _CountingClient([{"command": cmd}])) is None, cmd


def test_smoke_floor_none_when_model_makes_no_call(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    assert _propose_smoke(tmp_path, _CountingClient()) is None


def test_smoke_floor_none_when_no_readable_files(tmp_path):
    # Nothing on disk to excerpt → no call is even made (no floor, no spend).
    client = _CountingClient([{"command": "python -m py_compile main.py"}])
    assert _propose_smoke(tmp_path, client, files=("ghost.py",)) is None
    assert client.calls == 0


def test_smoke_floor_degrades_on_client_error(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")

    def _boom(**_kw):
        raise RuntimeError("endpoint down")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_boom)))
    assert _propose_smoke(tmp_path, client) is None


# --- late-binding the floor onto an empty frozen plan (ADR-0014) -----------


def _smoke() -> PlannedCheck:
    return PlannedCheck(
        name="smoke", command="python -m py_compile main.py", kind="smoke", provenance="model-smoke"
    )


def test_set_smoke_floor_binds_over_empty_plan():
    state = TaskState(goal="g", task_kind="edit")
    state.freeze_verification_plan([])  # tiers 1-3 discovered nothing
    state.set_smoke_floor([_smoke()])
    assert state.verification_plan == [_smoke()]


def test_set_smoke_floor_rejected_over_real_contract():
    state = TaskState(goal="g", task_kind="edit")
    state.freeze_verification_plan(
        [PlannedCheck(name="tests", command="make test", kind="test", provenance="Makefile:test")]
    )
    with pytest.raises(RuntimeError):  # a declared/detected contract is never displaced
        state.set_smoke_floor([_smoke()])


def test_set_smoke_floor_rejected_before_freeze():
    state = TaskState(goal="g", task_kind="edit")  # plan is None (unfrozen)
    with pytest.raises(RuntimeError):
        state.set_smoke_floor([_smoke()])


def test_vacuous_declared_check_flags_noops():
    # The non-vacuity guard (ADR-0038) rejects commands that don't run the project's code,
    # after unwrapping env/uv/npx/python -m wrappers; real check runners pass.
    assert vacuous_declared_check("")
    assert vacuous_declared_check("true")
    assert vacuous_declared_check("echo done")
    assert vacuous_declared_check(":")
    assert not vacuous_declared_check("python -m pytest test_x.py")
    assert not vacuous_declared_check("ruff check .")
    assert not vacuous_declared_check("uv run pytest")  # unwrapped to pytest


def _declared(command: str) -> PlannedCheck:
    return PlannedCheck(name="declared_1", command=command, kind="declared", provenance="model-declared")


def _floor(command: str) -> PlannedCheck:
    return PlannedCheck(name="floor", command=command, kind="floor", provenance="model-smoke")


def test_append_verification_floor_is_idempotent_and_preserves_declared():
    # ADR-0038: the immutable floor is appended beneath the declared contract, once.
    state = TaskState(goal="g", task_kind="edit")
    state.freeze_verification_plan([_declared("pytest")])
    state.append_verification_floor(_floor("python -m py_compile x.py"))
    assert [c.kind for c in (state.verification_plan or [])] == ["declared", "floor"]
    state.append_verification_floor(_floor("ruff check ."))  # a second attempt is a no-op
    assert sum(1 for c in (state.verification_plan or []) if c.kind == "floor") == 1


def test_amend_declared_contract_preserves_the_floor():
    # ADR-0038: an amendment rewrites the declared checks but can NEVER touch the immutable floor.
    state = TaskState(goal="g", task_kind="edit")
    state.freeze_verification_plan([_declared("pytest")])
    state.append_verification_floor(_floor("python -m py_compile x.py"))
    state.amend_declared_contract([_declared("pytest -k new")])
    assert [c.kind for c in (state.verification_plan or [])] == ["declared", "floor"]
    assert any("py_compile" in c.command for c in (state.verification_plan or []))  # floor survived
    assert any(c.command == "pytest -k new" for c in (state.verification_plan or []))  # declared replaced


def test_amend_before_freeze_raises():
    state = TaskState(goal="g", task_kind="edit")
    with pytest.raises(RuntimeError):
        state.amend_declared_contract([_declared("pytest")])
