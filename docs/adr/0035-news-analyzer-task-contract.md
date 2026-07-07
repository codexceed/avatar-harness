# ADR 0035 — The `news-analyzer` task contract: hermetic case-study adaptation, server-rendered UI, ops ergonomics as graded surface

- **Status:** Accepted — implemented 2026-07-05 (PR #97)
- **Date:** 2026-07-05
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-fable-5)
- **Related:** ADR-0004 (eval harness); ADR-0012 (wire-level mocking — this task is its first
  implementation); ADR-0020 (probe roles); `evals/tasks/news-analyzer.toml`;
  `evals/probes/news_app_smoke.py`;
  `docs/research/2026-07-04-news-analyzer-eval-development.md` (the development-run evidence)

## Context

Eval-0's seed tasks were single-concern (create one script, fix one function, answer one
question). We wanted a task with the shape of real product work: a small multi-component app
composing a UI, a REST API, two external services, and persistence. The source spec is the
aries engineering case study ("web app: search news via a public API → AI summary + sentiment
→ store in a db → display; focus on product design/UX and REST API design"). Three constraints
collide:

1. **Scoring must stay deterministic, free, and offline** (ADR-0004: no LLM judge; the probe is
   authoritative).
2. **The case study's deliverable is a human-usable app**, not a set of endpoints — "it passes"
   must mean a person can actually drive it.
3. **Field-testing generated "passing" apps against the real gnews + OpenAI services** exposed
   failure classes the first drafts never graded: undocumented/unvalidated config (an app that
   runs with search silently broken), unauthenticated news fetches (no way to supply the
   provider's API key), and raw-exception error pages that hide what the upstream said.

## Decision

Pin the task contract in the goal text precisely enough that a wire-level probe can drive the
whole app with zero discovery, along three deliberate lines:

1. **Hermetic adaptation of the case study.** External services become probe-owned local stubs
   reached via env-injected endpoints (`NEWS_API_URL`, `OPENAI_BASE_URL` — ADR-0012); the
   database becomes SQLite (`news.db`; nothing to provision in a scratch repo); hosting and
   handover sections of the source spec are dropped. The news stub is gnews-shaped **including
   its auth**: it 401s without the `apikey` query parameter carrying `NEWS_API_KEY`.
2. **Server-rendered-HTML-only UI.** The UI contract is plain HTML forms (search form →
   per-result analyze buttons → redirect home), so the probe can exercise it *functionally* —
   submitting exactly the requests a browser would — with stdlib HTTP alone. UX polish is not
   gradable; a working human flow is, and that is what is graded.
3. **Ops ergonomics are part of the graded contract**, not niceties: every config setting
   documented with the app (README or a marked docstring/comment block — a bare `os.environ`
   read does not count); fail-fast at startup naming any missing required variable; a legible
   HTML error page naming the news API when it degrades, with the server staying up. Each
   requirement traces to an observed field failure of a previously "passing" app.

## Alternatives rejected

- **A JavaScript/SPA frontend, graded via Playwright** — functional verification would require
  a headless browser in the eval loop (a heavy, flaky dependency against Principle C); without
  one, JS UI checks degrade to structural grep, which is exactly the "looks right" scoring the
  probes exist to avoid.
- **Structural HTML checks only (no form submission)** — rejected for the same reason: the
  dogfood that motivated the task was an app whose pages looked right and whose pipeline was
  dead.
- **Keeping the API-only contract** (first draft) — a correct JSON API with no operable UI
  passed while failing the case study's actual deliverable; the API-only golden variant is now
  a permanent must-fail counter-example.
- **Embedding the news key in `NEWS_API_URL`** — rejected in favor of a dedicated
  `NEWS_API_KEY`: a separate, documentable setting mirrors how operators actually configure
  such apps, and lets the probe verify authentication explicitly.
- **A permissive stub (no `apikey` gate, bare stub URL)** — allowed apps that cannot talk to
  any real, authenticated news API to pass; rejected after a field test against real gnews.

## Consequences

- The task is the suite's heaviest and most discriminating cell (~40–200k tokens/run;
  gpt-oss-120b fails it deterministically on stale pre-1.0 SDK usage while sonnet-class models
  pass — see the research note).
- The contract evolved across development runs, so **only artifacts graded on the final surface
  (`20260704T232723Z` onward) are comparable**; earlier run-2 cells must not be baselined
  (measured in the research note via deterministic re-scoring).
- The probe is grading surface: its golden app + surgical single-property counter-examples in
  `tests/test_evals.py` are the regression harness for every pinned clause; contract changes
  must extend both.
- UI content checks compare against `html.unescape`d pages — apps are free (and encouraged) to
  escape their output; the run-2 construct-validity bug (correct escaping graded as failure) is
  the recorded cautionary case.
