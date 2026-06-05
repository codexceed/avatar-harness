# avatar-harness — common developer tasks.
# `uv` manages the environment; the default dependency group (dev) is synced automatically.

.PHONY: install test run lint format typecheck check clean

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

# Lint.
lint:
	uv run ruff check .

# Auto-format.
format:
	uv run ruff format .

# Type-check.
typecheck:
	uv run pyright

# Full gate: lint + types + tests. Run before committing.
check: lint typecheck test

# Remove tooling caches and build artifacts.
clean:
	rm -rf .ruff_cache .pytest_cache dist build src/*.egg-info events
