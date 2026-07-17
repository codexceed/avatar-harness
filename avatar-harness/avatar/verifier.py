"""Verifier — proves completion via external evidence, never self-certification (§12).

It runs **no model**: every check is a predicate over structured `TaskState` plus
the workspace. For `edit`/`test_only` the *external signal* is the task's frozen
**verification plan** (ADR-0007) — commands the harness resolved (config override →
deterministic detection → cited LLM proposal) and froze onto `TaskState` before
editing began. The verifier is a **pure executor** over that plan: zero language
knowledge, it runs each frozen command itself via `ws.run` and reads the real exit
code. The gate is harness-owned, so the model can never self-certify (§5). The
three pass criteria (§12): no required check fails; no required check is skipped
for a disallowed reason; at least one positive signal.

When no plan was frozen (a direct library call), the config override tier alone
is used as the plan — never a detected or invented default.
"""

from avatar.config import HarnessConfig
from avatar.planner import classify_change_paths, config_override_checks
from avatar.state import CheckResult, PlannedCheck, TaskState, VerifierResult
from avatar.workspace import Workspace

# Skips the gate tolerates (§12 criterion 2): discovered absence, not evasion.
_ALLOWED_SKIPS = frozenset({"no test target exists in this repo"})

# Likely secrets / placeholders that must never land in a diff (always-on guard, §12).
_SECRET_MARKERS = ("AKIA", "-----BEGIN", "PLACEHOLDER", "<placeholder>")

_NO_TESTS_EXIT = 5  # pytest convention: no tests were collected.

# The legible no-contract failure (ADR-0007): never an invented check, never vacuous.
_NO_CONTRACT_EVIDENCE = (
    "no verification contract discovered — declare one via AVATAR_TEST_COMMAND / AVATAR_LINT_COMMAND"
)


class Verifier:
    """Disposes of a completion proposal via external evidence, never a model (§12).

    Args:
        config: The harness config; its `test_command`/`lint_command` override tier
            doubles as the plan when no frozen plan exists. `None` leaves both empty.
    """

    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config

    def verify(self, state: TaskState, ws: Workspace) -> VerifierResult:
        """Run the verification contract for the task's `task_kind` (§12).

        Args:
            state: The task state.
            ws: The run-scoped workspace.

        Returns:
            The disposition over the contract's checks.

        Raises:
            NotImplementedError: For an unrecognized `task_kind`.
        """
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
        # The contract is *no diff at the end*, not *no writes ever* (ADR-0005): transient
        # instrumentation is legal, so the check is the working tree vs the pinned
        # baseline at verification — the `files_modified` ledger records the writes but
        # does not fail a tree that nets to zero.
        diff = ws.diff()

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
                status="pass" if not diff else "fail",
                evidence=(
                    f"tree matches the pinned baseline; files touched transiently="
                    f"{sorted(state.files_modified)}"
                    if not diff
                    else f"net diff remains vs the pinned baseline; files_modified="
                    f"{sorted(state.files_modified)}"
                ),
            ),
            # The always-on secret/placeholder guard (§12) applies to every kind that can
            # write — a leftover instrumented diff must not smuggle a secret either.
            self._no_secrets(diff),
        ]
        return self._dispose(checks, positive={"grounded_in_evidence"})

    # --- edit -------------------------------------------------------------

    def _verify_edit(self, state: TaskState, ws: Workspace) -> VerifierResult:
        diff = ws.diff()
        plan = self._plan(state)
        command_checks = self._plan_checks(plan, ws)
        checks = [
            self._diff_present(diff, state),
            self._no_secrets(diff),
            *command_checks,
        ]
        if state.declared_change_kinds is not None:
            # A declared contract froze: the diff audits the declaration (ADR-0044).
            checks.append(self._change_kind_coverage(state, diff, ws))
        # Positive external signal: any frozen plan command passing — a targeted
        # test, or (when the repo declares none) clean lint over the diff (§12).
        return self._dispose(checks, positive={c.name for c in plan})

    # --- test_only --------------------------------------------------------

    def _verify_test_only(self, state: TaskState, ws: Workspace) -> VerifierResult:
        changed_tests = sorted(p for p in state.files_modified if _is_test_path(p))
        plan = [c for c in self._plan(state) if c.kind == "test"]
        command_checks = self._plan_checks(plan, ws, no_target_allowed=False)
        checks = [
            CheckResult(
                name="tests_changed",
                kind="required",
                status="pass" if changed_tests else "fail",
                evidence=f"test files changed={changed_tests}",
            ),
            *command_checks,
        ]
        return self._dispose(checks, positive={c.name for c in plan})

    # --- the frozen plan (ADR-0007) ----------------------------------------

    def _plan(self, state: TaskState) -> list[PlannedCheck]:
        """The plan to execute: the frozen one, else the config override tier alone.

        Once frozen, the plan IS the rubric — config had its say at resolution time
        and does not re-enter. The fallback covers direct library callers who never
        ran the planner; it stays language-free (overrides only, no detection).

        Args:
            state: The task state carrying the (possibly) frozen plan.

        Returns:
            The checks to execute (possibly empty: no contract).
        """
        if state.verification_plan is not None:
            return state.verification_plan
        return config_override_checks(self.config)

    def _plan_checks(
        self, plan: list[PlannedCheck], ws: Workspace, *, no_target_allowed: bool = True
    ) -> list[CheckResult]:
        """Execute every plan command; an empty plan is a legible contract failure.

        Args:
            plan: The frozen checks to run.
            ws: The run-scoped workspace.
            no_target_allowed: Whether a test command's no-tests-collected exit is a
                tolerated skip (`True` for edit; `False` for test_only, where the new
                tests must actually run).

        Returns:
            One `CheckResult` per plan entry, or the single `verification_contract`
            failure when the plan is empty.
        """
        if not plan:
            return [
                CheckResult(
                    name="verification_contract",
                    kind="required",
                    status="fail",
                    evidence=_NO_CONTRACT_EVIDENCE,
                )
            ]
        results: list[CheckResult] = []
        failed_chains: set[str] = set()
        for check in plan:
            # Short-circuit a failed `&&` chain (ADR-0045, PR #112 review): in shell a
            # failing segment guards every later one — including a mutating command. A
            # skipped segment reports as FAIL, never a vacuous pass; its chain already
            # failed, so the verdict is unchanged and the side effect never runs.
            if check.chain is not None and check.chain in failed_chains:
                results.append(
                    CheckResult(
                        name=check.name,
                        kind="required",
                        status="fail",
                        evidence=(
                            f"not run: an earlier segment of its `&&` chain failed [{check.provenance}]"
                        ),
                    )
                )
                continue
            result = self._command_check(
                check, ws, no_target_allowed=no_target_allowed and check.kind == "test"
            )
            if check.chain is not None and result.status == "fail":
                failed_chains.add(check.chain)
            results.append(result)
        return results

    # --- shared check builders -------------------------------------------

    def _diff_present(self, diff: str, state: TaskState) -> CheckResult:
        present = bool(diff or state.files_modified)
        return CheckResult(
            name="diff_present",
            kind="required",
            status="pass" if present else "fail",
            evidence=f"files_modified={sorted(state.files_modified)}",
        )

    def _change_kind_coverage(self, state: TaskState, diff: str, ws: Workspace) -> CheckResult:
        """Audit the declared change kinds against the change actually made (ADR-0044).

        Self-declared kinds without reconciliation would be self-certification one level
        up (declare `content`, ship code, dodge execution checks) — so every changed path
        must classify to a *declared* kind. Paths come from the diff's `+++ b/` targets
        unioned with the `files_modified` entries that still exist: `files_modified` is an
        append-only touch ledger, so a scratch file created then deleted mid-run is not
        part of the final change and must not fail the audit (PR #112 review) — deletions
        already need no kind coverage, matching `_diff_paths`. The union still catches
        files a shelled command wrote outside the edit tools. Only under-declaration
        fails; over-declaring is self-inflicted strictness the model may amend away
        (gated), not an integrity violation.

        Args:
            state: The task state carrying `declared_change_kinds` and `files_modified`.
            diff: The workspace diff whose paths are audited.
            ws: The run-scoped workspace (existence checks for the touch ledger).

        Returns:
            The required `change_kind_coverage` check result.
        """
        declared = set(state.declared_change_kinds or [])
        surviving = {p for p in state.files_modified if (ws.root / p).exists()}
        changed = surviving | _diff_paths(diff)
        offending: dict[str, list[str]] = {}
        for path in sorted(changed):
            (kind,) = classify_change_paths([path])
            if kind not in declared:
                offending.setdefault(kind, []).append(path)
        if offending:
            detail = "; ".join(f"{kind}: {paths}" for kind, paths in sorted(offending.items()))
            evidence = (
                f"undeclared change kind(s) — declared {sorted(declared)} but the diff touches "
                f"{detail}. Amend the contract (alter_verification) to declare and cover them."
            )
        else:
            evidence = f"declared {sorted(declared)} covers all changed paths"
        return CheckResult(
            name="change_kind_coverage",
            kind="required",
            status="fail" if offending else "pass",
            evidence=evidence,
        )

    def _no_secrets(self, diff: str) -> CheckResult:
        found = _scan_secrets(diff)
        return CheckResult(
            name="no_secrets",
            kind="required",
            status="fail" if found else "pass",
            evidence=f"markers={found}" if found else "no secret/placeholder markers in diff",
        )

    def _command_check(self, check: PlannedCheck, ws: Workspace, *, no_target_allowed: bool) -> CheckResult:
        """Run one frozen verification command and classify its result (§5, ADR-0007).

        The verifier runs this ITSELF: the command's real exit code is the external
        signal, and the check's provenance rides in the evidence so the rubric is
        auditable. A missing binary surfaces as a failed check (exit 127 from the
        workspace), never a crash.

        Args:
            check: The frozen check to execute.
            ws: The run-scoped workspace.
            no_target_allowed: Whether a `_NO_TESTS_EXIT` exit is a tolerated skip.

        Returns:
            The classified `CheckResult`.
        """
        timeout = self.config.command_timeout_seconds if self.config else None
        out = ws.run(check.command, timeout=timeout)
        tag = f"[{check.provenance}]"
        if out.timed_out:
            return CheckResult(
                name=check.name,
                kind="required",
                status="fail",
                evidence=f"timed out: {check.command!r} {tag}",
            )
        if out.exit_code == 0:
            return CheckResult(
                name=check.name, kind="required", status="pass", evidence=f"{check.command!r} exit=0 {tag}"
            )
        if no_target_allowed and out.exit_code == _NO_TESTS_EXIT:
            # No tests collected. Genuine absence (a test-less repo) is a tolerated skip; but if
            # the *pinned baseline* had tests, collecting none now is suppression, not absence —
            # emptying/deleting/rigging the graded tests to launder an exit-5 skip (ADR-0042,
            # Threat A). Fail, don't skip.
            if _baseline_had_tests(ws):
                return CheckResult(
                    name=check.name,
                    kind="required",
                    status="fail",
                    evidence=(
                        f"{check.command!r} collected no tests (exit={_NO_TESTS_EXIT}) but the "
                        f"pinned baseline had tests — suppression, not absence {tag}"
                    ),
                )
            return CheckResult(
                name=check.name,
                kind="required",
                status="skip",
                evidence=f"{check.command!r} exit={_NO_TESTS_EXIT} {tag}",
                skip_reason="no test target exists in this repo",
            )
        detail = (out.stderr or out.stdout).strip().splitlines()
        excerpt = f": {detail[0]}" if detail else ""
        return CheckResult(
            name=check.name,
            kind="required",
            status="fail",
            evidence=f"{check.command!r} exit={out.exit_code} {tag}{excerpt}",
        )

    # --- the gate (§12 pass criteria) ------------------------------------

    def _dispose(self, checks: list[CheckResult], *, positive: set[str]) -> VerifierResult:
        """Apply the §12 pass criteria to the executed checks and return the disposition.

        The gate passes only when all three criteria hold: no required check fails,
        no required check is skipped for a disallowed reason, and at least one
        positive-signal check passes. A `skip` is neither failure nor positive
        evidence, so a run whose only checks skipped cannot pass.

        Args:
            checks: Every check that was run (required and otherwise).
            positive: Names of the checks that count as positive external signal;
                at least one must have passed for the gate to pass.

        Returns:
            The `VerifierResult` carrying the pass/fail disposition, a legible
            summary, the checks, and a repair hint when it failed.
        """
        required = [c for c in checks if c.kind == "required"]
        failed = sorted(c.name for c in required if c.status == "fail")
        bad_skips = sorted(
            c.name for c in required if c.status == "skip" and (c.skip_reason or "") not in _ALLOWED_SKIPS
        )
        has_positive = any(c.status == "pass" for c in required if c.name in positive)

        passed = not failed and not bad_skips and has_positive
        if passed:
            summary = "verification passed"
        elif "verification_contract" in failed:
            summary = f"verification failed: {_NO_CONTRACT_EVIDENCE}"
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


def _diff_paths(diff: str) -> set[str]:
    r"""The workspace-relative paths a unified diff touches (its `+++ b/` targets).

    Deletions (`+++ /dev/null`) carry no `b/` target and are skipped — a removed
    file needs no kind coverage. Git C-quotes headers whose paths hold non-ASCII or
    control characters (`+++ "b/p\303\244th.md"`); those unquote here so such files
    cannot silently escape the kind audit (PR #112 review).

    Args:
        diff: The unified diff text.

    Returns:
        The touched paths, possibly empty.
    """
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            paths.add(line[len("+++ b/") :].strip())
        elif line.startswith('+++ "b/') and line.rstrip().endswith('"'):
            quoted = line.rstrip()[len('+++ "b/') : -1]
            # Reverse git's C-style quoting: escapes are octal bytes of the UTF-8 form.
            paths.add(quoted.encode("latin-1").decode("unicode_escape").encode("latin-1").decode("utf-8"))
    return paths


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or path.startswith("tests/")
        or "/tests/" in path
    )


def _baseline_had_tests(ws: Workspace) -> bool:
    """Whether the pinned baseline commit contained any test file (ADR-0042, Threat A).

    The suppression signal for an exit-5 no-tests skip: if the baseline HAD tests but the
    frozen check now collects none, the tests were emptied/deleted/rigged rather than never
    having existed. Structural (path-based) and conservative — it only distinguishes absence
    from suppression, and a genuinely test-less repo (no baseline test files) still skips.

    Args:
        ws: The run-scoped workspace, carrying the pinned baseline.

    Returns:
        `True` when the baseline tree held at least one test-shaped path.
    """
    return any(_is_test_path(p) for p in ws.baseline_paths())


def _scan_secrets(diff: str) -> list[str]:
    """Return the secret/placeholder markers found on added (`+`) diff lines.

    Args:
        diff: The unified diff to scan.

    Returns:
        The sorted markers found on added lines.
    """
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
    "diff_present": (
        "no change was made; edit a file with str_replace (or write_file to create) to address the goal"
    ),
    "no_unintended_diff": (
        "an investigate task must leave the repo unchanged: revert the leftover "
        "instrumentation so the diff vs the pinned baseline is empty — OR, if you meant to "
        "FIX this rather than explain it, call switch_to_editing to escalate to an edit task "
        "(which keeps and verifies your changes)"
    ),
    "tests_changed": "a test_only task must add or change tests; add the missing tests",
    "no_secrets": "remove the hard-coded secret/placeholder the diff introduces",
    "verification_contract": (
        "no verification contract discovered — declare one via AVATAR_TEST_COMMAND / AVATAR_LINT_COMMAND"
    ),
    "tests": "the tests fail; fix the change so the test command passes",
    "lint": "lint/type checks fail; clean up the diff",
    "smoke": (
        "the greenfield smoke check failed; fix the code so it parses/compiles, or declare a "
        "real contract via AVATAR_TEST_COMMAND / AVATAR_LINT_COMMAND"
    ),
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
