"""The per-run result row and its JSONL serialization."""

from pydantic import BaseModel


class ResultRow(BaseModel):
    """One scored eval run — the unit appended to ``evals/results/<ts>.jsonl``."""

    task: str
    model: str
    seed: int
    solved: bool
    outcome: str | None
    iterations: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    probe_exit: int | None = None
    workspace: str | None = None  # the scratch repo this ran in (for inspecting the agent's output)

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line.

        Returns:
            A one-line JSON string (no trailing newline).
        """
        return self.model_dump_json()
