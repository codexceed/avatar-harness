# Shell-syntax mangling at `Workspace.run` — a vacuous verification pass and an `incomplete` burn in one journal

**Date:** 2026-07-10
**Status:** measured — trajectory analysis of one interactive dogfood journal (two sessions), plus a
minimal local reproduction of the execution defect. Fixes landed on
`feat/change-kinds-declared-contract` (ADR-0045); the fix commits pin both failure modes as
regression tests (`tests/test_shell_syntax_boundary.py`).
**Artifact:** `~/Repos/tetris_glm/events/be46ea273029486fbc62ac5360a6c82f.jsonl` (403 events; not
committed — an interactive `jo` cockpit run against the `tetris_glm` scratch repo, GLM-class model).
Session 1 `88cc9b36…` (goal: "design a basic ASCII tetris game … design spec in markdown") ended
`success`; session 2 `7316fe36…` (goal: "Go ahead and implement") ended `incomplete`.
**Reproduce (the mangling):**
`python3 -c "import shlex,subprocess; cmd=\"grep -q '^# A' D.md && grep -q '^## MISSING' D.md\"; print(subprocess.run(shlex.split(cmd),capture_output=True).returncode)"`
against a `D.md` containing only `# A` → exit **0** (shell semantics: exit 1). The regression tests
reproduce both modes hermetically.

## What the journal shows (measured)

`Workspace.run` executes `shlex.split(command)` directly (`shell=False`); shell operators pass
through as literal argv words of the first program. Three observed consequences:

1. **A false verification pass (session 1).** The amended, frozen declared check was a 10-section
   `grep -q A DESIGN.md && grep -q B DESIGN.md && …` chain. Executed as ONE grep, patterns 2–10
   became unopenable *filenames* (`grep: &&: No such file or directory` … in the tool_end content)
   — and `grep -q` exits 0 on the first match despite operand errors. `verification_end
   passed: true` (event 72), `agent_end outcome: success` (event 74). **Actually verified: 1 of 10
   declared sections.**
2. **A hang that seeded a finalization spiral (session 2).** Frozen check `declared_2` was a heredoc
   (`python3 - <<'EOF' …`). `python3 -` blocked on stdin no shell would feed; killed at the command
   timeout (event 83, the run's only failed tool call). The model diagnosed it, verified via a
   workaround file (`test_logic.py`), fixed a real `rotate()` bug (turns 21–26) — then, unable to
   amend the frozen contract (amendments never grantable), circled "one final confirmation" from
   turn 27 to turn 50: all 50 decisions were `tool_call`, zero `final_answer`. `max_iterations`
   ended the run `incomplete` (event 329). The verifier never ran.
3. **Manufactured evidence (session 2).** Confirmation chains like
   `python3 test_logic.py && python3 -c "import tetromino, …"` reported `exit=0` while the import
   half never executed — it rode along as `sys.argv` of `test_logic.py`. (Imports were genuinely
   checked exactly once, standalone, at turn 12.)

## Interpretation

- The two sessions are one defect with two presentations: the **loud** form (hang → distrust of an
  unamendable contract → stop-failure paralysis → budget death) and the **quiet** form (a mangled
  chain that happens to exit 0 → vacuous `success`). The quiet form is worse: it corrupts the
  positive-evidence signal the whole verifier design rests on, and it would poison Phase-4 eval
  baselines that treat journal outcomes as ground truth.
- The finalization spiral is plausibly *caused* by the contract defect, not independent of it: the
  model's turn-14/17/49 thoughts all return to the frozen check it had watched hang. A stall budget
  (proposed, deferred) would bound the symptom; ADR-0045 removes the trigger.
- **Eval-integrity caveat:** any prior run whose declared contract contained `&&` chains may carry
  the same vacuous-pass pattern. Re-examine before using such runs as baselines.

## Disposition

ADR-0045 (this branch): model-authored command boundaries reject shell syntax model-correctably at
declaration time; `&&` splits quote-aware into one frozen check per segment, so the session-1
contract now yields 10 genuinely-graded checks and the session-2 heredoc dies at turn 3 with a
steer to the script-file form. `Workspace.run` itself is unchanged (the no-shell/sandbox seam is
load-bearing, ADR-0042).
