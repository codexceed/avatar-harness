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
from pathlib import Path
from typing import Any, Literal

from avatar_harness.config import HarnessConfig
from avatar_harness.state import PlannedCheck
from avatar_harness.workspace import Workspace

_Slot = Literal["test", "lint"]
_Declared = tuple[_Slot, str, str]  # (kind, command, provenance)

_SLOT_NAMES: dict[_Slot, str] = {"test": "tests", "lint": "lint"}

# Detection ranks (lower wins): CI > declared manifests/scripts > Makefile targets.
_RANK_CI = 0
_RANK_MANIFEST = 1
_RANK_MAKEFILE = 2

# Deterministic classification of a CI `run:` line into a slot. Substring match on
# the declared command text — no execution, no language inference beyond the tokens.
_CI_TEST_TOKENS = (
    "pytest",
    "unittest",
    "python -m tox",
    "tox -e",
    "nox",
    "go test",
    "cargo test",
    "npm test",
    "npm run test",
    "yarn test",
    "pnpm test",
    "make test",
    "mvn test",
    "gradle test",
    "rspec",
    "jest",
    "vitest",
    "ctest",
)
_CI_LINT_TOKENS = (
    "ruff",
    "flake8",
    "pylint",
    "mypy",
    "pyright",
    "pyrefly",
    "eslint",
    "golangci-lint",
    "go vet",
    "cargo clippy",
    "pre-commit",
    "npm run lint",
    "make lint",
    "rubocop",
    "biome",
)

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
        for command in _iter_run_commands(text):
            kind = _classify_command(command)
            if kind is not None:
                found.append(
                    (
                        _RANK_CI,
                        PlannedCheck(
                            name=_SLOT_NAMES[kind], command=command, kind=kind, provenance=provenance
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


def _classify_command(command: str) -> _Slot | None:
    """Classify a declared command line as a test or lint invocation, or neither.

    Args:
        command: One declared command line.

    Returns:
        `"test"`, `"lint"`, or `None` when the line is neither.
    """
    if any(token in command for token in _CI_TEST_TOKENS):
        return "test"
    if any(token in command for token in _CI_LINT_TOKENS):
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
        tool = pyproject.get("tool", {})
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
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    return scripts if isinstance(scripts, dict) else {}


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
    project = pyproject.get("project", {})
    deps.extend(d for d in project.get("dependencies", []) if isinstance(d, str))
    for group in project.get("optional-dependencies", {}).values():
        deps.extend(d for d in group if isinstance(d, str))
    for group in pyproject.get("dependency-groups", {}).values():
        deps.extend(d for d in group if isinstance(d, str))
    return deps


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
