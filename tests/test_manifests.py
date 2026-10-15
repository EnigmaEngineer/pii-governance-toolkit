"""Every committed measurement manifest, graded against the rule about what goes in one.

The rule has two halves. A manifest carries counts and verdicts, because those re-derive
on another machine. A wall time can still be published, but then it has to give the spread
rather than a tight range, so a reader can see how much of the figure is the machine.

Two checks used to enforce the first half, in `tests/test_roundtwo_warehouse.py` and
`tests/test_mask_applied.py`, each holding its own copy of the forbidden word list and
each guarding one file. Four of the six manifests were guarded by neither. The word list
moved to `tests/runner.py` and the coverage question moved here.

`KINDS` is the part worth arguing with. A manifest has to be named in it or
`check_every_committed_manifest_is_classified` fails, which means a seventh manifest
arrives at a decision about which half of the rule it obeys instead of landing ungraded.
`profiler-scaling.json` is a timings manifest because the thing it measures is a duration
and there is no count that stands in for it. That is not an exemption from the rule. It is
the other half of it, and the spread check is what that half costs.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.runner import assert_no_wall_time  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEASUREMENTS = os.path.join(HERE, "measurements")

COUNTS = "counts"
TIMINGS = "timings"

KINDS = {
    "arm-ablation.json": COUNTS,
    "batched-plan.json": TIMINGS,
    "lineage-rescue.json": COUNTS,
    "profiler-scaling.json": TIMINGS,
    "roundtwo-cost.json": COUNTS,
    "value-sampling.json": COUNTS,
    "weight-refusal.json": COUNTS,
}


def _committed():
    return sorted(n for n in os.listdir(MEASUREMENTS) if n.endswith(".json"))


def _path(name):
    return os.path.join(MEASUREMENTS, name)


def _dicts(node):
    """Every dict anywhere in a parsed manifest, the top one included."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _dicts(value)


def check_every_committed_manifest_is_classified():
    committed = set(_committed())
    named = set(KINDS)
    assert committed == named, (
        "measurements/ and KINDS disagree. Only in measurements/: {}. Only in KINDS: {}. "
        "A new manifest has to say whether it publishes counts or timings."
        .format(sorted(committed - named), sorted(named - committed)))


def check_every_counts_manifest_carries_no_wall_time():
    reached = 0
    for name, kind in sorted(KINDS.items()):
        if kind != COUNTS:
            continue
        assert_no_wall_time(_path(name))
        reached += 1
    assert reached == 5, "expected five counts manifests, graded {}".format(reached)


def check_the_wall_time_guard_can_fail():
    """A gate that has never refused anything is a gate nobody has tested.

    Both of the planted files go through the same helper the real check uses, so this
    grades the helper rather than a copy of its rule.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        clean = os.path.join(tmp, "clean.json")
        with open(clean, "w", encoding="utf-8") as fh:
            json.dump({"tables": 5, "verdict": "read"}, fh)
        assert assert_no_wall_time(clean) > 0

        for bad in ({"median_seconds": 0.2}, {"elapsed_ms": 204}, {"unit": "millis"}):
            planted = os.path.join(tmp, "planted.json")
            with open(planted, "w", encoding="utf-8") as fh:
                json.dump(bad, fh)
            try:
                assert_no_wall_time(planted)
            except AssertionError:
                continue
            raise AssertionError("the guard passed {}".format(bad))

        empty = os.path.join(tmp, "empty.json")
        open(empty, "w", encoding="utf-8").close()
        try:
            assert_no_wall_time(empty)
        except AssertionError:
            return
        raise AssertionError("the guard passed a file with nothing in it")


def check_every_published_duration_sits_beside_its_spread():
    """The second half of the rule, and the only half a timings manifest can satisfy.

    Any dict in a timings manifest that publishes a median has to publish the spread of
    the passes that median came from. A tight range with no spread reads as a property of
    the code and is mostly a property of the machine that ran it.
    """
    reached = 0
    for name, kind in sorted(KINDS.items()):
        if kind != TIMINGS:
            continue
        with open(_path(name), encoding="utf-8") as fh:
            published = json.load(fh)
        for node in _dicts(published):
            medians = [k for k in node if k.startswith("median")]
            if not medians:
                continue
            assert "spread" in node, (
                "{} publishes {} with no spread beside it".format(name, medians))
            assert node["spread"] >= 1.0, (
                "{} publishes a spread of {}, and the slowest pass over the fastest "
                "cannot be under one".format(name, node["spread"]))
            reached += 1
    assert reached > 0, "no published median was reached, so this checked nothing"


# The README publishes four rows of the plan split. `(columns, shape)` to the three shares,
# read off the file rather than typed here, because a second copy of the figures in this
# module would be the thing that goes stale.
README = os.path.join(HERE, "README.md")
PLAN_ROW = re.compile(
    r"^\s*(\d+)\s+(per column|batched)\s+\d+\s+\d+\s+"
    r"([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s*$")


def check_the_readme_plan_table_matches_the_manifest():
    """The README's shares against the manifest they came out of.

    Four rows rather than all eight, because the README is a front page and the narrow end
    and the wide end are what a reader needs. The rows it does show have to be the
    measurement's, and a regenerated manifest that moves them fails here.
    """
    published = {}
    with open(README, encoding="utf-8") as fh:
        for line in fh:
            hit = PLAN_ROW.match(line.rstrip("\n"))
            if hit:
                published[(int(hit.group(1)), hit.group(2))] = (
                    float(hit.group(3)), float(hit.group(4)), float(hit.group(5)))
    assert len(published) == 4, (
        "expected four plan rows in README.md, found {}".format(sorted(published)))

    with open(_path("batched-plan.json"), encoding="utf-8") as fh:
        measured = json.load(fh)
    by_key = {(m["columns"], m["shape"]):
              (m["share_planning"], m["share_executing"], m["share_unprofiled"])
              for m in measured["measurements"]}
    for key, shares in sorted(published.items()):
        assert key in by_key, "README publishes {} and the manifest has no such row".format(key)
        assert shares == by_key[key], (
            "README.md says {} for {} and the manifest says {}. Fix the README."
            .format(shares, key, by_key[key]))
