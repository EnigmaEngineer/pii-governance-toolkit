"""Checks on the round two cost measurement, standard library only.

The timing is not checked. A wall time assertion on a shared machine is a flaky check
pretending to be a performance guarantee. What is checked is the call counts, the fixture's
shape, and that the memoised path and the reprofiling reference agree, which is the thing
the timing comparison is only meaningful because of.
"""

from __future__ import annotations

from pii import classify, roundtwo
from pii.classify import ColumnProfile


def check_the_fixture_rescues_the_marts_and_nothing_else():
    profiles = roundtwo.profiles_for(25)
    upstream = roundtwo.upstream_for(25)
    results = classify.classify_warehouse(profiles, upstream_tables=upstream)
    rescued = classify.person_linked_by_derivation(results, upstream)
    assert rescued == tuple("grainbench.mart_{:03d}".format(i)
                            for i in range(roundtwo.RESCUED))


def check_the_two_shapes_agree_on_every_column():
    profiles = roundtwo.profiles_for(25)
    upstream = roundtwo.upstream_for(25)
    assert roundtwo.disagreements(profiles, upstream) == ()


def check_they_agree_with_a_discount_too():
    # The discount is the only thing round two does that the round one results do not
    # already carry, so agreeing without one says less than agreeing with one.
    profiles = roundtwo.profiles_for(25)
    upstream = roundtwo.upstream_for(25)
    for share in (0.0, 0.5, 1.0):
        got = roundtwo.disagreements(profiles, upstream,
                                     {"grainbench.mart_001": share})
        assert got == (), (share, got)


def check_the_repeat_is_the_rescued_columns_and_not_the_warehouse():
    # This is the finding. The repeat is bounded by the rescued set, so it shrinks as a
    # share of the work as the warehouse grows, which is the opposite of what `ot-085`
    # expected when it called the cost a per-column one.
    wide = roundtwo.arm_cost(roundtwo.profiles_for(400),
                             roundtwo.upstream_for(400))
    narrow = roundtwo.arm_cost(roundtwo.profiles_for(5), roundtwo.upstream_for(5))
    assert wide.rescued_columns == narrow.rescued_columns
    assert wide.wasted_share < narrow.wasted_share


def check_the_repeat_is_proportional_when_everything_rescues():
    # And the other extreme, which is the case the memoisation is worth something in.
    total = 100
    cost = roundtwo.arm_cost(roundtwo.profiles_for(total, total - 1),
                             roundtwo.upstream_for(total, total - 1))
    assert cost.rescued_tables == total - 1
    assert cost.reprofiling > 1.9 * cost.memoised


def check_an_empty_warehouse_reports_no_waste_rather_than_dividing_by_zero():
    cost = roundtwo.ArmCost(columns=0, rescued_tables=0, rescued_columns=0)
    assert cost.wasted_share == 0.0
    assert cost.reprofiling == 0


def check_a_fixture_too_small_to_hold_its_marts_is_refused():
    try:
        roundtwo.table_names(3, rescued=5)
    except ValueError as exc:
        assert "cannot hold" in str(exc)
    else:
        raise AssertionError("a 3 table fixture accepted 5 marts")


def check_the_counting_connection_counts_and_forwards():
    class Fake:
        def __init__(self):
            self.seen = []
            self.flag = "untouched"

        def execute(self, sql, *args, **kwargs):
            self.seen.append(sql)
            return "result"

    fake = Fake()
    counted = roundtwo.CountingConnection(fake)
    assert counted.execute("SELECT 1") == "result"
    counted.execute("SELECT 2")
    assert counted.count == 2
    assert counted.statements == ["SELECT 1", "SELECT 2"]
    assert fake.seen == ["SELECT 1", "SELECT 2"]
    # Anything that is not execute has to reach the real connection, or a measurement
    # wrapper silently changes the behaviour it is measuring.
    assert counted.flag == "untouched"
    counted.reset()
    assert counted.count == 0


def check_settle_table_refuses_results_that_are_not_its_profiles():
    profiles = roundtwo.profiles_for(5)
    one = [p for p in profiles if p.column == "email"]
    other = [p for p in profiles if p.column == "code"]
    try:
        classify.settle_table(one, [classify.classify_column(other[0])])
    except ValueError as exc:
        assert "does not belong" in str(exc)
    else:
        raise AssertionError("a result for another column was accepted")


def check_settle_table_refuses_a_length_mismatch():
    profiles = roundtwo.profiles_for(5)[:3]
    try:
        classify.settle_table(profiles, [classify.classify_column(profiles[0])])
    except ValueError as exc:
        assert "round one results" in str(exc)
    else:
        raise AssertionError("two profiles and one result was accepted")


def check_settle_table_on_cached_results_matches_classify_table():
    profiles = [p for p in roundtwo.profiles_for(5)
                if p.table == "grainbench.mart_000"]
    cached = tuple(classify.classify_column(p) for p in profiles)
    for linked in (None, True, False):
        direct = classify.classify_table(profiles, person_linked=linked)
        cheap = classify.settle_table(profiles, cached, person_linked=linked)
        assert direct == cheap, linked


def check_a_grain_source_is_asked_once_for_the_rescued_tables():
    profiles = roundtwo.profiles_for(25)
    upstream = roundtwo.upstream_for(25)
    asked = []

    def source(tables):
        asked.append(tables)
        return {t: 1.0 for t in tables}

    classify.classify_warehouse(profiles, upstream_tables=upstream,
                                grain_confidence=source)
    assert len(asked) == 1
    assert asked[0] == tuple("grainbench.mart_{:03d}".format(i)
                             for i in range(roundtwo.RESCUED))


def check_a_grain_source_that_answers_short_is_refused():
    profiles = roundtwo.profiles_for(25)
    upstream = roundtwo.upstream_for(25)
    try:
        classify.classify_warehouse(profiles, upstream_tables=upstream,
                                    grain_confidence=lambda tables: {})
    except ValueError as exc:
        assert "did not answer" in str(exc)
    else:
        raise AssertionError("a grain source answering for nothing was accepted")


def check_a_grain_source_is_not_called_when_nothing_rescues():
    # No upstream map means no round two, so a caller holding a connection should not pay
    # for one. Passing the map as an empty dict is the same case through a different door.
    profiles = roundtwo.profiles_for(25)
    calls = []

    def source(tables):
        calls.append(tables)
        return {}

    classify.classify_warehouse(profiles, grain_confidence=source)
    assert calls == []
    classify.classify_warehouse(profiles, upstream_tables={}, grain_confidence=source)
    assert calls == [()]


def check_a_dict_still_works_and_only_the_listed_table_moves():
    # The dict form is what every check and the sweeps pass, so it has to keep working
    # alongside the callable. A table the dict says nothing about keeps the undiscounted
    # rescue, which is the behaviour from before the discount existed.
    profiles = roundtwo.profiles_for(5)
    upstream = roundtwo.upstream_for(5)
    whole = {r.address: r.confidence
             for r in classify.classify_warehouse(profiles, upstream_tables=upstream)}
    listed = {r.address: r.confidence for r in classify.classify_warehouse(
        profiles, upstream_tables=upstream,
        grain_confidence={"grainbench.mart_000": 0.5})}
    moved = sorted(a for a in whole if whole[a] != listed[a])
    assert moved == ["grainbench.mart_000.day"], moved
    assert listed["grainbench.mart_000.day"] < whole["grainbench.mart_000.day"]


def check_the_profiles_the_fixture_builds_hold_no_values():
    for p in roundtwo.profiles_for(25):
        assert isinstance(p, ColumnProfile)
        for value in (p.table, p.column, p.sql_type):
            assert isinstance(value, str)
        assert p.sampled_signals == ()


# The checks below were all added by the 2026-10-13 audit, after a mutation pass over
# pii/roundtwo.py left 35 of 60 sites alive. Most of the survivors are fixture plausibility
# numbers no arm reads. These are the ones that were not.

def check_the_cost_records_are_immutable():
    # Yesterday's audit found four frozen dataclass flips surviving and added a check. The
    # three records added today had the same gap, which says the lesson needs to be a habit
    # rather than a one off fix.
    import dataclasses

    for cls in (roundtwo.ArmCost, roundtwo.GrainCost, roundtwo.Timing):
        assert dataclasses.fields(cls)
        assert cls.__dataclass_params__.frozen, cls.__name__


def check_the_statements_saved_is_a_difference_and_not_a_sum():
    # 397 is a published figure and it is this subtraction. A mutant turning it into an
    # addition reported 409 saved out of 403 sent and survived, because the only check on
    # it compared two warehouses rather than pinning the arithmetic.
    cost = roundtwo.GrainCost(eager_tables=400, lazy_tables=3,
                              eager_statements=403, lazy_statements=6)
    assert cost.saved == 397


def check_the_per_table_figure_is_statements_over_tables():
    cost = roundtwo.GrainCost(eager_tables=400, lazy_tables=3,
                              eager_statements=403, lazy_statements=6)
    assert abs(cost.per_eager_table - 1.0075) < 1e-9
    empty = roundtwo.GrainCost(eager_tables=0, lazy_tables=0,
                               eager_statements=0, lazy_statements=0)
    assert empty.per_eager_table == 0.0


def check_milliseconds_are_seconds_times_a_thousand():
    assert abs(roundtwo.Timing(seconds=0.013690).ms - 13.690) < 1e-9


def check_a_timing_is_a_positive_duration_and_not_a_clock_reading():
    # Not a performance assertion. `time.perf_counter` returns an arbitrary origin, so a
    # mutant that adds the start instead of subtracting it reports a number in the
    # thousands and every bound below is loose enough to survive a slow machine.
    timed = roundtwo.time_call(lambda: sum(range(1000)), passes=3)
    assert 0.0 <= timed.seconds < 5.0, timed.seconds


def check_time_call_takes_the_median_and_not_the_worst_pass():
    # The median is why one slow pass does not reach the published number, so the order
    # statistic is worth pinning. Driven through a stub whose durations are known rather
    # than through a sleep, because a sleep makes this a timing check.
    from pii import scale
    assert scale.median([0.1, 0.2, 9.0]) == 0.2
    assert scale.median([0.1, 0.3]) == 0.2
    assert scale.median([]) == 0.0


def check_a_mart_whose_source_is_not_in_the_warehouse_does_not_rescue():
    # Round two asks whether any upstream table names people, and a source the profiles do
    # not contain has no answer. The default has to be no. A mutant flipping it to yes
    # survived every other check here, because the fixture's upstream map only ever names
    # tables the fixture also profiles.
    profiles = roundtwo.profiles_for(5)
    mart = "grainbench.mart_000"
    upstream = dict(roundtwo.upstream_for(5))
    upstream[mart] = ("warehouse.nobody_profiled_this",)
    results = classify.classify_warehouse(profiles, upstream_tables=upstream)
    day = [r for r in results if r.address == "{}.day".format(mart)][0]
    assert day.category_key == "not_personal"
    assert day.confidence == 0.0

    cost = roundtwo.arm_cost(profiles, upstream)
    assert cost.rescued_tables == roundtwo.RESCUED - 1
    assert roundtwo.disagreements(profiles, upstream) == ()


def check_settle_table_catches_a_mismatch_in_either_half_of_the_address():
    # The guard reads `table != table or column != column` and a mutation turning the or
    # into an and survived the first version of this file, because the one check on it
    # passed a result whose table AND column both differed. Each half needs its own case
    # or the guard is half a guard. A rescued column landing on the wrong row of the same
    # table is the quietest form of this bug.
    profiles = roundtwo.profiles_for(5)
    mart_day = [p for p in profiles if p.address == "grainbench.mart_000.day"]
    same_table_other_column = [p for p in profiles
                               if p.address == "grainbench.mart_000.encounters"][0]
    other_table_same_column = [p for p in profiles
                               if p.address == "grainbench.mart_001.day"][0]

    for wrong in (same_table_other_column, other_table_same_column):
        try:
            classify.settle_table(mart_day, [classify.classify_column(wrong)])
        except ValueError as exc:
            assert "does not belong" in str(exc)
        else:
            raise AssertionError("accepted a result for {}".format(wrong.address))
