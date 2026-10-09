"""Check the counts the README publishes against what the runners actually collect.

Two numbers in `## Running the checks` say how many checks each runner holds. Both were
wrong by nine on 2026-10-09, and the replacement written the same hour was wrong by one
within the hour, because a check was added after the number was typed. A hand maintained
count of the checks is the one number in this repo guaranteed to go stale, since it changes
every time anybody does the thing the repo is for.

So it is derived here instead. `runner.collect` imports the modules and counts the `check_`
functions without running any of them, which is the same list the runner would execute.

This module needs duckdb, because counting the second runner means importing the modules
that import the driver. That means `tests/run_all.py` cannot catch its own count drifting
and only the full runner can. Stated rather than worked around. A standard library check
that counted only the standard library half would leave the other number unguarded, which
is the situation this replaces.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.runner import collect  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(HERE, "README.md")

LINE = re.compile(r"^python3 (tests/\S+)\s+(\d+) checks")


def _published():
    """The runner and the count off each line of the README's command block."""
    out = {}
    with open(README, encoding="utf-8") as fh:
        for line in fh:
            hit = LINE.match(line.strip())
            if hit:
                out[hit.group(1)] = int(hit.group(2))
    return out


def _collected():
    from tests.run_all import MODULES
    from tests.run_with_duckdb import EXTRA_MODULES
    return {
        "tests/run_all.py": len(collect(list(MODULES))),
        "tests/run_with_duckdb.py": len(collect(list(MODULES) + list(EXTRA_MODULES))),
    }


def check_the_readme_names_both_runners():
    published = _published()
    assert set(published) == {"tests/run_all.py", "tests/run_with_duckdb.py"}, published


def check_every_published_check_count_is_what_the_runner_collects():
    published = _published()
    collected = _collected()
    for runner, count in sorted(collected.items()):
        assert published.get(runner) == count, (
            "README.md says {} holds {} checks and it collects {}. Fix the README."
            .format(runner, published.get(runner), count))

