# avatar-harness — common developer tasks.
# `uv` manages the environment; the default dependency group (dev) is synced automatically.
# Soft-gate tools run ephemerally via `uvx` to keep the locked dev env minimal.

.PHONY: install test run lint format typecheck docstrings deps smoke \
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
	uv run avatar-harness "$(TASK)"

# Run the Eval-0 task suite (live; needs AVATAR_API_KEY + spend). Multi-model matrix:
#   make eval MODELS="openai/gpt-5.1,anthropic/claude-sonnet-4-6,google/gemini-3.1-pro-preview" SEEDS=3
EVAL_ARGS = $(if $(MODELS),--models "$(MODELS)") $(if $(SEEDS),--seeds $(SEEDS)) $(if $(TEMPERATURE),--temperature $(TEMPERATURE))
eval:
	uv run python -m evals.run $(EVAL_ARGS)

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
	uv run pydoclint src

# Dependency hygiene (unused / missing / transitive).
deps:
	uv run deptry src

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
	uv run python -m compileall -q src tests
	uv run python -c "import importlib, pkgutil, avatar_harness as p; [importlib.import_module(m.name) for m in pkgutil.walk_packages(p.__path__, p.__name__ + '.')]"

# --- HARD gate: fail-fast, staged. Run before committing. ---
# Stage 0 (compile/import) -> Stage 1 (lint/types/docs/deps) -> Stage 2 (tests).
check-hard:
	uv run ruff format --check .
	uv run ruff check .
	$(MAKE) smoke
	uv run pyrefly check
	uv run pydoclint src
	uv run deptry src
	uv run pytest

# --- SOFT gate: report only, never blocks (note the leading `-`). ---
check-soft:
	-uvx interrogate -c pyproject.toml -vv src
	-uvx vulture src --min-confidence 80
	-uv run --with pip-audit pip-audit
	-uv run python scripts/gen_api_docs.py --check
	# semgrep (deep SAST) — heavier; enable when desired:
	# -uvx semgrep --config auto src

# Default gate alias.
check: check-hard

# Remove tooling caches and build artifacts.
clean:
	rm -rf .ruff_cache .pytest_cache .pyrefly_cache dist build src/*.egg-info events
