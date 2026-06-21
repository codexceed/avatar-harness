# ADR 0029 — Streaming idle-timeout for model calls (ADR-0028 R5)

- **Status:** Proposed — **awaiting approval before implementation**
- **Date:** 2026-06-21
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0028 (transport retry + request timeout — this is its deferred **R5**), ADR-0003 (native tool-calling transport), §6 (decide / parse), §16 (retry semantics). Evidence: catalog **A9** + the 2026-06-20 calibration data in ADR-0028.

## Context — a flat timeout cannot tell "slow" from "stalled"

ADR-0028 R1 bounds each model call with a **flat** `request_timeout_seconds`. The 2026-06-20 data forced it into a narrow, fragile window:

```
 legit generation   ████████████████ up to ~203s (8–15k tokens on secret-safety)
 hung→NUL call                        ████████████ ~297–364s (the incident)
 flat timeout 240s            ✓ only because 203 < 240 < 297 — a data-dependent sliver
```

A flat per-request deadline measures **total elapsed time**, which conflates two unrelated things: a model *legitimately generating for a long time* and a *connection that has gone silent*. To never kill the former it must sit above the longest legit call; to ever catch the latter it must sit below the hang. That window (here ~210–290s) is narrow and breaks the moment a task generates >240s of tokens (longer Eval-2 work) or a provider returns its NUL faster. ADR-0028 records this as a ⚠️ consequence and names R5 as the durable fix.

The distinguishing signal the flat timeout throws away: **a healthy long call is still emitting bytes the whole time; a stalled one is not.** Streaming exposes exactly that signal.

## Decision (proposed)

**Stream the completion and bound the gap *between chunks* (an idle timeout), not the total call.** A call is "stalled" iff no token has arrived for `request_idle_timeout_seconds`; total generation length is irrelevant.

```
 non-streaming (R1)         streaming (R5)
 ─────────────────          ──────────────
 create() ─ block ─ reply   create(stream=True) → δ δ δ … δ  (token deltas)
 abort at TOTAL ≥ 240s      abort iff GAP between δ ≥ idle_timeout (e.g. 30s)
 (kills a 250s legit call)  (a 250s call streaming throughout is never idle → survives)
```

| | flat timeout (R1, keep as ceiling) | **idle timeout (R5, new)** |
| --- | --- | --- |
| hung / NUL connection (no bytes) | caught at `request_timeout` (≥ longest legit call → slow) | caught in ~`idle_timeout` (e.g. 30s), independent of legit length |
| legit 250s generation (bytes flowing) | **killed if timeout < 250s** | never idle → never killed |

**D1 · Stream native tool-calls and content.** Issue `chat.completions.create(stream=True)`; consume the delta iterator and **reassemble** the reply: concatenate `content` deltas, and accumulate `tool_call` deltas by index (the provider sends `id`/`function.name` once and `function.arguments` in fragments that must be string-joined). The reassembled message is then fed to the *existing* §6 path — `_decision_from_tool_call` / `parse_decision` — unchanged. R2/R3 still apply: an empty/NUL reassembled body is still `EmptyResponseError`; a malformed-but-non-empty one is still the model parse-retry.

**D2 · Idle watchdog.** New `request_idle_timeout_seconds` (proposed default **30**) bounds the wait for the *next* delta. The flat `request_timeout_seconds` is **kept** as a hard ceiling (belt-and-suspenders against a stream that dribbles one byte forever), but can be raised (e.g. 600s) since the idle timeout now does the fast detection.

**D3 · Fits inside the existing transport-retry (R3).** An idle-timeout abort raises `TransportError` (a stalled stream is a transport failure), so it flows through `_transport_retry` exactly like a NUL today: re-issue with backoff, surface on exhaustion. No new control path.

**D4 · Graceful fallback.** Streaming + tool-calls is not uniformly supported. Gate behind `stream_model_calls: bool` (proposed default **true**); when an endpoint rejects streaming or mis-frames tool-call deltas, fall back to the non-streaming R1 path for that client. The flat-timeout behavior remains the floor, so R5 can only help.

### The mechanism question (the real cost)

The harness loop offloads `decide()` via `asyncio.to_thread`; the per-chunk idle bound needs one of:

| option | how | cost |
| --- | --- | --- |
| **A — httpx read-timeout per chunk** | the SDK's underlying httpx client applies a *read* timeout to each network read; set it to `idle_timeout` so a silent socket raises mid-stream | lowest — reuses existing transport, no threads; depends on the SDK surfacing a per-read timeout |
| **B — async streaming + `asyncio.wait_for`** | use the async OpenAI client; wrap each `await anext(stream)` in `wait_for(idle_timeout)` | clean cancellation, but moves `decide` onto the event loop (drops `to_thread`) |
| **C — watchdog thread** | a sidecar thread cancels the stream if no chunk advances a shared deadline | most portable, most moving parts |

**Recommendation: A** (per-chunk read timeout) — it's the smallest change and maps a "silent socket" directly onto a timeout. B is the fallback if the SDK won't expose a per-read bound. This choice is the main thing to settle before implementation.

### Config (proposed)

| field | default | role |
| --- | --- | --- |
| `stream_model_calls` | `true` | master switch; `false` = exact R1 behavior |
| `request_idle_timeout_seconds` | `30` | abort if no delta for this long (the fast stall-detector) |
| `request_timeout_seconds` | `240` → raise to `600` | hard ceiling on the whole call (rarely hit once idle-timeout exists) |

## Consequences

| | |
| --- | --- |
| ✅ | Hang/NUL detected in ~30s **regardless** of how long legitimate generations run — the ADR-0028 calibration squeeze is gone |
| ✅ | `request_timeout_seconds` stops being a fragile sliver; it becomes a loose ceiling |
| ✅ | Reuses R2/R3/R4 wholesale — idle-abort is just another `TransportError` |
| ⚠️ | **Streaming tool-call reassembly is a new bug surface**: `tool_call` deltas are fragmented and provider-specific (index/id/arguments arrive piecemeal); a reassembly bug corrupts a decision. This is the main risk and needs its own tests. |
| ⚠️ | Heavier test fakes — a chunk/delta iterator + simulated inter-chunk stalls, vs today's one-shot message |
| ⚠️ | Some endpoints stream tool-calls poorly → D4 fallback is load-bearing, not optional |
| ⚠️ | Token-usage on a streamed call arrives differently (a final `usage` chunk, only with `stream_options={"include_usage": true}`) — the R3 usage accounting must read it from the stream, not `response.usage` |

## Alternatives rejected

| option | why not |
| --- | --- |
| Keep widening the flat timeout | The window is fundamentally fragile — any task with a >timeout legit call breaks it. Treats the symptom, not the conflation. |
| Raise `max_wall_clock_seconds` | Doesn't distinguish slow from stalled; just lets *both* run longer. |
| Heuristic on expected output length | Guessing a per-call token budget to derive a timeout is exactly the conflation streaming removes by measurement. |
| Per-provider tuned flat timeouts | A maintenance treadmill that still can't handle within-provider variance (a model that's fast on one task, slow on another). |

## Rollout

Behind `stream_model_calls` (default on, instantly revertible). Validate **offline** first — a scripted chunk-iterator fake proving: (1) reassembled tool-calls/content match the non-streaming decision, (2) an inter-chunk stall > idle-timeout raises `TransportError` while a long-but-active stream does not, (3) an empty stream → `EmptyResponseError`, (4) usage read from the stream. Then a **confirmatory eval at higher concurrency** (push the provider harder than the c4 R1–R4 run) to measure whether idle-detection recovers cells a 240s flat timeout would lose. Flip this ADR to Accepted on that signal.
