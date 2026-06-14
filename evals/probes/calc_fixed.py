"""Probe (modify-existing): the calc.add bug is fixed — add(a, b) returns a + b.

Runs with the scratch repo as cwd; imports the agent's calc.py from there.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))  # import calc.py from the scratch repo, not the probe's dir
try:
    import calc
except Exception as exc:
    print(f"probe: cannot import calc: {exc}")
    sys.exit(1)

ok = calc.add(2, 3) == 5 and calc.add(-1, 1) == 0
print("probe: add() fixed" if ok else "probe: add() still wrong")
sys.exit(0 if ok else 1)
