#!/usr/bin/env python3
"""Minimal OpenAI API-compatible terminal chatbot.

Examples:
    python scripts/chatbot.py --model openai/gpt-4o-mini
    python scripts/chatbot.py --base-url https://openrouter.ai/api/v1
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable

from openai import OpenAI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.getenv("CHATBOT_MODEL", "openai/gpt-4o-mini"),
        help="Model name (default: %(default)s or CHATBOT_MODEL).",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL") or os.getenv("AVATAR_BASE_URL"),
        help="OpenAI-compatible base URL (default: OPENAI_BASE_URL/AVATAR_BASE_URL).",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("OPENAI_API_KEY") or os.getenv("AVATAR_API_KEY"),
        help="API key (default: OPENAI_API_KEY/AVATAR_API_KEY).",
    )
    parser.add_argument(
        "--system",
        default="You are a helpful assistant.",
        help="System prompt.",
    )
    return parser


def _render_text_parts(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Iterable):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
            else:
                text = getattr(item, "text", None)
                item_type = getattr(item, "type", None)
                if item_type == "text" and text:
                    parts.append(str(text))
        if parts:
            return "\n".join(parts)
    return str(content)


def main() -> int:
    args = _build_parser().parse_args()

    if not args.api_key:
        print("Missing API key. Set OPENAI_API_KEY (or pass --api-key).", file=sys.stderr)
        return 2

    client = OpenAI(api_key=args.api_key, base_url=args.base_url)

    messages: list[dict[str, str]] = [{"role": "system", "content": args.system}]

    print("Chatbot ready. Type /exit to quit.")
    while True:
        try:
            user_text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return 0

        if not user_text:
            continue
        if user_text.lower() in {"/exit", "/quit", "exit", "quit"}:
            print("bye")
            return 0

        messages.append({"role": "user", "content": user_text})

        try:
            response = client.chat.completions.create(model=args.model, messages=messages)
        except Exception as exc:  # noqa: BLE001
            print(f"error> {exc}", file=sys.stderr)
            continue

        assistant_msg = response.choices[0].message
        assistant_text = _render_text_parts(assistant_msg.content)
        print(f"bot> {assistant_text}")
        messages.append({"role": "assistant", "content": assistant_text})


if __name__ == "__main__":
    raise SystemExit(main())
