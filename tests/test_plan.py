"""The phase split, graded without a database.

`pii/plan.split` takes a profile DuckDB produced and a wall time the caller measured and
turns them into three parts. Everything interesting about it is arithmetic over a dict, so
it is graded on dicts. The live half is in `tests/test_plan_warehouse.py`.
"""

from __future__ import annotations

from pii import plan

PROFILE = {
    "latency": 1.5,
    "planner": 0.08,
    "all_optimizers": 0.01,
    "physical_planner": 0.01,
}


def _split(wall=2.0, **over):
    got = dict(PROFILE)
    got.update(over)
    return plan.split("batched", 168, 1513, 130430, wall, got)


def check_the_three_parts_add_back_up_to_the_wall_time():
    taken = _split()
    assert abs(taken.planning - 0.10) < 1e-9
    assert abs(taken.executing - 1.40) < 1e-9
    assert abs(taken.unprofiled - 0.50) < 1e-9
    total = taken.planning + taken.executing + taken.unprofiled
    assert abs(total - taken.wall) < 1e-9
    shares = sum(taken.share(part) for part in
                 (plan.PLANNING, plan.EXECUTING, plan.UNPROFILED))
    assert abs(shares - 1.0) < 1e-9


def check_the_verdict_names_whichever_part_is_biggest():
    # All three outcomes, because a verdict that can only ever come back one way is not
    # reading the numbers.
    assert _split().verdict == plan.EXECUTING
    assert _split(wall=20.0).verdict == plan.UNPROFILED
    assert _split(planner=1.4).verdict == plan.PLANNING


def check_a_profile_that_cannot_support_the_split_is_refused():
    """Four ways the inputs can be wrong, each refused rather than returned.

    A missing metric is the one worth naming. Defaulting it to zero would report a phase
    that cost nothing, and a phase that cost nothing is indistinguishable here from a phase
    nobody measured.
    """
    cases = []
    for metric in plan.PLANNING_METRICS + ("latency",):
        missing = {k: v for k, v in PROFILE.items() if k != metric}
        cases.append((KeyError, lambda m=missing: plan.split(
            "batched", 1, 1, 1, 1.0, m)))
    # Phases adding to more than the latency they sit inside.
    cases.append((ValueError, lambda: _split(planner=2.0)))
    # A query that took longer than the wall clock around it.
    cases.append((ValueError, lambda: _split(wall=0.5)))

    for expected, call in cases:
        try:
            call()
        except expected:
            continue
        raise AssertionError("{} was not raised".format(expected.__name__))

    # And a share of nothing, which is a division rather than a split.
    taken = plan.split("batched", 1, 1, 1, 0.0, dict(PROFILE, latency=0.0,
                                                     planner=0.0, all_optimizers=0.0,
                                                     physical_planner=0.0))
    try:
        taken.share(plan.PLANNING)
    except ValueError:
        return
    raise AssertionError("a zero wall time returned a share")


def check_a_split_is_frozen():
    taken = _split()
    try:
        taken.latency = 99.0
    except Exception:
        return
    raise AssertionError("PlanSplit is not frozen")
