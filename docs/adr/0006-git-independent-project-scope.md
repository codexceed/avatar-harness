# ADR 0006 — Git-independent project scope for file discovery

- **Status:** Proposed
- **Date:** 2026-06-11
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8) — dogfood incident 2026-06-11 (an `investigate` run in a workspace containing its own `.venv` answered "what does this repo do?" from bundled `dist-info/METADATA` instead of the source).
- **Related:** `HARNESS_DESIGN.md` §10 (read tools), §9 (context builder); `DECISIONS.md` 2026-06-11 (list_files hidden-skip — **this ADR revisits that trade-off**); ADR-0001 (workspace as the mutation/observation chokepoint).

## Context

"What is *the project*?" is answered three different ways across the read surface, and they disagree:

| Surface | Scoping today | Result |
| --- | --- | --- |
| `search_repo` (rg) | rg's own ignore handling (honors `.gitignore` *if present*, has built-in defaults) | filters noise correctly, **needs no git** |
| `list_files` | pathlib glob + skip dot-prefixed segments | skips `.venv`/`.git`, but **not** `node_modules/`, `build/`, `*.egg-info/`, `__pycache__/` |
| `read_file` | path-confinement only | reads any in-workspace path, including artifacts |

The dogfood incident exposed the gap: a survey saw 8,487 on-disk files where 131 were the project. The obvious fix — define the project as `git ls-files` — was rejected in discussion: **file management must not depend on git.** The harness should operate on a plain directory (a tarball, an svn/hg checkout, a scratch dir with no VCS) without losing its sense of what the project is. Git is used elsewhere for the *verification baseline* (pinned `HEAD` diff, §15); that is a separate axis and out of scope here. This ADR governs only **discovery scoping**, and its constraint is: no hard git call.

The key precedent is already in the codebase twice. `search_repo` gets correct, git-optional noise-filtering for free by delegating to rg, which *opportunistically* honors `.gitignore` but degrades to built-in conventions without it. And the sensitive-path denylist (`AVATAR_SENSITIVE_PATH_GLOBS`) is exactly a convention-based, config-overridable glob policy applied at the `Workspace` chokepoint. We have the shape; we are not applying it to project scope.

## Decision (proposed)

Introduce one **git-independent project-scope policy** — a convention-based exclude-glob set, layered, applied at the `Workspace` chokepoint — and route all discovery through it.

1. **Convention defaults.** A curated denylist of artifact/dependency globs, matched on path segments: `.git/`, `.venv/`, `venv/`, `node_modules/`, `__pycache__/`, `*.egg-info/`, `*.dist-info/`, `dist/`, `build/`, `target/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`. Works on any directory, VCS or not. This *subsumes* the dot-prefix hidden-skip heuristic — dot-dirs that matter become explicit entries.
2. **Opportunistic ignore-file layering.** If a `.gitignore` / `.ignore` is present, honor it as *additional* excludes — never as a requirement. Absent it, conventions alone hold. (rg's model, made explicit.)
3. **Config override.** `AVATAR_PROJECT_EXCLUDE_GLOBS` (mirrors `AVATAR_SENSITIVE_PATH_GLOBS`): a JSON list that replaces the defaults for projects with unusual layouts.

Routing:
- `list_files` default scope = tree minus project-scope excludes (replaces pathlib hidden-skip).
- The context builder's repo overview draws from the same in-scope set.
- `read_file` of an excluded path stays **allowed** — reading into `.venv` to debug a dependency is legitimate — but the result is annotated `note: outside project scope`, so discovery never *steers* there while the capability remains.
- `search_repo` already satisfies the policy via rg; left as-is (its ignore handling and our default set may diverge at the margins — acceptable; both filter noise, neither is a security boundary).

## Alternatives considered

- **`git ls-files` as the project definition** (the prompting suggestion): rejected — introduces a hard git dependency on the discovery path and fails outright on non-git projects. Reuse of the existing git anchor is tempting but couples file management to VCS, which is precisely what we want to avoid.
- **Status quo — pathlib hidden-skip** (`DECISIONS.md` 2026-06-11): rejected now — catches dot-dirs but misses the large non-dot artifact dirs (`node_modules/`, `build/`, `*.egg-info/`). The 131-vs-8,487 incident is the friction that flips that earlier trade-off.
- **A full in-house `.gitignore` parser as a hard requirement:** rejected — reimplements git semantics *and* still requires the file to exist; conventions + opportunistic layering get the value without either cost.

## Consequences

- Discovery becomes VCS-agnostic: identical behavior on a git repo, an svn checkout, or an unzipped tarball.
- One policy object, shared glob-matching machinery with the sensitive-path denylist; the hidden-skip special case retires.
- `read_file` confinement is **unchanged** — the path-outside-workspace boundary is orthogonal and stays exactly as-is; this ADR only changes what *discovery* surfaces, never what is reachable.
- Default set will need occasional curation as ecosystems add artifact dirs; the config override is the escape hatch in the meantime.
- Trigger to implement: next change touching `list_files`/context scoping, or the next dogfood run where artifact noise distorts an answer (record the journal id here).
