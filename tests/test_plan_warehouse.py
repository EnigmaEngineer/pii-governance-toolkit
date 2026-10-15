"""The phase split against a real database, and the manifest it published.

The counts in `measurements/batched-plan.json` are re-derived here from the code as it
stands today, which is the half of the manifest that has to agree exactly. The shares are
this machine's and are graded on their verdict and on their ordering instead, because a
share measured on another machine will be close and will not be equal.
"""

from __future__ import annotations

import json
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import plan, profile, scale  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(HERE, "measurements", "batched-plan.json")

NARROW = 42


def _published():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def _expected_counts(width):
    """What the two shapes cost in statements, expressions and SQL characters.

    Counted off the same SQL builders the profiler calls, so this fails when a predicate
    is added and the manifest is not regenerated.
    """
    columns = scale.columns_for(width)
    sql, _keys, stride = profile._profile_table_sql(
        scale.BENCH_SCHEMA, scale.BENCH_TABLE, columns)
    pieces = [profile._profile_sql(scale.BENCH_SCHEMA, scale.BENCH_TABLE, c.name)
              for c in columns]
    return {
        "batched": {"statements": 1, "expressions": 1 + stride * width,
                    "sql_chars": len(sql)},
        "per column": {"statements": width,
                       "expressions": sum(4 + len(keys) for _, keys in pieces),
                       "sql_chars": sum(len(one) for one, _ in pieces)},
    }


def check_the_manifest_covers_the_shipped_width_ladder():
    published = _published()
    assert published["widths"] == list(scale.WIDTHS), (
        "the manifest publishes {} and the bench ladder is {}"
        .format(published["widths"], list(scale.WIDTHS)))
    seen = {(m["columns"], m["shape"]) for m in published["measurements"]}
    assert seen == {(w, s) for w in scale.WIDTHS for s in ("batched", "per column")}


def check_every_published_count_is_what_the_builders_produce_today():
    published = _published()
    checked = 0
    for width in published["widths"]:
        expected = _expected_counts(width)
        for row in published["measurements"]:
            if row["columns"] != width:
                continue
            for field, value in expected[row["shape"]].items():
                assert row[field] == value, (
                    "manifest says {} columns {} has {} of {} and the builder makes {}"
                    .format(width, row["shape"], field, row[field], value))
                checked += 1
    assert checked == len(published["widths"]) * 6, checked


def check_the_batched_statement_spends_its_time_executing_rather_than_planning():
    """The answer the manifest exists to publish, re-measured at the narrowest width.

    Only the narrow width, because the point here is that the verdict re-derives and the
    wide end costs seconds a pass. The ordering assertion is the stronger half. The batched
    shape issues one statement and so pays for planning once, and it is still slower, which
    is only possible if what it adds to execution is larger than what it saves.
    """
    con = duckdb.connect(":memory:")
    scale.build(con, NARROW, 500)
    columns = scale.columns_for(NARROW)

    sql, _keys, stride = profile._profile_table_sql(
        scale.BENCH_SCHEMA, scale.BENCH_TABLE, columns)
    got, wall = plan.profile_statement(con, sql)
    batched = plan.split("batched", NARROW, 1 + stride * NARROW, len(sql), wall, got)
    assert batched.verdict == plan.EXECUTING, batched.verdict

    totals = dict(latency=0.0, planner=0.0, all_optimizers=0.0, physical_planner=0.0)
    per_wall = 0.0
    for column in columns:
        one, _keys = profile._profile_sql(
            scale.BENCH_SCHEMA, scale.BENCH_TABLE, column.name)
        got, one_wall = plan.profile_statement(con, one)
        per_wall += one_wall
        for key in totals:
            totals[key] += float(got[key])
    per = plan.split("per column", NARROW, NARROW * 10, 0, per_wall, totals)

    assert per.share(plan.PLANNING) > batched.share(plan.PLANNING), (
        "one statement should spend a smaller share on planning than {} of them, "
        "got {} against {}".format(NARROW, batched.share(plan.PLANNING),
                                   per.share(plan.PLANNING)))
    verdicts = {v for m in _published()["measurements"] for v in m["verdict"]}
    assert verdicts == {plan.EXECUTING}, verdicts


def check_the_profile_comes_back_detailed_or_not_at_all():
    # `profiling_mode` has a standard setting that carries none of the three phases, and a
    # split built on it would be a split of nothing. This is the guard on the instrument.
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t AS SELECT i FROM range(0, 10) s(i)")
    got, wall = plan.profile_statement(con, "SELECT count(*) FROM t")
    assert wall > 0
    for metric in plan.PLANNING_METRICS + ("latency",):
        assert metric in got, metric
