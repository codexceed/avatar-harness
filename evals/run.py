"""The Eval-0 runner: provision -> run the harness (strict) -> score -> result row.

``run_task`` is the tested unit (offline with an injected `ScriptedModel`); ``main`` is the
matrix driver behind ``make eval`` (live, multi-model). Both score with the harness's own
deterministic verifier plus the task's success probe.
"""

import argparse
import asyncio
import json
import re
import shlex
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from avatar.config import HarnessConfig
from avatar.harness import Harness
from avatar.journal import JsonlEventJournal
from avatar.model_client import ModelClient
from evals.classify import classify, failure_histogram
from evals.journal_read import row_events
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


def _fixture_path(name: str, evals_root: Path = _EVALS_ROOT) -> Path | None:
    """Resolve a fixture name to its directory, or `None` for the bare 'empty' fixture.

    Args:
        name: The fixture name from the spec.
        evals_root: The ``evals`` directory to resolve fixtures under; defaults to this repo's, but
            `validate` passes a frozen copy restored from a trusted ref (ADR-0024).

    Returns:
        The fixture directory, or `None` when it is 'empty' / absent (a bare repo).
    """
    if name == "empty":
        return None
    candidate = evals_root / "fixtures" / name
    return candidate if candidate.exists() else None


def _resolve_probe(command: str, evals_root: Path = _EVALS_ROOT) -> str:
    """Make a repo-relative ``evals/...`` probe-script path absolute against the assets root.

    The probe runs with the scratch repo as cwd (so it inspects the agent's output), but the
    probe script itself lives under ``evals/`` — so its path must be absolute.

    Args:
        command: The probe command from the spec.
        evals_root: The ``evals`` directory the probe scripts live under; defaults to this repo's,
            but `validate` passes a frozen copy so a candidate can't grade against an edited probe.

    Returns:
        The command with any leading ``evals/...`` path made absolute (against ``evals_root``'s
        parent, so ``evals/probes/x.py`` resolves to ``<evals_root>/probes/x.py``).
    """
    base = evals_root.parent
    parts = shlex.split(command)
    return " ".join(str(base / p) if p.startswith("evals/") else p for p in parts)


def _slug(model: str) -> str:
    """A filesystem-safe label fragment for a model id.

    Args:
        model: The model id (may contain ``/``, ``:``, etc.).

    Returns:
        The id with any run of non ``[A-Za-z0-9._-]`` characters collapsed to ``-``.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model)


def _journal_events(row: ResultRow) -> list[dict]:
    """Materialize a row's journal events from its scratch repo, if still present.

    Reads through the shared streaming reader (`journal_read.row_events`), then materializes to
    a list because the classifier walks the events more than once. The distiller, which makes a
    single pass, consumes `row_events` directly without materializing.

    Args:
        row: The result row (its ``workspace`` points at the scratch repo).

    Returns:
        The parsed journal events, or ``[]`` when the journal is gone (e.g. cleaned up).
    """
    return list(row_events(row))


def run_task(
    spec: TaskSpec,
    *,
    config: HarnessConfig,
    model_client: ModelClient | None = None,
    seed: int = 0,
    workspace_root: Path | None = None,
    evals_root: Path | None = None,
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
        evals_root: The ``evals`` directory to resolve fixtures + probe scripts under; `None` uses
            this repo's. `validate` passes a frozen copy restored from a trusted ref so the candidate
            harness is graded against an untouched grading surface (ADR-0024 / ADR-0011 D1+D2).

    Returns:
        The scored `ResultRow` (its `workspace` field points at the scratch repo).
    """
    root = evals_root or _EVALS_ROOT
    label = f"{_slug(config.model)}__{spec.id}__seed{seed}__"
    repo = provision(_fixture_path(spec.fixture, root), parent=workspace_root, label=label)
    # Errors after provisioning still produce a row that carries the scratch path, so it maps to
    # its files and the cleanup contract holds (provision-stage failures propagate to the caller).
    try:
        # Point `log_path` at the eval journal so the Workspace hides it from the agent's file
        # tools (ADR-0018 / ADR-0023): otherwise `search_repo` recurses over `journal.jsonl` and
        # balloons it (the 875 MB blowup, 2026-06-15). The journal is still written via the
        # explicit `JsonlEventJournal` below; `log_path` only drives the search/list exclusion.
        cfg = config.model_copy(
            update={"workspace_root": str(repo), "log_path": str(repo / "journal.jsonl"), **spec.budgets}
        )
        client = Harness(config=cfg, model=model_client) if model_client is not None else Harness(config=cfg)
        # Option A: a probe-bearing task is graded by the probe, so the agent runs *non-strict* —
        # it delivers its best and we grade it, instead of thrashing toward an edit gate a fresh
        # creation can't satisfy. A no-probe task stays strict (the verifier is the grader).
        conversational = spec.success_probe is not None
        session = client.session(
            spec.goal,
            task_kind=spec.task_kind,
            conversational=conversational,
            journal=JsonlEventJournal(repo / "journal.jsonl"),
            unattended=True,  # batch: auto-deny tier-3/denylist asks (no human to resolve them)
        )
        state = asyncio.run(session.run())
        # `outcome == "success"` is the verifier's verdict only for a no-probe (strict) task; in
        # conversational mode it just means the agent reached `final_answer`. `is_solved` uses it
        # when there is no probe, AND as the positive signal a *guard* probe is ANDed with (ADR-0020)
        # — so a no-leak guard plus a give-up `incomplete` run does not score solved.
        reached_success = state.outcome == "success"
        probe_exit = (
            run_probe(_resolve_probe(spec.success_probe, root), repo, env=spec.env)
            if spec.success_probe
            else None
        )
        row = ResultRow(
            task=spec.id,
            model=cfg.model,
            seed=seed,
            solved=is_solved(
                reached_success,
                probe_exit,
                probe_is_guard=spec.probe_role == "guard",
                outcome=state.outcome,
                passing_outcomes=spec.passing_outcomes,
            ),
            outcome=state.outcome,
            iterations=state.iterations,
            prompt_tokens=state.prompt_tokens,
            completion_tokens=state.completion_tokens,
            probe_exit=probe_exit,
            probe_role=spec.probe_role,
            workspace=str(repo),
        )
    except Exception as exc:  # one bad run must not lose the matrix; keep the scratch path on the row
        row = ResultRow(
            task=spec.id,
            model=config.model,
            seed=seed,
            solved=False,
            outcome=f"error: {type(exc).__name__}: {exc}"[:200],
            iterations=0,
            workspace=str(repo),
        )
    # Classify and persist the bucket NOW, while the scratch journal still exists (it is deleted by
    # the caller's cleanup), so the `loop_oscillation` / `decision_error` refinements are captured
    # and every downstream consumer reads one consistent value off the row (ADR-0025). A solved row
    # short-circuits to "solved" before `classify` ever inspects events, so skip the journal read
    # for it — otherwise every green run on a clean matrix materializes a (potentially huge) journal
    # only to discard it.
    events = None if row.solved else _journal_events(row)
    row.failure_mode = classify(row, events)
    return row


def _load_specs() -> list[TaskSpec]:
    """Load every task spec under ``evals/tasks/``, sorted by filename.

    Returns:
        The loaded specs.
    """
    return [load_task_spec(p) for p in sorted((_EVALS_ROOT / "tasks").glob("*.toml"))]


def _select_specs(specs: list[TaskSpec], tasks: str | None) -> list[TaskSpec]:
    """Narrow the suite to the comma-separated task ids in ``tasks`` (`None` = all).

    Selection keeps suite (filename) order regardless of argument order, so a filtered
    results artifact stays deterministically ordered like the full matrix.

    Args:
        specs: The full loaded suite.
        tasks: Comma-separated task ids, or `None` to select everything.

    Returns:
        The selected specs, in suite order.

    Raises:
        ValueError: When an id names no spec — a typo must fail loud, never silently
            shrink (or empty) an expensive run.
    """
    if tasks is None:
        return specs
    wanted = {t.strip() for t in tasks.split(",") if t.strip()}
    known = {s.id for s in specs}
    unknown = sorted(wanted - known)
    if unknown:
        raise ValueError(f"unknown task(s): {', '.join(unknown)} — available: {', '.join(sorted(known))}")
    return [s for s in specs if s.id in wanted]


def _run_one(model: str, cfg: HarnessConfig, spec: TaskSpec, seed: int, run_workspace: Path) -> ResultRow:
    """Run and score one ``(model, spec, seed)`` cell, catching provision-stage failures.

    `run_task` already turns *run* errors into an error row that carries the scratch path; this
    wrapper additionally catches a *provision*-stage failure (which propagates out of `run_task`)
    so one bad cell never sinks the matrix. It is the unit dispatched to a worker thread.

    Args:
        model: The model id (used to label an error row when provisioning fails).
        cfg: The per-model harness config (temperature + model already applied).
        spec: The task spec for this cell.
        seed: The seed index for this cell.
        run_workspace: The run workspace to provision the scratch repo under.

    Returns:
        The scored `ResultRow`, or an error row if provisioning raised.
    """
    try:
        return run_task(spec, config=cfg, seed=seed, workspace_root=run_workspace)
    except Exception as exc:  # provision-stage failure (run_task handles run errors itself)
        return ResultRow(
            task=spec.id,
            model=model,
            seed=seed,
            solved=False,
            outcome=f"error: {type(exc).__name__}: {exc}"[:200],
            iterations=0,
        )


def _run_matrix(
    models: list[str],
    base: HarnessConfig,
    specs: list[TaskSpec],
    *,
    seeds: int,
    run_workspace: Path,
    concurrency: int,
) -> list[ResultRow]:
    """Run the full ``model * spec * seed`` matrix under a bounded thread pool.

    The cells are hermetic — `run_task` provisions a uniquely-labelled scratch repo per cell and
    drives its own `asyncio.run`, so a worker thread runs an entire trajectory in its own event
    loop with no shared mutable state. Concurrency is I/O-bound (model API + subprocess probes),
    so the GIL is released where it matters. Rows are reassembled in matrix order (model-major,
    then spec, then seed) regardless of completion order, so the results artifact is deterministic;
    only the live progress lines arrive as cells finish. ``concurrency=1`` reproduces the old
    strictly-sequential behaviour exactly.

    Args:
        models: The model ids, in matrix order.
        base: The base harness config (temperature already applied); per-model config is derived.
        specs: The task specs, in matrix order.
        seeds: The number of seeds per ``(model, spec)`` pair.
        run_workspace: The run workspace to provision scratch repos under.
        concurrency: The max number of cells run in parallel (clamped to ``>= 1``).

    Returns:
        The scored rows in matrix order.
    """
    cfg_by_model = {model: base.model_copy(update={"model": model}) for model in models}
    cells = [
        (model, cfg_by_model[model], spec, seed)
        for model in models
        for spec in specs
        for seed in range(seeds)
    ]
    rows: list[ResultRow | None] = [None] * len(cells)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(_run_one, model, cfg, spec, seed, run_workspace): i
            for i, (model, cfg, spec, seed) in enumerate(cells)
        }
        for future in as_completed(futures):
            index = futures[future]
            row = future.result()
            rows[index] = row
            print(f"{row.model}  {row.task}  seed={row.seed}  -> {'PASS' if row.solved else row.outcome}")
    return [row for row in rows if row is not None]


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


def _write_results(rows: list[ResultRow], stamp: str | None = None) -> Path:
    """Write rows to a timestamped JSONL file under ``evals/results/``.

    Args:
        rows: The result rows to persist.
        stamp: The timestamp for the filename; `None` generates a fresh one (so the JSONL and
            its sibling ``<stamp>.summary.json`` share one stamp when the caller passes it).

    Returns:
        The path written.
    """
    results = _EVALS_ROOT / "results"
    results.mkdir(exist_ok=True)
    stamp = stamp or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    path = results / f"{stamp}.jsonl"
    write_results(rows, path)
    return path


def build_summary(
    rows: list[ResultRow],
    *,
    models: list[str],
    seeds: int,
    temperature: float,
    stamp: str,
) -> dict:
    """Build the aggregate-metrics summary the runner persists alongside the per-run JSONL.

    Reuses the same metric/classifier functions ``main()`` prints, so the artifact and the
    stdout summary can never drift. The failure histogram reads each row's persisted
    ``failure_mode`` (set at scoring time, ADR-0025), so the refinements are already baked in —
    no journal resolver is threaded here. Floats are rounded to 4 decimals.

    Args:
        rows: The result rows from the run (each carrying its scoring-time ``failure_mode``).
        models: The model ids in matrix order (one ``per_model`` entry each).
        seeds: The seeds-per-task count for the run.
        temperature: The sampling temperature for the run.
        stamp: The shared timestamp (pairs the summary with ``<stamp>.jsonl``).

    Returns:
        A JSON-serializable summary dict.
    """
    per_model = []
    for model in models:
        mrows = [r for r in rows if r.model == model]
        per_model.append(
            {
                "model": model,
                "pass_at_1": round(pass_at_1(mrows), 4),
                "pass_caret_k": round(pass_caret_k(mrows), 4),
                "n": len(mrows),
            }
        )
    return {
        "stamp": stamp,
        "n": len(rows),
        "temperature": temperature,
        "seeds": seeds,
        "models": models,
        "overall_pass_at_1": round(pass_at_1(rows), 4),
        "per_model": per_model,
        "failure_histogram": failure_histogram(rows),
    }


def write_summary(summary: dict, path: Path) -> None:
    """Write the summary dict as a single JSON object to ``path``.

    Args:
        summary: The summary from `build_summary`.
        path: The destination ``<stamp>.summary.json`` file.
    """
    Path(path).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run the task suite across a model matrix, write results, print a summary.

    Args:
        argv: CLI args (``--models``, ``--tasks``, ``--seeds``, ``--workspace``,
            ``--no-cleanup``); `None` uses ``sys.argv``.

    Returns:
        Process exit code (0 on success, 1 when no specs are found).
    """
    parser = argparse.ArgumentParser(prog="evals", description="Run the Eval-0 task suite.")
    parser.add_argument("--models", default=None, help="comma-separated model ids; default = config model")
    parser.add_argument(
        "--tasks",
        default=None,
        help="comma-separated task ids to run (default: every spec under evals/tasks/); "
        "an unknown id is an error, never a silent skip",
    )
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
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="max cells run in parallel across the model x spec x seed matrix; default 1 (sequential)",
    )
    args = parser.parse_args(argv)

    base = HarnessConfig().model_copy(update={"temperature": args.temperature})
    models = [m.strip() for m in args.models.split(",")] if args.models else [base.model]
    try:
        specs = _select_specs(_load_specs(), args.tasks)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not specs:
        print("no task specs found under evals/tasks/")
        return 1

    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    run_workspace, preexisting = _resolve_run_workspace(args.workspace, stamp)

    rows = _run_matrix(
        models,
        base,
        specs,
        seeds=args.seeds,
        run_workspace=run_workspace,
        concurrency=args.concurrency,
    )

    out = _write_results(rows, stamp)
    print(f"\nwrote {len(rows)} rows -> {out}")
    # Per-model: a global pass^k conflates models (all rows of one task collapse into one
    # group), so report each model on its own — pass@1 (capability) and pass^k (reliability).
    for model in models:
        mrows = [r for r in rows if r.model == model]
        print(f"  {model}: pass@1={pass_at_1(mrows):.2f}  pass^k={pass_caret_k(mrows):.2f}  (n={len(mrows)})")
    print(f"overall pass@1={pass_at_1(rows):.2f}  (n={len(rows)})")
    # Each row already carries its journal-refined `failure_mode` (persisted at scoring time, while
    # the journal was live), so loop_oscillation / decision_error are baked in — the histogram just
    # tallies the stored buckets, no journal re-read and no ordering dependency on cleanup (ADR-0025).
    hist = failure_histogram(rows)
    if hist:
        print("failure modes: " + ", ".join(f"{k}={v}" for k, v in sorted(hist.items())))

    # Persist the aggregate metrics + histogram as a sibling artifact, sharing the results stamp.
    summary = build_summary(
        rows,
        models=models,
        seeds=args.seeds,
        temperature=args.temperature,
        stamp=stamp,
    )
    summary_path = out.with_name(f"{stamp}.summary.json")
    write_summary(summary, summary_path)
    print(f"wrote summary -> {summary_path}")

    if args.cleanup:
        _cleanup_workspaces(rows, run_workspace, preexisting=preexisting)
        print("cleaned up scratch workspaces (--no-cleanup to keep)")
    else:
        print(f"run workspace kept: {run_workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
