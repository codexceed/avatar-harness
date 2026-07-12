# avatar-harness — common developer tasks.
# `uv` manages the environment; the default dependency group (dev) is synced automatically.
# Soft-gate tools run ephemerally via `uvx` to keep the locked dev env minimal.

.PHONY: install test run eval eval-diff eval-matrix lint format typecheck docstrings deps smoke \
        docs-api docs-serve docs-validate check check-hard check-soft clean

# Mintlify's CLI needs Node LTS (not 25+); use fnm to pin it (repo .node-version = 22).
MINT := $(shell command -v fnm >/dev/null 2>&1 && echo "fnm exec --using=22 mint" || echo "mint")

# Install/sync dependencies (including the dev group).
install:
	uv sync

# Run the test suite.
test:
	uv run pytest

# Run the harness on a task:  make run TASK="fix the failing auth test"
TASK ?= Describe this repository.
run:
	uv run avatar "$(TASK)"

# Run the Eval-0 task suite (live; needs AVATAR_API_KEY + spend). Multi-model matrix:
#   make eval MODELS="openai/gpt-5.1,anthropic/claude-sonnet-4-6,google/gemini-3.1-pro-preview" SEEDS=3
# Keep the scratch repos to inspect output:  make eval NO_CLEANUP=1
# Choose where they go:                       make eval WORKSPACE=./myrun
# Run cells in parallel (default 1):          make eval CONCURRENCY=4
# Run a subset of tasks:                      make eval TASKS="news-analyzer,secret-safety"
EVAL_ARGS = $(if $(MODELS),--models "$(MODELS)") $(if $(TASKS),--tasks "$(TASKS)") \
	$(if $(SEEDS),--seeds $(SEEDS)) \
	$(if $(TEMPERATURE),--temperature $(TEMPERATURE)) $(if $(WORKSPACE),--workspace $(WORKSPACE)) \
	$(if $(CONCURRENCY),--concurrency $(CONCURRENCY)) $(if $(NO_CLEANUP),--no-cleanup)
eval:
	uv run python -m evals.run $(EVAL_ARGS)

# Regression-diff two result files:  make eval-diff BASELINE=evals/results/A.jsonl CANDIDATE=evals/results/B.jsonl
eval-diff:
	uv run python -m evals.diff $(BASELINE) $(CANDIDATE)

# Standing reliability matrix: the four tracked models, 3 seeds, 8-way concurrent, output kept.
# A named shortcut for the recurring regression run (delegates to `eval` via target-specific vars).
# Command-line vars still win, so any knob is overridable:
#   make eval-matrix                        # the pinned set: 4 models x 3 seeds, CONCURRENCY=8
#   make eval-matrix SEEDS=5 CONCURRENCY=4  # override seeds/concurrency
#   make eval-matrix MATRIX_MODELS="a,b"    # swap the model set
MATRIX_MODELS ?= x-ai/grok-4.5,openai/gpt-oss-120b,openai/gpt-5.6-sol,z-ai/glm-5.2
eval-matrix: MODELS = $(MATRIX_MODELS)
eval-matrix: SEEDS = 3
eval-matrix: CONCURRENCY = 8
eval-matrix: NO_CLEANUP = 1
eval-matrix: eval

# --- Individual checks ---

# Lint.
lint:
	uv run ruff check .

# Auto-format.
format:
	uv run ruff format .

# Type-check (Pyrefly — sole hard type gate).
typecheck:
	uv run pyrefly check

# Docstring<->signature agreement (Google style).
docstrings:
	uv run pydoclint avatar-harness jo-cli evals

# Dependency hygiene (unused / missing / transitive). deptry resolves imports against a
# single package's declared deps, so it runs inside each member (the virtual workspace root
# declares none). evals is dev tooling, not a distributable package — not deptry-gated.
deps:
	cd avatar-harness && uv run deptry avatar
	cd jo-cli && uv run deptry jo

# Generate Mintlify API-reference MDX from docstrings (source of truth = the code).
docs-api:
	uv run python scripts/gen_api_docs.py

# Live-preview the docs site at http://localhost:3000 (run `make docs-api` first).
docs-serve:
	cd docs && $(MINT) dev

# Validate the docs build in strict mode (broken links, missing pages, MDX errors).
docs-validate:
	cd docs && $(MINT) validate

# Stage 0: compiles AND imports — the cheap "does it run" gate, before tests.
smoke:
	uv run python -m compileall -q avatar-harness jo-cli tests evals
	uv run python -c "import importlib, pkgutil, avatar, jo; [importlib.import_module(m.name) for p in (avatar, jo) for m in pkgutil.walk_packages(p.__path__, p.__name__ + '.')]"

# --- HARD gate: fail-fast, staged. Run before committing. ---
# Stage 0 (compile/import) -> Stage 1 (lint/types/docs/deps) -> Stage 2 (tests).
check-hard:
	uv run ruff format --check .
	uv run ruff check .
	$(MAKE) smoke
	uv run pyrefly check
	uv run pydoclint avatar-harness jo-cli evals
	cd avatar-harness && uv run deptry avatar
	cd jo-cli && uv run deptry jo
	uv run pytest

# --- SOFT gate: report only, never blocks (note the leading `-`). ---
check-soft:
	-uvx interrogate -c pyproject.toml -vv avatar-harness jo-cli
	-uvx vulture avatar-harness jo-cli --min-confidence 80
	-uv run --with pip-audit pip-audit
	-uv run python scripts/gen_api_docs.py --check
	# semgrep (deep SAST) — heavier; enable when desired:
	# -uvx semgrep --config auto avatar-harness jo-cli

# Default gate alias.
check: check-hard

# Remove tooling caches and build artifacts.
clean:
	rm -rf .ruff_cache .pytest_cache .pyrefly_cache dist build avatar-harness/*.egg-info jo-cli/*.egg-info events
