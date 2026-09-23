"""Run every check that needs nothing but the standard library.

    python3 tests/run_all.py

DuckDB is not imported anywhere below. The loading path has its own runner, which fails
rather than skips when the dependency is missing, because a suite that quietly skips the
half needing a database reports a number that means something different every time.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.runner import run_checks  # noqa: E402

MODULES = [
    "tests.test_taxonomy",
    "tests.test_safeharbor",
    "tests.test_schema",
    "tests.test_rng",
    "tests.test_corpus",
    "tests.test_naive",
    "tests.test_reidentify",
    "tests.test_coverage",
    "tests.test_classify",
    "tests.test_lineage",
    "tests.test_mask",
    "tests.test_review",
    "tests.test_access",
    "tests.test_compliance",
    "tests.test_deps",
]


if __name__ == "__main__":
    sys.exit(run_checks(MODULES, verbose="-v" in sys.argv))
