# ADR 0003 — A robust transport for file creation (and large mutations)

- **Status:** Accepted — **Options A and B both implemented 2026-06-09** (maintainer call: A taken first, then B as the complementary half); Option C rejected
- **Date:** 2026-06-09
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8) — incident analysis and option design
- **Related:** `HARNESS_DESIGN.md` §6 (decision protocol), §10 (tools), §18 (reuse: streaming/tool-call plumbing); ADR-0002 D4 (`run_command`); journal `events/0ad6c3fe…jsonl` (the motivating incident)

## Context

A live cockpit dogfood (2026-06-09, goal: *"Write a python script for openai compatible LLM APIs with rich colors, syntax highlighting and streaming."*) failed in a characteristic way: 24 consecutive read-only turns, two probes for a file that was never created, then a hallucinated `final_answer` ("Implemented the requested…"). The verifier correctly failed `diff_present`; conversational mode delivered the reply anyway.

The journal plus the model's recorded thoughts pinpoint the mechanism: the model repeatedly **attempted** to emit `apply_patch`, and every attempt died as a `DecisionParseError` inside the model client's retry loop. The retry nudge asked for "one valid JSON decision", and the model coped by downgrading to a trivially-valid read call. The observability half of this is fixed (the retry trace is now evidence + a typed `decision_error` event); **this ADR is about the structural half**: why the patch could not get through, and what transport would survive.

The structural problem, precisely:

1. **The decision protocol is one JSON object** (`response_format={"type": "json_object"}` → `parse_decision`). An `apply_patch` call for a ~200-line new file means embedding a full unified diff as a JSON string: every newline `\n`-escaped, every quote escaped, inside a `git apply`-grade hunk whose line counts must be exact. Three independent failure surfaces multiply: JSON escaping × diff syntax × output-length truncation.
2. **A unified diff buys nothing for a new file.** The diff format's value is anchoring a change to existing content (the clean-apply staleness check). A new-file hunk has no anchor — it is pure payload wearing a fragile costume (`@@ -0,0 +1,N @@` with N exactly right, every line `+`-prefixed).
3. **Failure compounds per attempt.** Each retry re-emits the entire artifact; a model that truncates once tends to truncate again. Nothing about retrying changes the transport's odds.

## Options

### A. Native tool-calling (function calling) for the decision protocol

Replace "reply with one JSON object" with the provider's tool-call API: each registered tool becomes a function schema; the decision is the function call the provider returns; `final_answer`/`ask_user` become functions too.

- **For:** providers enforce argument-level JSON validity server-side (and constrained decoding handles escaping); streaming tool-call deltas are reassemblable (§18 lists this plumbing in `cli_chat/` as already-debugged); this is how every shipping coding agent transports patches; eliminates the malformed-decision class wholesale rather than per-tool.
- **Against:** touches the `ModelClient` adapter contract (`build_messages` + `parse_decision` become provider-shaped); "OpenAI-compatible" endpoints vary in tool-call fidelity — needs the JSON fallback kept for endpoints without it; a bigger change than the MVP tail wants.

### B. A dedicated `write_file` tool for file creation

A tier-1 tool: `write_file(path, content, overwrite: bool = False)` — path-confined, denylist-checked, staged into the workspace diff exactly like `apply_patch`'s output (the `run_command` mutation-capture precedent, PR #9). `apply_patch` remains the only way to *modify* existing content (the clean-apply staleness invariant is untouched — `write_file` with `overwrite=False` refuses to clobber, and modification stays diff-anchored).

- **For:** removes the diff costume from the no-anchor case — content rides as one plain string (still JSON-escaped, but one failure surface instead of three); tiny, registry-local change (Principle A: a tool, not a framework); the verifier/artifact pipeline sees it as a normal staged change; directly fixes the incident's task shape ("write a script" = create a file).
- **Against:** does not help large *modifications* (still diff-over-JSON); a second mutation path to keep within the permission/confinement story (mitigated: same `Workspace` chokepoint, same tier).

### C. Out-of-band patch body (two-step: declare, then transmit)

The decision JSON carries only metadata (`apply_patch`, target paths); the harness then prompts for the patch body as a raw (non-JSON) reply.

- **Against (rejected):** doubles the turns for every edit; invents a bespoke two-phase protocol no provider optimizes for; the raw reply still hits truncation; stateful mid-decision exchanges complicate replay. Not pursued.

## Recommendation

> **Outcome (2026-06-09):** the maintainer took **A first**, then **B**. A: the default
> transport in `OpenAIModelClient` (`AVATAR_NATIVE_TOOL_CALLS`, default on) — tool schemas
> ride `tools=` (from each tool's real pydantic `input_schema`), `final_answer`/`ask_user`
> are functions, malformed-argument retries keep §18 `tool_call_id` pairing + the
> `retry_trace`, and a content-only reply falls back to the legacy `parse_decision` path
> (endpoints without tool-call support keep working). B: `write_file(path, content,
> overwrite=False)` — tier-1 alongside `apply_patch`, declared-path gated (confinement +
> denylist for free), staged into the diff via a new `Workspace.write_file` chokepoint
> method; an existing target is refused toward `apply_patch` unless `overwrite=true`, so
> modification stays diff-anchored (the clean-apply staleness invariant is untouched).

**B now, A later — they compose.** *(Superseded by the outcome above — kept for the record.)*

- **B (`write_file`)** is the MVP-sized fix: it addresses the exact failure (new-file creation), is one registry entry plus tests, and respects every existing invariant (confinement, denylist, staging, tiers, verification). Recommend building it next.
- **A (native tool-calling)** is the durable fix for the whole class (large modifications included) and aligns with §18's already-catalogued streaming/tool-call plumbing. Recommend scheduling it as its own increment — it is an adapter-layer migration with a fallback mode, not a tail item.
- **C** is rejected outright.

Interim mitigation already shipped (with the observability fix): the in-client retry nudge now instructs the model to *re-send the same intended action*, not merely "a valid decision" — reducing the downgrade-to-a-read cope.

## Consequences

- Until B lands, "create a file" tasks remain the protocol's weakest case; the failure is now at least *visible* (retry trace + `decision_error` events + the rendered verification verdict).
- B adds a second mutating tool; the permission table and §10 tool list need a row, and ADR-0002's approval flow applies unchanged (tier-1 → gated like `apply_patch` in the cockpit).
- A, when taken, supersedes the `json_object` prompt contract; `parse_decision` stays as the validation boundary for the fallback path.
