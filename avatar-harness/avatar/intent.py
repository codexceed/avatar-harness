"""ModeClassifier — LLM mode routing, visible and correctable (revises ADR-0002 D3).

The first-word heuristic misrouted a conversational follow-up ("Now make the UI
richer…" → `investigate`) and the run burned its whole budget structurally unable to
edit (dogfood `events/04849a5a…jsonl`). D3's objection to a classifier was *hiddenness*,
not LLM-ness — so this one is: a one-shot, schema-constrained call on a cheap dedicated
model (`AVATAR_CLASSIFIER_MODEL`), whose verdict the cockpit displays and `/mode`
overrides. Any failure returns `None` and the caller falls back to the heuristic —
classification can degrade, never block.
"""

import json
from collections.abc import Sequence
from typing import Any

from avatar.config import HarnessConfig

_KINDS = ("edit", "investigate", "test_only")

# The whole protocol is one forced function call with one enum argument — the provider
# validates the shape, so there is no prose to parse.
_CLASSIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "set_task_mode",
        "description": "Declare what kind of task the user's new request is.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(_KINDS),
                    "description": (
                        "edit: create or change project files. "
                        "investigate: answer/explain/diagnose without changing files. "
                        "test_only: add or change tests only."
                    ),
                }
            },
            "required": ["kind"],
        },
    },
}

_SYSTEM = (
    "You route requests for a coding agent. Classify the NEW request in the context of "
    "the conversation: a follow-up that continues build work is 'edit' even when phrased "
    "conversationally. Call set_task_mode exactly once."
)

_MAX_HISTORY_LINES = 8


class ModeClassifier:
    """Classifies a goal into a `task_kind` with one cheap, schema-constrained call.

    Args:
        config: Harness config; `classifier_model` names the (cheap) model to use and
            `base_url`/`api_key` are shared with the main endpoint.
        client: An injected OpenAI-compatible client, or `None` to build one lazily on
            first use (mirrors `OpenAIModelClient` — credentials are call-time only).
    """

    def __init__(self, config: HarnessConfig, client: Any = None) -> None:
        self.config = config
        self._client = client

    def _ensure_client(self) -> Any:
        """Return the OpenAI-compatible client, constructing it on first use.

        Returns:
            The injected client, or one constructed from `config` on first call.
        """
        if self._client is None:
            from openai import OpenAI  # noqa: PLC0415 — lazy: `openai` is an optional extra

            self._client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
        return self._client

    def classify(self, prompt: str, history: Sequence[str] = ()) -> str | None:
        """Classify `prompt` (in conversation context) into a task kind, or `None`.

        `None` means "no usable verdict" — endpoint error, junk reply, unknown kind —
        and the caller falls back to the heuristic. The classifier can degrade the
        routing quality, never the goal itself.

        Args:
            prompt: The user's new request.
            history: Recent conversation lines (newest last) for context.

        Returns:
            One of `edit`/`investigate`/`test_only`, or `None` when unusable.
        """
        context = "\n".join(list(history)[-_MAX_HISTORY_LINES:])
        user = f"Conversation so far:\n{context or '(none)'}\n\nNEW request: {prompt}"
        try:
            response = self._ensure_client().chat.completions.create(
                model=self.config.classifier_model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user},
                ],
                tools=[_CLASSIFY_TOOL],
                tool_choice={"type": "function", "function": {"name": "set_task_mode"}},
                temperature=0,
            )
            calls = getattr(response.choices[0].message, "tool_calls", None)
            if not calls:
                return None
            kind = json.loads(calls[0].function.arguments or "{}").get("kind")
        except Exception:  # any failure degrades to the heuristic, never blocks a goal
            return None
        return kind if kind in _KINDS else None
