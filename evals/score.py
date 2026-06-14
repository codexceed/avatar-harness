"""Scoring — deterministic, no model: verifier pass AND success-probe exit 0.

Two independent signals, both required. The probe is authored per task and run *outside*
the agent loop, so it also catches a verifier that passed for the wrong reason (the
`probe_failed` leakage proxy, ADR-0011).
"""

import os
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

_PROBE_TIMEOUT_SECONDS = 120
_EXIT_NOT_FOUND = 127
_EXIT_TIMEOUT = 124


def is_solved(verifier_passed: bool, probe_exit: int | None) -> bool:
    """Whether a run counts as solved (option A: the probe is authoritative when present).

    A task-authored success probe IS the success criterion when declared — the agent runs
    blind (non-strict) and we grade the result. The harness verifier's verdict is not required
    (a fresh creation can't satisfy the edit gate's positive-signal rule, so demanding it would
    veto a working solution). When no probe is declared, the verifier decides (e.g. investigate's
    grounded-answer gate).

    Args:
        verifier_passed: Whether the harness verifier passed; used only when there is no probe.
        probe_exit: The success-probe exit code, or `None` when no probe was declared.

    Returns:
        `probe_exit == 0` when a probe ran, else `verifier_passed`.
    """
    if probe_exit is not None:
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
