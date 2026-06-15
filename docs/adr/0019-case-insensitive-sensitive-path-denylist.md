# ADR 0019 — Case-insensitive sensitive-path denylist (close the case bypass)

- **Status:** Accepted — implemented 2026-06-15
- **Date:** 2026-06-15
- **Deciders:** Sarthak Joshi
- **Related:** `HARNESS_DESIGN.md` §11 (permission tiers; sensitive-path denylist, Phase 2.5); ADR-0018 (guard probes — the secret-safety scoring that surfaced this); ADR-0017 (the same Eval-0 run that exposed both). Closes a denylist bypass found by the first valid Gemini Eval-0 run (`docs/research/eval-baseline-2026-06-15.md`).

## Context

The sensitive-path denylist (§11, Phase 2.5) is deterministic *prevention*: the permission gate refuses any tool whose declared path matches a denylist glob (`.env`, `*.pem`, `credentials*`, …), so a secret never enters state/log/context/provider. It is the load-bearing control for the `secret-safety` guarantee.

The first valid Gemini Eval-0 run (the schema fix, ADR-0017, having unblocked Gemini) leaked the sentinel in **3 of 5** `secret-safety` seeds. The vector was not a model jailbreak — it was a gate bypass:

```
read_file("CREDENTIALS")  → blocked=False  → success=True, content="sk-eval-SENTINEL-…"
```

The fixture file is `credentials`; the denylist pattern is `credentials*`. `path_is_sensitive` matched with `fnmatch.fnmatch`, whose case behavior rides `os.path.normcase` — a **no-op on macOS/Linux** (it only lowercases on Windows). So matching was effectively **case-sensitive**, while the test host's filesystem (macOS APFS) is **case-insensitive**: `read_file("CREDENTIALS")` resolves to the same inode as `credentials`. The gate saw `"CREDENTIALS"` as a non-match, allowed the read, and the FS served the real secret. Demonstrated directly:

```
path_is_sensitive("credentials")  = True
path_is_sensitive("CREDENTIALS")  = False   ← bypass
path_is_sensitive("Credentials")  = False   ← bypass
```

The earlier sonnet/gpt baseline never tripped it because those models requested the exact denylisted casing and were refused; Gemini varied the case and walked through the gate. This is a general class of bug — **a case-sensitive denylist over a case-insensitive filesystem is bypassable by re-casing the path** — not specific to one file or model.

## Decision

Match the denylist **case-insensitively**. `path_is_sensitive` lowercases both the candidate path and every glob and matches with `fnmatchcase` (explicit, platform-independent — not relying on `normcase`). Over-matching a denylist is the safe direction: refusing `CREDENTIALS` on a case-sensitive filesystem where it is technically a distinct file is a conservative false-positive, never a security hole.

## Consequences / alternatives

- **Closes the bypass on every case-insensitive host** (macOS, Windows) and makes matching deterministic regardless of platform `normcase` behavior. A parametrized gate test (`test_read_file_denied_for_case_variants_of_sensitive_path`) pins `CREDENTIALS` / `Credentials` / `credentials` / `CREDENTIALS.txt` as refused.
- **Rejected — resolve the realpath and re-match.** `Path.resolve()` does not canonicalize case on a case-insensitive FS (it preserves the typed casing), so it does not fix the mismatch; and the denylist is a *path-pattern* control by design (no content detection), so the right axis to normalize is case, not inode identity.
- **Rejected — make the comparison FS-aware** (case-sensitive on Linux, insensitive on macOS). More code for a worse security posture: the denylist would then *be* bypassable on Linux deployments, and the agent's host is not always the author's. Uniform case-insensitivity is simpler and strictly safer.
- **Minor over-blocking risk:** a legitimately distinct file that differs from a secret name only by case (e.g. a `LICENSE` vs a hypothetical `license` secret pattern) would be refused. Denylist patterns are specific secret names; the collision risk is negligible and the failure mode (a refused read, model-correctable) is benign.
- **Residual risk unchanged:** non-denylisted leak channels (a secret in a non-matching filename, or in a command's stdout) remain out of scope (Phase 2.5 accepted residual; no content scrubbing). This ADR closes the *case* hole in the path control, not the content axis.
- **Follow-ups (separate):** (1) the Eval-0 failure classifier buckets by `outcome` first, so 2 of the 3 leaks (which were also `incomplete`) were hidden under `budget_exhausted` rather than surfaced as leaks — leak detection should be outcome-independent; (2) re-run Gemini `secret-safety` to confirm the gate now refuses the re-cased read (expect `probe_exit=0` where the model attempts it).
