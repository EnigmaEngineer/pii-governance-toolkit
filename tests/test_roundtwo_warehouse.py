"""Round two's cost against a real database, so the statement counts are real counts.

Everything here needs the driver. The point of the file is that a claim about round trips
cannot be made from a fixture built in Python, and the central claim of 2026-10-13 is that
round two sends none.
"""

from __future__ import annotations

import os
import tempfile

from pii import classify, crawl, grain, lineage, profile, roundtwo

_state = {}


def _warehouse():
    """The sample warehouse, built once per process and shared.

    Planting it costs a few seconds and five checks want it. `tests/test_grain_warehouse.py`
    does the same thing for the same reason.
    """
    if _state:
        return _state
    import duckdb

    from pii import corpus
    from scripts.plant import load

    path = os.path.join(tempfile.mkdtemp(prefix="roundtwo-"), "pii.duckdb")
    # The same defaults scripts/plant.py carries, written out rather than read off its
    # argument parser, which is what tests/test_grain_warehouse.py does and why.
    load(path, corpus.generate(n_patients=1000, seed=20260917))
    con = duckdb.connect(path)
    profiles = profile.profile_crawl(con, crawl.crawl(con))
    graph = lineage.warehouse_graph(con)
    tables = sorted({p.table for p in profiles})
    _state.update(con=con, profiles=profiles, graph=graph, tables=tables,
                  upstream=graph.upstream_table_map(tables))
    return _state


def check_round_two_sends_no_statements_at_all():
    # This is the half of `ot-085` that was wrong. The thread called round two's cost
    # database round trips because the value arm sends a query per column. It does, at
    # profile time. By the time the classifier runs, the counts are on the profile and the
    # arms reach no connection, so a second pass over them cannot cost a statement.
    state = _warehouse()
    counted = roundtwo.CountingConnection(state["con"])
    classify.classify_warehouse(state["profiles"], upstream_tables=state["upstream"],
                                grain_confidence={})
    assert counted.count == 0


def check_classify_shipped_only_measures_the_grain_of_what_it_rescues():
    state = _warehouse()
    counted = roundtwo.CountingConnection(state["con"])
    grain.classify_shipped(counted, state["profiles"], state["graph"])
    lazy = counted.count

    counted.reset()
    grain.confidence_map(counted, state["graph"], state["tables"])
    eager = counted.count

    assert lazy < eager, (lazy, eager)
    # One table of the five rescues and it is pre-aggregated, so it costs the catalog
    # lookup for the weight column and the grain statement itself.
    assert lazy == 2, lazy


def check_a_grouped_table_costs_two_statements_and_an_ungrouped_one_costs_one():
    # `ot-138` said one statement per table. It is one for a table the graph says was never
    # grouped and two for one it says was, because the weight column is checked against the
    # catalog before it is used. Worth pinning, because the per-table figure the cost curve
    # publishes is between the two and a reader will want to know why.
    state = _warehouse()
    counted = roundtwo.CountingConnection(state["con"])
    grain.confidence_map(counted, state["graph"], ["analytics.encounter_daily"])
    grouped = counted.count
    counted.reset()
    grain.confidence_map(counted, state["graph"], ["raw.patient"])
    plain = counted.count
    assert (grouped, plain) == (2, 1), (grouped, plain)


def check_the_lazy_grain_gives_the_same_answer_as_the_eager_one():
    state = _warehouse()
    lazily = grain.classify_shipped(state["con"], state["profiles"], state["graph"])
    eagerly = classify.classify_warehouse(
        state["profiles"], upstream_tables=state["upstream"],
        grain_confidence=dict(grain.confidence_map(
            state["con"], state["graph"], state["tables"])))
    assert [(r.address, r.category_key, r.confidence) for r in lazily] == \
           [(r.address, r.category_key, r.confidence) for r in eagerly]


def check_the_memoised_and_reprofiling_shapes_agree_on_the_shipped_warehouse():
    state = _warehouse()
    measured = dict(grain.confidence_map(
        state["con"], state["graph"], state["tables"]))
    assert roundtwo.disagreements(
        state["profiles"], state["upstream"], measured) == ()


def check_the_fixture_builds_the_tables_it_says_it_does():
    import duckdb

    path = os.path.join(tempfile.mkdtemp(prefix="roundtwo-bench-"), "bench.duckdb")
    con = duckdb.connect(path)
    made = roundtwo.build(con, 9)
    assert made == 9
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
        [roundtwo.BENCH_SCHEMA]).fetchall()
    assert len(rows) == 9, rows
    # The marts have to be genuinely grouped or the grain measurement has nothing to read.
    grouped = con.execute(
        "SELECT count(*) FROM {}.mart_000".format(roundtwo.BENCH_SCHEMA)).fetchone()[0]
    source = con.execute(
        "SELECT count(*) FROM {}.person".format(roundtwo.BENCH_SCHEMA)).fetchone()[0]
    assert grouped < source, (grouped, source)


def check_the_bench_graph_names_the_weight_column_the_bench_tables_carry():
    # A graph built by hand can name a column the table does not have, which is the stale
    # lineage failure `witness_for` started refusing on 2026-10-12. Checked here because
    # this graph is built by hand and that refusal would read as a fixture bug.
    import duckdb

    from pii import mask
    path = os.path.join(tempfile.mkdtemp(prefix="roundtwo-graph-"), "bench.duckdb")
    con = duckdb.connect(path)
    roundtwo.build(con, 9)
    graph = roundtwo.graph_for(9)
    for table in roundtwo.table_names(9):
        weight = mask.weight_for(graph, table)
        if weight is None:
            continue
        assert weight in grain.columns_of(con, table), (table, weight)
        assert grain.witness_for(con, graph, table).was_grouped


def check_the_grain_cost_saves_more_as_the_warehouse_grows():
    import duckdb

    path = os.path.join(tempfile.mkdtemp(prefix="roundtwo-curve-"), "bench.duckdb")
    con = duckdb.connect(path)
    saved = []
    for total in (9, 25):
        roundtwo.build(con, total)
        graph = roundtwo.graph_for(total)
        names = list(roundtwo.table_names(total))
        rescued = [n for n in names if ".mart_" in n]
        cost = roundtwo.grain_cost(con, graph, names, rescued)
        assert cost.lazy_statements == 2 * roundtwo.RESCUED, cost
        saved.append(cost.saved)
    assert saved[1] > saved[0], saved


def check_the_committed_manifest_still_re_derives():
    """The figures the README quotes, re-derived rather than trusted.

    Every other measurement in this repo has had this check since the day it was
    published. This one was written after the audit found a timing range in the README that
    nothing could reproduce. The manifest holds counts and no wall times, so this check is
    sound on any machine, which is the reason it can exist at all.
    """
    import json

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "measurements", "roundtwo-cost.json"),
              encoding="utf-8") as fh:
        committed = json.load(fh)

    w = _warehouse()
    cost = roundtwo.arm_cost(w["profiles"], w["upstream"])
    counted = roundtwo.CountingConnection(w["con"])
    grain.classify_shipped(counted, w["profiles"], w["graph"])
    lazy_statements = counted.count
    counted.reset()
    grain.confidence_map(counted, w["graph"], w["tables"])
    eager_statements = counted.count

    shipped = committed["shipped"]
    assert shipped["columns"] == cost.columns, (shipped["columns"], cost.columns)
    assert shipped["arm_runs_memoised"] == cost.memoised
    assert shipped["arm_runs_reprofiling"] == cost.reprofiling
    assert shipped["statements_lazy"] == lazy_statements, (
        "manifest says classify_shipped sends {} statements and it sends {}. "
        "Regenerate with scripts/roundtwo_probe.py --manifest".format(
            shipped["statements_lazy"], lazy_statements))
    assert shipped["statements_eager"] == eager_statements

    for row in committed["grain_curve"]:
        assert row["eager"] - row["lazy"] == row["saved"], row

    for row in committed["double_pass"]:
        profiles = roundtwo.profiles_for(row["tables"], row["tables"] - 1)
        upstream = roundtwo.upstream_for(row["tables"], row["tables"] - 1)
        c = roundtwo.arm_cost(profiles, upstream)
        assert row["columns"] == c.columns, (row, c.columns)
        assert row["arm_runs_memoised"] == c.memoised, (row, c.memoised)
        assert row["arm_runs_reprofiling"] == c.reprofiling, (row, c.reprofiling)


def check_the_manifest_carries_no_wall_time():
    """A committed figure has to re-derive somewhere else and a millisecond does not.

    This is the check that stops the defect coming back by a different door. A future run
    that adds a timing to the manifest gets told here rather than in a README nobody can
    reproduce.
    """
    import json

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "measurements", "roundtwo-cost.json"),
              encoding="utf-8") as fh:
        text = fh.read()
    for word in ("_ms", "seconds", "millis"):
        assert word not in text, (
            "{} is a wall time and the manifest is counts only".format(word))
