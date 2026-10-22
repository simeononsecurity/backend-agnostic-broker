#!/usr/bin/env python3
"""Run the ADR 0027 lifecycle failure drills N times and fail on any flake.

The drills (``tests/drills/``, pytest marker ``drill``) are excluded from the
default fast run. This runner is the release gate / CI entrypoint: it executes
the whole drill suite ``--repeat`` times (default 3) and exits non-zero if *any*
repetition fails. A drill that is flaky is a defect, not a retry — so every
repetition must be green.

Usage::

    python scripts/failure_drills.py              # 3 repetitions
    python scripts/failure_drills.py --repeat 5
    python scripts/failure_drills.py -k d10        # one drill, 3x (debugging)

Exit code 0 iff every repetition passed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DRILLS = _REPO_ROOT / "tests" / "drills"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ADR 0027 lifecycle failure drills.")
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="number of full-suite repetitions; all must pass (default 3)",
    )
    parser.add_argument(
        "-k",
        dest="keyword",
        default=None,
        help="pytest -k expression to select a subset of drills (debugging)",
    )
    parser.add_argument(
        "--maxfail",
        type=int,
        default=1,
        help="stop a repetition after this many failures (default 1)",
    )
    args = parser.parse_args(argv)

    if args.repeat < 1:
        print("--repeat must be >= 1", file=sys.stderr)
        return 2

    base = [
        sys.executable,
        "-m",
        "pytest",
        str(_DRILLS),
        "-m",
        "drill",
        "-p",
        "no:cacheprovider",
        f"--maxfail={args.maxfail}",
        "-ra",
    ]
    if args.keyword:
        base += ["-k", args.keyword]

    failures: list[int] = []
    t0 = time.monotonic()
    for i in range(1, args.repeat + 1):
        print(f"\n===== failure-drill repetition {i}/{args.repeat} =====", flush=True)
        rc = subprocess.call(base, cwd=str(_REPO_ROOT))
        if rc != 0:
            failures.append(i)
            print(f"----- repetition {i} FAILED (rc={rc}) -----", flush=True)
        else:
            print(f"----- repetition {i} passed -----", flush=True)

    elapsed = time.monotonic() - t0
    print(
        f"\n===== drills: {args.repeat - len(failures)}/{args.repeat} repetitions "
        f"passed in {elapsed:.0f}s =====",
        flush=True,
    )
    if failures:
        print(f"FLAKY/FAILED repetitions: {failures}", file=sys.stderr, flush=True)
        return 1
    print("all repetitions green", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
