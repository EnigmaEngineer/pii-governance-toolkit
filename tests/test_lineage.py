"""Checks for the lineage graph and the SQL reader.

Nothing here touches a database. The reader takes a string and returns edges, which is the
property that made it worth writing as a reader rather than as a call into the engine's own
lineage view. The catalog half lives in `tests/test_crawl.py` where the connection already
exists.

The refusal checks are the important half of this file. A parser that returns an empty
graph and a parser that returns a wrong graph look identical to a caller that only counts
edges, and the difference between them is the whole argument for this module.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import lineage, schema  # noqa: E402
from pii.lineage import ColumnRef, Edge, EdgeKind, Graph  # noqa: E402

MART = "analytics.encounter_daily"
MART_COLUMNS = ["day", "department", "postal_code", "encounters", "mean_length_of_stay_h"]


def _mart_graph() -> Graph:
    return lineage.read_insert_select(schema.DERIVED_SQL[MART], MART_COLUMNS)


def _kinds(graph: Graph):
    return {(e.source.address, e.target.address): e.kind for e in graph.edges}


def check_the_mart_statement_parses_with_no_refusals():
    g = _mart_graph()
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert len(g.edges) == 7, len(g.edges)


def check_a_grouping_key_is_not_an_aggregate():
    # The one people get wrong. A GROUP BY drops duplicate rows and leaves the values in
    # the grouping key exactly as they were.
    kinds = _kinds(_mart_graph())
    assert kinds[("raw.patient.postal_code", MART + ".postal_code")] is EdgeKind.GROUPED
    assert kinds[("raw.encounter.department", MART + ".department")] is EdgeKind.GROUPED


def check_a_cast_is_read_as_a_cast_and_carries_the_target_type():
    g = _mart_graph()
    edge = [e for e in g.edges if e.target.column == "day"][0]
    assert edge.kind is EdgeKind.CAST, edge.kind
    assert "DATE" in edge.detail, edge.detail


def check_the_type_inside_a_cast_is_not_read_as_a_column():
    # This failed on the first run of the reader against the real statement. `DATE` came
    # back as an unqualified column reference and the select item was refused over it.
    g = _mart_graph()
    names = {e.source.column for e in g.edges}
    assert "DATE" not in names, sorted(names)
    assert "date" not in names, sorted(names)


def check_a_row_count_names_no_column_and_is_still_a_derivation():
    g = _mart_graph()
    into = g.into(MART + ".encounters")
    assert len(into) == 2, into
    assert all(e.kind is EdgeKind.AGGREGATE for e in into)
    assert {e.source.column for e in into} == {"*"}
    # And the target is therefore not a root, because it did come from somewhere.
    assert MART + ".encounters" not in g.roots([MART + ".encounters"])


def check_an_aggregate_over_two_columns_records_both():
    g = _mart_graph()
    sources = {e.source.address for e in g.into(MART + ".mean_length_of_stay_h")}
    assert sources == {"raw.encounter.admitted_at", "raw.encounter.discharged_at"}, sources


def check_a_function_name_is_not_a_column():
    g = _mart_graph()
    names = {e.source.column for e in g.edges}
    for word in ("count", "avg", "date_diff"):
        assert word not in names, (word, sorted(names))


def check_the_reader_refuses_what_it_cannot_prove():
    cases = {
        "INSERT INTO a.b SELECT * FROM raw.patient": "star expansion",
        "INSERT INTO a.b WITH x AS (SELECT 1) SELECT c FROM x": "unsupported: with",
        "INSERT INTO a.b SELECT c FROM r.p UNION SELECT d FROM r.q": "unsupported: union",
        "INSERT INTO a.b SELECT row_number() OVER () AS n FROM r.p": "unsupported: over",
        "INSERT INTO a.b SELECT t.c AS c FROM (SELECT 1 AS c) t": "subquery in from",
        "INSERT INTO a.b SELECT p.d::DATE AS d FROM r.p p": "shorthand cast",
    }
    for sql, expected in cases.items():
        g = lineage.read_insert_select(sql)
        assert g.edges == (), (sql, g.edges)
        assert any(r.what == expected for r in g.refusals), (sql, [r.what for r in g.refusals])


def check_an_unqualified_column_with_two_sources_is_refused_not_guessed():
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT city AS c FROM raw.patient p JOIN raw.claim k ON 1=1")
    assert g.edges == ()
    assert [r.what for r in g.refusals] == ["unqualified column"]


def check_an_unqualified_column_with_one_source_resolves():
    g = lineage.read_insert_select("INSERT INTO a.b SELECT city AS c FROM raw.patient")
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert g.edges[0].source.address == "raw.patient.city"


def check_an_arity_mismatch_falls_back_to_the_alias_and_says_so():
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT p.city AS c FROM raw.patient p", ["c", "extra"])
    assert any(r.what == "arity mismatch" for r in g.refusals), g.refusals
    assert g.edges[0].target.column == "c"


def check_an_explicit_column_list_beats_the_alias():
    g = lineage.read_insert_select(
        "INSERT INTO a.b (renamed) SELECT p.city AS c FROM raw.patient p")
    assert g.edges[0].target.column == "renamed", g.edges[0]


def check_a_comment_in_the_statement_does_not_become_a_column():
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT p.city AS c -- pick the city\n FROM raw.patient p")
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {e.source.address for e in g.edges} == {"raw.patient.city"}


def check_a_join_key_is_not_a_data_flow():
    key = Edge(ColumnRef("raw.patient", "patient_id"),
               ColumnRef("raw.encounter", "patient_id"), EdgeKind.JOIN_KEY)
    g = Graph(edges=(key,))
    assert g.derivation_edges() == ()
    # A foreign key must not make the referencing column look derived, or every child
    # table in the warehouse would inherit its parent's labels through its own id column.
    assert g.roots(["raw.encounter.patient_id"]) == ("raw.encounter.patient_id",)
    assert g.sources_of("raw.encounter.patient_id") == ()


def check_walking_upstream_survives_a_cycle():
    a = Edge(ColumnRef("s.one", "x"), ColumnRef("s.two", "x"), EdgeKind.COPY)
    b = Edge(ColumnRef("s.two", "x"), ColumnRef("s.one", "x"), EdgeKind.COPY)
    g = Graph(edges=(a, b))
    assert set(g.sources_of("s.one.x")) == {"s.one.x", "s.two.x"}


class _Result:
    """A stand in for `classify.Classification` with only what `propagate` reads."""

    def __init__(self, address, category, confidence):
        self.address = address
        self.category_key = category
        self.confidence = confidence


def check_a_grouped_column_inherits_and_an_aggregated_one_does_not():
    g = Graph(edges=(
        Edge(ColumnRef("raw.p", "postal_code"), ColumnRef("m.t", "geo"), EdgeKind.GROUPED),
        Edge(ColumnRef("raw.p", "postal_code"), ColumnRef("m.t", "n"), EdgeKind.AGGREGATE),
    ))
    results = [
        _Result("raw.p.postal_code", "postal_code", 0.90),
        _Result("m.t.geo", "not_personal", 0.0),
        _Result("m.t.n", "not_personal", 0.0),
    ]
    by = {i.address: i for i in lineage.propagate(g, results)}
    assert by["m.t.geo"].inherited_category == "postal_code"
    assert by["m.t.geo"].upgrade is True
    assert by["m.t.n"].inherited_category == "not_personal"
    assert by["m.t.n"].upgrade is False


def check_the_strongest_upstream_wins_when_two_columns_feed_one():
    # Otherwise a personal column is laundered by concatenating a harmless one onto it.
    g = Graph(edges=(
        Edge(ColumnRef("raw.p", "email"), ColumnRef("m.t", "blob"), EdgeKind.TRANSFORM),
        Edge(ColumnRef("raw.p", "city"), ColumnRef("m.t", "blob"), EdgeKind.COPY),
    ))
    results = [
        _Result("raw.p.email", "email", 0.90),
        _Result("raw.p.city", "not_personal", 0.10),
        _Result("m.t.blob", "not_personal", 0.0),
    ]
    by = {i.address: i for i in lineage.propagate(g, results)}
    # Only the COPY edge preserves the value, so the city is what arrives. A transform is
    # not value preserving and cannot be the thing that carries the email down.
    assert by["m.t.blob"].inherited_category == "not_personal", by["m.t.blob"]

    g2 = Graph(edges=(
        Edge(ColumnRef("raw.p", "email"), ColumnRef("m.t", "blob"), EdgeKind.COPY),
        Edge(ColumnRef("raw.p", "city"), ColumnRef("m.t", "blob"), EdgeKind.COPY),
    ))
    by2 = {i.address: i for i in lineage.propagate(g2, results)}
    assert by2["m.t.blob"].inherited_category == "email", by2["m.t.blob"]


def check_a_root_says_so_rather_than_claiming_it_inherited_nothing():
    g = Graph(edges=())
    out = lineage.propagate(g, [_Result("raw.p.email", "email", 0.9)])
    assert out[0].inherited_category is None
    assert out[0].disagrees is False
    assert "root" in out[0].rule


def check_the_nullability_breach_check_finds_the_one_from_2026_09_18():
    # `analytics.encounter_daily.department` was declared NOT NULL over a nullable source
    # and nothing could see it until the corpus wrote a null and the load died. The schema
    # is fixed now, so the shape is reconstructed here rather than left untested.
    g = Graph(edges=(
        Edge(ColumnRef("raw.encounter", "department"),
             ColumnRef(MART, "department"), EdgeKind.GROUPED),
    ))
    nullable = {"raw.encounter.department": True, MART + ".department": False}
    breaches = lineage.nullable_into_not_null(g, nullable)
    assert len(breaches) == 1, breaches
    assert breaches[0].source == "raw.encounter.department"
    assert breaches[0].target == MART + ".department"


def check_an_aggregate_over_a_nullable_column_is_not_a_breach():
    # `count(*)` is never null whatever it counted, so reporting this would train whoever
    # reads the output to ignore it.
    g = Graph(edges=(
        Edge(ColumnRef("raw.encounter", "department"),
             ColumnRef(MART, "encounters"), EdgeKind.AGGREGATE),
    ))
    nullable = {"raw.encounter.department": True, MART + ".encounters": False}
    assert lineage.nullable_into_not_null(g, nullable) == ()


def check_the_live_schema_has_no_nullability_breach_left():
    g = _mart_graph()
    assert lineage.nullable_into_not_null(g, schema.nullable_by_address()) == ()


def check_an_unknown_column_is_skipped_rather_than_assumed_nullable():
    g = Graph(edges=(
        Edge(ColumnRef("raw.x", "a"), ColumnRef("raw.y", "b"), EdgeKind.COPY),
    ))
    assert lineage.nullable_into_not_null(g, {}) == ()
    assert lineage.nullable_into_not_null(g, {"raw.x.a": True}) == ()


def check_most_of_this_warehouse_is_out_of_reach_of_lineage():
    # The number that decides how much this layer is worth. If it ever gets close to the
    # column count, either the warehouse changed or the reader started guessing.
    g = _mart_graph()
    addresses = ["{}.{}".format(fqn, c.name) for fqn, c in schema.all_columns()]
    roots = g.roots(addresses)
    assert len(addresses) == 42, len(addresses)
    assert len(roots) == 37, len(roots)


def check_lineage_does_not_separate_created_at_from_admitted_at():
    # The classifier cannot separate these two and the expectation was that lineage would.
    # It does not. Both columns are loaded rather than derived, so neither has an upstream.
    g = _mart_graph()
    addresses = ["{}.{}".format(fqn, c.name) for fqn, c in schema.all_columns()]
    roots = set(g.roots(addresses))
    assert "raw.patient.created_at" in roots
    assert "raw.encounter.admitted_at" in roots


def check_the_declared_references_are_real():
    assert schema.check_foreign_keys_are_real() is None


def check_a_reference_to_a_non_key_column_is_refused():
    import pii.schema as declared

    original = declared.FOREIGN_KEYS
    try:
        declared.FOREIGN_KEYS = (
            declared.ForeignKey("raw.encounter", "patient_id", "raw.patient", "email"),
        )
        problem = declared.check_foreign_keys_are_real()
        assert problem is not None and "not a key" in problem, problem
    finally:
        declared.FOREIGN_KEYS = original


def check_a_reference_to_a_column_that_does_not_exist_is_refused():
    import pii.schema as declared

    original = declared.FOREIGN_KEYS
    try:
        declared.FOREIGN_KEYS = (
            declared.ForeignKey("raw.encounter", "patient_id", "raw.patient", "nope"),
        )
        problem = declared.check_foreign_keys_are_real()
        assert problem is not None and "does not exist" in problem, problem
    finally:
        declared.FOREIGN_KEYS = original


def check_declaring_references_did_not_move_the_pinned_fingerprint():
    # The references are deliberately not a field on `Column`. If that ever changes, this
    # is the check that says the pinned value has to be re-derived and the crawler regraded.
    assert schema.fingerprint() == "1501a19ca3d8", schema.fingerprint()


# --- checks written against named mutation survivors -------------------------------

def check_every_dataclass_here_is_frozen():
    # Seven of them were mutable with nothing saying otherwise, which a mutation pass found
    # by flipping `frozen=True` on each and watching the suite stay green. An edge that can
    # be edited after the graph is built is an edge nobody can trust.
    import dataclasses

    for cls in (ColumnRef, Edge, lineage.Refusal, Graph, lineage._Source,
                lineage.Inherited, lineage.NullabilityBreach):
        assert dataclasses.is_dataclass(cls), cls
        assert cls.__dataclass_params__.frozen, cls.__name__


def check_a_comma_inside_a_string_literal_does_not_split_a_select_item():
    # `_split_top_level` tracks string state and nothing exercised it. A separator inside a
    # literal would cut one select item into two and shift every target column after it.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT concat(p.city, ', ', p.postal_code) AS where_they_live "
        "FROM raw.patient p")
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {e.target.column for e in g.edges} == {"where_they_live"}
    assert {e.source.address for e in g.edges} == {
        "raw.patient.city", "raw.patient.postal_code"}


def check_a_doubled_quote_inside_a_literal_does_not_end_the_literal():
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT concat(p.city, 'it''s, fine') AS c FROM raw.patient p")
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {e.source.address for e in g.edges} == {"raw.patient.city"}


def check_a_function_whose_name_ends_in_cast_is_not_a_cast():
    # `_strip_cast_types` searches for the substring and a bare search finds one inside
    # `forecast(`. Rewriting that call would throw away everything after its first ` as `.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT forecast(e.department) AS f FROM raw.encounter e")
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {e.source.address for e in g.edges} == {"raw.encounter.department"}
    assert g.edges[0].kind is EdgeKind.TRANSFORM, g.edges[0].kind


def check_a_cast_nested_inside_another_expression_is_still_stripped():
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT length(CAST(p.postal_code AS VARCHAR)) AS n "
        "FROM raw.patient p")
    assert g.refusals == (), [str(r) for r in g.refusals]
    names = {e.source.column for e in g.edges}
    assert names == {"postal_code"}, names


def check_two_casts_in_one_expression_are_both_stripped():
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT concat(CAST(p.city AS VARCHAR), "
        "CAST(p.postal_code AS VARCHAR)) AS c FROM raw.patient p")
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {e.source.column for e in g.edges} == {"city", "postal_code"}


def check_the_cast_detail_names_the_type_and_nothing_else():
    g = _mart_graph()
    edge = [e for e in g.edges if e.target.column == "day"][0]
    assert edge.detail == "cast to DATE, grouping key", repr(edge.detail)


def check_the_aggregate_detail_says_which_function_it_was():
    g = _mart_graph()
    avg_edge = [e for e in g.edges if e.target.column == "mean_length_of_stay_h"][0]
    assert avg_edge.detail == "avg over the group", repr(avg_edge.detail)
    count_edge = [e for e in g.edges if e.target.column == "encounters"][0]
    assert count_edge.detail == "row count over the join grain", repr(count_edge.detail)


def check_a_group_by_naming_the_expression_works_like_one_naming_the_position():
    # The mart uses `GROUP BY 1, 2, 3` so the name branch had no fixture at all.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT p.city AS c, count(*) AS n FROM raw.patient p "
        "GROUP BY p.city")
    by_target = {e.target.column: e for e in g.edges}
    assert by_target["c"].kind is EdgeKind.GROUPED, by_target["c"]


def check_a_scalar_subquery_in_a_select_item_is_refused():
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT (SELECT max(x.city) FROM raw.patient x) AS c "
        "FROM raw.claim")
    assert g.edges == ()
    assert any(r.what == "scalar subquery" for r in g.refusals), g.refusals


def check_a_refusal_names_which_select_item_it_was():
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT k.claim_id AS a, k.payer_name::VARCHAR AS b FROM raw.claim k")
    shorthand = [r for r in g.refusals if r.what == "shorthand cast"]
    assert shorthand and "item 2" in shorthand[0].detail, [str(r) for r in g.refusals]


def check_a_table_with_no_alias_can_be_qualified_by_its_own_name():
    for qualifier in ("raw.patient", "patient"):
        g = lineage.read_insert_select(
            "INSERT INTO a.b SELECT {}.city AS c FROM raw.patient".format(qualifier))
        assert g.refusals == (), (qualifier, [str(r) for r in g.refusals])
        assert g.edges[0].source.address == "raw.patient.city", (qualifier, g.edges[0])


def check_merging_two_graphs_keeps_the_refusals_of_both():
    one = lineage.read_insert_select("INSERT INTO a.b SELECT * FROM raw.patient")
    two = lineage.read_insert_select("INSERT INTO a.c SELECT p.city AS c FROM raw.patient p")
    merged = one.merge(two)
    assert len(merged.edges) == len(one.edges) + len(two.edges)
    assert len(merged.refusals) == len(one.refusals) + len(two.refusals)
    assert len(merged.refusals) == 1, merged.refusals


def check_downstream_lookup_returns_what_the_column_reaches():
    g = _mart_graph()
    reached = {e.target.address for e in g.out_of("raw.patient.postal_code")}
    assert reached == {MART + ".postal_code"}, reached
    assert g.out_of("raw.patient.email") == ()


def check_a_personal_upstream_beats_a_more_confident_harmless_one():
    # The laundering case, and the one a mutant walked straight through. Picking the most
    # confident candidate regardless of category lets a strong `not_personal` reading hide
    # a weaker personal one that is sharing the same target column.
    g = Graph(edges=(
        Edge(ColumnRef("raw.p", "loud"), ColumnRef("m.t", "blob"), EdgeKind.COPY),
        Edge(ColumnRef("raw.p", "quiet"), ColumnRef("m.t", "blob"), EdgeKind.COPY),
    ))
    results = [
        _Result("raw.p.loud", "not_personal", 0.99),
        _Result("raw.p.quiet", "national_id", 0.40),
        _Result("m.t.blob", "not_personal", 0.0),
    ]
    by = {i.address: i for i in lineage.propagate(g, results)}
    assert by["m.t.blob"].inherited_category == "national_id", by["m.t.blob"]


def check_the_most_confident_personal_upstream_is_the_one_that_wins():
    # And it is the confidence that orders them rather than the category name. `email`
    # sorts before `national_id` alphabetically and is the weaker reading here.
    g = Graph(edges=(
        Edge(ColumnRef("raw.p", "a"), ColumnRef("m.t", "blob"), EdgeKind.COPY),
        Edge(ColumnRef("raw.p", "b"), ColumnRef("m.t", "blob"), EdgeKind.COPY),
    ))
    results = [
        _Result("raw.p.a", "email", 0.40),
        _Result("raw.p.b", "national_id", 0.95),
        _Result("m.t.blob", "not_personal", 0.0),
    ]
    by = {i.address: i for i in lineage.propagate(g, results)}
    assert by["m.t.blob"].inherited_category == "national_id", by["m.t.blob"]

    results[1] = _Result("raw.p.b", "national_id", 0.10)
    by = {i.address: i for i in lineage.propagate(g, results)}
    assert by["m.t.blob"].inherited_category == "email", by["m.t.blob"]


def check_a_cast_says_what_it_coarsened_the_value_to():
    g = Graph(edges=(
        Edge(ColumnRef("raw.e", "admitted_at"), ColumnRef("m.t", "day"),
             EdgeKind.CAST, "cast to DATE"),
    ))
    results = [
        _Result("raw.e.admitted_at", "event_date", 0.80),
        _Result("m.t.day", "not_personal", 0.0),
    ]
    out = {i.address: i for i in lineage.propagate(g, results)}["m.t.day"]
    assert "coarsened to day" in out.rule, out.rule

    # A cast that lands on a type carrying no unit says nothing about granularity.
    g2 = Graph(edges=(
        Edge(ColumnRef("raw.p", "postal_code"), ColumnRef("m.t", "geo"),
             EdgeKind.CAST, "cast to VARCHAR"),
    ))
    results2 = [
        _Result("raw.p.postal_code", "postal_code", 0.90),
        _Result("m.t.geo", "not_personal", 0.0),
    ]
    out2 = {i.address: i for i in lineage.propagate(g2, results2)}["m.t.geo"]
    assert "coarsened" not in out2.rule, out2.rule


def check_an_upstream_column_nobody_classified_is_said_so_rather_than_assumed_safe():
    g = Graph(edges=(
        Edge(ColumnRef("raw.p", "unknown"), ColumnRef("m.t", "c"), EdgeKind.COPY),
    ))
    out = lineage.propagate(g, [_Result("m.t.c", "not_personal", 0.0)])[0]
    assert out.inherited_category is None
    assert "not classified" in out.rule, out.rule


def check_a_string_literal_at_the_top_of_the_select_list_protects_its_comma():
    # The earlier literal check put the comma inside a function call, where the bracket
    # depth protects it and the string tracking is never load bearing. A literal sitting
    # directly in the select list is the case that needs it, and without it the item splits
    # in two and every target column after this one shifts by one.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT p.city AS c, 'x, y' AS lit, p.postal_code AS z "
        "FROM raw.patient p",
        ["c", "lit", "z"])
    assert g.refusals == (), [str(r) for r in g.refusals]
    by_source = {e.source.column: e.target.column for e in g.edges}
    assert by_source == {"city": "c", "postal_code": "z"}, by_source


def check_a_doubled_quote_at_the_top_of_the_select_list_does_not_end_the_literal():
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT p.city AS c, 'it''s, fine' AS lit FROM raw.patient p",
        ["c", "lit"])
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {e.source.column for e in g.edges} == {"city"}


def check_the_star_and_subquery_refusals_name_the_right_select_item():
    # Same gap the shorthand cast refusal had. A refusal whose message points at the wrong
    # item sends whoever reads it to the wrong line of a statement they did not write.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT k.claim_id AS a, * FROM raw.claim k")
    star = [r for r in g.refusals if r.what == "star expansion"]
    assert star and "item 2" in star[0].detail, [str(r) for r in g.refusals]

    g2 = lineage.read_insert_select(
        "INSERT INTO a.b SELECT k.claim_id AS a, k.payer_name AS b, "
        "(SELECT max(x.city) FROM raw.patient x) AS c FROM raw.claim k")
    sub = [r for r in g2.refusals if r.what == "scalar subquery"]
    assert sub and "item 3" in sub[0].detail, [str(r) for r in g2.refusals]


def check_a_cast_wrapping_another_cast_terminates_and_strips_both():
    # The nested case the forward only scan has to handle. An outer cast is rewritten
    # first and the inner one is then found after the rewritten span rather than by
    # starting over.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT CAST(CAST(p.birth_date AS DATE) AS VARCHAR) AS c "
        "FROM raw.patient p")
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {e.source.column for e in g.edges} == {"birth_date"}, g.edges


def check_an_empty_string_literal_closes_rather_than_swallowing_the_rest():
    # `''` is an empty literal and not the start of an escaped quote. Reading it as an
    # escape leaves the scanner inside a string for the rest of the select list, so every
    # separator after it stops splitting and the whole tail becomes one item.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT '' AS blank, p.city AS c FROM raw.patient p",
        ["blank", "c"])
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {(e.source.column, e.target.column) for e in g.edges} == {("city", "c")}


def check_an_escaped_quote_inside_a_literal_keeps_the_item_boundaries():
    # `''''` is a literal holding one apostrophe, which is what a surname like O'Brien
    # needs. Miscounting it by one character flips whether the next comma is inside the
    # string.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT '''' AS tick, p.city AS c FROM raw.patient p",
        ["tick", "c"])
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {(e.source.column, e.target.column) for e in g.edges} == {("city", "c")}


def check_a_degenerate_literal_does_not_raise_out_of_the_splitter():
    # The scanner reads one character ahead and the lookahead has to be bounded. Three of
    # these inputs made an unbounded version walk off the end of the string.
    for text in ("''", "'", "'''", "a,''", "''," , "'',''"):
        lineage._split_top_level(text)


def check_an_aliased_table_cannot_also_be_named_by_its_table_name():
    # SQL's own rule. Once a source carries an alias the table name stops resolving, and
    # the reader has to refuse rather than quietly accept both spellings.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT patient.city AS c FROM raw.patient p")
    assert g.edges == ()
    assert [r.what for r in g.refusals] == ["unknown qualifier"], g.refusals


def check_a_cast_at_the_start_of_an_arithmetic_expression_is_still_a_cast():
    # The guard that stops `forecast(` being read as a cast looks at the character before
    # the match. At position zero there is no such character and an off by one there reads
    # the last character of the whole expression instead, which is a digit here.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT CAST(p.birth_date AS DATE) + 1 AS c FROM raw.patient p")
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {e.source.column for e in g.edges} == {"birth_date"}, g.edges


def check_a_real_cast_inside_a_call_whose_name_ends_in_cast_is_still_found():
    # Two matches in one expression. The first is skipped by the guard and the scan has to
    # resume close enough behind to still see the second.
    g = lineage.read_insert_select(
        "INSERT INTO a.b SELECT forecast(CAST(e.admitted_at AS DATE)) AS c "
        "FROM raw.encounter e")
    assert g.refusals == (), [str(r) for r in g.refusals]
    assert {e.source.column for e in g.edges} == {"admitted_at"}, g.edges


def check_an_unbalanced_bracket_is_left_alone_rather_than_crashing():
    # A truncated statement should come back as whatever the reader could make of it. The
    # bracket matcher walks to the end of the string looking for a close that is not there.
    # An unmatched open bracket leaves the expression exactly as it came in, because the
    # matcher ran off the end and there is nothing it can safely rewrite.
    assert lineage._strip_cast_types("CAST(p.city AS VARCHAR") == "CAST(p.city AS VARCHAR"

    # One close bracket short of balanced is a different case. The matcher does find a
    # close, so the inner cast is rewritten and the result is still unbalanced. The output
    # is not valid SQL and it does not need to be, because the only thing that reads it is
    # the name scanner. What matters is that the type name is gone and the column is not.
    stripped = lineage._strip_cast_types("length(CAST(p.city AS VARCHAR)")
    assert "VARCHAR" not in stripped, stripped
    assert "p.city" in stripped, stripped
    assert lineage._names_in("length(CAST(p.city AS VARCHAR)") == ["p.city"]


# Upstream tables, which is what the classifier asks the graph for.


def _copy_and_count_graph() -> Graph:
    """A grouping key copied out of a table and a count taken over it, side by side."""
    return Graph(edges=(
        Edge(ColumnRef("raw.patient", "postal_code"),
             ColumnRef("mart.daily", "postal_code"), EdgeKind.GROUPED),
        Edge(ColumnRef("raw.encounter", "*"),
             ColumnRef("mart.daily", "n"), EdgeKind.AGGREGATE),
        Edge(ColumnRef("raw.patient", "patient_id"),
             ColumnRef("raw.encounter", "patient_id"), EdgeKind.JOIN_KEY),
    ))


def check_upstream_tables_follows_a_value_preserving_edge():
    assert _copy_and_count_graph().upstream_tables("mart.daily") == ("raw.patient",)


def check_upstream_tables_ignores_an_aggregate_by_default():
    # `raw.encounter` feeds the count and carries none of its values into it, so a table
    # built only out of counts is not answering for the table it counted.
    assert "raw.encounter" not in _copy_and_count_graph().upstream_tables("mart.daily")
    # A named column behind an aggregate rather than a star, so the default actually
    # decides something. With the star source the two settings agree and a pass flipping
    # the default was invisible.
    named = Graph(edges=(
        Edge(ColumnRef("raw.encounter", "admitted_at"),
             ColumnRef("mart.daily", "mean_stay"), EdgeKind.AGGREGATE),))
    assert named.upstream_tables("mart.daily") == ()
    assert named.upstream_tables("mart.daily", value_preserving_only=False) == (
        "raw.encounter",)


def check_upstream_tables_can_be_asked_to_include_an_aggregate():
    got = _copy_and_count_graph().upstream_tables(
        "mart.daily", value_preserving_only=False)
    assert "raw.patient" in got
    # The star source is still dropped, because `raw.encounter.*` names a table and not a
    # column in it, and letting it through makes every row count look like a carried value.
    assert "raw.encounter" not in got


def check_upstream_tables_ignores_a_join_key():
    assert _copy_and_count_graph().upstream_tables("raw.encounter") == ()


def check_upstream_tables_walks_more_than_one_hop():
    g = Graph(edges=(
        Edge(ColumnRef("a.t", "c"), ColumnRef("b.t", "c"), EdgeKind.COPY),
        Edge(ColumnRef("b.t", "c"), ColumnRef("c.t", "c"), EdgeKind.COPY),
    ))
    assert set(g.upstream_tables("c.t")) == {"a.t", "b.t"}


def check_upstream_tables_never_reports_the_table_itself():
    g = Graph(edges=(
        Edge(ColumnRef("a.t", "x"), ColumnRef("a.t", "y"), EdgeKind.COPY),))
    assert g.upstream_tables("a.t") == ()


def check_upstream_tables_survives_a_cycle():
    g = Graph(edges=(
        Edge(ColumnRef("a.t", "c"), ColumnRef("b.t", "c"), EdgeKind.COPY),
        Edge(ColumnRef("b.t", "c"), ColumnRef("a.t", "c"), EdgeKind.COPY),
    ))
    assert g.upstream_tables("a.t") == ("b.t",)


def check_upstream_tables_of_an_unknown_table_is_empty():
    assert _copy_and_count_graph().upstream_tables("nothing.here") == ()


def check_the_upstream_map_covers_every_table_it_is_given():
    g = _copy_and_count_graph()
    m = g.upstream_table_map(["mart.daily", "raw.patient"])
    assert sorted(m) == ["mart.daily", "raw.patient"]
    assert m["raw.patient"] == ()


def check_the_upstream_map_carries_the_value_preserving_setting_through():
    named = Graph(edges=(
        Edge(ColumnRef("raw.encounter", "admitted_at"),
             ColumnRef("mart.daily", "mean_stay"), EdgeKind.AGGREGATE),))
    assert named.upstream_table_map(["mart.daily"]) == {"mart.daily": ()}
    assert named.upstream_table_map(
        ["mart.daily"], value_preserving_only=False) == {
            "mart.daily": ("raw.encounter",)}


def check_the_real_mart_traces_back_to_both_source_tables():
    # The mart's grouping keys come out of `raw.encounter` and `raw.patient`, so both are
    # upstream by a value preserving edge and neither arrives only through the count.
    assert set(_mart_graph().upstream_tables("analytics.encounter_daily")) == {
        "raw.encounter", "raw.patient"}
