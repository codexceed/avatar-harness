"""ASCII Tetris — arrow-key navigation, with a deterministic scripted mode.

Interactive:  python3 tetris.py            (raw terminal, real-time gravity)
Scripted:     python3 tetris.py --no-raw --seed 42   (turn-based, graded surface)

See README.md for the full contract (keys, frame format, scoring, 7-bag RNG).
"""

import random
import sys

WIDTH = 10
HEIGHT = 20
SENTINEL = "-- end frame --"
LINE_SCORES = {1: 100, 2: 300, 3: 500, 4: 800}

# Standard tetromino spawn shapes as (row, col) offsets, row 0 at the top.
SHAPES = {
    "I": ((0, 0), (0, 1), (0, 2), (0, 3)),
    "O": ((0, 0), (0, 1), (1, 0), (1, 1)),
    "T": ((0, 1), (1, 0), (1, 1), (1, 2)),
    "S": ((0, 1), (0, 2), (1, 0), (1, 1)),
    "Z": ((0, 0), (0, 1), (1, 1), (1, 2)),
    "J": ((0, 0), (1, 0), (1, 1), (1, 2)),
    "L": ((0, 2), (1, 0), (1, 1), (1, 2)),
}
SPAWN_COL = 3


def rotate_cw(cells):
    """Rotate a normalized cell set clockwise within its bounding box."""
    max_r = max(r for r, _ in cells)
    rotated = {(c, max_r - r) for r, c in cells}
    min_r = min(r for r, _ in rotated)
    min_c = min(c for _, c in rotated)
    return {(r - min_r, c - min_c) for r, c in rotated}


class Game:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.bag = []
        self.board = set()
        self.score = 0
        self.lines = 0
        self.over = False
        self.next_letter = self._draw()
        self.current = set()
        self._spawn()

    def _draw(self):
        if not self.bag:
            self.bag = list("IJLOSTZ")
            self.rng.shuffle(self.bag)
        return self.bag.pop(0)

    def _spawn(self):
        letter = self.next_letter
        self.next_letter = self._draw()
        cells = {(r, c + SPAWN_COL) for r, c in SHAPES[letter]}
        if cells & self.board:
            self.over = True
            self.current = set()
            return
        self.current = cells

    def _fits(self, cells):
        return all(0 <= r < HEIGHT and 0 <= c < WIDTH for r, c in cells) and not (cells & self.board)

    def move(self, dr, dc):
        moved = {(r + dr, c + dc) for r, c in self.current}
        if self._fits(moved):
            self.current = moved
            return True
        return False

    def rotate(self):
        min_r = min(r for r, _ in self.current)
        min_c = min(c for _, c in self.current)
        rel = {(r - min_r, c - min_c) for r, c in self.current}
        rotated = {(r + min_r, c + min_c) for r, c in rotate_cw(rel)}
        if self._fits(rotated):
            self.current = rotated

    def soft_drop(self):
        if self.move(1, 0):
            self.score += 1

    def hard_drop(self):
        dropped = 0
        while self.move(1, 0):
            dropped += 1
        self.score += 2 * dropped
        self._lock()

    def _lock(self):
        self.board |= self.current
        self._clear_full_rows()
        self._spawn()

    def _clear_full_rows(self):
        full = [r for r in range(HEIGHT) if all((r, c) in self.board for c in range(WIDTH))]
        if not full:
            return
        self.score += LINE_SCORES[len(full)]
        self.lines += len(full)
        kept = {(r, c) for r, c in self.board if r not in full}
        self.board = {(r + sum(1 for f in full if f > r), c) for r, c in kept}

    def render(self, out):
        rows = [f"Score: {self.score}", f"Lines: {self.lines}", f"Next: {self.next_letter}"]
        border = "+" + "-" * WIDTH + "+"
        rows.append(border)
        for r in range(HEIGHT):
            line = "|"
            for c in range(WIDTH):
                if (r, c) in self.current:
                    line += "@"
                elif (r, c) in self.board:
                    line += "#"
                else:
                    line += "."
            rows.append(line + "|")
        rows.append(border)
        rows.append(SENTINEL)
        out.write("\n".join(rows) + "\n")
        out.flush()


def read_key(stream):
    """Read one key from a byte stream: arrows (ANSI), space, q. None on EOF."""
    while True:
        b = stream.read(1)
        if not b:
            return None
        if b == b"\x1b":
            seq = stream.read(2)
            if seq == b"[D":
                return "LEFT"
            if seq == b"[C":
                return "RIGHT"
            if seq == b"[B":
                return "DOWN"
            if seq == b"[A":
                return "UP"
            continue
        if b == b" ":
            return "SPACE"
        if b == b"q":
            return "QUIT"
        # Anything else (including newlines) is ignored: no frame is rendered.


def run_scripted(seed):
    game = Game(seed)
    out = sys.stdout
    game.render(out)
    stream = sys.stdin.buffer
    while True:
        key = read_key(stream)
        if key is None or key == "QUIT":
            return 0
        if key == "LEFT":
            game.move(0, -1)
        elif key == "RIGHT":
            game.move(0, 1)
        elif key == "DOWN":
            game.soft_drop()
        elif key == "UP":
            game.rotate()
        elif key == "SPACE":
            game.hard_drop()
        game.render(out)
        if game.over:
            out.write("GAME OVER\n")
            out.flush()
            return 0


def run_interactive(seed):
    import curses
    import io
    import time

    def loop(screen):
        curses.curs_set(0)
        screen.nodelay(True)
        game = Game(seed)
        last_tick = time.monotonic()
        while not game.over:
            key = screen.getch()
            if key == ord("q"):
                return
            if key == curses.KEY_LEFT:
                game.move(0, -1)
            elif key == curses.KEY_RIGHT:
                game.move(0, 1)
            elif key == curses.KEY_DOWN:
                game.soft_drop()
            elif key == curses.KEY_UP:
                game.rotate()
            elif key == ord(" "):
                game.hard_drop()
            if time.monotonic() - last_tick > 0.5:
                if not game.move(1, 0):
                    game._lock()
                last_tick = time.monotonic()
            screen.erase()
            buf = io.StringIO()
            game.render(buf)
            for i, line in enumerate(buf.getvalue().splitlines()[:-1]):
                screen.addstr(i, 0, line)
            screen.refresh()
            time.sleep(0.02)
        screen.addstr(HEIGHT + 5, 0, "GAME OVER")
        screen.refresh()
        time.sleep(1.0)

    curses.wrapper(loop)
    return 0


def main(argv):
    seed = 0
    if "--seed" in argv:
        seed = int(argv[argv.index("--seed") + 1])
    if "--no-raw" in argv:
        return run_scripted(seed)
    return run_interactive(seed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
