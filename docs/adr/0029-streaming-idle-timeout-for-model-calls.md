# ADR 0029 — Streaming idle-timeout for model calls (ADR-0028 R5)

- **Status:** Accepted
- **Date:** 2026-06-21
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0028 (transport retry + request timeout — this is its deferred **R5**), ADR-0003 (native tool-calling transport), §6 (decide / parse), §16 (retry semantics). Evidence: catalog **A9** + the 2026-06-20 calibration data in ADR-0028.

> **Resolution note.** Implemented as an **async** streaming path with a per-call httpx **read** timeout as the idle watchdog, true **mid-call cancellation**, and a **session-scoped non-streaming fallback**. This supersedes the "mechanism question" below (which recommended option A, a sync per-chunk read timeout): async was chosen for cancellation, not for the idle bound. The original proposal text is kept for context; the resolved design is in the two sections that follow it.

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

## Resolved design (implemented)

The idle bound landed as a hybrid of A and B: **async client** (B's mechanism) for the cancellation it unlocks, but the idle watchdog itself is **A** — the httpx per-**read** timeout — passed *per streaming call* (no `asyncio.wait_for`, no per-chunk sampling). A plain float `timeout=` on the SDK's `create()` becomes exactly that per-read bound, so no `httpx` import is needed (it stays an indirect, openai-provided dep).

- **A correction to ADR-0028's framing.** R1's `request_timeout_seconds` was described as a per-*request* deadline, but httpx has only per-*operation* timeouts (connect/read/write/pool) and **no total**. So R1's "240s" was really a per-read bound the whole time — which is why a single 358s generation that kept emitting bytes slipped past it. R5 makes that per-read bound explicit and small (`request_idle_timeout_seconds`) and stops pretending a flat total exists.

- **`adecide()` (async) beside `decide()` (sync).** `ModelClient` gains a concrete `adecide` that defaults to `await asyncio.to_thread(self.decide, …)`, so the scripted fakes are untouched; `OpenAIModelClient` overrides it with the streaming path. The runner calls `adecide`. The sync `decide()` (and its 25 unit tests) stay as a real SDK entry point; the two share the pure helpers (`_decision_from_tool_call`, `parse_decision`, `_is_empty_body`, `_UsageTally`, `_backoff`).

- **True mid-call cancellation.** `to_thread(decide)` could not be cancelled, so ESC/wall-clock only took effect *between* turns. The runner now races the model call against the cancellation token (exposed as a lazily-created `asyncio.Event`); on cancel it cancels the task — aborting the in-flight httpx request — and the streaming consumer's `finally: await stream.close()` releases the connection (best-effort, suppressed so a failing `close()` can't mask the propagating `CancelledError`). Reuses the existing `_stop_incomplete(kind="cancelled")` bookkeeping.

- **Wall-clock bounds the call, mid-flight.** The idle timeout bounds only the gap *between* chunks, so a live-but-runaway stream could otherwise overrun `max_wall_clock_seconds` within a single turn (the budget was checked only between turns). The runner's race now passes the **remaining** wall-clock budget as the `asyncio.wait` timeout; an empty `done` set means the deadline elapsed mid-call → abort the stream, end `incomplete` (budget). Both child futures are drained via `gather(return_exceptions=True)` so neither leaks an unretrieved-exception warning.

- **Discrimination is the heart of the fallback (D4).** A streamed failure is sorted three ways: **capability** → `StreamingUnsupportedError` (a streaming-rejection 4xx whose message names streaming as unsupported, or unusable tool-call framing) flips a runtime per-instance `_streaming_unsupported` flag and re-issues the *same* request non-streaming for the rest of the session; **model-correctable** → `DecisionParseError` (well-framed call, bad-JSON args) surfaces to the runner's existing parse handler; **transient** → `TransportError` (idle `ReadTimeout`, connection error, 429, 5xx, generic 4xx) flows through the R3 transport-retry. *Default to `TransportError` when in doubt* — a flaky provider is retried, not wrongly downgraded.

### Config (implemented)

| field | default | role |
| --- | --- | --- |
| `stream_model_calls` | `true` | master switch; `false` = exact non-streaming async behavior |
| `request_idle_timeout_seconds` | `30` | the per-read idle bound: abort if no delta for this long (the fast stall-detector) |
| `request_timeout_seconds` | `240` | loose ceiling on a non-streaming call (rarely hit once the idle-timeout exists) |

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
