"""Scoring — deterministic, no model: verifier pass AND success-probe exit 0.

Two independent signals, both required. The probe is authored per task and run *outside*
the agent loop, so it also catches a verifier that passed for the wrong reason (the
`probe_failed` leakage proxy, ADR-0011).
"""

import shlex
import subprocess
from pathlib import Path

_PROBE_TIMEOUT_SECONDS = 120
_EXIT_NOT_FOUND = 127
_EXIT_TIMEOUT = 124


def is_solved(verifier_passed: bool, probe_exit: int | None) -> bool:
    """Whether a run counts as solved.

    Args:
        verifier_passed: Whether the harness verifier passed (positive external signal).
        probe_exit: The success-probe exit code, or `None` when no probe was declared
            (the verifier alone then decides).

    Returns:
        `True` iff the verifier passed and the probe (if any) exited 0.
    """
    return verifier_passed and (probe_exit is None or probe_exit == 0)


def run_probe(command: str, cwd: Path) -> int:
    """Run a success probe in `cwd`, returning its exit code (never raises).

    Args:
        command: The probe command (argv form, no shell metacharacters).
        cwd: The directory to run it in (the scratch repo).

    Returns:
        The probe's exit code; 127 for an empty/missing program, 124 on timeout.
    """
    argv = shlex.split(command)
    if not argv:
        return _EXIT_NOT_FOUND
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
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
