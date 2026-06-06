"""Verifier — proves completion via external evidence, never self-certification (§12).

It runs **no model**: every check is a predicate over structured `TaskState` plus
the workspace diff. Phase 1 implements the `investigate` gate only — the read-only
contract — which is self-contained (no test/lint command resolution needed, so it
sidesteps the §4.3 command-source gap). The `edit` / `test_only` gates land in Phase 2.
"""

from avatar_harness.state import CheckResult, TaskState, VerifierResult
from avatar_harness.workspace import Workspace


class Verifier:
    def verify(self, state: TaskState, ws: Workspace) -> VerifierResult:
        if state.task_kind == "investigate":
            return self._verify_investigate(state, ws)
        raise NotImplementedError(f"verifier for task_kind={state.task_kind!r} arrives in Phase 2")

    def _verify_investigate(self, state: TaskState, ws: Workspace) -> VerifierResult:
        answer = (state.final_answer or "").strip()

        # The answer references at least one file the agent actually read.
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

        failed = {c.name for c in checks if c.kind == "required" and c.status == "fail"}
        passed = not failed
        return VerifierResult(
            passed=passed,
            summary="investigate verified" if passed else f"verification failed: {sorted(failed)}",
            checks=checks,
            recommended_next_action=None if passed else _recommend(failed),
        )


def _recommend(failed: set[str]) -> str:
    if "grounded_in_evidence" in failed:
        return "answer cites no file it read; ground it in the sources you inspected"
    if "no_unintended_diff" in failed:
        return "an investigate task must not modify files; revert the changes"
    return "provide a final answer that cites concrete evidence"
