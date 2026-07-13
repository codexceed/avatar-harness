r"""Lower-bound functional probe for the broad ``tetris-hard`` task (formerly ``tetris-playable``).

The task pins only a small rendered-frame observation seam. This probe treats the game as a
black box and checks gameplay invariants: repeatability, movement, some working rotation,
hard-drop physics, locking, an adaptively played line clear, top-out, and real-terminal
movement/gravity. It does not reproduce a reference RNG, rotation rule, or score table.

Grading conventions (maintainer rulings, 2026-07-12):

- **Hidden rows are legitimate.** Guideline-style games spawn (and may kick) pieces partly
  above the visible field, so a frame may show 1-4 falling cells. Checks assert on the
  visible cells and never demand all four be on screen.
- **Adaptive phases drive by replayed prefix, not a held-open pipe.** The pinned determinism
  ("same seed + key sequence -> same frames") lets the probe relaunch the game with its full
  key history plus the next key and read the frame at that position — so gameplay is graded
  identically for streaming and stdin-slurping implementations.
- **The streaming contract is its own, separately reported phase.** One live check verifies
  the goal's process-keys-as-they-arrive sentence over a held-open pipe. Whether it gates
  the exit code is the one-line policy below (`_STREAMING_GATES`); either way the verdict is
  printed on its own `transport` line so results decompose into gameplay vs transport.
- **GAME OVER may print anywhere in the final output** (inside the last frame or after the
  last sentinel) — the goal pins "before a successful exit", not a position.
"""

import contextlib
import fcntl
import os
import re
import select
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

_WIDTH = 10
_HEIGHT = 20
_SENTINEL = "-- end frame --"
_SEED = 42
_LEFT = b"\x1b[D"
_RIGHT = b"\x1b[C"
_DOWN = b"\x1b[B"
_UP = b"\x1b[A"
_SPACE = b" "
_QUIT = b"q"
_PROCESS_TIMEOUT_SECONDS = 30.0
_STREAM_TIMEOUT_SECONDS = 5.0
_LINE_CLEAR_DEADLINE_SECONDS = 150.0
# Scoring policy for the streaming (transport) phase: gameplay and transport are graded
# independently; flip this to make a streaming failure also fail the probe.
_STREAMING_GATES = False


@dataclass(frozen=True)
class _Frame:
    score: int
    lines: int
    active: frozenset[tuple[int, int]]
    locked: frozenset[tuple[int, int]]


def _parse_frame(text: str) -> _Frame | str:
    lines = [line.rstrip("\r") for line in text.splitlines()]
    score = next((re.fullmatch(r"\s*Score:\s*(\d+)\s*", line) for line in lines if "Score:" in line), None)
    cleared = next((re.fullmatch(r"\s*Lines:\s*(\d+)\s*", line) for line in lines if "Lines:" in line), None)
    rows: list[str] = []
    for line in lines:
        match = re.fullmatch(r"\|([.#@]{10})\|", line)
        if match:
            rows.append(match.group(1))
    if score is None or cleared is None:
        return "frame is missing an integer 'Score: N' or 'Lines: N' line"
    if len(rows) != _HEIGHT:
        return f"frame contains {len(rows)} parseable board rows, expected {_HEIGHT}"
    active: set[tuple[int, int]] = set()
    locked: set[tuple[int, int]] = set()
    for r, row in enumerate(rows):
        for c, glyph in enumerate(row):
            if glyph == "@":
                active.add((r, c))
            elif glyph == "#":
                locked.add((r, c))
    return _Frame(int(score.group(1)), int(cleared.group(1)), frozenset(active), frozenset(locked))


def _split_frames(output: str) -> list[_Frame] | str:
    parts = output.replace("\r\n", "\n").split(_SENTINEL)
    if len(parts) == 1:
        return "stdout contained no frame sentinel"
    frames: list[_Frame] = []
    for index, text in enumerate(parts[:-1]):
        parsed = _parse_frame(text.strip("\n"))
        if isinstance(parsed, str):
            return f"frame {index}: {parsed}"
        frames.append(parsed)
    return frames


def _launch(entry: str, seed: int = _SEED) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, entry, "--no-raw", "--seed", str(seed)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _drive(entry: str, keys: bytes, seed: int = _SEED) -> tuple[list[_Frame] | str, int, str, str]:
    """Batch-run the game: write every key, close stdin, return (frames, exit, stdout, stderr)."""
    proc = _launch(entry, seed)
    try:
        out_b, err_b = proc.communicate(input=keys, timeout=_PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return "game did not exit after input was consumed", -1, "", ""
    output = out_b.decode("utf-8", errors="replace")
    frames = _split_frames(output)
    return frames, proc.returncode, output, err_b.decode("utf-8", errors="replace")


def _shift(cells: frozenset[tuple[int, int]], dr: int, dc: int) -> frozenset[tuple[int, int]]:
    return frozenset((r + dr, c + dc) for r, c in cells)


def _norm(cells: frozenset[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    min_r = min(r for r, _ in cells)
    min_c = min(c for _, c in cells)
    return frozenset((r - min_r, c - min_c) for r, c in cells)


_STANDARD_SHAPES = {
    "I": frozenset({(0, 0), (0, 1), (0, 2), (0, 3)}),
    "O": frozenset({(0, 0), (0, 1), (1, 0), (1, 1)}),
    "T": frozenset({(0, 1), (1, 0), (1, 1), (1, 2)}),
    "S": frozenset({(0, 1), (0, 2), (1, 0), (1, 1)}),
    "Z": frozenset({(0, 0), (0, 1), (1, 1), (1, 2)}),
    "J": frozenset({(0, 0), (1, 0), (1, 1), (1, 2)}),
    "L": frozenset({(0, 2), (1, 0), (1, 1), (1, 2)}),
}


def _rotate_shape(cells: frozenset[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    max_r = max(r for r, _ in cells)
    return _norm(frozenset((c, max_r - r) for r, c in cells))


def _classify_piece(cells: frozenset[tuple[int, int]]) -> str | None:
    candidate = _norm(cells)
    for letter, spawn in _STANDARD_SHAPES.items():
        orientation = spawn
        for _ in range(4):
            if candidate == orientation:
                return letter
            orientation = _rotate_shape(orientation)
    return None


def _connected(cells: frozenset[tuple[int, int]]) -> bool:
    if not cells:
        return False
    seen = {next(iter(cells))}
    pending = list(seen)
    while pending:
        r, c = pending.pop()
        for neighbour in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if neighbour in cells and neighbour not in seen:
                seen.add(neighbour)
                pending.append(neighbour)
    return seen == set(cells)


def _plausible_piece(cells: frozenset[tuple[int, int]]) -> bool:
    """The visible part of a falling piece: 1-4 orthogonally connected cells.

    Fewer than four cells is legitimate — guideline-style games keep spawn rows (and the
    occasional upward kick) above the visible field, so only the piece's bottom part shows.
    """
    return 1 <= len(cells) <= 4 and _connected(cells)


def _spin_ok(cells: frozenset[tuple[int, int]]) -> bool:
    """Validate a mid-rotation piece, which may clip the top edge entirely.

    A vertical I rotated at spawn can sit wholly above the field, so zero visible
    cells is valid here too.
    """
    return len(cells) == 0 or _plausible_piece(cells)


def _drop_distance(active: frozenset[tuple[int, int]], locked: frozenset[tuple[int, int]]) -> int:
    distance = 0
    while True:
        moved = _shift(active, distance + 1, 0)
        if any(r >= _HEIGHT for r, _ in moved) or moved & locked:
            return distance
        distance += 1


def _settle(locked: frozenset[tuple[int, int]]) -> tuple[frozenset[tuple[int, int]], int]:
    full = [r for r in range(_HEIGHT) if all((r, c) in locked for c in range(_WIDTH))]
    if not full:
        return locked, 0
    kept = {(r, c) for r, c in locked if r not in full}
    shifted = {(r + sum(1 for full_row in full if full_row > r), c) for r, c in kept}
    return frozenset(shifted), len(full)


def _check_readme(_entry: str) -> str | None:
    path = Path("README.md")
    if not path.is_file():
        return "README.md not found"
    text = re.sub(r"[*_`]", "", path.read_text(encoding="utf-8", errors="replace").lower())
    requirements = {
        "the run command": r"tetris\.py",
        "arrow-key controls": r"arrow|left|right|up|down|[←-↓]",
        "hard drop": r"space|hard[ -]?drop",
        "quit": r"quit|\bq\b",
    }
    missing = [name for name, pattern in requirements.items() if not re.search(pattern, text)]
    return f"README.md does not document {', '.join(missing)}" if missing else None


def _check_boot_and_determinism(entry: str) -> str | None:  # noqa: PLR0911
    keys = _LEFT + _RIGHT + _UP + _DOWN + _SPACE
    first_run, code, _out, err = _drive(entry, keys)
    second_run, code_2, _out_2, _err_2 = _drive(entry, keys)
    if isinstance(first_run, str):
        return f"boot: {first_run} (stderr tail: {err.strip()[-300:]!r})"
    if isinstance(second_run, str):
        return f"determinism: second run failed to render: {second_run}"
    if code != 0 or code_2 != 0:
        return f"determinism script exited {code} / {code_2}, expected 0 / 0"
    if first_run != second_run:
        return "same seed and key sequence produced different rendered frames"
    if not first_run:
        return "no startup frame was rendered"
    initial = first_run[0]
    if initial.score != 0 or initial.lines != 0:
        return f"initial Score/Lines are {initial.score}/{initial.lines}, expected 0/0"
    if not _plausible_piece(initial.active):
        return f"startup falling piece is not 1-4 orthogonally connected cells: {sorted(initial.active)}"
    return None


def _check_streaming(entry: str) -> str | None:  # noqa: C901
    """The goal's transport sentence: a key on a HELD-OPEN pipe must produce a flushed frame.

    Reported on its own `transport` line; gates the exit code only if `_STREAMING_GATES`.
    """
    proc = _launch(entry)
    if proc.stdin is None or proc.stdout is None:
        proc.kill()
        proc.communicate()
        return "game launched without pipes"
    fd = proc.stdout.fileno()
    buffer = b""

    def read_until_sentinel() -> bool:
        nonlocal buffer
        deadline = time.monotonic() + _STREAM_TIMEOUT_SECONDS
        while _SENTINEL.encode() not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                return False
            buffer += chunk
        buffer = buffer.split(_SENTINEL.encode(), 1)[1]
        return True

    try:
        if not read_until_sentinel():
            return "no startup frame arrived while stdin stayed open"
        try:
            proc.stdin.write(_LEFT)
            proc.stdin.flush()
        except BrokenPipeError:
            return "game exited before any key was sent on a held-open pipe"
        if not read_until_sentinel():
            if proc.poll() is not None:
                return "game exited when a key arrived on a held-open pipe"
            return (
                "no flushed frame arrived after a key on a held-open pipe; input appears buffered until EOF"
            )
        return None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


def _check_movement(entry: str) -> str | None:  # noqa: PLR0911
    frames, _code, _out, err = _drive(entry, _LEFT + _RIGHT + _DOWN)
    if isinstance(frames, str):
        return f"movement: {frames} (stderr tail: {err.strip()[-300:]!r})"
    if len(frames) < 4:
        return f"movement: expected startup plus three key frames, got {len(frames)}"
    initial, left, restored, down = frames[:4]
    if left.active != _shift(initial.active, 0, -1):
        return "movement: Left did not move the falling piece one column left from its spawn"
    if restored.active != initial.active:
        return "movement: Right did not undo the preceding Left"
    # A soft drop may reveal a cell that was hidden above the field: the shifted visible
    # cells must all still be there, and anything new must enter at the top row.
    descended = _shift(initial.active, 1, 0)
    revealed = down.active - descended
    if not descended <= down.active or len(down.active) > 4 or any(r != 0 for r, _ in revealed):
        return "movement: Down did not soft-drop the falling piece one row"
    if down.score < initial.score:
        return "movement: score decreased after a soft drop"
    return None


def _is_square(cells: frozenset[tuple[int, int]]) -> bool:
    shape = _norm(cells)
    return shape == frozenset({(0, 0), (0, 1), (1, 0), (1, 1)})


def _check_rotation(entry: str) -> str | None:
    saw_non_square = False
    for seed in range(8):
        # Two soft drops first so a piece spawned partly above the field is fully visible
        # before its rotation is judged (harmless descent for fully visible spawns).
        frames, _code, _out, err = _drive(entry, _DOWN * 2 + _UP, seed)
        if isinstance(frames, str):
            return f"rotation: {frames} (stderr tail: {err.strip()[-300:]!r})"
        if len(frames) < 4:
            return f"rotation: expected startup, two soft drops, and a rotation frame, got {len(frames)}"
        before, after = frames[2], frames[3]
        if len(before.active) != 4:
            continue  # this seed's piece never fully entered the field; try another
        if _is_square(before.active):
            continue
        saw_non_square = True
        if not _spin_ok(after.active) or after.active & after.locked:
            return "rotation: Up produced an invalid falling piece"
        if after.active == before.active:
            return "rotation: Up was a no-op for a non-square piece in open space"
        return None
    return None if saw_non_square else "rotation: seeds 0-7 never produced a rotatable non-square piece"


def _check_drop(entry: str) -> str | None:  # noqa: PLR0911
    frames, _code, _out, err = _drive(entry, _SPACE)
    if isinstance(frames, str):
        return f"hard drop: {frames} (stderr tail: {err.strip()[-300:]!r})"
    if len(frames) < 2:
        return f"hard drop: expected startup and response frames, got {len(frames)}"
    before, after = frames[:2]
    # A partly hidden spawn locks all four cells even though fewer were visible before the
    # drop: the visible cells' landing must be present, and exactly four cells must lock.
    landing = _shift(before.active, _drop_distance(before.active, before.locked), 0)
    if not landing <= after.locked:
        return f"hard drop: piece did not lock at its lowest legal position ({sorted(after.locked)})"
    if len(after.locked) != len(before.locked) + 4:
        delta = len(after.locked) - len(before.locked)
        return f"hard drop: locking changed the board by {delta} cells, expected 4"
    if not _plausible_piece(after.active):
        return "hard drop: no new falling piece spawned after lock"
    if after.score < before.score:
        return "hard drop: score decreased"
    return None


def _check_quit_and_game_over(entry: str) -> str | None:
    _frames, code, _out, err = _drive(entry, _QUIT)
    if code != 0 or "Traceback" in err:
        return f"quit: q exited {code} or printed a traceback"
    frames, top_code, output, top_err = _drive(entry, _SPACE * 200)
    if isinstance(frames, str):
        return f"game over: {frames} (stderr tail: {top_err.strip()[-300:]!r})"
    if top_code != 0:
        return f"game over: top-out exited {top_code}, expected 0"
    # The goal pins "before a successful exit", not a position relative to the sentinel —
    # printing GAME OVER inside the final frame is a compliant choice.
    if "GAME OVER" not in output.upper():
        return "game over: repeated hard drops never produced GAME OVER"
    return None


class _ReplayGame:
    """Adaptive driver over the pinned determinism: relaunch with the full key prefix.

    Every observation is a fresh batch run (keys + EOF together), so gameplay grading never
    depends on the streaming transport — a stdin-slurping game is graded on the same frames
    as a streaming one. Frames are indexed by key position (startup = 0), which also makes
    the driver immune to a farewell frame rendered on EOF.
    """

    def __init__(self, entry: str, seed: int = _SEED):
        self.entry = entry
        self.seed = seed
        self.keys: list[bytes] = []

    def current(self) -> _Frame | str:
        frames, _code, _out, err = _drive(self.entry, b"".join(self.keys), self.seed)
        if isinstance(frames, str):
            return f"replay: {frames} (stderr tail: {err.strip()[-300:]!r})"
        if len(frames) <= len(self.keys):
            return (
                f"replay: {len(frames)} frames for {len(self.keys)} keys — "
                "the game stopped responding partway through its own replayed history"
            )
        return frames[len(self.keys)]

    def send_batch(self, new_keys: list[bytes]) -> list[_Frame] | str:
        base = len(self.keys)
        self.keys.extend(new_keys)
        frames, _code, _out, err = _drive(self.entry, b"".join(self.keys), self.seed)
        if isinstance(frames, str):
            return f"replay: {frames} (stderr tail: {err.strip()[-300:]!r})"
        if len(frames) < len(self.keys) + 1:
            return (
                f"replay: {len(frames)} frames for {len(self.keys)} keys — "
                "the game stopped rendering mid-script (unexpected top-out or a dropped key?)"
            )
        return frames[base + 1 : len(self.keys) + 1]

    def send(self, key: bytes) -> _Frame | str:
        result = self.send_batch([key])
        return result if isinstance(result, str) else result[0]


def _check_seven_bag(entry: str) -> str | None:
    # The whole script is decision-free (park direction alternates by index), so one batch
    # run covers all seven pieces; classification reads the intermediate frames afterward.
    # Three soft drops per piece pull a partly hidden spawn fully into view first.
    keys: list[bytes] = []
    classify_at: list[int] = []
    for index in range(7):
        keys.extend([_DOWN] * 3)
        classify_at.append(len(keys))  # frame index == keys consumed (startup is 0)
        keys.extend([_LEFT if index % 2 == 0 else _RIGHT] * _WIDTH)
        keys.append(_SPACE)
    frames, _code, _out, err = _drive(entry, b"".join(keys))
    if isinstance(frames, str):
        return f"7-bag: {frames} (stderr tail: {err.strip()[-300:]!r})"
    if len(frames) < len(keys) + 1:
        return f"7-bag: {len(frames)} frames for {len(keys)} keys — the game stopped rendering mid-script"
    seen: list[str] = []
    for index, frame_index in enumerate(classify_at):
        cells = frames[frame_index].active
        if len(cells) != 4:
            return f"7-bag: piece {index + 1} still shows {len(cells)} cells after three soft drops"
        letter = _classify_piece(cells)
        if letter is None:
            return f"7-bag: piece {index + 1} is not a standard tetromino: {sorted(cells)}"
        seen.append(letter)
    expected = set(_STANDARD_SHAPES)
    if set(seen) != expected:
        return f"7-bag: first bag contained {seen}, expected each of {sorted(expected)} once"
    return None


def _board_cost(board: frozenset[tuple[int, int]], cleared: int) -> int:
    heights: list[int] = []
    holes = 0
    for col in range(_WIDTH):
        occupied = sorted(r for r, c in board if c == col)
        if not occupied:
            heights.append(0)
            continue
        heights.append(_HEIGHT - occupied[0])
        holes += sum((r, col) not in board for r in range(occupied[0], _HEIGHT))
    bumpiness = sum(abs(a - b) for a, b in pairwise(heights))
    return cleared * 100_000 - holes * 1_000 - max(heights) * 50 - sum(heights) * 10 - bumpiness * 5


def _choose_placement(
    orientations: list[_Frame],
) -> tuple[int, int, frozenset[tuple[int, int]], frozenset[tuple[int, int]], int] | None:
    best = None
    best_cost = -(10**9)
    for rotations, orientation in enumerate(orientations[:4]):
        if len(orientation.active) != 4:
            continue  # partly hidden orientation (e.g. an upward kick at the top edge): unplannable
        shape = _norm(orientation.active)
        width = max(c for _, c in shape) + 1
        spawn_row = min(r for r, _ in orientation.active)
        for target_col in range(_WIDTH - width + 1):
            active = frozenset((r + spawn_row, c + target_col) for r, c in shape)
            if active & orientation.locked:
                continue
            landing = _shift(active, _drop_distance(active, orientation.locked), 0)
            settled, cleared = _settle(orientation.locked | landing)
            cost = _board_cost(settled, cleared)
            if cost > best_cost:
                best_cost = cost
                best = rotations, target_col, settled, landing, cleared
    return best


def _check_line_clear(entry: str) -> str | None:  # noqa: C901, PLR0911, PLR0912
    game = _ReplayGame(entry)
    deadline = time.monotonic() + _LINE_CLEAR_DEADLINE_SECONDS
    frame = game.current()
    if isinstance(frame, str):
        return f"line clear: {frame}"
    for placement_index in range(40):
        if time.monotonic() > deadline:
            return f"line clear: replay budget exhausted after {placement_index} placements"
        # Soft-drop to a safe depth before enumerating rotations: at spawn, vertical
        # orientations of I/S/Z can clip above the visible field, which would blind the
        # planner to exactly the placements that avoid holes. Landing is depth-invariant
        # (hard drop always goes to the bottom), so the descent costs nothing. A game may
        # legitimately lock a piece on a blocked soft drop — then plan the NEXT piece.
        for _ in range(12):
            deep_enough = len(frame.active) == 4 and min(r for r, _ in frame.active) >= 3
            if deep_enough:
                break
            previous_locked = frame.locked
            stepped = game.send(_DOWN)
            if isinstance(stepped, str):
                return f"line clear: {stepped}"
            frame = stepped
            if frame.locked != previous_locked:
                continue  # locked mid-descent: the new spawn keeps normalizing
        if len(frame.active) != 4:
            return f"line clear: placement {placement_index} never showed a four-cell falling piece"
        start = frame
        spins = game.send_batch([_UP] * 4)
        if isinstance(spins, str):
            return f"line clear: {spins}"
        for spun in spins:
            if spun.locked != start.locked:
                return "line clear: rotation altered the locked board"
            if not _spin_ok(spun.active) or spun.active & spun.locked:
                return "line clear: rotation produced an invalid falling piece"
        orientations = [start, *spins[:3]]
        frame = spins[3]
        choice = _choose_placement(orientations)
        if choice is None:
            return "line clear: no legal placement could be planned"
        rotations, target_col, expected_board, _landing, expected_clears = choice
        if rotations:
            spun_back = game.send_batch([_UP] * rotations)
            if isinstance(spun_back, str):
                return f"line clear: {spun_back}"
            frame = spun_back[-1]
        current_col = min(c for _, c in frame.active)
        key = _LEFT if current_col > target_col else _RIGHT
        steps = abs(current_col - target_col)
        before_score = frame.score
        before_lines = frame.lines
        moves = game.send_batch([key] * steps + [_SPACE])
        if isinstance(moves, str):
            return f"line clear: {moves}"
        if steps and min(c for _, c in moves[-2].active) != target_col:
            return f"line clear: could not steer placement {placement_index} to column {target_col}"
        frame = moves[-1]
        if frame.locked != expected_board:
            return f"line clear: hard-drop/clear physics diverged on placement {placement_index}"
        if frame.score < before_score:
            return "line clear: score decreased after a placement"
        if expected_clears:
            if frame.lines != before_lines + expected_clears:
                return "line clear: full rendered row vanished without updating Lines"
            if frame.score <= before_score:
                return "line clear: clearing a row did not increase Score"
            return None
    return "line clear: adaptive play did not clear a row within 40 pieces"


_CSI_FINAL = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ`abcdefghijklmnopqrstuvwxyz{|}~"


def _emulate_screen(data: bytes) -> dict[int, str]:  # noqa: C901, PLR0912, PLR0915
    grid: dict[int, dict[int, str]] = {}
    row = col = 0
    scroll_top = 0
    scroll_bottom = 39
    text = data.decode("utf-8", errors="replace")
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\x1b":
            end = index + 1
            if end < len(text) and text[end] == "[":
                end += 1
                start = end
                while end < len(text) and text[end] not in _CSI_FINAL:
                    end += 1
                final = text[end] if end < len(text) else ""
                raw_params = text[start:end].lstrip("?")
                nums = [int(part) if part.isdigit() else 0 for part in raw_params.split(";")]
                amount = (nums[0] or 1) if nums else 1
                if final in "Hf":
                    row = (nums[0] or 1) - 1 if nums else 0
                    col = (nums[1] or 1) - 1 if len(nums) > 1 else 0
                elif final == "A":
                    row = max(0, row - amount)
                elif final == "B":
                    row += amount
                elif final == "C":
                    col += amount
                elif final == "D":
                    col = max(0, col - amount)
                elif final == "G":
                    col = amount - 1
                elif final == "d":
                    row = amount - 1
                elif final == "J" and nums and nums[0] == 2:
                    grid.clear()
                    row = col = 0
                elif final == "K":
                    mode = nums[0] if nums else 0
                    cells = grid.setdefault(row, {})
                    if mode == 2:
                        cells.clear()
                    elif mode == 1:
                        for cell_col in [c for c in cells if c <= col]:
                            del cells[cell_col]
                    else:
                        for cell_col in [c for c in cells if c >= col]:
                            del cells[cell_col]
                elif final == "r":
                    scroll_top = (nums[0] or 1) - 1 if nums else 0
                    scroll_bottom = (nums[1] or 40) - 1 if len(nums) > 1 else 39
                index = end + 1
            else:
                control = text[end] if end < len(text) else ""
                if control == "M":  # reverse index; scroll down inside the active margins
                    if row == scroll_top:
                        for scroll_row in range(scroll_bottom, scroll_top, -1):
                            grid[scroll_row] = dict(grid.get(scroll_row - 1, {}))
                        grid[scroll_top] = {}
                    else:
                        row = max(0, row - 1)
                elif control == "D":  # index; scroll up inside the active margins
                    if row == scroll_bottom:
                        for scroll_row in range(scroll_top, scroll_bottom):
                            grid[scroll_row] = dict(grid.get(scroll_row + 1, {}))
                        grid[scroll_bottom] = {}
                    else:
                        row += 1
                elif control == "E":
                    row += 1
                    col = 0
                index = end + 1
            continue
        if char == "\r":
            col = 0
        elif char == "\n":
            row += 1
        elif char == "\b":
            col = max(0, col - 1)
        elif char == "\t":
            col = (col // 8 + 1) * 8
        elif char.isprintable():
            grid.setdefault(row, {})[col] = char
            col += 1
        index += 1
    return {
        r: "".join(cells.get(c, " ") for c in range(max(cells) + 1)) for r, cells in grid.items() if cells
    }


def _screen_board(data: bytes) -> _Frame | str:
    lines = _emulate_screen(data)
    rows: list[str] = []
    starts: list[int] = []
    for row in sorted(lines):
        match = re.search(r"\|([.#@]{10})\|", lines[row])
        if match:
            rows.append(match.group(1))
            starts.append(match.start())
    if len(rows) < _HEIGHT:
        return f"interactive mode exposed only {len(rows)} parseable board rows"
    rows = rows[-_HEIGHT:]
    starts = starts[-_HEIGHT:]
    if len(set(starts)) != 1:
        return f"interactive board rows are misaligned at columns {sorted(set(starts))[:6]}"
    active = frozenset((r, c) for r, line in enumerate(rows) for c, glyph in enumerate(line) if glyph == "@")
    locked = frozenset((r, c) for r, line in enumerate(rows) for c, glyph in enumerate(line) if glyph == "#")
    return _Frame(0, 0, active, locked)


def _check_interactive(entry: str) -> str | None:  # noqa: C901, PLR0911, PLR0912, PLR0915
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    proc = subprocess.Popen(
        [sys.executable, entry],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "TERM": "xterm"},
    )
    os.close(slave)
    data = b""

    def drain(seconds: float) -> None:
        nonlocal data
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                data += chunk

    try:
        drain(0.35)
        initial = _screen_board(data)
        if isinstance(initial, str):
            drain(0.8)  # a slow first paint is not a defect; look again before judging
            initial = _screen_board(data)
        if isinstance(initial, str):
            return f"interactive: {initial}"
        if not _plausible_piece(initial.active):
            return f"interactive: startup board shows falling cells {sorted(initial.active)}"
        direction = _LEFT if min(c for _, c in initial.active) > 0 else _RIGHT
        # Curses enables xterm application-cursor mode (`CSI ? 1 h` / `ESC =`), in which a
        # physical arrow key sends ESC O D/C. A raw/cbreak implementation that leaves the
        # terminal in normal cursor mode receives the familiar ESC [ D/C bytes instead.
        application_cursor = b"\x1b[?1h" in data or b"\x1b=" in data
        interactive_key = direction
        if application_cursor:
            interactive_key = b"\x1bOD" if direction == _LEFT else b"\x1bOC"
        # A human whose keypress seems ignored presses again: retry up to three times so an
        # implementation that applies keys a beat late is graded on whether the piece moves,
        # not on first-press latency.
        initial_center = sum(c for _, c in initial.active)
        moved = initial
        moved_ok = False
        for _ in range(3):
            with contextlib.suppress(OSError):
                os.write(master, interactive_key)
            drain(0.35)
            moved = _screen_board(data)
            if isinstance(moved, str):
                return f"interactive movement: {moved}"
            if not moved.active:
                continue
            moved_center = sum(c for _, c in moved.active) * len(initial.active) / len(moved.active)
            if direction == _LEFT and moved_center < initial_center:
                moved_ok = True
                break
            if direction == _RIGHT and moved_center > initial_center:
                moved_ok = True
                break
        if not moved_ok:
            side = "left" if direction == _LEFT else "right"
            return (
                f"interactive: three {side} presses never moved the visible falling piece "
                f"({sorted(initial.active)} -> {sorted(moved.active) if not isinstance(moved, str) else '?'})"
            )
        gravity_start = moved
        fell = False
        for _ in range(6):
            drain(0.45)
            fallen = _screen_board(data)
            if isinstance(fallen, str):
                return f"interactive gravity: {fallen}"
            if not fallen.active:
                continue
            if max(r for r, _ in fallen.active) > max(r for r, _ in gravity_start.active):
                fell = True
                break
            if len(fallen.locked) > len(gravity_start.locked):
                fell = True  # the piece locked and respawned: gravity clearly ran
                break
        if not fell:
            return "interactive: falling piece did not advance under automatic gravity within ~3s"
        with contextlib.suppress(OSError):
            os.write(master, b"q\r")
        drain(0.3)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            return "interactive: q did not exit promptly"
        if proc.returncode != 0:
            return f"interactive: q exited with code {proc.returncode}"
        return None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)


def main() -> int:
    """Run all lower-bound gameplay checks plus the separately reported transport check."""
    if len(sys.argv) < 2 or not Path(sys.argv[1]).is_file():
        print(f"probe: entry file not found (expected argv[1]; got {sys.argv[1:]})")
        return 1
    entry = sys.argv[1]
    phases = [
        ("README", _check_readme),
        ("boot + determinism", _check_boot_and_determinism),
        ("movement", _check_movement),
        ("rotation", _check_rotation),
        ("7-bag", _check_seven_bag),
        ("hard drop", _check_drop),
        ("quit + game over", _check_quit_and_game_over),
        ("line clear", _check_line_clear),
        ("interactive gameplay", _check_interactive),
    ]
    for name, check in phases:
        reason = check(entry)
        if reason is not None:
            print(f"probe: {reason}")
            return 1
        print(f"probe: {name} ok")
    streaming_reason = _check_streaming(entry)
    if streaming_reason is None:
        print("probe: transport (streaming) ok")
    else:
        gate = "gating" if _STREAMING_GATES else "non-gating"
        print(f"probe: transport (streaming) FAILED [{gate}]: {streaming_reason}")
        if _STREAMING_GATES:
            return 1
    print(f"probe: playable terminal Tetris ok via {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
