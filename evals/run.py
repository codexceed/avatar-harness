"""The Eval-0 runner: provision -> run the harness (strict) -> score -> result row.

``run_task`` is the tested unit (offline with an injected `ScriptedModel`); ``main`` is the
matrix driver behind ``make eval`` (live, multi-model). Both score with the harness's own
deterministic verifier plus the task's success probe.
"""

import argparse
import asyncio
import shlex
from datetime import UTC, datetime
from pathlib import Path

from avatar_harness.config import HarnessConfig
from avatar_harness.harness import Harness
from avatar_harness.journal import JsonlEventJournal
from avatar_harness.model_client import ModelClient
from evals.metrics import pass_at_1, pass_caret_k
from evals.provision import provision
from evals.result import ResultRow
from evals.score import is_solved, run_probe
from evals.spec import TaskSpec, load_task_spec

_EVALS_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _EVALS_ROOT.parent
_DEFAULT_SEEDS = 3


def _fixture_path(name: str) -> Path | None:
    """Resolve a fixture name to its directory, or `None` for the bare 'empty' fixture.

    Args:
        name: The fixture name from the spec.

    Returns:
        The fixture directory, or `None` when it is 'empty' / absent (a bare repo).
    """
    if name == "empty":
        return None
    candidate = _EVALS_ROOT / "fixtures" / name
    return candidate if candidate.exists() else None


def _resolve_probe(command: str) -> str:
    """Make a repo-relative ``evals/...`` probe-script path absolute against the repo root.

    The probe runs with the scratch repo as cwd (so it inspects the agent's output), but the
    probe script itself lives in this repo — so its path must be absolute.

    Args:
        command: The probe command from the spec.

    Returns:
        The command with any leading ``evals/...`` path made absolute.
    """
    parts = shlex.split(command)
    return " ".join(str(_REPO_ROOT / p) if p.startswith("evals/") else p for p in parts)


def run_task(
    spec: TaskSpec,
    *,
    config: HarnessConfig,
    model_client: ModelClient | None = None,
    seed: int = 0,
) -> ResultRow:
    """Run one task hermetically and score it.

    Args:
        spec: The task spec.
        config: The base harness config; workspace root and budgets are overridden per task.
        model_client: A model client to inject (tests pass a `ScriptedModel`); `None`
            builds the default client from `config`.
        seed: The seed index (recorded on the row; varies the matrix, not the engine).

    Returns:
        The scored `ResultRow`.
    """
    repo = provision(_fixture_path(spec.fixture))
    cfg = config.model_copy(update={"workspace_root": str(repo), **spec.budgets})
    harness = Harness(config=cfg, model=model_client) if model_client is not None else Harness(config=cfg)
    session = harness.session(
        spec.goal, task_kind=spec.task_kind, journal=JsonlEventJournal(repo / "journal.jsonl")
    )
    state = asyncio.run(session.run())

    verifier_passed = state.outcome == "success"
    probe_exit = (
        run_probe(_resolve_probe(spec.success_probe), repo, env=spec.env) if spec.success_probe else None
    )
    return ResultRow(
        task=spec.id,
        model=cfg.model,
        seed=seed,
        solved=is_solved(verifier_passed, probe_exit),
        outcome=state.outcome,
        iterations=state.iterations,
        prompt_tokens=state.prompt_tokens,
        completion_tokens=state.completion_tokens,
        probe_exit=probe_exit,
    )


def _load_specs() -> list[TaskSpec]:
    """Load every task spec under ``evals/tasks/``, sorted by filename.

    Returns:
        The loaded specs.
    """
    return [load_task_spec(p) for p in sorted((_EVALS_ROOT / "tasks").glob("*.toml"))]


def _write_results(rows: list[ResultRow]) -> Path:
    """Write rows to a timestamped JSONL file under ``evals/results/``.

    Args:
        rows: The result rows to persist.

    Returns:
        The path written.
    """
    results = _EVALS_ROOT / "results"
    results.mkdir(exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    path = results / f"{stamp}.jsonl"
    path.write_text("".join(r.to_jsonl() + "\n" for r in rows), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    """Run the task suite across a model matrix, write results, print a summary.

    Args:
        argv: CLI args (``--models`` comma-separated, ``--seeds`` N); `None` uses ``sys.argv``.

    Returns:
        Process exit code (0 on success, 1 when no specs are found).
    """
    parser = argparse.ArgumentParser(prog="evals", description="Run the Eval-0 task suite.")
    parser.add_argument("--models", default=None, help="comma-separated model ids; default = config model")
    parser.add_argument("--seeds", type=int, default=_DEFAULT_SEEDS, help="seeds per task")
    args = parser.parse_args(argv)

    base = HarnessConfig()
    models = [m.strip() for m in args.models.split(",")] if args.models else [base.model]
    specs = _load_specs()
    if not specs:
        print("no task specs found under evals/tasks/")
        return 1

    rows: list[ResultRow] = []
    for model in models:
        cfg = base.model_copy(update={"model": model})
        for spec in specs:
            for seed in range(args.seeds):
                row = run_task(spec, config=cfg, seed=seed)
                rows.append(row)
                print(f"{model}  {spec.id}  seed={seed}  -> {'PASS' if row.solved else row.outcome}")

    out = _write_results(rows)
    print(f"\nwrote {len(rows)} rows -> {out}")
    print(f"pass@1={pass_at_1(rows):.2f}  pass^k={pass_caret_k(rows):.2f}  (n={len(rows)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
