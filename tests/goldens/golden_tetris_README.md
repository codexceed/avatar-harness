# ASCII Tetris

A terminal Tetris with arrow-key navigation, in a single stdlib-only file: `tetris.py`.

## Running

| Mode | Command | Notes |
| --- | --- | --- |
| Interactive | `python3 tetris.py` | Real-time gravity, raw terminal, arrow keys. |
| Scripted | `python3 tetris.py --no-raw --seed 42` | Turn-based, deterministic; reads keys from stdin. |

## Controls (both modes)

| Key | Bytes (scripted mode) | Action |
| --- | --- | --- |
| Left arrow | `ESC [ D` (`\x1b[D`) | Move the piece one column left. |
| Right arrow | `ESC [ C` (`\x1b[C`) | Move the piece one column right. |
| Down arrow | `ESC [ B` (`\x1b[B`) | Soft drop: one row down, +1 point. |
| Up arrow | `ESC [ A` (`\x1b[A`) | Rotate clockwise (blocked rotation is a no-op). |
| Space | `0x20` | Hard drop: +2 points per row descended, then the piece locks. |
| `q` | `0x71` | Quit immediately with exit code 0. |

Blocked moves are no-ops. In scripted mode a blocked Down is a no-op too (only Space locks),
gravity never runs on a timer, and every other byte (including newlines) is ignored.

## Scripted-mode frame contract

One frame is rendered at startup and after every recognized key, flushed immediately:
`Score: <n>`, `Lines: <n>`, `Next: <letter>`, then the 10 x 20 playfield bordered by
`+----------+` with `|` walls — `.` empty, `#` locked, `@` the falling piece — and the
sentinel line `-- end frame --`. When a new piece cannot spawn, the final frame is
followed by `GAME OVER` and a clean exit 0. EOF on stdin also exits 0.

## Scoring

| Event | Points |
| --- | --- |
| Soft drop | 1 per row |
| Hard drop | 2 per row descended |
| Single line clear | 100 |
| Double | 300 |
| Triple | 500 |
| Tetris (4 lines) | 800 |

`Lines:` counts total cleared rows.

## Pieces

The 7 standard tetrominoes (I, O, T, S, Z, J, L), spawned near the top center, drawn with a
7-bag randomizer: `random.Random(seed)` shuffles a fresh `IJLOSTZ` bag per refill and pieces
are drawn in order. Rotation is the clockwise bounding-box rotation (no wall kicks).
