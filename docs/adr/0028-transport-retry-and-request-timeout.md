# ADR 0028 — Transport-layer retry + request timeout for model calls (NUL/hang resilience)

- **Status:** Proposed — **R1–R4 implemented 2026-06-20** (offline-validated; the confirmatory `--concurrency` eval re-run + merge remain). R5 (streaming idle-timeout) deferred.
- **Date:** 2026-06-20
- **Deciders:** Sarthak Joshi
- **Related:** §6 (decide / parse-retry), §16 (retry semantics — system vs model-correctable), ADR-0003 (decision transport), ADR-0026 (bounded concurrency — the trigger). Evidence: catalog **A9** + proposal `CP-transport-retry-nul-resilience`. Raw: `eval_run_20260620T142752Z/` (the concurrent run).

## Context — one provider hang destroys a whole run

Running the eval matrix with `--concurrency > 1` (ADR-0026) put 4 simultaneous requests on `minimax/minimax-m3` via OpenRouter. The provider degraded under the parallel load: it held each connection ~5–6 min and then returned a **`\x00` (NUL) body**. Three cells failed — two truncated to a **single turn → `outcome=incomplete`** (a one-line bug fix that the same cell solved in 6 turns when run serially). pass@1 dropped 0.90→0.85, nearly read as a code regression. A serial re-run scored **20/20, zero NULs** — confirming the cells were destroyed by transport, not model capability.

```
 turn_start  14:32:26
 [model call hangs ~346s]                  ← no request timeout
 decision_error  raw="\x00" recovered    ← NUL body = HTTP 200, treated as a PARSE error
 list_files ✔
 agent_end  outcome=incomplete             ← the hang ate the wall-clock budget; run truncated
```

Three load-bearing facts about the current code:

| # | fact | location |
| --- | --- | --- |
| 1 | The OpenAI client is built with **no `timeout=` and no `max_retries=`**. The SDK default timeout (~600s) **equals `max_wall_clock_seconds`** (600) — so a single hung call can consume the *entire* run budget. | `model_client.py:484`; `config.py:52` |
| 2 | A NUL/empty body is a **successful HTTP 200**, so the SDK's transport retry (408/409/429/5xx/connection) **never fires**. It falls through to `parse_decision()`. | SDK; `model_client.py:566`, `:614` |
| 3 | `parse_decision("\x00")` raises `DecisionParseError`, which routes into the **parse-retry** (`max_parse_retries=2`): it **re-prompts the model in-conversation**. That is the wrong remedy for a transport hang — it pollutes context and each re-prompt is *another* call that can hang. | `model_client.py:530–580` |

The harness conflates two different failures: **the model emitted bad reasoning** (re-prompt it) vs **the transport returned nothing** (re-issue the request, with backoff). Today both go through the model-reasoning path. §16 actually *forbids* this: "system failures (timeout, network) are surfaced, never auto-retried [into the model loop]." The NUL is a system failure wearing a parse error's clothes.

## Decision

**Split transport failures from parse failures. Bound every request, retry transport at the transport layer with backoff, and surface exhaustion as a system failure — never into the model loop.**

```
 model reply ─┬─ valid decision ─────────────► return
              ├─ non-empty but malformed ────► PARSE-retry (re-prompt model)   [unchanged, §6]
              └─ empty / whitespace / NUL ───► TRANSPORT-retry (re-issue, backoff+jitter)  [NEW]
                                                  └─ exhausted ─► system failure (§16), NOT model loop
```

**R1 · Bound every request.** New config `request_timeout_seconds` (default **240**, must stay under `max_wall_clock_seconds`). Pass `timeout=` to the client so a hang fails fast instead of eating the 600s budget; build the client with `max_retries=0` (the transport-retry loop owns retries, and the SDK won't retry a 200-with-empty-body anyway).

> **Calibration (load-bearing).** A *flat* per-request timeout is squeezed between two walls. The 2026-06-20 journals show **legitimate** model calls of **160–203s** (a model emitting 8–15k tokens on `secret-safety` — the gap sits between `turn_start` and `model_usage`, i.e. one generation), while the **hung** calls returned their NUL after **297–364s**. So the timeout must be **above** ~203s (or it kills real work — the first cut at `90` would have) and **below** ~297s (or it never catches the hang). `240` is that window. It is fragile — a harder task with a >240s legit call, or a provider that NULs faster, breaks it — which is precisely the tension **R5** dissolves (an *idle* timeout fires on no-bytes-flowing, decoupled from total generation length).

**R2 · Classify NUL/empty as a transport error.** In `_decide_native` / `_decide_json`, when there are **no `tool_calls` AND** the body is empty / whitespace-only / all-`\x00`, raise a new **`EmptyResponseError`** (a transport error), *not* `DecisionParseError`. A *non-empty but malformed* body stays a parse error (genuinely model-correctable).

**R3 · Retry transport with backoff + jitter.** A bounded loop (`transport_max_retries=3`, distinct from `max_parse_retries`) that **re-issues the same `messages`** — no conversation pollution. Backoff `1s → 2s → 4s` with jitter (cap ~20s). Covers `EmptyResponseError`, request timeouts, connection resets, and 5xx/429 the SDK surfaces.

**R4 · Exhaustion = clean system failure.** If transport retries are spent, surface a system error (§16) so the cell records a distinct terminal state — **not** a silent one-turn `incomplete`. Emit a `transport_retry` / `transport_error` observation event so the journal stops mislabeling these as `decision_error` (which today conflates them with model-quality issues and corrupts the `failure_mode` histogram).

**R5 · (optional, strongest) Stream with an idle timeout.** `stream=True` plus a per-chunk idle timeout detects a stalled stream in *seconds* and catches an empty stream immediately, instead of waiting for the full request timeout. Deferred — R1–R4 close the incident; R5 is a latency optimization.

### Reconciliation with §16 (this is the fix, not a violation)

§16: *model-correctable* errors loop back through the model; *system failures* are surfaced, never auto-retried. The retry here is at the **transport layer** (re-call the endpoint) — it never feeds the failure through the model. On exhaustion it surfaces as a system failure, exactly as §16 mandates. The **current** behavior is the §16 violation: it routes a transport NUL into the model's parse-retry.

### Config defaults

| field | default | rationale |
| --- | --- | --- |
| `request_timeout_seconds` | `240` | above the longest legit generation (~203s) so real work survives; below the hang→NUL latency (~297s) so a stall is caught; under the 600s wall clock |
| `transport_max_retries` | `2` | low so the worst case (`timeout × (retries+1)` = 720s on a fully-dead endpoint) is a bounded, surfaced overrun — not many wall clocks |
| backoff | `1s·2^n` + jitter, cap 20s | decorrelate retries so the herd doesn't re-synchronize on the provider |

## Consequences

| | |
| --- | --- |
| ✅ | One provider hang no longer destroys a run — it fails one request, retried in ~90s, not ~600s |
| ✅ | `failure_mode` histogram stops mislabeling provider NULs as `decision_error`; transport noise is distinguishable from model defects |
| ✅ | Restores the §16 contract: transport failures surface as system errors, never re-prompt the model |
| ✅ | Validated **offline** (a scripted client that returns NUL-then-valid) → near-zero eval spend; a real provider NUL can't be summoned on demand |
| ⚠️ | A genuinely-down endpoint costs up to `request_timeout × (transport_max_retries + 1)` ≈ 720s before the cell errors — a bounded, surfaced overrun (the wall clock isn't checked mid-`decide`), and still far better than a silent 600s truncation |
| ⚠️ | The flat 240s timeout is a *calibrated window*, not a robust bound: a legit call >240s would be killed and a faster NUL missed. R5 (streaming idle-timeout) is the durable fix; revisit when task generations grow (Eval-2) |
| ⚠️ | Eval-runner concurrency + jitter (separate change) is the complementary fix: it prevents the synchronized thundering-herd that triggered the NULs in the first place |

## Alternatives rejected

| option | why not |
| --- | --- |
| Raise `max_parse_retries` | Treats a transport hang as a model error — re-prompts (pollutes context), and each retry can hang again. Wrong layer. |
| Lower `max_wall_clock_seconds` | Punishes legitimate long tasks (e.g. `secret-safety` burns 8–15k tokens) to paper over a transport hang; doesn't stop the NUL. |
| Rely on the SDK's built-in retry alone | The SDK won't retry a 200-with-NUL-body (it's a "success"); the empty-body case must be caught explicitly. |
| Do nothing; just always run serially | Forfeits ADR-0026's concurrency; the hang can still occur serially under provider load, just less often. |
