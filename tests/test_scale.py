"""Checks for the scaling bench, over the parts of it that need no database.

The bench publishes a curve and a growth exponent, and both are arithmetic over numbers it
collected. The arithmetic is what can be wrong without anybody noticing, because a ratio
printed the wrong way round still looks like a measurement.

The fixture generation is checked here too. A bench whose columns are all one template
measures one kind of work and reports it as the general case, and the templates are chosen
to span what the predicate set does rather than to look like a warehouse.
"""

from __future__ import annotations

import json
import os

from pii import scale


def check_the_templates_cover_a_match_a_reject_and_an_empty_column():
    kinds = {name for name, _t, _e in scale.VALUE_TEMPLATES}
    # Each of these is a different amount of work for the predicate set, and a bench
    # missing one of them measures the wrong thing without saying so.
    assert {"email", "card", "code", "sex", "null"} <= kinds, kinds


def check_column_names_are_unique_and_carry_their_template():
    names = [scale.column_name(i) for i in range(len(scale.VALUE_TEMPLATES) * 3)]
    assert len(set(names)) == len(names)
    assert names[0].endswith("_email")
    assert names[len(scale.VALUE_TEMPLATES)].endswith("_email")


def check_columns_for_returns_the_requested_width_with_catalog_metadata():
    cols = scale.columns_for(7)
    assert len(cols) == 7
    assert [c.name for c in cols] == [scale.column_name(i) for i in range(7)]
    assert all(c.sql_type == "VARCHAR" for c in cols)
    # Every generated column is nullable and none is a key. The bench is about width and
    # a primary key on one of 672 columns would be a second variable.
    assert all(c.nullable and not c.is_key for c in cols)


def check_columns_for_zero_is_empty_rather_than_an_error():
    assert scale.columns_for(0) == ()


def check_per_column_ms_divides_by_the_width_and_survives_zero():
    t = scale.Timing("batched", 100, 5000, 1, 1234, 2.0)
    assert abs(t.per_column_ms - 20.0) < 1e-9
    assert scale.Timing("batched", 0, 0, 1, 0, 2.0).per_column_ms == 0.0


def check_speedups_divides_per_column_by_batched_and_not_the_other_way():
    # A ratio above one here means the per column shape took longer. The published line
    # inverts it to say how much slower batched was, so getting this backwards reverses
    # the headline result of the day.
    timings = [
        scale.Timing("per column", 42, 5000, 42, 400, 1.0),
        scale.Timing("batched", 42, 5000, 1, 200, 2.0),
    ]
    (width, time_ratio, sql_ratio), = scale.speedups(timings)
    assert width == 42
    assert abs(time_ratio - 0.5) < 1e-9
    assert abs(sql_ratio - 2.0) < 1e-9


def check_speedups_skips_a_width_that_only_has_one_shape():
    timings = [scale.Timing("per column", 42, 5000, 42, 400, 1.0)]
    assert scale.speedups(timings) == []


def check_the_growth_exponent_reads_one_for_linear_and_two_for_quadratic():
    linear = [scale.Timing("batched", w, 1, 1, 10 * w, 0.0) for w in (10, 100)]
    assert abs(scale.growth_exponent(linear, "batched") - 1.0) < 1e-9
    quadratic = [scale.Timing("batched", w, 1, 1, w * w, 0.0) for w in (10, 100)]
    assert abs(scale.growth_exponent(quadratic, "batched") - 2.0) < 1e-9


def check_the_growth_exponent_refuses_to_guess_from_one_point():
    one = [scale.Timing("batched", 42, 1, 1, 100, 0.0)]
    assert scale.growth_exponent(one, "batched") == 0.0
    assert scale.growth_exponent(one, "per column") == 0.0


def check_the_widths_start_at_the_shipped_warehouse_column_count():
    from pii.schema import TABLES
    shipped = sum(len(t.columns) for t in TABLES)
    assert scale.WIDTHS[0] == shipped, (scale.WIDTHS[0], shipped)
    assert list(scale.WIDTHS) == sorted(scale.WIDTHS)


def check_the_default_widths_stop_below_the_width_that_kills_the_process():
    # The batched shape at `CEILING_WIDTH` was killed by the OOM killer on both machines.
    # A default that reaches it turns a measurement into a dead run, which is how the
    # ceiling got found in the first place.
    assert scale.CEILING_WIDTH not in scale.WIDTHS
    assert max(scale.WIDTHS) < scale.CEILING_WIDTH


def check_the_sweep_cliff_is_the_highest_differing_value_and_not_the_lowest():
    # The first version of this walked up from the bottom of the sweep and returned 0.20,
    # which is the end of the list rather than an edge. 0.25 is where coming down from the
    # shipped 0.90 first changes anything.
    from pii import sample, sample_bench

    def row(c, signals):
        return sample_bench.FixtureSweepRow(c, signals, 2, 1, 1, 1, True)

    rows = [row(0.20, 4), row(0.25, 4), row(0.50, 3),
            row(sample.SHAPE_CONCENTRATION, 3), row(1.00, 3)]
    assert sample_bench.cliff(rows) == 0.25


def check_the_sweep_cliff_is_zero_when_nothing_below_the_shipped_value_differs():
    from pii import sample, sample_bench

    def row(c):
        return sample_bench.FixtureSweepRow(c, 3, 2, 1, 1, 1, True)

    assert sample_bench.cliff([row(0.20), row(sample.SHAPE_CONCENTRATION)]) == 0.0
    # And zero when the shipped value is not in the sweep at all, rather than guessing a
    # baseline from whatever happened to be first.
    assert sample_bench.cliff([row(0.20), row(0.25)]) == 0.0


# The committed scaling measurement. Only the machine independent half is checked here.
# The seconds and the peak memory are this machine on this day and pinning them would make
# the suite fail on a faster laptop, which is not a defect in the repo.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCALING = os.path.join(HERE, "measurements", "profiler-scaling.json")

REGENERATE_SCALING = (
    "for W in 42 84 168 336; do for K in per batched; do "
    "python3 scripts/scale_probe.py --widths $W --only $K --passes 5 "
    "--manifest measurements/profiler-scaling.json; done; done")


def _scaling():
    with open(SCALING, encoding="utf-8") as fh:
        return json.load(fh)


def check_the_published_statement_counts_are_one_per_column_and_one_per_table():
    for entry in _scaling()["timings"]:
        if entry["shape"] == "per column":
            assert entry["statements"] == entry["columns"], entry
        elif entry["shape"] == "batched":
            assert entry["statements"] == 1, entry
        else:
            raise AssertionError("unknown shape {}".format(entry["shape"]))


def check_the_published_sql_lengths_are_what_the_code_builds_today():
    from pii import profile
    for entry in _scaling()["timings"]:
        columns = scale.columns_for(entry["columns"])
        if entry["shape"] == "per column":
            built = sum(len(profile._profile_sql(
                scale.BENCH_SCHEMA, scale.BENCH_TABLE, c.name)[0]) for c in columns)
        else:
            built = len(profile._profile_table_sql(
                scale.BENCH_SCHEMA, scale.BENCH_TABLE, columns)[0])
        assert built == entry["sql_chars"], (
            "{} at {} columns builds {} characters of SQL and the manifest says {}. "
            "Regenerate with: {}".format(entry["shape"], entry["columns"], built,
                                         entry["sql_chars"], REGENERATE_SCALING))


def check_the_published_measurement_found_no_disagreement_between_the_shapes():
    # The batched path is only worth having if it returns the same numbers, and this is the
    # committed evidence that it did on the run the figures came off.
    assert _scaling()["disagreements"] == []


def check_the_published_measurement_names_the_ceiling_it_stopped_below():
    manifest = _scaling()
    assert manifest["ceiling_width"] == scale.CEILING_WIDTH
    batched_widths = [e["columns"] for e in manifest["timings"]
                      if e["shape"] == "batched"]
    # No batched reading at the ceiling, because there is no reading to have. The process
    # is killed before it prints one.
    assert scale.CEILING_WIDTH not in batched_widths
