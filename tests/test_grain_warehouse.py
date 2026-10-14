"""Re-derive the committed lineage rescue measurement and fail when it drifts.

`tests/test_grain.py` says the arithmetic is sound. This says the numbers it produced on
the sample warehouse and its two regroupings are still the numbers in
`measurements/lineage-rescue.json`, which is what a README is allowed to quote.

It also pins the reason `pii/grain.py` exists rather than wrapping the k measurement that
was already in `pii/mask.py`. That reason is a refusal, and a refusal that stops being
raised would quietly make this whole module redundant without anything failing.

It needs a real database, so it runs under `tests/run_with_duckdb.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import classify, corpus, crawl, grain, lineage, mask, profile  # noqa: E402
from scripts.grain_probe import grade_all  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(HERE, "measurements", "lineage-rescue.json")
SHIPPED = "analytics.encounter_daily"

REGENERATE = ("python3 scripts/plant.py --db /tmp/pii.duckdb && "
              "python3 scripts/grain_probe.py --db /tmp/pii.duckdb "
              "--manifest measurements/lineage-rescue.json")

_STATE = {}


def _built():
    """A planted warehouse plus the two coarse marts, built once for every check here."""
    if "rows" in _STATE:
        return _STATE

    from scripts.plant import load

    import duckdb

    tmp = tempfile.mkdtemp(prefix="grain-check-")
    path = os.path.join(tmp, "pii.duckdb")
    # The same defaults scripts/plant.py carries. Written out rather than read off its
    # argument parser, for the reason tests/test_ablation_warehouse.py gives.
    load(path, corpus.generate(n_patients=1000, seed=20260917))
    con = duckdb.connect(path)
    for variant in grain.COARSER:
        grain.build_variant(con, variant)

    graph = lineage.warehouse_graph(con)
    for variant in grain.COARSER:
        graph = graph.merge(
            lineage.read_insert_select(variant.insert_sql(), list(variant.columns)))

    tables = [SHIPPED] + [v.table for v in grain.COARSER]
    everything = sorted(set(tables) | {"raw.encounter", "raw.patient"})
    upstream = graph.upstream_table_map(everything)
    profiles = profile.profile_crawl(con, crawl.crawl(con))

    _STATE.update(
        con=con, graph=graph, upstream=upstream, profiles=profiles, tables=tables,
        rows=grade_all(con, profiles, graph, upstream, tables), tmp=tmp,
    )
    return _STATE


def _committed():
    with open(MANIFEST) as fh:
        return json.load(fh)


def check_the_committed_manifest_is_still_what_the_probe_produces():
    got = grain.RescueReport(rows=tuple(_built()["rows"])).manifest()
    want = _committed()
    if got != want:
        raise AssertionError(
            "the lineage rescue measurement has drifted. regenerate with:\n  {}\n"
            "got {}\nwant {}".format(REGENERATE, got, want))


def check_the_shipped_mart_keeps_its_date_in_the_review_band():
    # The flagship result of the whole repo. A Safe Harbor temporal column on a mart that
    # names nobody still reaches a person, and now it reaches one on a measured share
    # rather than on the fact of being derived.
    row = [r for r in _built()["rows"] if r.table == SHIPPED][0]
    assert row.band == "review"
    assert row.singleton_rows == 1386
    assert row.rows == 1444


def check_coarsening_the_grain_takes_the_rescue_away():
    moved = [r for r in _built()["rows"] if r.moved]
    assert len(moved) == 2
    for row in moved:
        assert row.binary_band == "review"
        assert row.band == "ignore"


def check_k_on_the_grain_cannot_tell_the_rescued_table_from_the_refused_one():
    # The original plan was a k. Two of the three tables carry a k of 1
    # and end in different bands, so a rule reading k would have kept the binary it was
    # supposed to replace.
    report = grain.RescueReport(rows=tuple(_built()["rows"]))
    assert report.k_separates() is False
    ones = [r for r in _built()["rows"] if r.k_anonymity == 1]
    assert sorted({r.band for r in ones}) == ["ignore", "review"]


def check_the_weight_column_of_every_coarse_mart_comes_off_the_graph():
    state = _built()
    for variant in grain.COARSER:
        assert mask.weight_for(state["graph"], variant.table) == "encounters"


def check_the_coarse_marts_are_derived_from_a_table_that_names_people():
    # Without this the rescue would never fire on them and the comparison would be two
    # tables nobody tried to rescue.
    state = _built()
    for variant in grain.COARSER:
        assert state["upstream"][variant.table] == ("raw.encounter",)


def check_the_residual_measurement_refuses_on_the_table_that_needs_it():
    # The circularity `pii/grain.py` exists because of. `mask.residual_sql` is the other
    # k in this repo and it will not run while a quasi identifier of the table sits in
    # review, and the column in review is the one the measurement would inform.
    state = _built()
    results = classify.classify_warehouse(
        state["profiles"], upstream_tables=state["upstream"])
    policies = [p for p in mask.generate(results) if p.table == SHIPPED]
    try:
        mask.residual_sql(SHIPPED, policies, "encounters")
    except ValueError as exc:
        assert "day is unresolved" in str(exc)
        return
    raise AssertionError(
        "residual_sql ran on {} while its date was in review, so pii/grain.py may no "
        "longer be needed".format(SHIPPED))


def check_measuring_the_mart_by_row_count_reports_it_safer_than_it_is():
    # The failure `mask.weight_for` exists to stop, measured rather than described.
    state = _built()
    by_weight = grain.witness_for(state["con"], state["graph"], SHIPPED)
    by_rows = grain.measure_grain(state["con"], SHIPPED, None)
    assert by_rows.singleton_share == 1.0
    assert by_weight.singleton_share < 1.0
    assert by_rows.population == by_weight.rows


def check_classify_shipped_uses_the_graph_it_is_handed():
    # A mutant that inverted the `graph is None` test survived, because rebuilding the
    # graph from the catalog gives the same answer as the one every caller passes in. An
    # empty graph is the case where the two differ: no upstream edges means nothing to
    # rescue, so the mart's date loses its evidence again.
    state = _built()
    empty = lineage.Graph(edges=())
    got = [r for r in grain.classify_shipped(state["con"], state["profiles"], empty)
           if r.address == SHIPPED + ".day"][0]
    assert got.category_key == "not_personal"
    assert got.confidence == 0.0

    passed = [r for r in grain.classify_shipped(
        state["con"], state["profiles"], state["graph"])
        if r.address == SHIPPED + ".day"][0]
    assert passed.category_key == "event_date"
    assert passed.confidence == 0.4319


def check_classify_shipped_builds_its_own_graph_when_it_is_given_none():
    state = _built()
    got = [r for r in grain.classify_shipped(state["con"], state["profiles"])
           if r.address == SHIPPED + ".day"][0]
    assert got.confidence == 0.4319


def check_a_weight_column_the_table_does_not_carry_is_a_refusal():
    # The obfuscated arm found this on 2026-10-12. A graph in the original column names
    # pointed at a database where every column had been renamed, and the tool sent the
    # missing name to the engine and got a binder error back with its own SQL in it.
    state = _built()
    stale = lineage.Graph(edges=(
        lineage.Edge(
            source=lineage.ColumnRef(table="raw.encounter", column="*"),
            target=lineage.ColumnRef(table=SHIPPED, column="renamed_away"),
            kind=lineage.EdgeKind.AGGREGATE,
            detail="count(*)",
        ),
    ))
    try:
        grain.witness_for(state["con"], stale, SHIPPED)
    except ValueError as exc:
        assert "no such column" in str(exc)
        assert "renamed_away" in str(exc)
        return
    raise AssertionError("a weight column that does not exist was sent to the engine")
