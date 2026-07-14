import sys

ROWS = ["...@......", "...@@@...."] + ["." * 10] * 18
FRAME = (
    "Score: 0\nLines: 0\nNext: O\n+----------+\n"
    + "".join("|" + r + "|\n" for r in ROWS)
    + "+----------+\n-- end frame --\n"
)
sys.stdout.write(FRAME)
sys.stdout.flush()
while True:
    b = sys.stdin.buffer.read(1)
    if not b or b == b"q":
        raise SystemExit(0)
    if b in (b" ", b"A", b"B", b"C", b"D"):
        sys.stdout.write(FRAME)
        sys.stdout.flush()
