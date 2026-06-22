"""The grader (the verification contract).

It lives outside the agent's workspace, so the agent can run it (through the harness) but
its file tools cannot read or edit it. It runs the candidate `pipeline.py`, then compares
the D column of the produced `out.csv` against the answer key in `validation.csv` (sitting
next to this file): numeric with a small tolerance where the expected value is a number,
exact-string otherwise.
"""

import csv
import subprocess
import sys
from pathlib import Path

EXPECTED = Path(__file__).resolve().parent / "validation.csv"
TOL = 1e-9


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_pipeline_d_column():
    proc = subprocess.run([sys.executable, "pipeline.py"], capture_output=True, text=True)
    assert proc.returncode == 0, f"pipeline.py failed:\n{proc.stderr}"

    got = _rows("out.csv")
    want = _rows(EXPECTED)
    assert len(got) == len(want), f"row count {len(got)} != expected {len(want)}"

    for i, (g, w) in enumerate(zip(got, want), start=1):
        gd, wd = g.get("D"), w["D"]
        try:
            wnum = float(wd)
        except ValueError:
            assert gd == wd, f"row {i}: D={gd!r}, expected {wd!r}"
            continue
        try:
            gnum = float(gd)
        except (ValueError, TypeError):
            raise AssertionError(f"row {i}: D={gd!r} is not numeric, expected {wd!r}")
        assert abs(gnum - wnum) < TOL, f"row {i}: D={gd!r}, expected {wd!r}"
