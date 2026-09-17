"""Run the whole suite including the checks that need a real database.

    python3 tests/run_with_duckdb.py

It fails rather than skips when the driver is missing. A runner that skips reports a
different number depending on what happened to be installed, and the number that gets
published is whichever one somebody read.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.run_all import MODULES  # noqa: E402
from tests.runner import run_checks  # noqa: E402


def main() -> int:
    try:
        import duckdb  # noqa: F401
    except ImportError as exc:
        print("FAIL: this runner needs duckdb and it is not importable: {}".format(exc))
        print("      pip install -r requirements.txt")
        return 2
    return run_checks(MODULES + ["tests.test_warehouse"], verbose="-v" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
