# ADR 0012 — Wire-level API mocking for eval probes (mock the endpoint, not the client library)

- **Status:** Accepted — implemented 2026-07-05 by the `news-analyzer` probe
  (`evals/probes/news_app_smoke.py`, PR #97): one local stub server plays both external APIs
  (an OpenAI-compatible `chat/completions` reached via `OPENAI_BASE_URL`, and a gnews-shaped,
  `apikey`-gated news endpoint via `NEWS_API_URL`); the app under test runs as a real
  subprocess speaking real HTTP. `chatbot_smoke.py` retains library-level mocking (its
  program is a CLI, not a server — the layer is chosen per probe as fits the task).
- **Date:** 2026-06-14
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8)
- **Related:** `docs/eval-harness-design.md` (the success probe); ADR-0004 (eval harness); ADR-0011 (verifier integrity); the `create-chatbot` probe (`evals/probes/chatbot_smoke.py`)

## Context

The eval success probe must verify that the agent's program actually exercises the OpenAI API
**without real network calls** — scoring has to be deterministic, free, and key-free. There are
two layers at which to intercept the call:

- **Library level (what Eval-0 ships):** swap the `openai` Python module in `sys.modules` with a
  fake that records `client.chat.completions.create(...)` (and the legacy `ChatCompletion.create`).
- **Wire level:** run a local fake HTTP server that speaks the OpenAI REST contract
  (`POST /v1/chat/completions`), point the program at it via `OPENAI_BASE_URL`, and have it record
  and validate the requests.

**On the term "wire":** *the wire* is the on-the-wire protocol — the actual HTTP request/response
bytes that cross the network between client and server. A **wire mock** intercepts at that network
protocol boundary (a fake *server*); a **library mock** substitutes an in-process *object* (the SDK
module) before any bytes are sent. The names contrast *where* the seam sits: the transport boundary
vs. the import boundary. This matters because **"OpenAI API compatible" is defined by the wire
protocol, not by any one SDK** — so the wire is the more faithful place to test the contract.

The library mock works and is the right thin choice now, but it couples the probe to (a) the
`openai` Python SDK specifically and (b) our hand-stubbed call shapes.

## Decision (proposed)

Keep the **library mock for Eval-0.** Adopt a **reusable wire-level fake** — a local
OpenAI-compatible HTTP server selected via `OPENAI_BASE_URL` — when any of these triggers fires:

1. A task uses a **non-`openai`-SDK client** (`httpx`/`requests`/`litellm`) or a **non-Python target**.
2. We want **contract-level fidelity** — reject malformed requests (wrong/empty `model`/`messages`,
   bad auth) with a real `4xx`, instead of a library mock that accepts any arguments.
3. The suite grows enough **API-shaped tasks** that one server amortizes across them.
4. **Calibration** shows the library mock producing false negatives (e.g. an agent used `httpx` to
   hit `/v1/chat/completions` and was wrongly failed because nothing intercepted it).

**Cheap design hook to land now** so the later switch is configuration, not rewrites:
standardize on **`OPENAI_BASE_URL` in the task `[env]`** from the start (we already inject
`OPENAI_API_KEY` there), so tasks already point at a configurable endpoint.

Why the wire is the right *eventual* layer: it tests against the **actual API contract** rather than
our assumptions about one SDK's internals; it **generalizes across clients and languages**; and it
**matches the harness's own model calls**, which already go through a `base_url`.

## Alternatives considered

- **Library mock as the permanent approach** — rejected long-term: it couples to the `openai` SDK
  and our stubbed shapes (a maintenance treadmill as SDKs add surfaces — the Responses API,
  streaming), misses non-SDK clients, and accepts any arguments (no contract validation). It is the
  right choice for Eval-0's single Python+SDK task (Principle C: don't build the framework before the
  second concrete case).
- **Socket / transport-level interception** (`responses`/`vcr`/an MITM proxy) — bulletproof even
  against a hardcoded `https://api.openai.com`, but heavy. Revisit only if hardcoded-base-URL scripts
  become a real false-negative source.
- **Real (cheap) API calls** — rejected: non-deterministic (flaky scoring), costs money per grade,
  and needs a real key — defeating the probe's purpose.

## Consequences

- Eval-0 stays thin (library mock); the wire fake is a deliberate later slice with explicit triggers.
- **The key constraint of a wire mock:** the program must honor `OPENAI_BASE_URL`. The `openai` SDK
  and well-behaved `httpx` code do; a hardcoded endpoint escapes it (→ needs transport interception
  or a prompt convention). Standardizing `OPENAI_BASE_URL` in task env now de-risks the migration.
- A wire fake doubles as infrastructure for **ADR-0011 integrity** (central call recording, auth
  enforcement, leaked-key detection) and for **non-Python eval targets**.
- **Residual (accepted for Eval-0):** until adopted, the probe certifies SDK *wiring / control flow*,
  not live *wire-contract correctness* — a documented limitation of the library mock.
