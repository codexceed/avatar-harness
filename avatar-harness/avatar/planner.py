"""VerificationPlanner — per-session verification-plan resolution (ADR-0007).

The harness-owned collaborator that resolves *what proves this repo's work*,
once per session, before editing begins. Resolution is tiered:

1. **Config override (always wins):** `AVATAR_TEST_COMMAND` / `AVATAR_LINT_COMMAND`,
   per slot. The user's stated contract is never overridden.
2. **Deterministic detection (no LLM, no Python assumption):** read repo artifacts —
   CI workflows, `package.json`, `pyproject.toml`, tox/nox, `Cargo.toml`, `go.mod`,
   `.pre-commit-config.yaml`, Makefile — and extract their *declared* test/lint
   invocations. CI-derived commands rank above arbitrary Makefile targets (CI is
   the gate the project actually trusts — least gameable).
3. **LLM fallback (evidence-grounded only, opt-in via `AVATAR_PLANNER_MODEL`):** the
   model may *propose* a command for a slot detection left empty, but only citing
   the artifact it came from; the harness validates the citation before accepting.
   A proposal without valid provenance is rejected. No model configured → this tier
   simply does not exist (resolution stays deterministic and offline).

The resolved plan is a list of `PlannedCheck` (name, command, kind, provenance).
The *runner* freezes it onto `TaskState` at the investigating → editing boundary
and journals it; the `Verifier` then executes it with zero language knowledge.
Python-ecosystem tools are emitted as `python -m <tool>` so an installed-but-not-
on-PATH tool still resolves (the ADR's robustness floor).
"""

import json
import re
import shlex
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from avatar.config import HarnessConfig
from avatar.state import PlannedCheck
from avatar.workspace import Workspace

_Slot = Literal["test", "lint"]
_Declared = tuple[_Slot, str, str]  # (kind, command, provenance)

_SLOT_NAMES: dict[_Slot, str] = {"test": "tests", "lint": "lint"}

# Detection ranks (lower wins): CI > declared manifests/scripts > Makefile targets.
_RANK_CI = 0
_RANK_MANIFEST = 1
_RANK_MAKEFILE = 2

# Deterministic classification of a CI `run:` step into a slot, keyed on the
# *program position* of each command segment — never substring presence, so
# `pip install pytest ruff` is a setup line, not the test command (PR-#40 review).
# Setup/dependency programs: never a verification invocation.
_SETUP_PROGRAMS = frozenset(
    {
        "pip",
        "pip3",
        "apt",
        "apt-get",
        "apk",
        "dnf",
        "yum",
        "brew",
        "cd",
        "export",
        "echo",
        "curl",
        "wget",
        "git",
        "source",
        "set",
        "mkdir",
        "chmod",
    }
)
# Programs whose invocation IS the test / lint run.
_TEST_PROGRAMS = frozenset({"pytest", "tox", "nox", "unittest", "jest", "vitest", "rspec", "ctest"})
_LINT_PROGRAMS = frozenset(
    {
        "ruff",
        "flake8",
        "pylint",
        "mypy",
        "pyright",
        "pyrefly",
        "eslint",
        "golangci-lint",
        "rubocop",
        "biome",
        "pre-commit",
        "pre_commit",
    }
)

# `&&` / `||` / `;` chain segments are classified independently.
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# `npm init`'s default test script declares an absence, not a contract.
_NPM_PLACEHOLDER = "no test specified"

_MAKE_TARGET_RE = re.compile(r"^(test|tests|lint)\s*:(?!=)")

# Wrapper/runner tokens carry no citation value ("npm run lint" is cited by "lint",
# not by "npm"); flags likewise. Used by the LLM-proposal citation validator.
_RUNNER_TOKENS = frozenset(
    {
        "npm",
        "npx",
        "yarn",
        "pnpm",
        "run",
        "make",
        "python",
        "python3",
        "uv",
        "uvx",
        "go",
        "cargo",
        "-m",
        "exec",
        "bundle",
    }
)

_EXCERPT_CHARS = 1_500  # per-artifact excerpt budget handed to the LLM fallback

# One forced function call with a constrained schema — no prose to parse (the
# `intent.ModeClassifier` precedent). The model must cite `source_path`.
_PROPOSE_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_verification_check",
        "description": (
            "Propose ONE verification command this repository itself declares, citing the "
            "artifact file it is declared in. Never invent a command the repo does not declare."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["test", "lint"]},
                "command": {"type": "string", "description": "The declared command, verbatim."},
                "source_path": {
                    "type": "string",
                    "description": "Repo-relative path of the artifact declaring this command.",
                },
            },
            "required": ["kind", "command", "source_path"],
        },
    },
}

_PROPOSE_SYSTEM = (
    "You resolve a repository's declared verification contract. Given excerpts of the repo's "
    "build/CI artifacts, propose the declared test or lint command for the requested slots by "
    "calling propose_verification_check once per slot. Only propose commands the artifacts "
    "actually declare, with the citing source_path. If nothing is declared, make no call."
)

# The greenfield floor (ADR-0014): the model AUTHORS one smoke command for code it just
# wrote in a repo that declares no contract; the harness runs it and the real exit code is
# the signal (never the model's say-so). A single constrained tool call — the `_PROPOSE_TOOL`
# precedent, authoring instead of citing — which the model may decline by making no call.
_SMOKE_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_smoke_check",
        "description": (
            "Propose ONE command that smoke-tests the code just written by parsing / compiling / "
            "type-checking it (a NON-executing check) and exiting non-zero if it is broken — never "
            "a no-op you assert passes. The harness runs it and reads the real exit code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "A single shell command that runs the new code and exits 0 on success.",
                },
                "rationale": {
                    "type": "string",
                    "description": "What passing this command proves about the code.",
                },
            },
            "required": ["command"],
        },
    },
}

_SMOKE_SYSTEM = (
    "You verify freshly-written code in a repository that declares no test or lint contract. "
    "Given the files just written, call propose_smoke_check ONCE with a single command that "
    "parses / compiles / type-checks what was written and fails (non-zero exit) if it is broken. "
    "For SAFETY the harness runs only NON-executing checkers — pick one of: "
    "`python -m py_compile <files>`, `ruff check`, `node --check <file>`, `tsc --noEmit`, "
    "`go vet ./...` / `go build ./...`, `gofmt -l <files>`, `ruby -c <file>`, `perl -c <file>`, "
    "`php -l <file>`, `deno check <file>`. Reference the files just written, and prefer a "
    "DEPENDENCY-FREE check (compiling/parsing won't fail on uninstalled third-party imports). "
    "Do NOT use code runners (`python -c`, `node -e`, `pytest`, a shell `-c` wrapper) — they are "
    "rejected. If none of these fit the stack, make no call."
)

# The floor runs a MODEL-AUTHORED command unattended, outside the before_tool_call permission
# gate (invariant #4, ADR-0014 §security). A denylist cannot bound that — `python -c "..."`,
# `node -e "..."`, `bash -c "..."` are each a SINGLE argv, so arbitrary execution hides behind an
# allowed program name. So the floor is an ALLOWLIST: after `effective_invocation` unwraps
# `python -m`/`npx`/`uv run`/`sudo`, the program must be a known NON-executing checker (parses /
# compiles / type-checks without running the project's code). A runner safe only in a check
# sub-mode carries its required token(s); `None` = safe in any invocation. Deliberately excluded:
# tools with a code-exec escape hatch — pylint (`--init-hook`), mypy/pyright (config-declared
# plugins), eslint/biome (repo-config plugins), cargo (build.rs / proc-macros run at check time).
_SMOKE_ALLOWED: dict[str, set[str] | None] = {
    "py_compile": None,  # python -m py_compile <files> — compiles, never executes
    "compileall": None,  # python -m compileall — same, over a tree
    "ruff": None,  # Rust static analyzer; no code-execution escape
    "gofmt": None,  # format check; runs nothing
    "tsc": {"--noEmit"},  # type-check without emit/run
    "node": {"--check"},  # parse-only; bare `node FILE` would execute
    "deno": {"check"},
    "go": {"vet", "build"},  # neither runs the program (unlike `go run`/`go test`)
    "ruby": {"-c"},  # syntax check only
    "perl": {"-c"},
    "php": {"-l"},  # lint / syntax check
}


class VerificationPlanner:
    """Resolves the per-session verification plan (ADR-0007) — harness-owned.

    Args:
        config: Harness config; `test_command`/`lint_command` are the override tier
            and `planner_model` (unset by default) opts into the LLM fallback.
        client: An injected OpenAI-compatible client for the fallback, or `None` to
            build one lazily on first use (the `ModeClassifier` precedent; the
            fallback is never consulted unless `planner_model` is set).
    """

    def __init__(self, config: HarnessConfig | None = None, client: Any = None) -> None:
        self.config = config
        self._client = client

    def resolve(self, ws: Workspace) -> list[PlannedCheck]:
        """Resolve the verification plan for `ws`: override → detection → LLM fallback.

        Args:
            ws: The run-scoped workspace whose repo artifacts are read.

        Returns:
            The resolved checks, test slot first; empty when nothing is declared
            anywhere (the verifier then fails legibly — never an invented default).
        """
        detected = _detect(ws.root)
        overrides = {c.kind: c for c in config_override_checks(self.config)}
        plan: list[PlannedCheck] = []
        missing: list[str] = []
        for kind in ("test", "lint"):
            check = overrides.get(kind) or detected.get(kind)
            if check is not None:
                plan.append(check)
            else:
                missing.append(kind)
        if missing:
            plan.extend(self._llm_propose(ws.root, missing))
        plan.sort(key=lambda c: 0 if c.kind == "test" else 1)
        return plan

    # --- tier 3: LLM fallback (evidence-grounded only) --------------------

    def _llm_propose(self, root: Path, missing: list[str]) -> list[PlannedCheck]:
        """Ask the (opt-in) planner model to propose checks for the unresolved slots.

        The model only ever proposes; every proposal must cite an existing repo
        artifact that actually declares the command, or it is rejected. Any
        endpoint failure degrades to "no proposal" — resolution never blocks.

        Args:
            root: The workspace root the citations are validated against.
            missing: The slots (`test`/`lint`) detection left unresolved.

        Returns:
            The citation-validated proposals (possibly empty).
        """
        model = self.config.planner_model if self.config else None
        if not model:
            return []
        excerpts = _artifact_excerpts(root)
        if not excerpts:
            return []  # nothing to cite → nothing to propose
        user = f"Unresolved slots: {', '.join(missing)}.\n\nRepository artifacts:\n{excerpts}"
        try:
            response = self._ensure_client().chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _PROPOSE_SYSTEM},
                    {"role": "user", "content": user},
                ],
                tools=[_PROPOSE_TOOL],
                temperature=0,
            )
            calls = getattr(response.choices[0].message, "tool_calls", None) or []
            raw = [json.loads(call.function.arguments or "{}") for call in calls]
        except Exception:  # any failure degrades to detection-only, never blocks
            return []
        accepted: list[PlannedCheck] = []
        taken: set[str] = set()
        for args in raw:
            kind, command, source = args.get("kind"), args.get("command"), args.get("source_path")
            if kind not in missing or kind in taken or not command or not source:
                continue
            if not _citation_valid(root, command, source):
                continue  # no validated provenance → rejected (ADR-0007 tier 3)
            taken.add(kind)
            accepted.append(
                PlannedCheck(name=_SLOT_NAMES[kind], command=command, kind=kind, provenance=f"llm:{source}")
            )
        return accepted

    def _ensure_client(self) -> Any:
        """Return the OpenAI-compatible client, constructing it on first use.

        Returns:
            The injected client, or one constructed from `config` on first call.
        """
        if self._client is None:
            from openai import OpenAI  # noqa: PLC0415 — lazy: `openai` is an optional extra

            api_key = self.config.api_key if self.config else None
            base_url = self.config.base_url if self.config else None
            self._client = OpenAI(api_key=api_key, base_url=base_url)
        return self._client

    # --- tier 4: greenfield smoke floor (ADR-0014) ------------------------

    def propose_smoke_check(self, ws: Workspace, files_modified: list[str]) -> PlannedCheck | None:
        """Have the model author ONE executable smoke check for freshly-written code.

        The greenfield floor: used only when tiers 1-3 resolved nothing and the run
        wrote code, so there is genuinely no declared contract to discover. The model
        chooses *which* command; the harness still runs it and reads the real exit code,
        so this is author-and-run, never the self-certification §5 forbids. Resolved at
        verification time (the artifact under test does not exist at the freeze boundary).
        Runs on `config.model` (the main model) so the floor needs zero extra config; any
        endpoint failure degrades to "no floor", never blocks.

        Args:
            ws: The run-scoped workspace whose just-written files are excerpted.
            files_modified: The repo-relative paths the run created or changed.

        Returns:
            The `model-smoke` check, or `None` when nothing runnable was proposed.
        """
        model = self.config.model if self.config else None
        if not model or not files_modified:
            return None
        excerpts = _modified_excerpts(ws.root, files_modified)
        if not excerpts:
            return None
        try:
            response = self._ensure_client().chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SMOKE_SYSTEM},
                    {"role": "user", "content": f"Files just written:\n{excerpts}"},
                ],
                tools=[_SMOKE_TOOL],
                temperature=0,
            )
            calls = getattr(response.choices[0].message, "tool_calls", None) or []
            if not calls:
                return None
            args = json.loads(calls[0].function.arguments or "{}")
        except Exception:  # any failure degrades to "no floor", never blocks (tier-3 contract)
            return None
        command = (args.get("command") or "").strip()
        if not _is_safe_smoke(command):
            return None
        return PlannedCheck(name="smoke", command=command, kind="smoke", provenance="model-smoke")


def config_override_checks(config: HarnessConfig | None) -> list[PlannedCheck]:
    """The override tier: checks declared explicitly in config (always win, per slot).

    Shared by the planner (tier 1) and the verifier's no-plan fallback, so the
    two read the user's stated contract identically.

    Args:
        config: The harness config, or `None` (no overrides).

    Returns:
        Up to one test and one lint check, for the non-empty config commands.
    """
    checks: list[PlannedCheck] = []
    if config is not None and config.test_command:
        checks.append(
            PlannedCheck(
                name="tests",
                command=config.test_command,
                kind="test",
                provenance="config:AVATAR_TEST_COMMAND",
            )
        )
    if config is not None and config.lint_command:
        checks.append(
            PlannedCheck(
                name="lint",
                command=config.lint_command,
                kind="lint",
                provenance="config:AVATAR_LINT_COMMAND",
            )
        )
    return checks


# --- tier 2: deterministic detection ---------------------------------------


def _detect(root: Path) -> dict[str, PlannedCheck]:
    """Detect the repo's declared test/lint commands; best-ranked candidate per slot.

    Args:
        root: The workspace root to read artifacts from.

    Returns:
        The winning check per slot kind (`test`/`lint`); a slot detection cannot
        resolve is simply absent.
    """
    candidates = [*_ci_candidates(root), *_manifest_candidates(root), *_makefile_candidates(root)]
    best: dict[str, tuple[int, PlannedCheck]] = {}
    for rank, check in candidates:
        current = best.get(check.kind)
        if current is None or rank < current[0]:
            best[check.kind] = (rank, check)
    return {kind: ranked[1] for kind, ranked in best.items()}


def _ci_candidates(root: Path) -> list[tuple[int, PlannedCheck]]:
    """Extract declared test/lint invocations from CI workflow `run:` steps.

    Args:
        root: The workspace root.

    Returns:
        Ranked candidates from `.github/workflows/*.yml|yaml`, in file order.
    """
    found: list[tuple[int, PlannedCheck]] = []
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return found
    for wf in sorted(p for p in workflows.iterdir() if p.suffix in (".yml", ".yaml")):
        text = _read(wf)
        provenance = f"ci:.github/workflows/{wf.name}"
        for line in _iter_run_commands(text):
            for segment in _split_segments(line):
                kind = _classify_command(segment)
                if kind is not None:
                    found.append(
                        (
                            _RANK_CI,
                            PlannedCheck(
                                name=_SLOT_NAMES[kind], command=segment, kind=kind, provenance=provenance
                            ),
                        )
                    )
    return found


def _iter_run_commands(text: str) -> list[str]:
    """Pull the command lines out of YAML `run:` steps (inline and block scalars).

    A deliberately small line-based scan — no YAML dependency, no execution.

    Args:
        text: The workflow file text.

    Returns:
        The declared command lines, in order.
    """
    commands: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        marker = stripped.removeprefix("- ")
        if marker.startswith("run:"):
            value = marker.split("run:", 1)[1].strip()
            if value in ("|", "|-", ">", ">-", ""):
                indent = len(lines[i]) - len(lines[i].lstrip())
                i += 1
                while i < len(lines) and (
                    not lines[i].strip() or (len(lines[i]) - len(lines[i].lstrip())) > indent
                ):
                    if lines[i].strip():
                        commands.append(lines[i].strip())
                    i += 1
                continue
            commands.append(value)
        i += 1
    return commands


def _split_segments(line: str) -> list[str]:
    """Split a shell line into its `&&` / `||` / `;` chained command segments.

    Args:
        line: One declared command line.

    Returns:
        The non-empty segments, stripped, in order.
    """
    return [segment.strip() for segment in _SEGMENT_SPLIT_RE.split(line) if segment.strip()]


def effective_invocation(command: str) -> tuple[str, list[str]]:
    """The effective program + args of one command segment (program-position parse).

    Strips leading env-var assignments (`CI=1 pytest`) and unwraps runner wrappers
    (`sudo`, `uv run`, `npx`, `python -m <module>`) so classification keys on what
    actually executes — never on a token appearing anywhere in the line.

    Args:
        command: One command segment (no `&&`/`;` chaining).

    Returns:
        `(program, args)` — program is the basename (empty when nothing remains).
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    while tokens and _ENV_ASSIGN_RE.match(tokens[0]):
        tokens.pop(0)
    while tokens:
        program = tokens[0].rsplit("/", 1)[-1]
        if program == "sudo":
            tokens = [t for t in tokens[1:] if not t.startswith("-")] or []
            continue
        if program in ("uv", "uvx") and tokens[1:2] == ["run"]:
            tokens = tokens[2:]
            continue
        if program == "npx":
            tokens = tokens[1:]
            continue
        if program in ("python", "python3") and tokens[1:2] == ["-m"]:
            tokens = tokens[2:]
            continue
        return program, tokens[1:]
    return "", []


# Programs that don't exercise the project's code — a declared verification check built on one is
# vacuous (it passes without proving anything). Rejected at declaration so a model-declared contract
# (ADR-0038) can't be a no-op the model asserts passes. NOT exhaustive: the immutable floor (a check
# the model can't author or amend) is the real anti-vacuity anchor; this only blocks the obvious.
_VACUOUS_PROGRAMS = frozenset({"true", "false", ":", "echo", "printf", "cat", "ls", "pwd", "test", "["})


def vacuous_declared_check(command: str) -> bool:
    """Whether a model-declared verification command is vacuous (proves nothing — ADR-0038).

    A check is vacuous when it is empty or its effective program (after unwrapping
    env/`sudo`/`uv run`/`npx`/`python -m`) does not run the project's code — e.g. `true`,
    `echo ok`, `:`. Such a command exits 0 regardless of the artifact, so it can't be a real
    contract. This is a *lower bound* guard, not proof of adequacy: the immutable floor beneath
    the declared contract is what ultimately anchors non-vacuity.

    Args:
        command: The declared check command to validate.

    Returns:
        `True` when the command is vacuous and must be rejected.
    """
    program, _args = effective_invocation(command)
    return not program or program in _VACUOUS_PROGRAMS


def _first_positional(args: list[str]) -> str:
    """The first non-flag argument (the sub-command/target position), or `""`.

    Args:
        args: The program's arguments.

    Returns:
        The first argument not starting with `-`, or `""` when none.
    """
    return next((a for a in args if not a.startswith("-")), "")


def _classify_node_runner(args: list[str]) -> _Slot | None:
    """Classify an `npm`/`yarn`/`pnpm` invocation by its sub-command.

    `install`/`ci` and friends are setup; `test` and `run test*`/`run lint*`
    are the declared scripts.

    Args:
        args: The runner's arguments.

    Returns:
        The slot, or `None`.
    """
    sub = _first_positional(args)
    if sub == "test":
        return "test"
    if sub == "run":
        idx = args.index("run")
        script = args[idx + 1] if idx + 1 < len(args) else ""
        if script.startswith("test"):
            return "test"
        if script.startswith("lint"):
            return "lint"
    return None


def _classify_make(args: list[str]) -> _Slot | None:
    """Classify a `make` invocation by its target.

    Args:
        args: The make arguments.

    Returns:
        The slot for a `test`/`tests`/`lint` target, else `None`.
    """
    target = _first_positional(args)
    if target in ("test", "tests"):
        return "test"
    return "lint" if target == "lint" else None


def _classify_go(args: list[str]) -> _Slot | None:
    """Classify a `go` invocation by its sub-command (`test` / `vet`).

    Args:
        args: The go arguments.

    Returns:
        The slot, or `None`.
    """
    sub = _first_positional(args)
    return "test" if sub == "test" else ("lint" if sub == "vet" else None)


def _classify_cargo(args: list[str]) -> _Slot | None:
    """Classify a `cargo` invocation by its sub-command (`test` / `clippy`).

    Args:
        args: The cargo arguments.

    Returns:
        The slot, or `None`.
    """
    sub = _first_positional(args)
    return "test" if sub == "test" else ("lint" if sub == "clippy" else None)


def _classify_jvm_build(args: list[str]) -> _Slot | None:
    """Classify an `mvn`/`gradle`/`gradlew` invocation by its `test` task.

    Args:
        args: The build-tool arguments.

    Returns:
        `"test"` when a test task is named, else `None`.
    """
    return "test" if "test" in args else None


# Runner programs classified by their sub-command/target, not their own name.
_RUNNER_CLASSIFIERS: dict[str, Callable[[list[str]], _Slot | None]] = {
    "npm": _classify_node_runner,
    "yarn": _classify_node_runner,
    "pnpm": _classify_node_runner,
    "make": _classify_make,
    "go": _classify_go,
    "cargo": _classify_cargo,
    "mvn": _classify_jvm_build,
    "gradle": _classify_jvm_build,
    "gradlew": _classify_jvm_build,
}


def _classify_command(segment: str) -> _Slot | None:
    """Classify one command segment as a test or lint invocation, or neither.

    Keys on the program position: dependency/setup lines (`pip install …`,
    `npm ci`, `apt-get …`, `uv sync`) are skipped outright, and a runner like
    `make`/`npm`/`go`/`cargo` classifies by its sub-command/target — so a tool
    name appearing as an install *argument* never classifies (PR-#40 review).

    Args:
        segment: One command segment.

    Returns:
        `"test"`, `"lint"`, or `None` when the segment is neither.
    """
    program, args = effective_invocation(segment)
    if not program or program in _SETUP_PROGRAMS:
        return None
    runner = _RUNNER_CLASSIFIERS.get(program)
    if runner is not None:
        return runner(args)
    if program in _TEST_PROGRAMS:
        return "test"
    if program in _LINT_PROGRAMS:
        return "lint"
    return None


def _manifest_candidates(root: Path) -> list[tuple[int, PlannedCheck]]:
    """Extract declared commands from package manifests (rank below CI, above Makefile).

    Args:
        root: The workspace root.

    Returns:
        Ranked candidates from `package.json`, `pyproject.toml`, tox/nox,
        `Cargo.toml`, `go.mod`, and `.pre-commit-config.yaml`.
    """
    declared = [*_npm_declarations(root), *_python_declarations(root)]
    markers: dict[str, list[_Declared]] = {
        "Cargo.toml": [("test", "cargo test", "Cargo.toml")],
        "go.mod": [("test", "go test ./...", "go.mod"), ("lint", "go vet ./...", "go.mod")],
        ".pre-commit-config.yaml": [
            ("lint", "python -m pre_commit run --all-files", ".pre-commit-config.yaml")
        ],
    }
    for marker, entries in markers.items():
        if (root / marker).is_file():
            declared.extend(entries)
    return [
        (
            _RANK_MANIFEST,
            PlannedCheck(name=_SLOT_NAMES[kind], command=command, kind=kind, provenance=provenance),
        )
        for kind, command, provenance in declared
    ]


def _npm_declarations(root: Path) -> list[_Declared]:
    """The test/lint commands `package.json` scripts declare.

    Args:
        root: The workspace root.

    Returns:
        `(kind, command, provenance)` triples for the declared scripts.
    """
    scripts = _package_json_scripts(root)
    declared: list[_Declared] = []
    test_script = scripts.get("test", "")
    if test_script and _NPM_PLACEHOLDER not in test_script:
        declared.append(("test", "npm test", "package.json:scripts.test"))
    if scripts.get("lint"):
        declared.append(("lint", "npm run lint", "package.json:scripts.lint"))
    return declared


def _python_declarations(root: Path) -> list[_Declared]:
    """The test/lint commands the Python manifests declare (`python -m` invocations).

    Args:
        root: The workspace root.

    Returns:
        `(kind, command, provenance)` triples from `pyproject.toml`, tox, and nox.
    """
    declared: list[_Declared] = []
    pyproject = _load_pyproject(root)
    if pyproject is not None:
        # Untrusted input: a malformed pyproject (string where a table is expected)
        # must be skipped, never raised on, and never substring-matched (PR-#40 review).
        tool = _as_dict(pyproject.get("tool"))
        deps = _declared_python_deps(pyproject)
        if "pytest" in tool or any(d.startswith("pytest") for d in deps):
            declared.append(("test", "python -m pytest", "pyproject.toml:pytest"))
        if "ruff" in tool or any(d.startswith("ruff") for d in deps):
            declared.append(("lint", "python -m ruff check", "pyproject.toml:ruff"))
    if (root / "tox.ini").is_file():
        declared.append(("test", "python -m tox", "tox.ini"))
    if (root / "noxfile.py").is_file():
        declared.append(("test", "python -m nox", "noxfile.py"))
    return declared


def _package_json_scripts(root: Path) -> dict[str, str]:
    """The `scripts` table of `package.json`, or empty when absent/invalid.

    Args:
        root: The workspace root.

    Returns:
        The declared npm scripts mapping.
    """
    path = root / "package.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(_read(path))
    except (ValueError, OSError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return {}
    # Untrusted input: only str -> str entries are declared scripts; an object,
    # null, or numeric value is malformed and skipped, never raised on (PR-#40 review).
    return {k: v for k, v in scripts.items() if isinstance(k, str) and isinstance(v, str)}


def _load_pyproject(root: Path) -> dict | None:
    """Parse `pyproject.toml`, or `None` when absent/invalid.

    Args:
        root: The workspace root.

    Returns:
        The parsed document, or `None`.
    """
    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        return tomllib.loads(_read(path))
    except (tomllib.TOMLDecodeError, OSError):
        return None


def _declared_python_deps(pyproject: dict) -> list[str]:
    """Every dependency string declared in a pyproject (deps, extras, dep-groups).

    Args:
        pyproject: The parsed `pyproject.toml` document.

    Returns:
        The flat list of declared requirement strings.
    """
    deps: list[str] = []
    project = _as_dict(pyproject.get("project"))
    deps.extend(_str_items(project.get("dependencies")))
    for group in _as_dict(project.get("optional-dependencies")).values():
        deps.extend(_str_items(group))
    for group in _as_dict(pyproject.get("dependency-groups")).values():
        deps.extend(_str_items(group))
    return deps


def _as_dict(value: object) -> dict:
    """`value` when it is a mapping, else an empty dict (untrusted-artifact guard).

    Args:
        value: A parsed toml/json value of unknown shape.

    Returns:
        The dict, or `{}` for any non-dict.
    """
    return value if isinstance(value, dict) else {}


def _str_items(value: object) -> list[str]:
    """The string items of a parsed list, else empty (untrusted-artifact guard).

    A string is NOT iterated character-wise — a malformed scalar where a list is
    expected yields nothing rather than garbage.

    Args:
        value: A parsed toml/json value of unknown shape.

    Returns:
        The string items, or `[]` for any non-list value.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _makefile_candidates(root: Path) -> list[tuple[int, PlannedCheck]]:
    """Extract `test`/`lint` targets from a Makefile (the lowest detection rank).

    Args:
        root: The workspace root.

    Returns:
        Ranked candidates for the declared targets.
    """
    found: list[tuple[int, PlannedCheck]] = []
    for name in ("Makefile", "makefile", "GNUmakefile"):
        path = root / name
        if not path.is_file():
            continue
        for line in _read(path).splitlines():
            match = _MAKE_TARGET_RE.match(line)
            if match is None:
                continue
            target = match.group(1)
            kind = "lint" if target == "lint" else "test"
            found.append(
                (
                    _RANK_MAKEFILE,
                    PlannedCheck(
                        name=_SLOT_NAMES[kind],
                        command=f"make {target}",
                        kind=kind,
                        provenance=f"{name}:{target}",
                    ),
                )
            )
        break  # first existing makefile wins
    return found


# --- LLM citation validation ------------------------------------------------


def _citation_valid(root: Path, command: str, source: str) -> bool:
    """Whether `source` is a real in-root artifact that actually declares `command`.

    The declared-ness test is token-based: at least one *meaningful* token of the
    command (not a wrapper like `npm`/`make`/`python`, not a flag) must appear in
    the cited file. "The script/target actually exists" — ADR-0007.

    Args:
        root: The workspace root the citation must stay inside.
        command: The proposed command.
        source: The repo-relative path the proposal cites.

    Returns:
        `True` only for an existing, in-root citation that declares the command.
    """
    try:
        cited = (root / source).resolve()
    except OSError:
        return False
    if not cited.is_relative_to(root.resolve()) or not cited.is_file():
        return False
    text = _read(cited)
    if not text:
        return False
    if command in text:
        return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    meaningful = [t for t in tokens if t not in _RUNNER_TOKENS and not t.startswith("-")]
    candidates = meaningful or [t for t in tokens if not t.startswith("-")]
    return any(token in text for token in candidates)


# --- greenfield smoke floor helpers (ADR-0014) -----------------------------


def _modified_excerpts(root: Path, files_modified: list[str]) -> str:
    """Bounded excerpts of the files a run just wrote, for the smoke-floor prompt.

    Args:
        root: The workspace root the paths are relative to.
        files_modified: The repo-relative paths the run created or changed.

    Returns:
        Concatenated `=== path ===` excerpt blocks; empty when none are readable files.
    """
    blocks: list[str] = []
    for rel in sorted(files_modified):
        path = root / rel
        if not path.is_file():
            continue
        text = _read(path)
        if text:
            blocks.append(f"=== {rel} ===\n{text[:_EXCERPT_CHARS]}")
    return "\n\n".join(blocks)


def _is_safe_smoke(command: str) -> bool:
    """Whether `command` is an allowlisted, non-executing verification command (ADR-0014).

    The floor runs this unattended, outside the permission gate, so it is bounded by an
    ALLOWLIST, never a denylist: the command must resolve (after `effective_invocation`
    unwraps `python -m`/`npx`/`uv run`/`sudo`) to a known checker that does not execute the
    project's code, must stay inside the workspace, and — for a runner safe only in a check
    sub-mode — must carry the required token. Arbitrary execution (`python -c`, `node -e`, a
    shell `-c` wrapper, any unlisted program) is rejected, yielding no floor.

    Args:
        command: The model-proposed command.

    Returns:
        `True` only for an allowlisted, workspace-confined, non-executing invocation.
    """
    program, cmd_args = effective_invocation(command)
    if program not in _SMOKE_ALLOWED:
        return False
    if any(_escapes_workspace(a) for a in cmd_args):
        return False
    required = _SMOKE_ALLOWED[program]
    return required is None or any(token in required for token in cmd_args)


def _escapes_workspace(arg: str) -> bool:
    """Whether a command argument points outside the workspace (absolute / parent escape).

    Args:
        arg: One command argument (a flag or a path).

    Returns:
        `True` for an absolute path, a `~` expansion, or a `..` parent escape; `False`
        for a flag or a workspace-relative path.
    """
    if arg.startswith("-"):
        return False  # a flag, not a path
    return arg.startswith(("/", "~")) or ".." in arg.split("/")


def _artifact_excerpts(root: Path) -> str:
    """Bounded excerpts of the repo's build/CI artifacts, for the LLM fallback prompt.

    Args:
        root: The workspace root.

    Returns:
        Concatenated `=== path ===` excerpt blocks; empty when no artifacts exist.
    """
    names = [
        "Makefile",
        "makefile",
        "GNUmakefile",
        "justfile",
        "package.json",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "noxfile.py",
        "Cargo.toml",
        "go.mod",
        ".pre-commit-config.yaml",
    ]
    paths = [root / n for n in names]
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        paths.extend(sorted(p for p in workflows.iterdir() if p.suffix in (".yml", ".yaml")))
    blocks: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        text = _read(path)
        if text:
            blocks.append(f"=== {path.relative_to(root)} ===\n{text[:_EXCERPT_CHARS]}")
    return "\n\n".join(blocks)


def _read(path: Path) -> str:
    """Read a text file defensively; empty string on any error.

    Args:
        path: The file to read.

    Returns:
        The file text, or `""` when unreadable.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
