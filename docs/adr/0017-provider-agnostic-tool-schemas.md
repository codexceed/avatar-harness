# ADR 0017 — Provider-agnostic tool schemas: no `prefixItems`-without-`items` (the tuple trap)

- **Status:** Accepted — implemented 2026-06-15
- **Date:** 2026-06-15
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0001 (native tool calling; OpenAI-style `tools=` schemas); `HARNESS_DESIGN.md` §10 (tool definitions), §9 (`ContextBuilder` emits `input_model.model_json_schema()`). Surfaced by the first live Eval-0 baseline (`docs/research/eval-baseline-2026-06-15.md`, Finding 1).

## Context

Tool inputs are pydantic models; `ContextBuilder` serializes each via `model_json_schema()` (`context.py:165`) and the model client passes the result through verbatim as the provider's function `parameters` (`model_client.py:306`). The harness's settled assumption (ADR-0001) is that an **OpenAI-style JSON Schema passes through unchanged** to any provider.

The first live multi-model Eval-0 baseline broke that assumption. **18 of 20 Gemini runs died** with a provider `400 BadRequest` *before the agent loop could act* (iterations=0):

```
GenerateContentRequest.tools[0].function_declarations[0].parameters
  .properties[line_range].any_of[0].items: missing field.   (INVALID_ARGUMENT)
```

Root cause: `read_file`'s `line_range: tuple[int, int] | None` renders, via pydantic, as a JSON-Schema array using `prefixItems` + `minItems`/`maxItems` and **no `items`** key. Gemini's `GenerateContentRequest` validator (Google AI Studio / Vertex backends) requires every array schema to declare `items` and rejects the request. OpenAI and Anthropic accept (or ignore) `prefixItems`, so the *identical* payload only breaks on Gemini. It was intermittent (2/20 slipped through) because OpenRouter load-balances the slug across strict and lenient upstream routes.

The consequence for the eval-driven loop is corrosive: "Gemini pass@1 = 0.10" is a *scaffold artifact, not a capability number* — the benchmark-measures-your-scaffold trap. Until the schema is provider-safe, a third of the model matrix produces uninterpretable numbers.

## Decision

**Author tool input schemas to a provider-agnostic lowest common denominator at the source** — specifically, do not use Python `tuple` types in tool input models, because they emit `prefixItems`-without-`items`. `read_file.line_range` becomes `list[int]`, which renders as a plain `{"type": "array", "items": {"type": "integer"}}` that every provider accepts. The exactly-two-elements / `1 <= start <= end` contract that the tuple type previously carried structurally is enforced by a pydantic `field_validator`, so invalid input is fed back to the model as a recoverable correction (§10) rather than lost.

This is the only `tuple`-typed field in the tool surface today, so the change is a single field plus its validator.

## Consequences / alternatives

- **Rejected — a boundary sanitizer in the model client** (rewrite `prefixItems → items` before sending, per-provider). More general, but it adds provider-quirk-normalization machinery to the wire boundary for a *single* known quirk and a *single* affected field. That is abstraction ahead of enforcement (Principle C, rule of three): build the sanitizer when a second, structurally-different quirk appears that source-level discipline cannot reach. The source-level fix keeps schemas simple and the wire path dumb.
- **Cost:** tool authors must avoid `tuple` (and other types that emit `prefixItems`) in input models. A regression test (`test_read_file_schema_arrays_are_provider_agnostic`) pins the invariant — every array branch declares `items`, none uses `prefixItems` — and will fail loudly if a future tuple field reintroduces the trap. Generalize it to a registry-wide schema lint if a third tool needs an array input.
- **The validator is a net gain:** the old `tuple[int, int]` accepted reversed or zero/negative ranges silently; the validator now rejects them as model-correctable feedback.
- **Follow-up:** re-run Gemini on the full Eval-0 matrix to obtain its first valid capability reading; record the result under `docs/research/`.
