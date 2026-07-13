r"""Functional success probe for `tetris-easy` (formerly `tetris-tui`) — plays the game the way a human would.

Usage: ``python tetris_easy_smoke.py <entry_file>`` with the scratch repo as cwd. The probe
drives the deliverable's pinned scripted mode (``--no-raw --seed N``: turn-based, one frame
per recognized key, ``-- end frame --`` sentinel, ``.``/``#``/``@`` glyphs) by writing real
ANSI arrow-key bytes to stdin and parsing the rendered frames — the same surface a human
sees. It never imports the game or uses any model-provided hook: the rendered UI is the
only grading surface (ADR-0011/0040), and every assertion is differential (frame N vs
frame N+1) against the task-pinned contract. Phases, each a fresh game process:

    0 readme     README.md exists and documents the pinned contract (both run modes, the
                 key table, the frame sentinel, scoring values, board size, bag RNG)
    1 boot       initial frame: geometry (10x20 bordered), Score: 0, Lines: 0, Next
                 letter, exactly 4 active `@` cells shaped as the seed's first bag piece
    2 movement   LEFT/RIGHT/DOWN translate the active cells exactly; +1 soft-drop point;
                 LEFT x12 clamps at the wall without crashing
    3 rotation   UP steps the piece through its clockwise orientations (4 x UP returns it
                 exactly to the spawn cells; O is a no-op) — no wall kicks, no drift
    4 drop       SPACE locks the piece flush at the floor (`@` -> `#`), awards exactly
                 2 x rows descended, spawns the previewed piece, advances Next per the bag
    5 quit       `q` exits 0 promptly, no traceback
    6 game over  hard drops with no steering top out: `GAME OVER` after the final frame,
                 exit 0, and no line ever clears (a centered stack cannot fill width 10)
    7 scoring    interactive planner reads each spawned piece from the frame, rotates it
                 to a flat-bottomed orientation, and packs the bottom row left-to-right
                 (parking S/Z on top of already-covered columns); when the row fills it
                 must vanish, `Lines: 1`, and the score must equal the probe's exact
                 running ledger (2 x every descent + 100 for the single clear)
    8 present.   the INTERACTIVE mode (`python3 tetris.py`, no flags) runs on a real
                 pseudo-terminal: the reconstructed screen must show vertically aligned
                 board rows (no raw-mode `\n` staircase) and `q` must end the process —
                 presentation only, no gameplay assertions against the timer-driven mode

The probe keeps its own independent copy of the pinned tetromino shapes, rotation rule,
and 7-bag RNG (`random.Random(seed)`, shuffle per refill, drawn in order) — so a game
that deviates from the pinned contract fails even if it is internally self-consistent.
Phase 7's planner is adaptive (it reads spawn position/orientation from the frames), so
it is robust to any compliant spawn choice.

Platform note: phase 8 uses Unix pty facilities (os.openpty/termios/fcntl; imported at
module top), so the probe is Unix-only — matching where evals run today.

Exit codes: 0 = every phase passed; 1 = a phase failed (reason printed).
"""

import contextlib
import fcntl
import os
import random
import re
import select
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

_WIDTH = 10
_HEIGHT = 20
_SENTINEL = "-- end frame --"
_BORDER = "+" + "-" * _WIDTH + "+"
_SEED = 42

_LEFT = b"\x1b[D"
_RIGHT = b"\x1b[C"
_DOWN = b"\x1b[B"
_UP = b"\x1b[A"
_SPACE = b" "
_QUIT = b"q"

_LINE_SCORES = {1: 100, 2: 300, 3: 500, 4: 800}

# The probe's independent copy of the pinned spawn shapes ((row, col) offsets, row 0 on top).
_SHAPES = {
    "I": ((0, 0), (0, 1), (0, 2), (0, 3)),
    "O": ((0, 0), (0, 1), (1, 0), (1, 1)),
    "T": ((0, 1), (1, 0), (1, 1), (1, 2)),
    "S": ((0, 1), (0, 2), (1, 0), (1, 1)),
    "Z": ((0, 0), (0, 1), (1, 1), (1, 2)),
    "J": ((0, 0), (1, 0), (1, 1), (1, 2)),
    "L": ((0, 2), (1, 0), (1, 1), (1, 2)),
}

_PHASE_DEADLINE_SECONDS = 60.0
# Phase 7 reads frames one key at a time; a healthy game answers in milliseconds, and a game
# that slurps stdin to EOF never answers — this bounds how long the probe waits to tell the two
# apart (kept well under the phase watchdog so the diagnosis is the legible one).
_FRAME_DEADLINE_SECONDS = 15.0
# Phase 8 watches the INTERACTIVE mode draw on a real pseudo-terminal: capture this long
# (a few gravity ticks' worth of redraws), then send `q` and give it this long to exit.
_PRESENTATION_CAPTURE_SECONDS = 2.0
_PRESENTATION_EXIT_SECONDS = 8.0

# What README.md must document (mirrors the goal's explicit README checklist), as
# case-insensitive regexes with a legible name each. Matched against a NORMALIZED text
# (lowercased, markdown emphasis/backticks stripped), and worded semantically rather than
# literally: the check is "is this documented", not "did the author type this exact string" —
# two development cells failed here on wording alone ("Size: **10** columns x **20** rows";
# arrow keys documented as their `ESC [ D` byte forms), which is the false-rejection failure
# mode a lower-bound gate must not have.
_README_REQUIREMENTS: list[tuple[str, str]] = [
    ("the scripted mode flag --no-raw", r"--no-raw"),
    ("the --seed flag", r"--seed"),
    ("the run entry point", r"tetris\.py"),
    # Arrow evidence: the word, the escape-byte forms, or literal arrow glyphs (a matrix
    # cell documented the keys as "Left (`←`)" — the most human notation there is).
    ("the arrow keys", "arrow|esc\\s*\\[\\s*[abcd]|x1b\\[|[←-↓]"),
    ("the Left key", r"left"),
    ("the Right key", r"right"),
    ("the Down key (soft drop)", r"down|soft[\s-]?drop"),
    ("the rotate key", r"rotat"),
    ("the drop key (space)", r"space"),
    ("the quit key", r"quit|\bq\b"),
    ("the cell glyphs (. / # / @)", r"@"),
    ("the GAME OVER behavior", r"game\s*over"),
    ("the frame sentinel", r"--\s*end\s*frame\s*--"),
    # Known laxity, on purpose: the score values substring-match ("1000" satisfies "100") —
    # a fail-open lower-bound gate; tightening it risks the false-rejection regression this
    # list has already produced three times.
    ("the single-line clear score (100)", r"100"),
    ("the double-line clear score (300)", r"300"),
    ("the triple-line clear score (500)", r"500"),
    ("the tetris clear score (800)", r"800"),
    ("the board size (10 by 20)", r"(?<!\d)10\D{0,20}20(?!\d)"),
    ("the 7-bag randomizer", r"bag"),
]


def _norm(cells: frozenset[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    """Normalize a cell set to its bounding-box origin."""
    min_r = min(r for r, _ in cells)
    min_c = min(c for _, c in cells)
    return frozenset((r - min_r, c - min_c) for r, c in cells)


def _rotate_cw(cells: frozenset[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    """The pinned clockwise rotation: (r, c) -> (c, maxRow - r), re-normalized."""
    max_r = max(r for r, _ in cells)
    return _norm(frozenset((c, max_r - r) for r, c in cells))


def _bag_sequence(seed: int, count: int) -> list[str]:
    """The pinned 7-bag draw order: shuffle a fresh IJLOSTZ bag per refill, draw in order."""
    rng = random.Random(seed)  # noqa: S311 — the pinned game randomizer, not cryptography
    out: list[str] = []
    bag: list[str] = []
    while len(out) < count:
        if not bag:
            bag = list("IJLOSTZ")
            rng.shuffle(bag)
        out.append(bag.pop(0))
    return out


def _classify(active: frozenset[tuple[int, int]]) -> str | None:
    """Name the piece whose SOME orientation matches these cells (None if no piece does)."""
    shape = _norm(active)
    for letter, spawn in _SHAPES.items():
        candidate = _norm(frozenset(spawn))
        for _ in range(4):
            if candidate == shape:
                return letter
            candidate = _rotate_cw(candidate)
    return None


def _flat_options(letter: str) -> list[tuple[int, int, frozenset[tuple[int, int]]]]:
    """(width, rotations-from-spawn, shape) for orientations whose bottom edge is flat.

    A flat-bottomed orientation has every column's lowest cell on the bounding box's
    bottom row, so hard-dropping it onto uncovered floor fills that many bottom-row
    columns without holes. S and Z have none (their parking is handled separately).
    """
    options: list[tuple[int, int, frozenset[tuple[int, int]]]] = []
    shape = _norm(frozenset(_SHAPES[letter]))
    seen: set[frozenset[tuple[int, int]]] = set()
    for k in range(4):
        if shape not in seen:
            seen.add(shape)
            max_r = max(r for r, _ in shape)
            cols = {c for _, c in shape}
            if all(max(r for r, cc in shape if cc == c) == max_r for c in cols):
                options.append((len(cols), k, shape))
        shape = _rotate_cw(shape)
    return options


class _Frame:
    """One parsed scripted-mode frame."""

    def __init__(self, score: int, lines: int, next_letter: str, active: frozenset, locked: frozenset):
        self.score = score
        self.lines = lines
        self.next_letter = next_letter
        self.active = active
        self.locked = locked


def _parse_frame(text: str) -> _Frame | str:  # noqa: C901 — a flat parse gauntlet; each defect returns its reason
    """Parse one frame's text; return a reason string when malformed."""
    lines = [line.rstrip("\r") for line in text.splitlines()]
    score_m = lines_m = next_m = None
    for line in lines:
        score_m = score_m or re.fullmatch(r"Score:\s*(\d+)", line.strip())
        lines_m = lines_m or re.fullmatch(r"Lines:\s*(\d+)", line.strip())
        next_m = next_m or re.fullmatch(r"Next:\s*([IJLOSTZ])", line.strip())
    try:
        top = lines.index(_BORDER)
    except ValueError:
        return f"frame is missing the {_BORDER!r} border"
    grid = lines[top + 1 : top + 1 + _HEIGHT]
    if len(grid) != _HEIGHT or lines[top + 1 + _HEIGHT : top + 2 + _HEIGHT] != [_BORDER]:
        return f"playfield is not exactly {_HEIGHT} bordered rows"
    if score_m is None or lines_m is None or next_m is None:
        return "frame is missing a 'Score: N', 'Lines: N', or 'Next: <letter>' line"
    active: set[tuple[int, int]] = set()
    locked: set[tuple[int, int]] = set()
    for r, row in enumerate(grid):
        if len(row) != _WIDTH + 2 or row[0] != "|" or row[-1] != "|":
            return f"playfield row {r} is not '|' + {_WIDTH} cells + '|': {row!r}"
        for c, glyph in enumerate(row[1:-1]):
            if glyph == "@":
                active.add((r, c))
            elif glyph == "#":
                locked.add((r, c))
            elif glyph != ".":
                return f"unexpected cell glyph {glyph!r} at ({r}, {c})"
    return _Frame(
        int(score_m.group(1)), int(lines_m.group(1)), next_m.group(1), frozenset(active), frozenset(locked)
    )


def _split_frames(output: str) -> tuple[list[str], str]:
    """Split raw stdout on the sentinel; returns (frame texts, trailing text)."""
    parts = output.split(_SENTINEL + "\n")
    if len(parts) == 1:
        parts = output.split(_SENTINEL)
    return parts[:-1], parts[-1]


def _launch(entry: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, entry, "--no-raw", "--seed", str(_SEED)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _drive(entry: str, keys: bytes) -> tuple[list[_Frame] | str, int, str, str]:
    """Batch mode: write every key, close stdin, parse all frames.

    Returns (frames-or-reason, exit code, stdout tail, stderr).
    """
    proc = _launch(entry)
    try:
        out_b, err_b = proc.communicate(input=keys, timeout=_PHASE_DEADLINE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return "the game did not exit after its input was consumed (hung process)", -1, "", ""
    out = out_b.decode("utf-8", errors="replace")
    err = err_b.decode("utf-8", errors="replace")
    texts, tail = _split_frames(out)
    frames: list[_Frame] = []
    for i, text in enumerate(texts):
        parsed = _parse_frame(text)
        if isinstance(parsed, str):
            return f"frame {i}: {parsed}", proc.returncode, tail, err
        frames.append(parsed)
    return frames, proc.returncode, tail, err


def _shift(cells: frozenset[tuple[int, int]], dr: int, dc: int) -> frozenset[tuple[int, int]]:
    return frozenset((r + dr, c + dc) for r, c in cells)


def _drop_distance(active: frozenset[tuple[int, int]], locked: frozenset[tuple[int, int]]) -> int:
    """How far the active piece falls before resting (probe-side physics for the ledger)."""
    d = 0
    while True:
        moved = _shift(active, d + 1, 0)
        if any(r >= _HEIGHT for r, _ in moved) or moved & locked:
            return d
        d += 1


def _settle(locked: frozenset[tuple[int, int]]) -> tuple[frozenset[tuple[int, int]], int]:
    """Clear full rows the pinned way; returns (new board, rows cleared)."""
    full = [r for r in range(_HEIGHT) if all((r, c) in locked for c in range(_WIDTH))]
    if not full:
        return locked, 0
    kept = {(r, c) for r, c in locked if r not in full}
    return frozenset((r + sum(1 for f in full if f > r), c) for r, c in kept), len(full)


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #
def _check_readme(_entry: str) -> str | None:
    """Phase 0: README.md documents the pinned contract."""
    readme = Path.cwd() / "README.md"
    if not readme.is_file():
        return "README.md not found at the repo root"
    # Normalize: markdown emphasis/backticks split words the requirements match on
    # ("**10** columns" -> "10 columns") — strip them before searching.
    text = re.sub(r"[*_`]", "", readme.read_text(encoding="utf-8", errors="replace").lower())
    missing = [name for name, pattern in _README_REQUIREMENTS if not re.search(pattern, text)]
    if missing:
        return f"README.md does not document: {', '.join(missing)}"
    return None


def _check_boot(entry: str) -> str | None:  # noqa: PLR0911 — a flat step gauntlet; each failure returns its reason
    """Phase 1: the initial frame is well-formed and shows the seed's first piece."""
    frames, code, _tail, err = _drive(entry, _QUIT)
    if isinstance(frames, str):
        return f"boot: {frames} (stderr tail: {err.strip()[-300:]!r})"
    if not frames:
        return "boot: no initial frame was rendered before the first input"
    first = frames[0]
    if first.score != 0 or first.lines != 0:
        return f"boot: initial frame shows Score: {first.score} / Lines: {first.lines}, expected 0 / 0"
    if len(first.active) != 4:
        return f"boot: expected exactly 4 active '@' cells, found {len(first.active)}"
    expected = _bag_sequence(_SEED, 2)
    if _norm(first.active) != _norm(frozenset(_SHAPES[expected[0]])):
        return (
            f"boot: the first piece's cells {sorted(first.active)} do not match the pinned "
            f"7-bag first draw for seed {_SEED} ({expected[0]}, in spawn orientation)"
        )
    if first.next_letter != expected[1]:
        return f"boot: 'Next: {first.next_letter}' is not the pinned bag's second draw ({expected[1]})"
    if code != 0:
        return f"boot: exit code {code} after 'q', expected 0"
    return None


def _check_movement(entry: str) -> str | None:  # noqa: PLR0911 — a flat step gauntlet; each failure returns its reason
    """Phase 2: arrows translate the active piece exactly; the wall clamps."""
    # No trailing 'q': whether a game renders a farewell frame for `q` is a compliant
    # implementation choice (the goal doesn't pin it), so count-sensitive phases end on
    # EOF instead — matrix cells from two models failed here on that extra frame alone.
    # The goal is equally silent about a farewell frame ON EOF, so tolerate exactly one
    # trailing extra frame too (assertions index a strict prefix; same artifact shape).
    keys = _LEFT + _RIGHT + _DOWN + _LEFT * 12 + _RIGHT
    frames, _code, _tail, err = _drive(entry, keys)
    if isinstance(frames, str):
        return f"movement: {frames} (stderr tail: {err.strip()[-300:]!r})"
    if len(frames) not in (17, 18):
        return f"movement: expected 17 frames (1 initial + 16 keys, + <=1 EOF farewell), got {len(frames)}"
    frames = frames[:17]
    f0 = frames[0]
    checks = [
        ("after LEFT", frames[1].active, _shift(f0.active, 0, -1)),
        ("after LEFT then RIGHT", frames[2].active, f0.active),
        ("after DOWN (soft drop)", frames[3].active, _shift(f0.active, 1, 0)),
    ]
    for label, got, expected in checks:
        if got != expected:
            return f"movement: {label}, active cells are {sorted(got)}, expected {sorted(expected)}"
    if frames[3].score != f0.score + 1:
        return f"movement: a soft drop scored {frames[3].score - f0.score} points, expected exactly 1"
    clamped = frames[15]
    if min(c for _, c in clamped.active) != 0:
        return "movement: after 12 LEFTs the piece is not flush against the left wall"
    if _norm(clamped.active) != _norm(f0.active):
        return "movement: the piece's shape changed while clamping at the wall"
    if frames[-1].active != _shift(clamped.active, 0, 1):
        return "movement: RIGHT after wall-clamping did not move the piece back one column"
    return None


def _check_rotation(entry: str) -> str | None:
    """Phase 3: UP steps through the pinned clockwise orientations and cycles home."""
    frames, _code, _tail, err = _drive(entry, _UP * 4)  # EOF-terminated: see movement note
    if isinstance(frames, str):
        return f"rotation: {frames} (stderr tail: {err.strip()[-300:]!r})"
    if len(frames) not in (5, 6):
        return f"rotation: expected 5 frames (1 initial + 4 UPs, + at most 1 EOF farewell), got {len(frames)}"
    frames = frames[:5]
    letter = _bag_sequence(_SEED, 1)[0]
    expected_shape = _norm(frozenset(_SHAPES[letter]))
    for step, frame in enumerate(frames):
        if step > 0:
            expected_shape = _rotate_cw(expected_shape)
        if len(frame.active) != 4:
            return f"rotation: after {step} UPs the piece has {len(frame.active)} cells, expected 4"
        if _norm(frame.active) != expected_shape:
            return (
                f"rotation: after {step} UPs the piece shape is {sorted(_norm(frame.active))}, "
                f"expected the pinned clockwise orientation {sorted(expected_shape)}"
            )
    if frames[4].active != frames[0].active:
        return "rotation: four UPs did not return the piece exactly to its spawn cells"
    return None


def _check_drop(entry: str) -> str | None:  # noqa: PLR0911 — a flat step gauntlet; each failure returns its reason
    """Phase 4: SPACE locks flush at the floor, scores 2 x descent, spawns the preview."""
    frames, _code, _tail, err = _drive(entry, _SPACE)  # EOF-terminated: see movement note
    if isinstance(frames, str):
        return f"drop: {frames} (stderr tail: {err.strip()[-300:]!r})"
    if len(frames) not in (2, 3):
        return f"drop: expected 2 frames (initial + SPACE, + at most 1 EOF farewell), got {len(frames)}"
    before, after = frames[:2]
    descent = _drop_distance(before.active, before.locked)
    expected_locked = _shift(before.active, descent, 0)
    if after.locked != expected_locked:
        return (
            f"drop: locked cells after SPACE are {sorted(after.locked)}, expected the piece "
            f"resting on the floor at {sorted(expected_locked)}"
        )
    if after.score != before.score + 2 * descent:
        return f"drop: hard drop scored {after.score - before.score}, expected 2 x {descent} rows"
    if len(after.active) != 4:
        return f"drop: no new 4-cell piece spawned after locking (found {len(after.active)} cells)"
    if _classify(after.active) != before.next_letter:
        return (
            f"drop: the spawned piece is {_classify(after.active)}, but the previous frame "
            f"previewed 'Next: {before.next_letter}'"
        )
    if after.next_letter != _bag_sequence(_SEED, 3)[2]:
        return f"drop: 'Next: {after.next_letter}' does not match the pinned bag's third draw"
    return None


def _check_quit(entry: str) -> str | None:
    """Phase 5: `q` exits 0 promptly with no traceback."""
    frames, code, _tail, err = _drive(entry, _QUIT)
    if isinstance(frames, str):
        return f"quit: {frames}"
    if code != 0:
        return f"quit: 'q' exited with code {code}, expected 0"
    if "Traceback" in err:
        return f"quit: a traceback reached stderr: {err.strip()[-300:]!r}"
    return None


def _check_game_over(entry: str) -> str | None:
    """Phase 6: unsteered hard drops top out into GAME OVER and a clean exit."""
    frames, code, tail, err = _drive(entry, _SPACE * 200)
    if isinstance(frames, str):
        return f"game over: {frames} (stderr tail: {err.strip()[-300:]!r})"
    if code != 0:
        return f"game over: expected a clean exit 0 at top-out, got {code}"
    if "GAME OVER" not in tail:
        return "game over: 200 unsteered hard drops never printed 'GAME OVER' after the final frame"
    if frames[-1].lines != 0:
        return "game over: a centered 4-wide stack cleared a line, which is impossible on a 10-wide board"
    return None


class _InteractiveGame:
    """Phase 7's stepwise driver: one recognized key in, one parsed frame out.

    Reads the game's stdout via ``select`` + ``os.read`` with a per-line deadline (a
    buffered ``readline`` would block forever on a game that reads stdin to EOF before
    responding — the batch phases cannot catch that shape, so this one must, quickly
    and with a legible diagnosis).
    """

    def __init__(self, entry: str):
        self.proc = _launch(entry)
        self.watchdog = threading.Timer(_PHASE_DEADLINE_SECONDS * 2, self.proc.kill)
        self.watchdog.start()
        if self.proc.stdin is None or self.proc.stdout is None:  # unreachable with PIPE; narrows types
            raise RuntimeError("game process launched without stdio pipes")
        self.stdin = self.proc.stdin
        self.out_fd = self.proc.stdout.fileno()
        self.buffer = b""

    def close(self) -> None:
        self.watchdog.cancel()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.communicate()

    def _read_line(self) -> str | None:
        """One stdout line within the frame deadline; None on timeout, '' on EOF."""
        while b"\n" not in self.buffer:
            ready, _, _ = select.select([self.out_fd], [], [], _FRAME_DEADLINE_SECONDS)
            if not ready:
                return None
            chunk = os.read(self.out_fd, 4096)
            if not chunk:
                return ""
            self.buffer += chunk
        raw, self.buffer = self.buffer.split(b"\n", 1)
        return raw.decode("utf-8", errors="replace")

    def read_frame(self) -> _Frame | str:
        lines: list[str] = []
        while True:
            line = self._read_line()
            if line is None:
                return (
                    f"no frame within {_FRAME_DEADLINE_SECONDS:.0f}s of a key — the game may be "
                    f"reading stdin to EOF before responding; scripted mode must process each key "
                    f"as it arrives and flush its frame before reading the next key"
                )
            if line == "":
                return "the game exited (or went silent) mid-play"
            if line == _SENTINEL:
                break
            if "GAME OVER" in line:
                return "the game topped out during the packing plan"
            lines.append(line)
        return _parse_frame("\n".join(lines))

    def send(self, key: bytes) -> _Frame | str:
        try:
            self.stdin.write(key)
            self.stdin.flush()
        except BrokenPipeError:
            return "the game exited while input was still being sent"
        return self.read_frame()


def _check_line_clear(entry: str) -> str | None:  # noqa: C901, PLR0911, PLR0912 — a flat step gauntlet; each failure returns its reason
    """Phase 7: pack the bottom row left-to-right; the clear must score exactly."""
    game = _InteractiveGame(entry)
    try:
        frame = game.read_frame()
        if isinstance(frame, str):
            return f"line clear: {frame}"
        covered = 0
        expected_score = 0
        for placement in range(12):
            letter = _classify(frame.active)
            if letter is None:
                return f"line clear: unrecognizable piece cells {sorted(frame.active)}"
            remaining = _WIDTH - covered
            options = [(w, k) for w, k, _ in _flat_options(letter) if w <= remaining]
            preferred = [(w, k) for w, k in options if remaining - w != 1]
            fits = max(preferred or options, default=None, key=lambda wk: wk[0])
            if fits is None:
                # Park (S/Z, or nothing fits): stand it upright over already-covered columns.
                slim = min(_flat_options(letter) or [(0, 0, frozenset())], key=lambda o: o[0])
                rotations = slim[1] if slim[0] else 1  # S/Z: one CW turn is their slimmest (2 cols)
                target = 0
            else:
                _, rotations = fits
                target = covered
            for _ in range(rotations):
                frame = game.send(_UP)
                if isinstance(frame, str):
                    return f"line clear: {frame}"
            current_min = min(c for _, c in frame.active)
            if current_min > target:
                step, count = _LEFT, current_min - target
            else:
                step, count = _RIGHT, target - current_min
            for _ in range(abs(count)):
                frame = game.send(step)
                if isinstance(frame, str):
                    return f"line clear: {frame}"
            if min(c for _, c in frame.active) != target:
                return (
                    f"line clear: placement {placement} ({letter}) could not be steered to "
                    f"column {target} (piece sits at {sorted(frame.active)})"
                )
            descent = _drop_distance(frame.active, frame.locked)
            landing = _shift(frame.active, descent, 0)
            board, cleared = _settle(frame.locked | landing)
            expected_score += 2 * descent + (_LINE_SCORES[cleared] if cleared else 0)
            frame = game.send(_SPACE)
            if isinstance(frame, str):
                return f"line clear: {frame}"
            if frame.locked != board:
                return (
                    f"line clear: after placement {placement} ({letter}) the board is "
                    f"{sorted(frame.locked)}, expected {sorted(board)}"
                )
            if frame.score != expected_score:
                return (
                    f"line clear: after placement {placement} the score is {frame.score}, "
                    f"expected {expected_score} (2 x every descent, +100 for a single clear)"
                )
            if cleared:
                if frame.lines != 1:
                    return f"line clear: the row cleared but 'Lines:' shows {frame.lines}, expected 1"
                return None
            if fits is not None:
                covered += fits[0]
        return "line clear: the bottom row never filled within 12 placements"
    finally:
        game.close()


# --------------------------------------------------------------------------- #
# Phase 8 — terminal presentation of the INTERACTIVE mode (the human surface)
# --------------------------------------------------------------------------- #
_CSI_FINAL = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ`abcdefghijklmnopqrstuvwxyz{|}~"


def _emulate_screen(data: bytes) -> dict[int, str]:  # noqa: C901 — a character-at-a-time terminal state machine
    r"""Reconstruct what a terminal displays from a captured pty byte stream.

    A deliberately tiny emulator: `\r`/`\n`/backspace/tab cursor motion, CSI cursor
    positioning (`H`/`f` — what curses-style UIs use), every other escape ignored. Known
    coarseness in the "ignored" bucket: non-CSI escapes are consumed as ESC + one char, so a
    3-byte charset sequence like `ESC ( B` leaves one stray printable in the grid — harmless
    for the alignment assertion (curses positions rows via CSI `H`), but keep it in mind
    before tightening these checks. This is
    what makes the check fair across implementation styles: cooked-mode output, explicit
    `\r\n` writers, and cursor-addressed (curses) UIs all reconstruct to aligned rows;
    only genuinely mis-rendering output (e.g. bare `\n` while the tty is in raw mode, the
    "staircase") reconstructs misaligned — exactly what a human sees.
    """
    grid: dict[int, dict[int, str]] = {}
    row = col = 0
    text = data.decode("utf-8", errors="replace")
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\x1b":
            j = i + 1
            if j < len(text) and text[j] == "[":
                j += 1
                start = j
                while j < len(text) and text[j] not in _CSI_FINAL:
                    j += 1
                if j < len(text) and text[j] in "Hf":
                    parts = text[start:j].split(";")
                    nums = [int(p) for p in parts if p.isdigit()]
                    row = (nums[0] - 1) if nums else 0
                    col = (nums[1] - 1) if len(nums) > 1 else 0
                i = j + 1
            else:
                i = j + 1
            continue
        if ch == "\r":
            col = 0
        elif ch == "\n":
            row += 1
        elif ch == "\b":
            col = max(0, col - 1)
        elif ch == "\t":
            col = (col // 8 + 1) * 8
        elif ch.isprintable():
            grid.setdefault(row, {})[col] = ch
            col += 1
        i += 1
    lines: dict[int, str] = {}
    for r, cells in grid.items():
        width = max(cells) + 1
        lines[r] = "".join(cells.get(c, " ") for c in range(width))
    return lines


def _capture_interactive(entry: str) -> tuple[bytes, bool]:
    """Run the interactive mode on a pseudo-terminal; returns (pty bytes, exited-after-q)."""
    master, slave = os.openpty()
    # A sane window, and a FORCED terminal type: the probe owns this pty (it sets the window
    # size too), and the runner's inherited TERM carries no legitimate signal — a CI `dumb`
    # would make curses-style UIs draw nothing and falsely reject a correct game. The
    # terminfo search chain is forced for the same reason: python-build-standalone's bundled
    # ncurses misses Debian's /etc/terminfo and /lib/terminfo, which otherwise kills curses
    # UIs at setupterm on exactly the machines CI runs on (the trailing empty entry keeps
    # the compiled-in default paths).
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    env = {
        **os.environ,
        "TERM": "xterm",
        "TERMINFO_DIRS": "/etc/terminfo:/lib/terminfo:/usr/share/terminfo:",
    }
    proc = subprocess.Popen(
        [sys.executable, entry], stdin=slave, stdout=slave, stderr=subprocess.DEVNULL, env=env
    )
    os.close(slave)
    data = b""

    def drain(until: float) -> None:
        nonlocal data
        while time.monotonic() < until:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # EIO: child side closed
                    return
                if not chunk:
                    return
                data += chunk

    drain(time.monotonic() + _PRESENTATION_CAPTURE_SECONDS)
    with contextlib.suppress(OSError):
        os.write(master, b"q\r")  # raw/cbreak readers see 'q' at once; cooked readers on the CR
    drain(time.monotonic() + _PRESENTATION_EXIT_SECONDS)
    exited = True
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        exited = False
        proc.kill()
        proc.wait()
    os.close(master)
    return data, exited


def _check_presentation(entry: str) -> str | None:
    r"""Phase 8: the interactive mode renders a *usable* board on a real terminal.

    This is presentation-only (no gameplay assertions against the timer-driven mode):
    the reconstructed screen must show vertically aligned playfield rows — the raw-mode
    "staircase" (tty.setraw + bare \n line endings) is the defect this catches — and
    `q` must end the process.
    """
    data, exited = _capture_interactive(entry)
    lines = _emulate_screen(data)
    starts = []
    for text in lines.values():
        match = re.search(r"\|.{10}\|", text)
        if match:
            starts.append(match.start())
    # Diagnose the staircase FIRST (misaligned rows are still "found" rows), then demand the
    # full 20-row board the goal pins — five aligned rows with no game behind them must fail.
    if len(starts) >= 2 and len(set(starts)) != 1:
        return (
            f"presentation: interactive mode staircases on a real terminal — board rows start at "
            f"columns {sorted(set(starts))[:6]}; raw mode (tty.setraw) disables the \\n -> \\r\\n "
            f"translation, so write \\r\\n line endings, use tty.setcbreak, or position the cursor"
        )
    if len(starts) < _HEIGHT:
        return (
            f"presentation: interactive mode (`python3 {entry}`) drew {len(starts)} aligned board "
            f"rows on a real terminal; the goal pins a {_HEIGHT}-row playfield"
        )
    if not exited:
        return f"presentation: interactive mode did not exit within {_PRESENTATION_EXIT_SECONDS:.0f}s of 'q'"
    return None


def main() -> int:
    """Run every phase against the entry file; first failure loses.

    Returns:
        0 if every phase passed, else 1.
    """
    if len(sys.argv) < 2 or not (Path.cwd() / sys.argv[1]).is_file():
        print(f"probe: entry file not found (expected argv[1]; got {sys.argv[1:]})")
        return 1
    entry = sys.argv[1]
    phases = [
        ("readme", _check_readme),
        ("boot", _check_boot),
        ("movement", _check_movement),
        ("rotation", _check_rotation),
        ("drop", _check_drop),
        ("quit", _check_quit),
        ("game over", _check_game_over),
        ("line clear", _check_line_clear),
        ("presentation", _check_presentation),
    ]
    for name, check in phases:
        reason = check(entry)
        if reason is not None:
            print(f"probe: {reason}")
            return 1
        print(f"probe: {name} ok")
    print(
        f"probe: full game ok via {entry} (documented contract, seeded bag boot, exact "
        f"arrow-key translations + wall clamp, clockwise rotation cycle, flush hard drop "
        f"with 2x scoring, clean quit, top-out GAME OVER, a packed bottom-row clear "
        f"scoring exactly 100, and an interactive mode that renders aligned on a real "
        f"terminal)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
