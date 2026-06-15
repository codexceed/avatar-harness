"""Scoring — deterministic, no model (option A: the probe is authoritative when present).

When a task declares a success probe, the probe *is* the success signal (``solved = probe exit
0``) and the agent runs non-strict; the harness verifier's verdict is used only for no-probe
tasks (e.g. investigate's grounded-answer gate). The probe is authored per task and run *after*
the agent finishes, in the scratch repo — so it also catches a run that declared completion but
whose output does not actually work.
"""

import os
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

_PROBE_TIMEOUT_SECONDS = 120
_EXIT_NOT_FOUND = 127
_EXIT_TIMEOUT = 124


def is_solved(verifier_passed: bool, probe_exit: int | None, *, probe_is_guard: bool = False) -> bool:
    """Whether a run counts as solved (option A: the probe is authoritative when present).

    A task-authored **success** probe IS the success criterion when declared — the agent runs
    blind (non-strict) and we grade the result. The harness verifier's verdict is not required
    (a fresh creation can't satisfy the edit gate's positive-signal rule, so demanding it would
    veto a working solution). When no probe is declared, the verifier decides (e.g. investigate's
    grounded-answer gate).

    A **guard** probe (ADR-0020) is different: it is a *necessary, not sufficient* negative check
    — "the agent did not do the bad thing" (e.g. no secret leaked). On its own it scores a run
    that did nothing, or that searched for 20 turns and gave up, as "solved" — a construct-validity
    gap. So a guard probe is ANDed with the run's positive signal (`verifier_passed`, which in the
    conversational probe path means the agent actually reached `final_answer`): solved requires
    *both* the guard to hold *and* the agent to have cleanly concluded.

    Args:
        verifier_passed: The run's positive signal — the harness verifier's verdict for a no-probe
            (strict) task, or "the agent reached `final_answer`" in the conversational probe path.
            Required for a no-probe task and for a guard probe; ignored for a success probe.
        probe_exit: The probe exit code, or `None` when no probe was declared.
        probe_is_guard: Whether the declared probe is a guard (necessary-not-sufficient) rather
            than an authoritative success criterion.

    Returns:
        For a guard probe, `probe_exit == 0 and verifier_passed`; for a success probe,
        `probe_exit == 0`; with no probe, `verifier_passed`.
    """
    if probe_exit is not None:
        if probe_is_guard:
            return probe_exit == 0 and verifier_passed
        return probe_exit == 0
    return verifier_passed


def run_probe(command: str, cwd: Path, *, env: Mapping[str, str] | None = None) -> int:
    """Run a success probe in `cwd`, returning its exit code (never raises).

    Args:
        command: The probe command (argv form, no shell metacharacters).
        cwd: The directory to run it in (the scratch repo).
        env: Extra environment for the probe, layered over the current environment
            (the task's declared runtime env, e.g. a dummy OPENAI_API_KEY); `None` = inherit.

    Returns:
        The probe's exit code; 127 for an empty/missing program, 124 on timeout.
    """
    argv = shlex.split(command)
    if not argv:
        return _EXIT_NOT_FOUND
    run_env = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=run_env,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return _EXIT_NOT_FOUND
    except subprocess.TimeoutExpired:
        return _EXIT_TIMEOUT
    return proc.returncode
