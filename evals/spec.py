"""Task specs — declarative, versioned eval tasks (docs/eval-harness-design.md).

TOML via the stdlib ``tomllib`` rather than YAML: zero new dependencies (Principle C),
so the eval tooling stays dependency-free. The design doc's YAML examples map field-for-field.
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class TaskSpec(BaseModel):
    """One eval task: a goal scored hermetically in a fresh scratch repo.

    `fail_to_pass`/`pass_to_pass` (SWE-bench partition) and `oracle`/`hidden` (ADR-0011
    integrity) are carried now so later slices add behavior, not schema.
    """

    id: str
    goal: str
    task_kind: Literal["edit", "investigate", "test_only"] = "edit"
    fixture: str = "empty"
    success_probe: str | None = None
    budgets: dict[str, int] = Field(default_factory=dict)
    # Runtime env for the task's program (injected into the success-probe subprocess), so a
    # task can declare what its program needs to run — e.g. a dummy OPENAI_API_KEY. The user
    # sets it explicitly; it is environment, not a hint to the agent (the agent never sees it).
    env: dict[str, str] = Field(default_factory=dict)
    fail_to_pass: list[str] = Field(default_factory=list)
    pass_to_pass: list[str] = Field(default_factory=list)
    oracle: list[str] = Field(default_factory=list)
    hidden: list[str] = Field(default_factory=list)
    guards: str = ""


def load_task_spec(path: Path) -> TaskSpec:
    """Load and validate a TOML task spec.

    Validation is delegated to `TaskSpec.model_validate`, which raises pydantic's
    `ValidationError` when required fields (`id`, `goal`) are missing or mistyped.

    Args:
        path: The ``.toml`` spec file.

    Returns:
        The validated `TaskSpec`.
    """
    with Path(path).open("rb") as fh:
        data = tomllib.load(fh)
    return TaskSpec.model_validate(data)
