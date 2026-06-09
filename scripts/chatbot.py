"""Tiny interactive chatbot for OpenAI-compatible Chat Completions APIs.

Examples:
    python scripts/chatbot.py --model gpt-4o-mini
    python scripts/chatbot.py --base-url http://localhost:11434/v1 --api-key dummy
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - runtime environment dependent
    OpenAI = None  # type: ignore[assignment]


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Interactive chatbot using OpenAI-compatible APIs."
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        help="Model name to send in chat.completions.create().",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("OPENAI_API_KEY"),
        help="API key (defaults to OPENAI_API_KEY).",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL"),
        help="Base URL for OpenAI-compatible servers (e.g. http://localhost:11434/v1).",
    )
    parser.add_argument(
        "--system",
        default="You are a helpful assistant.",
        help="Initial system prompt.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="Sampling temperature.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable token streaming.",
    )
    return parser.parse_args()


def build_client(api_key: str | None, base_url: str | None) -> Any:
    """Create an OpenAI SDK client with optional OpenAI-compatible base URL."""
    if OpenAI is None:
        raise SystemExit(
            "The 'openai' package is required. Install with: pip install openai"
        )

    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def main() -> int:
    """Run a REPL chatbot session."""
    args = parse_args()
    client = build_client(api_key=args.api_key, base_url=args.base_url)

    messages: list[dict[str, str]] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})

    print("Chatbot started. Type /exit to quit, /clear to reset history.")
    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user_input:
            continue
        if user_input.lower() in {"/exit", "/quit"}:
            return 0
        if user_input.lower() == "/clear":
            messages = [{"role": "system", "content": args.system}] if args.system else []
            print("assistant> (history cleared)")
            continue

        messages.append({"role": "user", "content": user_input})

        try:
            if args.no_stream:
                resp = client.chat.completions.create(
                    model=args.model,
                    messages=messages,
                    temperature=args.temperature,
                )
                text = (resp.choices[0].message.content or "").strip()
                print(f"assistant> {text}")
            else:
                stream = client.chat.completions.create(
                    model=args.model,
                    messages=messages,
                    temperature=args.temperature,
                    stream=True,
                )
                print("assistant> ", end="", flush=True)
                parts: list[str] = []
                for chunk in stream:
                    delta = chunk.choices[0].delta.content or ""
                    if delta:
                        parts.append(delta)
                        print(delta, end="", flush=True)
                print()
                text = "".join(parts).strip()
        except Exception as exc:  # pragma: no cover - network/provider dependent
            print(f"assistant> [error] {exc}", file=sys.stderr)
            messages.pop()
            continue

        messages.append({"role": "assistant", "content": text})


if __name__ == "__main__":
    raise SystemExit(main())
