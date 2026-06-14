"""Functional success probe for `create-chatbot` — a turn must round-trip.

Usage: ``python chatbot_smoke.py <entry_file>`` with the scratch repo as cwd. The entry file
is named by the task (the prompt tells the agent the filename), so there is no discovery
guesswork — the probe runs exactly that file. It injects a fake ``openai`` module, feeds user
lines on stdin, runs ``<entry_file>`` as ``__main__``, and exits 0 iff the fake client
received a chat/completions call (a turn actually round-tripped). Strict by design: "parses +
imports a client" is not enough — the dogfood showed scripts that look right but never run a turn.

Exit codes: 0 = a turn round-tripped; 1 = missing entry file / no call observed.
"""

import contextlib
import io
import runpy
import sys
import types
from pathlib import Path

_calls: list[object] = []
_REPLY = "Hello from the mock."  # the canned assistant reply; a passing chatbot prints it


def _make_response() -> object:
    """Build a response that satisfies both modern and legacy access shapes.

    Returns:
        An object supporting ``.choices[0].message.content`` and ``["choices"][0]...``.
    """

    class _Duck(dict):
        def __getattr__(self, key: str) -> object:
            return self[key]

    return _Duck(choices=[_Duck(message=_Duck(content=_REPLY), text=_REPLY)])


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


def _target_script() -> Path | None:
    """The entry file named on the command line, resolved against cwd (the scratch repo).

    Returns:
        The entry file path if it was named and exists, else `None`.
    """
    if len(sys.argv) < 2:
        return None
    candidate = Path.cwd() / sys.argv[1]
    return candidate if candidate.is_file() else None


def main() -> int:
    """Run the probe.

    A turn must genuinely round-trip: the client is called AND the assistant's reply is printed.
    (Requiring the reply in stdout rejects a script that fires a call but crashes before using it.)

    Returns:
        0 if a turn round-tripped against the mocked client, else 1.
    """
    _install_fake_openai()
    script = _target_script()
    if script is None:
        print(f"probe: entry file not found (expected argv[1]; got {sys.argv[1:]})")
        return 1

    sys.stdin = io.StringIO("hello\nquit\nexit\n")
    out = io.StringIO()
    raised: Exception | None = None
    try:
        with contextlib.redirect_stdout(out):
            runpy.run_path(str(script), run_name="__main__")
    except (EOFError, SystemExit, KeyboardInterrupt):
        pass
    except Exception as exc:  # printed reply still counts as a round-trip; checked below
        raised = exc

    printed_reply = _REPLY in out.getvalue()
    if _calls and printed_reply:
        print(f"probe: round-trip ok ({len(_calls)} call(s), reply printed) via {script.name}")
        return 0
    if not _calls:
        reason = "no chat/completions call observed"
    elif raised is not None:
        reason = f"call fired but the script crashed before printing the reply ({raised})"
    else:
        reason = "call fired but the reply was not printed"
    print(f"probe: {reason} in {script.name}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
