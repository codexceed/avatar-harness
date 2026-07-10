"""Shell-syntax boundary: vet command strings the way the harness executes them (ADR-0045).

`Workspace.run` executes a single argv with NO shell (`shlex.split` + `subprocess.run`),
so shell operators are never operators — a `&&` chain runs its FIRST program with the
rest of the line as literal arguments, and a heredoc blocks forever on stdin that no
shell will ever feed. One dogfood journal (`tetris_glm/events/be46ea…jsonl`) showed both
ends of that failure: a declared grep chain that verification passed vacuously (later
patterns became unopenable *filenames*; `grep -q` exit-0'd on the first match anyway)
and a declared heredoc that hung to timeout and fed a finalization spiral.

This module is the shared gate every model-authored command boundary applies BEFORE a
string reaches `Workspace.run`: `&&` normalizes to a conjunction of single-argv segments
(matching the planner's per-segment classification, §12/ADR-0044); every other operator
is a legible, model-correctable rejection (§10) — never a silent mangle.
"""

import shlex

# The character class `shlex` (punctuation_chars=True) lexes into standalone operator
# tokens; a token drawn entirely from it is shell syntax, not an argument. Quoting
# protects operators only when mixed with other text (`python -c "a; b()"` stays one
# argument); an argument that IS a bare quoted operator (`grep -q '&&' f`) loses its
# quotes to posix lexing and becomes indistinguishable from the real operator — those
# are detected separately (quote-preserving probe below) and rejected legibly rather
# than silently mis-split (PR #112 review).
_PUNCTUATION = frozenset("();<>|&")
_QUOTES = ("'", '"')


def _quoted_operator(command: str) -> str | None:
    """The first argument that is a bare quoted shell operator (`'&&'`, `";"`), if any.

    Posix lexing strips quotes, making such an argument indistinguishable from the real
    operator downstream — this quote-preserving probe catches it while the quotes still
    show (PR #112 review).

    Args:
        command: The raw command string, before posix lexing.

    Returns:
        The offending token with its quotes, or `None`.
    """
    probe = shlex.shlex(command, posix=False, punctuation_chars=True)
    probe.whitespace_split = True
    try:
        for raw in probe:
            unquoted = len(raw) > 1 and raw[0] in _QUOTES and raw[-1] == raw[0]
            inner = raw[1:-1] if unquoted else ""
            if inner and all(ch in _PUNCTUATION for ch in inner):
                return raw
    except ValueError:
        return None  # unbalanced quotes — the posix pass reports it legibly
    return None


def argv_segments(command: str) -> tuple[list[str], str]:
    """Split `command` on `&&` into single-argv segments, rejecting other shell operators.

    Args:
        command: The model-supplied, shell-style command string.

    Returns:
        `(segments, "")` — each segment re-joined via `shlex.join`, argv-equivalent to
        the original — or `([], reason)` naming the unrunnable construct.
    """
    quoted = _quoted_operator(command)
    if quoted is not None:
        return [], (
            f"quoted shell-operator argument {quoted} is not supported: after "
            "quote removal it is indistinguishable from the real operator"
        )
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError as exc:  # unbalanced quotes etc. — legible, not a raise
        return [], f"unparseable command: {exc}"
    return _split_on_conjunction(tokens)


def _split_on_conjunction(tokens: list[str]) -> tuple[list[str], str]:
    """Group posix-lexed `tokens` into `&&`-separated segments, rejecting other operators.

    Args:
        tokens: The command's tokens, operators standalone (punctuation_chars lexing).

    Returns:
        `(segments, "")`, or `([], reason)` on an operator token or an empty segment.
    """
    segments: list[str] = []
    current: list[str] = []
    for word in tokens:
        if word == "&&":
            if not current:
                return [], "empty command segment around '&&'"
            segments.append(shlex.join(current))
            current = []
        elif word and all(ch in _PUNCTUATION for ch in word):
            return [], (
                f"shell operator {word!r} cannot work here: commands execute as a single argv without a shell"
            )
        else:
            current.append(word)
    if current:
        segments.append(shlex.join(current))
    elif segments:  # trailing `&&` with nothing after it
        return [], "empty command segment around '&&'"
    if not segments:
        return [], "empty command"
    return segments, ""
