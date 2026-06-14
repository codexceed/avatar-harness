"""Functional success probe for `create-chatbot` — a turn must round-trip.

Run with the scratch repo as cwd. Injects a fake ``openai`` module, feeds a couple of user
lines on stdin, runs the agent's script as ``__main__``, and exits 0 iff the fake client
received a chat/completions call (a turn actually round-tripped). Strict by design:
"parses + imports a client" is not enough — the dogfood showed scripts that look right but
never run a turn.

Exit codes: 0 = a turn round-tripped; 1 = no usable script / no call observed.
"""

import io
import runpy
import sys
import types
from pathlib import Path

_calls: list[object] = []


def _make_response() -> object:
    """Build a response that satisfies both modern and legacy access shapes.

    Returns:
        An object supporting ``.choices[0].message.content`` and ``["choices"][0]...``.
    """

    class _Duck(dict):
        def __getattr__(self, key: str) -> object:
            return self[key]

    return _Duck(choices=[_Duck(message=_Duck(content="Hello from the mock."), text="Hello from the mock.")])


def _record_call(*args: object, **kwargs: object) -> object:
    """Record one chat/completions call and return a canned response.

    Module-level so the stub class bodies below can reference it (a class body cannot
    see an enclosing function's locals).
    """
    _calls.append(kwargs or args)
    return _make_response()


def _install_fake_openai() -> None:
    """Install a fake ``openai`` module recording chat/completions calls (modern + legacy)."""

    class _Completions:
        def create(self, *args: object, **kwargs: object) -> object:
            return _record_call(*args, **kwargs)

    class _Chat:
        def __init__(self) -> None:
            self.completions = _Completions()

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.chat = _Chat()
            self.responses = _Completions()  # newer Responses API surface

    class _ChatCompletion:
        create = staticmethod(_record_call)

    mod = types.ModuleType("openai")
    mod.OpenAI = _Client  # type: ignore[attr-defined]
    mod.Client = _Client  # type: ignore[attr-defined]
    mod.AzureOpenAI = _Client  # type: ignore[attr-defined]
    mod.ChatCompletion = _ChatCompletion  # type: ignore[attr-defined]
    mod.Completion = _ChatCompletion  # type: ignore[attr-defined]
    mod.api_key = ""  # type: ignore[attr-defined]
    sys.modules["openai"] = mod


def _find_script() -> Path | None:
    """Find the agent's chatbot script: a .py in cwd that references an OpenAI client.

    Returns:
        The most likely script, or `None` if none reference openai.
    """
    candidates = [p for p in sorted(Path.cwd().rglob("*.py")) if ".git" not in p.parts]
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "openai" in text or "chat.completions" in text:
            return path
    return None


def main() -> int:
    """Run the probe.

    Returns:
        0 if a turn round-tripped against the mocked client, else 1.
    """
    _install_fake_openai()
    script = _find_script()
    if script is None:
        print("probe: no openai-using script found")
        return 1

    sys.stdin = io.StringIO("hello\nquit\nexit\n")
    try:
        runpy.run_path(str(script), run_name="__main__")
    except (EOFError, SystemExit, KeyboardInterrupt):
        pass
    except Exception as exc:  # a script error after a call still counts; we check _calls below
        print(f"probe: script raised after import: {exc}")

    if _calls:
        print(f"probe: round-trip ok ({len(_calls)} call(s)) via {script.name}")
        return 0
    print(f"probe: no chat/completions call observed in {script.name}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
