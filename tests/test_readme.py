"""Check the figures the README publishes against what the repo already knows.

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

The obfuscated recall check below needs no driver and lives here anyway, because both
checks are about README figures and splitting them across two modules by dependency is how
the second one stops being found.
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

# `all three   20/20   12   2   0` in the ablation table, and the sentence under the
# obfuscated result that quotes the same two cells as its baseline.
ABLATION_ALL = re.compile(r"^all three\s+(\d+)/(\d+)\s+(\d+)\s")
OBFUSCATED = re.compile(
    r"recall falls from (\d+) of (\d+) to \d+ of \d+ and automatic\s*\n?\s*"
    r"masking from (\d+) to \d+")


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


def check_the_obfuscated_sentence_starts_from_the_ablation_tables_own_baseline():
    """The sentence under the obfuscated result against the table four blocks above it.

    This is the one that went stale. The planted label on `analytics.encounter_daily.day`
    moved, the denominator went from 19 to 20, the ablation table followed it because it is
    generated, and the prose under the obfuscated result kept quoting 19 to 9 for three
    weeks. Both halves of that sentence are cells of the table and neither was derived.

    A full re-measurement of the obfuscated arm would need the warehouse rebuilt with every
    column renamed, which is `scripts/classify_probe.py --db ...` and is not a check's
    budget. This grades the baseline the sentence claims, which is the half that drifted.
    """
    with open(README, encoding="utf-8") as fh:
        text = fh.read()

    table = None
    for line in text.splitlines():
        hit = ABLATION_ALL.match(line.strip())
        if hit:
            table = (int(hit.group(1)), int(hit.group(2)), int(hit.group(3)))
            break
    assert table is not None, "no 'all three' row found in the ablation table"

    hit = OBFUSCATED.search(text)
    assert hit is not None, "no obfuscated recall sentence found"
    claimed = (int(hit.group(1)), int(hit.group(2)), int(hit.group(3)))

    assert claimed == table, (
        "the obfuscated sentence starts from {} and the ablation table says {}. "
        "Fix the sentence.".format(claimed, table))
