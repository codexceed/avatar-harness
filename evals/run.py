"""The Eval-0 runner: provision -> run the harness (strict) -> score -> result row.

``run_task`` is the tested unit (offline with an injected `ScriptedModel`); ``main`` is the
matrix driver behind ``make eval`` (live, multi-model). Both score with the harness's own
deterministic verifier plus the task's success probe.
"""

import argparse
import asyncio
import shlex
import shutil
from datetime import UTC, datetime
from pathlib import Path

from avatar_harness.config import HarnessConfig
from avatar_harness.harness import Harness
from avatar_harness.journal import JsonlEventJournal
from avatar_harness.model_client import ModelClient
from evals.classify import failure_histogram
from evals.metrics import pass_at_1, pass_caret_k
from evals.provision import provision
from evals.result import ResultRow, write_results
from evals.score import is_solved, run_probe
from evals.spec import TaskSpec, load_task_spec

_EVALS_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _EVALS_ROOT.parent
_DEFAULT_SEEDS = 3
# Eval samples by default (>0) so each seed is an independent draw — pass^k/CIs then measure
# behavioral reliability, not just provider noise. Pass --temperature 0 for a deterministic run.
_DEFAULT_TEMPERATURE = 0.7


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
    workspace_root: Path | None = None,
) -> ResultRow:
    """Run one task hermetically and score it.

    Args:
        spec: The task spec.
        config: The base harness config; workspace root and budgets are overridden per task.
        model_client: A model client to inject (tests pass a `ScriptedModel`); `None`
            builds the default client from `config`.
        seed: The seed index (recorded on the row; varies the matrix, not the engine).
        workspace_root: The run workspace to provision the scratch repo under; `None` uses
            the system temp dir.

    Returns:
        The scored `ResultRow` (its `workspace` field points at the scratch repo).
    """
    label = f"{config.model.replace('/', '-')}__{spec.id}__seed{seed}__"
    repo = provision(_fixture_path(spec.fixture), parent=workspace_root, label=label)
    cfg = config.model_copy(update={"workspace_root": str(repo), **spec.budgets})
    harness = Harness(config=cfg, model=model_client) if model_client is not None else Harness(config=cfg)
    # Option A: a probe-bearing task is graded by the probe, so the agent runs *non-strict* —
    # it delivers its best and we grade it, instead of thrashing toward an edit gate a fresh
    # creation can't satisfy. A no-probe task stays strict (the verifier is the grader).
    conversational = spec.success_probe is not None
    session = harness.session(
        spec.goal,
        task_kind=spec.task_kind,
        conversational=conversational,
        journal=JsonlEventJournal(repo / "journal.jsonl"),
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
        workspace=str(repo),
    )


def _load_specs() -> list[TaskSpec]:
    """Load every task spec under ``evals/tasks/``, sorted by filename.

    Returns:
        The loaded specs.
    """
    return [load_task_spec(p) for p in sorted((_EVALS_ROOT / "tasks").glob("*.toml"))]


def _resolve_run_workspace(workspace: str | None, stamp: str) -> tuple[Path, bool]:
    """Resolve the run workspace: an explicit path, else an auto ``eval_run_<stamp>`` in cwd.

    Args:
        workspace: An explicit workspace path, or `None` to auto-generate one in the cwd.
        stamp: The timestamp used in the auto-generated name.

    Returns:
        ``(path, preexisting)`` — the workspace dir (created if needed) and whether it already
        existed (so cleanup never deletes a directory the runner did not create).
    """
    if workspace is not None:
        path = Path(workspace)
        preexisting = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        return path, preexisting
    path = Path.cwd() / f"eval_run_{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path, False


def _cleanup_workspaces(rows: list[ResultRow], run_workspace: Path, *, preexisting: bool) -> None:
    """Remove what the runner created — never a pre-existing user directory.

    For an auto-generated (or runner-created) workspace, the whole run dir goes. For a
    user-supplied existing directory, only the per-run scratch repos are removed; the dir and
    any pre-existing content are left untouched.

    Args:
        rows: The result rows (their `workspace` paths are the scratch repos to remove).
        run_workspace: The run workspace directory.
        preexisting: Whether `run_workspace` existed before this run.
    """
    if preexisting:
        for row in rows:
            if row.workspace:
                shutil.rmtree(row.workspace, ignore_errors=True)
    else:
        shutil.rmtree(run_workspace, ignore_errors=True)


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
    write_results(rows, path)
    return path


def main(argv: list[str] | None = None) -> int:
    """Run the task suite across a model matrix, write results, print a summary.

    Args:
        argv: CLI args (``--models``, ``--seeds``, ``--workspace``, ``--no-cleanup``); `None`
            uses ``sys.argv``.

    Returns:
        Process exit code (0 on success, 1 when no specs are found).
    """
    parser = argparse.ArgumentParser(prog="evals", description="Run the Eval-0 task suite.")
    parser.add_argument("--models", default=None, help="comma-separated model ids; default = config model")
    parser.add_argument("--seeds", type=int, default=_DEFAULT_SEEDS, help="seeds per task")
    parser.add_argument(
        "--temperature",
        type=float,
        default=_DEFAULT_TEMPERATURE,
        help="sampling temperature; >0 makes each seed an independent sample (needed for pass^k)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="run workspace dir for scratch repos; default = ./eval_run_<timestamp>",
    )
    parser.add_argument(
        "--no-cleanup",
        dest="cleanup",
        action="store_false",
        help="keep the run workspace (scratch repos) for inspection; default removes it",
    )
    args = parser.parse_args(argv)

    base = HarnessConfig().model_copy(update={"temperature": args.temperature})
    models = [m.strip() for m in args.models.split(",")] if args.models else [base.model]
    specs = _load_specs()
    if not specs:
        print("no task specs found under evals/tasks/")
        return 1

    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    run_workspace, preexisting = _resolve_run_workspace(args.workspace, stamp)

    rows: list[ResultRow] = []
    for model in models:
        cfg = base.model_copy(update={"model": model})
        for spec in specs:
            for seed in range(args.seeds):
                try:
                    row = run_task(spec, config=cfg, seed=seed, workspace_root=run_workspace)
                except Exception as exc:  # one bad model/run must not lose the whole matrix
                    row = ResultRow(
                        task=spec.id,
                        model=model,
                        seed=seed,
                        solved=False,
                        outcome=f"error: {type(exc).__name__}: {exc}"[:200],
                        iterations=0,
                    )
                rows.append(row)
                print(f"{model}  {spec.id}  seed={seed}  -> {'PASS' if row.solved else row.outcome}")

    out = _write_results(rows)
    print(f"\nwrote {len(rows)} rows -> {out}")
    # Per-model: a global pass^k conflates models (all rows of one task collapse into one
    # group), so report each model on its own — pass@1 (capability) and pass^k (reliability).
    for model in models:
        mrows = [r for r in rows if r.model == model]
        print(f"  {model}: pass@1={pass_at_1(mrows):.2f}  pass^k={pass_caret_k(mrows):.2f}  (n={len(mrows)})")
    print(f"overall pass@1={pass_at_1(rows):.2f}  (n={len(rows)})")
    hist = failure_histogram(rows)
    if hist:
        print("failure modes: " + ", ".join(f"{k}={v}" for k, v in sorted(hist.items())))

    if args.cleanup:
        _cleanup_workspaces(rows, run_workspace, preexisting=preexisting)
        print("cleaned up scratch workspaces (--no-cleanup to keep)")
    else:
        print(f"run workspace kept: {run_workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
