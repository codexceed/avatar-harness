"""Verifier — proves completion via external evidence, never self-certification (§12).

It runs **no model**: every check is a predicate over structured `TaskState` plus
the workspace. For `edit`/`test_only` the *external signal* is a command the
verifier runs **itself** (`config.test_command` / `config.lint_command`) — not the
model's `run_tests` output. The gate is harness-owned, so the model can never
self-certify (§5). The three pass criteria (§12): no required check fails; no
required check is skipped for a disallowed reason; at least one positive signal.
"""

from avatar_harness.config import HarnessConfig
from avatar_harness.state import CheckResult, TaskState, VerifierResult
from avatar_harness.workspace import Workspace

# Skips the gate tolerates (§12 criterion 2): discovered absence, not evasion.
_ALLOWED_SKIPS = frozenset(
    {
        "no test target exists in this repo",
        "no lint command configured",
    }
)

# Likely secrets / placeholders that must never land in a diff (always-on guard, §12).
_SECRET_MARKERS = ("AKIA", "-----BEGIN", "PLACEHOLDER", "<placeholder>")

_NO_TESTS_EXIT = 5  # pytest convention: no tests were collected.


class Verifier:
    """Disposes of a completion proposal via external evidence, never a model (§12)."""

    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config

    def verify(self, state: TaskState, ws: Workspace) -> VerifierResult:
        """Run the verification contract for the task's `task_kind` (§12)."""
        if state.task_kind == "investigate":
            return self._verify_investigate(state, ws)
        if state.task_kind == "edit":
            return self._verify_edit(state, ws)
        if state.task_kind == "test_only":
            return self._verify_test_only(state, ws)
        raise NotImplementedError(f"no verifier for task_kind={state.task_kind!r}")

    # --- investigate (Phase 1) -------------------------------------------

    def _verify_investigate(self, state: TaskState, ws: Workspace) -> VerifierResult:
        answer = (state.final_answer or "").strip()
        cited = sorted(p for p in state.files_read if p in answer)
        inspected = bool(state.files_read or state.commands_run or state.evidence)

        checks = [
            CheckResult(
                name="answer_present",
                kind="required",
                status="pass" if answer else "fail",
                evidence=f"answer length={len(answer)}",
            ),
            CheckResult(
                name="grounded_in_evidence",
                kind="required",
                status="pass" if (inspected and cited) else "fail",
                evidence=f"files_read={len(state.files_read)}, cited_in_answer={cited}",
            ),
            CheckResult(
                name="no_unintended_diff",
                kind="required",
                status="pass" if (not ws.diff() and not state.files_modified) else "fail",
                evidence=f"files_modified={sorted(state.files_modified)}",
            ),
        ]
        return self._dispose(checks, positive={"grounded_in_evidence"})

    # --- edit -------------------------------------------------------------

    def _verify_edit(self, state: TaskState, ws: Workspace) -> VerifierResult:
        diff = ws.diff()
        checks = [
            self._diff_present(diff, state),
            self._no_secrets(diff),
            self._command_check("tests", self._test_command(), ws, no_target_allowed=True),
            self._command_check("lint", self._lint_command(), ws, no_target_allowed=False),
        ]
        # Positive external signal: a passing test, or (absent tests) clean lint over the diff.
        return self._dispose(checks, positive={"tests", "lint"})

    # --- test_only --------------------------------------------------------

    def _verify_test_only(self, state: TaskState, ws: Workspace) -> VerifierResult:
        changed_tests = sorted(p for p in state.files_modified if _is_test_path(p))
        checks = [
            CheckResult(
                name="tests_changed",
                kind="required",
                status="pass" if changed_tests else "fail",
                evidence=f"test files changed={changed_tests}",
            ),
            self._command_check("tests", self._test_command(), ws, no_target_allowed=False),
        ]
        return self._dispose(checks, positive={"tests"})

    # --- shared check builders -------------------------------------------

    def _diff_present(self, diff: str, state: TaskState) -> CheckResult:
        present = bool(diff or state.files_modified)
        return CheckResult(
            name="diff_present",
            kind="required",
            status="pass" if present else "fail",
            evidence=f"files_modified={sorted(state.files_modified)}",
        )

    def _no_secrets(self, diff: str) -> CheckResult:
        found = _scan_secrets(diff)
        return CheckResult(
            name="no_secrets",
            kind="required",
            status="fail" if found else "pass",
            evidence=f"markers={found}" if found else "no secret/placeholder markers in diff",
        )

    def _command_check(
        self, name: str, command: str, ws: Workspace, *, no_target_allowed: bool
    ) -> CheckResult:
        """Run a verification command and classify its result into a `CheckResult`.

        The verifier runs this ITSELF (§5): the command's exit code is the external
        signal. An empty command is a skip — allowed for lint, disallowed for tests.
        """
        if not command:
            reason = "no lint command configured" if name == "lint" else "no test command configured"
            return CheckResult(name=name, kind="required", status="skip", evidence=reason, skip_reason=reason)
        timeout = self.config.command_timeout_seconds if self.config else None
        out = ws.run(command, timeout=timeout)
        if out.timed_out:
            return CheckResult(name=name, kind="required", status="fail", evidence=f"timed out: {command!r}")
        if out.exit_code == 0:
            return CheckResult(name=name, kind="required", status="pass", evidence=f"{command!r} exit=0")
        if no_target_allowed and out.exit_code == _NO_TESTS_EXIT:
            return CheckResult(
                name=name,
                kind="required",
                status="skip",
                evidence=f"{command!r} exit={_NO_TESTS_EXIT}",
                skip_reason="no test target exists in this repo",
            )
        return CheckResult(
            name=name, kind="required", status="fail", evidence=f"{command!r} exit={out.exit_code}"
        )

    # --- the gate (§12 pass criteria) ------------------------------------

    def _dispose(self, checks: list[CheckResult], *, positive: set[str]) -> VerifierResult:
        required = [c for c in checks if c.kind == "required"]
        failed = sorted(c.name for c in required if c.status == "fail")
        bad_skips = sorted(
            c.name for c in required if c.status == "skip" and (c.skip_reason or "") not in _ALLOWED_SKIPS
        )
        has_positive = any(c.status == "pass" for c in required if c.name in positive)

        passed = not failed and not bad_skips and has_positive
        if passed:
            summary = "verification passed"
        elif failed:
            summary = f"verification failed: {failed}"
        elif bad_skips:
            summary = f"verification failed: disallowed skip {bad_skips}"
        else:
            summary = "verification failed: no positive external signal"
        return VerifierResult(
            passed=passed,
            summary=summary,
            checks=checks,
            recommended_next_action=None if passed else _recommend(failed, bad_skips, has_positive),
        )

    def _test_command(self) -> str:
        return self.config.test_command if self.config else ""

    def _lint_command(self) -> str:
        return self.config.lint_command if self.config else ""


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or path.startswith("tests/")
        or "/tests/" in path
    )


def _scan_secrets(diff: str) -> list[str]:
    """Return the secret/placeholder markers found on added (`+`) diff lines."""
    found: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        upper = line.upper()
        for marker in _SECRET_MARKERS:
            if marker.upper() in upper:
                found.add(marker)
    return sorted(found)


# Repair direction per failed check, in priority order (§12 recommended_next_action).
_FAIL_HINTS = {
    "diff_present": "no change was made; apply a patch that addresses the goal",
    "tests_changed": "a test_only task must add or change tests; add the missing tests",
    "no_secrets": "remove the hard-coded secret/placeholder the diff introduces",
    "tests": "the tests fail; fix the change so the test command passes",
    "lint": "lint/type checks fail; clean up the diff",
}


def _recommend(failed: list[str], bad_skips: list[str], has_positive: bool) -> str:
    for name, hint in _FAIL_HINTS.items():
        if name in failed:
            return hint
    if bad_skips:
        return "configure a test command so completion can be verified"
    if not has_positive:
        return "no external signal proves success; ensure a test or lint check passes over the diff"
    return "provide positive external evidence of completion"
