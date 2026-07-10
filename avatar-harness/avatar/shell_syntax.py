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
# tokens; a token drawn entirely from it is shell syntax, not an argument. Quoted
# occurrences never surface as such tokens, so `python -c "a; b()"` stays one argument.
_PUNCTUATION = frozenset("();<>|&")


def argv_segments(command: str) -> tuple[list[str], str]:
    """Split `command` on `&&` into single-argv segments, rejecting other shell operators.

    Args:
        command: The model-supplied, shell-style command string.

    Returns:
        `(segments, "")` — each segment re-joined via `shlex.join`, argv-equivalent to
        the original — or `([], reason)` naming the unrunnable construct.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError as exc:  # unbalanced quotes etc. — legible, not a raise
        return [], f"unparseable command: {exc}"
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
    if tokens and tokens[-1] == "&&":
        return [], "empty command segment around '&&'"
    if current:
        segments.append(shlex.join(current))
    if not segments:
        return [], "empty command"
    return segments, ""
