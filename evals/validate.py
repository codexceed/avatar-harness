"""`validate` — Workflow B's canary ladder over FROZEN grading assets (ADR-0024, Increment 3).

The **only eval-spender** in the improvement loop. Given a candidate harness (the agent's worktree)
and a pinned baseline run, it climbs a cost-staged ladder and stops at the first rung that settles
the question:

  1. **local**  — the candidate's own unit/TDD tests + `make check`. Cheapest; a failure ends the
     ladder with ZERO eval spend.
  2. **canary** — a 1-seed re-run on the *affected* models x *target* tasks. A cheap raw screen: it
     must show the target newly passing with no raw regression, or the ladder stops before the
     expensive matrix.
  3. **matrix** — the full model x task x seed re-run, judged GLOBALLY by paired McNemar plus a
     per-model **agnosticism** check (a change must not lift the matrix overall while regressing a
     single model). This is why we validate globally, never per-failed-task.

Every eval rung runs against the grading surface (`evals/tasks` · `evals/probes` · `evals/fixtures`)
restored from a **trusted git ref**, never the agent's worktree — a pragmatic ADR-0011 D1+D2 that
stops a candidate from grading itself against a spec/probe it just edited. Necessary, not sufficient
(see `evals/CLAUDE.md` §6).

`run_ladder` is the tested unit (offline, with injected stage runners); `main` is the live driver
the Workflow B script shells out to. Verdicts reuse `evals.stats` (McNemar + clustered CI), the same
machinery `evals.diff` reports with — so a validated PR and a manual `python -m evals.diff` agree.
"""

import argparse
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from avatar.config import HarnessConfig
from evals.result import ResultRow, load_results
from evals.run import run_task
from evals.spec import load_task_spec
from evals.stats import McNemarResult, mcnemar, mean_ci

LadderStage = Literal["local", "canary", "matrix"]

_ALPHA = 0.05
# The grading surface frozen per rung — specs, success/guard probes, and fixtures. `src/` (the
# harness under test) is deliberately NOT frozen: the candidate's code IS what we are validating.
_GRADING_SUBDIRS = ("tasks", "probes", "fixtures")

# A callable that runs a (models, tasks, seeds) sub-matrix and returns its rows. The live driver
# binds this to the eval runner pointed at the frozen assets; tests inject a scripted matrix.
RunEval = Callable[[Sequence[str], Sequence[str], int], list[ResultRow]]
RunLocal = Callable[[], tuple[bool, str]]


class StageOutcome(BaseModel):
    """The result of one ladder rung."""

    stage: LadderStage
    ran: bool
    passed: bool  # did this rung's gate clear (proceed / overall success)?
    detail: str
    improved: bool = False
    regressed: bool = False
    agnostic: bool = True


class ValidationVerdict(BaseModel):
    """The ladder's verdict — `passed` is the single gate Workflow B acts on."""

    passed: bool
    stage_reached: LadderStage
    stages: list[StageOutcome] = Field(default_factory=list)
    summary: str = ""


class ValidationScope(BaseModel):
    """The matrix shape the ladder validates over — the canary subset vs. the full matrix.

    `affected_models` x `target_tasks` (at 1 seed) is the cheap canary screen; `all_models` x
    `all_tasks` x `seeds` is the global matrix the McNemar + agnosticism verdict is computed over.
    """

    affected_models: list[str]
    target_tasks: list[str]
    all_models: list[str]
    all_tasks: list[str]
    seeds: int = 5


def _key(row: ResultRow) -> tuple[str, str, int]:
    """The pairing key for a row: ``(model, task, seed)`` (mirrors `evals.stats._key`).

    Args:
        row: The result row.

    Returns:
        The ``(model, task, seed)`` tuple.
    """
    return (row.model, row.task, row.seed)


def _raw_delta(baseline: Sequence[ResultRow], candidate: Sequence[ResultRow]) -> tuple[int, int]:
    """Raw paired flips between two runs — the cheap canary screen (no significance test).

    Args:
        baseline: The pinned baseline rows.
        candidate: The candidate rows (only keys present in both are compared).

    Returns:
        ``(newly_passing, newly_failing)`` — counts of base-fail→cand-pass and base-pass→cand-fail
        over the shared ``(model, task, seed)`` keys.
    """
    base = {_key(r): r.solved for r in baseline}
    newly_passing = newly_failing = 0
    for r in candidate:
        was = base.get(_key(r))
        if was is None:
            continue
        if r.solved and not was:
            newly_passing += 1
        elif not r.solved and was:
            newly_failing += 1
    return newly_passing, newly_failing


def _significant(mc: McNemarResult) -> Literal["improvement", "regression", "none"]:
    """Classify a McNemar result as a significant improvement, regression, or no change.

    Args:
        mc: The paired McNemar result.

    Returns:
        ``"improvement"`` / ``"regression"`` when ``p < alpha`` and the discordant counts differ,
        else ``"none"`` (sampling noise).
    """
    if mc.p_value < _ALPHA and mc.regressions != mc.improvements:
        return "regression" if mc.regressions > mc.improvements else "improvement"
    return "none"


def _matrix_gate(
    baseline: Sequence[ResultRow], candidate: Sequence[ResultRow], models: Sequence[str]
) -> StageOutcome:
    """Judge the full matrix globally: significant overall improvement + per-model agnosticism.

    A change passes only when the paired McNemar over the whole matrix is a significant improvement
    AND no single model shows a significant regression (the agnosticism check) — so a change that
    lifts the aggregate while breaking one model, or that merely trades one task for another, is
    rejected.

    Args:
        baseline: The pinned baseline rows.
        candidate: The candidate matrix rows.
        models: The model ids to check individually for agnosticism.

    Returns:
        The `StageOutcome` for the matrix rung.
    """
    overall = _significant(mcnemar(baseline, candidate))
    improved = overall == "improvement"
    overall_regressed = overall == "regression"
    regressed_models = [
        m
        for m in models
        if _significant(
            mcnemar([r for r in baseline if r.model == m], [r for r in candidate if r.model == m])
        )
        == "regression"
    ]
    agnostic = not regressed_models
    passed = improved and not overall_regressed and agnostic

    b, c = mean_ci(baseline), mean_ci(candidate)
    head = f"matrix: pass@1 {b.mean:.2f}->{c.mean:.2f}"
    if passed:
        detail = f"{head} — significant improvement, no per-model regression"
    elif overall_regressed:
        detail = f"{head} — significant overall regression"
    elif not improved:
        detail = f"{head} — no significant change (nothing to merge)"
    else:  # improved overall but a model regressed
        detail = (
            f"{head} — overall improved but {','.join(regressed_models)} regressed (agnosticism violation)"
        )
    return StageOutcome(
        stage="matrix",
        ran=True,
        passed=passed,
        detail=detail,
        improved=improved,
        regressed=overall_regressed or bool(regressed_models),
        agnostic=agnostic,
    )


def run_ladder(
    baseline: Sequence[ResultRow],
    scope: ValidationScope,
    *,
    run_local: RunLocal,
    run_eval: RunEval,
) -> ValidationVerdict:
    """Climb the cost-staged validation ladder, stopping at the first decisive rung.

    Args:
        baseline: The pinned baseline run's rows (the comparison anchor at every rung).
        scope: The canary subset vs. full-matrix shape to validate over.
        run_local: Runs the candidate's unit/TDD + `make check`; returns ``(ok, detail)``.
        run_eval: Runs a ``(models, tasks, seeds)`` sub-matrix against frozen assets -> rows.

    Returns:
        The `ValidationVerdict`; `passed` is the single gate Workflow B acts on, `stage_reached`
        records how far up the ladder spend went.
    """
    stages: list[StageOutcome] = []

    # Rung 1 — local: a failed unit/local check ends the ladder before any eval spend.
    ok, detail = run_local()
    stages.append(StageOutcome(stage="local", ran=True, passed=ok, detail=detail))
    if not ok:
        return ValidationVerdict(
            passed=False, stage_reached="local", stages=stages, summary=f"local checks failed: {detail}"
        )

    # Rung 2 — canary: a cheap 1-seed raw screen on the affected models x target tasks.
    canary = run_eval(scope.affected_models, scope.target_tasks, 1)
    newly_passing, newly_failing = _raw_delta(baseline, canary)
    survived = newly_passing > 0 and newly_failing == 0
    stages.append(
        StageOutcome(
            stage="canary",
            ran=True,
            passed=survived,
            detail=(
                f"canary: +{newly_passing} newly-passing, -{newly_failing} regressed "
                f"(1 seed x {len(scope.affected_models)} model(s) x {len(scope.target_tasks)} task(s))"
            ),
            improved=newly_passing > 0,
            regressed=newly_failing > 0,
        )
    )
    if not survived:
        why = "canary regressed a passing run" if newly_failing else "canary showed no improvement"
        return ValidationVerdict(
            passed=False,
            stage_reached="canary",
            stages=stages,
            summary=f"{why}; full matrix skipped (saved eval spend)",
        )

    # Rung 3 — matrix: the global verdict (paired McNemar + per-model agnosticism).
    matrix = run_eval(scope.all_models, scope.all_tasks, scope.seeds)
    outcome = _matrix_gate(baseline, matrix, scope.all_models)
    stages.append(outcome)
    return ValidationVerdict(
        passed=outcome.passed, stage_reached="matrix", stages=stages, summary=outcome.detail
    )


def frozen_assets(ref: str, repo_root: Path, dest: Path) -> Path:
    """Restore the grading surface (`evals/{tasks,probes,fixtures}`) from a trusted ref.

    The anti-Goodhart guard: validation grades against these restored assets, never the candidate's
    worktree — so a candidate that edited a spec or probe to make itself pass is still graded against
    the untouched original. `src/` is intentionally not frozen (it is what we are validating).

    Args:
        ref: A trusted git ref (e.g. ``"main"`` or a pinned SHA) to restore from.
        repo_root: The git repository to read the ref out of.
        dest: A directory to materialize the assets under (``<dest>/evals/...``).

    Returns:
        The restored ``<dest>/evals`` directory.
    """
    repo_root, dest = Path(repo_root), Path(dest)
    out = dest / "evals"
    out.mkdir(parents=True, exist_ok=True)
    present = [
        f"evals/{sub}"
        for sub in _GRADING_SUBDIRS
        if subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-e", f"{ref}:evals/{sub}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    ]
    if not present:
        return out
    # `git archive <ref> <paths>` emits a tar of the ref's tree (never the dirty worktree); extract
    # it under dest so paths land at <dest>/evals/<sub>/...
    archive = subprocess.run(
        ["git", "-C", str(repo_root), "archive", ref, *present], check=True, capture_output=True
    )
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    return out


def _make_run_local(worktree: Path) -> RunLocal:
    """Build the local rung: run `make check` in the candidate worktree.

    Args:
        worktree: The candidate harness checkout.

    Returns:
        A `RunLocal` that returns ``(ok, detail)`` from `make check`.
    """

    def run_local() -> tuple[bool, str]:
        proc = subprocess.run(
            ["make", "check"], cwd=str(worktree), capture_output=True, text=True, check=False
        )
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
        return proc.returncode == 0, f"make check rc={proc.returncode}: {tail[0]}"

    return run_local


def _make_run_eval(assets_root: Path, temperature: float) -> RunEval:
    """Build the live eval rung: run the candidate harness against frozen grading assets.

    The harness under test is the `avatar` package resolved in the running interpreter — i.e. the
    candidate worktree's, when Workflow B invokes `python -m evals.validate` from that worktree. Only
    the *grading surface* is pinned (via `assets_root`); the code being validated is the live import.

    Args:
        assets_root: The frozen ``evals`` directory from `frozen_assets`.
        temperature: Sampling temperature for the re-runs.

    Returns:
        A `RunEval` bound to the frozen assets.
    """

    def run_eval(models: Sequence[str], tasks: Sequence[str], seeds: int) -> list[ResultRow]:
        specs = [load_task_spec(assets_root / "tasks" / f"{t}.toml") for t in tasks]
        base = HarnessConfig().model_copy(update={"temperature": temperature})
        rows: list[ResultRow] = []
        with tempfile.TemporaryDirectory(prefix="validate_run_") as tmp:
            ws = Path(tmp)
            for model in models:
                cfg = base.model_copy(update={"model": model})
                for spec in specs:
                    for seed in range(seeds):
                        rows.append(
                            run_task(
                                spec,
                                config=cfg,
                                seed=seed,
                                workspace_root=ws,
                                evals_root=assets_root,
                            )
                        )
        return rows

    return run_eval


def main(argv: list[str] | None = None) -> int:
    """Validate a candidate worktree against a pinned baseline via the canary ladder.

    Args:
        argv: ``--baseline <results.jsonl> --worktree <dir> --trusted-ref <ref>
            --affected-models a,b --target-tasks t1,t2 --models a,b --tasks t1,t2 --seeds N
            --temperature F``; `None` uses ``sys.argv``.

    Returns:
        Process exit code — 0 when the candidate passes the ladder, 1 otherwise.
    """
    parser = argparse.ArgumentParser(prog="evals.validate", description="Canary-ladder a candidate harness.")
    parser.add_argument("--baseline", required=True, help="pinned baseline results JSONL")
    parser.add_argument("--worktree", default=".", help="candidate harness checkout (default: cwd)")
    parser.add_argument("--trusted-ref", default="HEAD", help="git ref to freeze grading assets from")
    parser.add_argument("--affected-models", required=True, help="comma-separated canary models")
    parser.add_argument("--target-tasks", required=True, help="comma-separated canary tasks")
    parser.add_argument("--models", required=True, help="comma-separated full-matrix models")
    parser.add_argument("--tasks", required=True, help="comma-separated full-matrix tasks")
    parser.add_argument("--seeds", type=int, default=5, help="seeds per cell for the full matrix")
    parser.add_argument("--temperature", type=float, default=0.7, help="sampling temperature")
    args = parser.parse_args(argv)

    def split(s: str) -> list[str]:
        return [x.strip() for x in s.split(",") if x.strip()]

    baseline = load_results(Path(args.baseline))
    worktree = Path(args.worktree).resolve()
    scope = ValidationScope(
        affected_models=split(args.affected_models),
        target_tasks=split(args.target_tasks),
        all_models=split(args.models),
        all_tasks=split(args.tasks),
        seeds=args.seeds,
    )
    with tempfile.TemporaryDirectory(prefix="frozen_assets_") as frozen:
        assets_root = frozen_assets(args.trusted_ref, worktree, Path(frozen))
        verdict = run_ladder(
            baseline,
            scope,
            run_local=_make_run_local(worktree),
            run_eval=_make_run_eval(assets_root, args.temperature),
        )

    for st in verdict.stages:
        print(f"  [{st.stage}] {'PASS' if st.passed else 'STOP'} — {st.detail}")
    print(f"\nverdict: {'PASS' if verdict.passed else 'FAIL'} (reached {verdict.stage_reached})")
    print(verdict.summary)
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
