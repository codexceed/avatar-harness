#!/usr/bin/env python3
"""Minimal OpenAI API-compatible CLI chatbot.

Examples:
    python scripts/chatbot.py --model gpt-4o-mini
    python scripts/chatbot.py --base-url http://localhost:11434/v1 --model llama3.1

Environment:
    OPENAI_API_KEY    API key for the endpoint (optional for some local servers)
    OPENAI_BASE_URL   Optional API base URL override
"""

from __future__ import annotations

import argparse
import os
import sys


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the chatbot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="Model name to use (default: %(default)s)",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL"),
        help="OpenAI-compatible API base URL (default: OPENAI_BASE_URL)",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("OPENAI_API_KEY"),
        help="API key (default: OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--system",
        default="You are a helpful assistant.",
        help="System prompt used at the start of the conversation.",
    )
    return parser.parse_args()


def main() -> int:
    """Run an interactive chat loop until the user exits."""
    try:
        from openai import OpenAI
    except ImportError:
        print("The `openai` package is required. Install it with: pip install openai", file=sys.stderr)
        return 2

    args = parse_args()
    client = OpenAI(api_key=args.api_key, base_url=args.base_url)

    messages = [{"role": "system", "content": args.system}]

    print("Chatbot started. Type 'exit' or 'quit' to stop.")
    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return 0

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("bye")
            return 0

        messages.append({"role": "user", "content": user_input})

        try:
            response = client.chat.completions.create(
                model=args.model,
                messages=messages,
                temperature=0.7,
            )
        except Exception as exc:
            print(f"error> request failed: {exc}", file=sys.stderr)
            messages.pop()  # remove the failed user turn so retry is clean
            continue

        assistant_text = (response.choices[0].message.content or "").strip()
        if not assistant_text:
            assistant_text = "(empty response)"
        print(f"assistant> {assistant_text}")
        messages.append({"role": "assistant", "content": assistant_text})


if __name__ == "__main__":
    raise SystemExit(main())
