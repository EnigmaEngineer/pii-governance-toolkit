from __future__ import annotations

import datetime as dt

from pii import access, corpus
from pii.access import Grant, Privilege, QueryEvent, RoleMember
from pii.lineage import ColumnRef, Edge, EdgeKind, Graph

D = dt.date


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def _edge(src, tgt, kind):
    st, sc = src.rsplit(".", 1)
    tt, tc = tgt.rsplit(".", 1)
    return Edge(ColumnRef(st, sc), ColumnRef(tt, tc), kind)


# A patient postal code copied into a mart as a grouping key, a count beside it, and a
# second hop so the walk has something to walk. This is the shape the module is about.
COPY_GRAPH = Graph(edges=(
    _edge("raw.patient.postal_code", "mart.daily.postal_code", EdgeKind.GROUPED),
    _edge("mart.daily.postal_code", "mart.weekly.postal_code", EdgeKind.COPY),
    _edge("raw.patient.mrn", "mart.daily.patients", EdgeKind.AGGREGATE),
))


# Carriers


def check_a_grouping_key_carries_the_value_downstream():
    assert access.carriers_of(COPY_GRAPH, "raw.patient.postal_code") == (
        "mart.daily.postal_code", "mart.weekly.postal_code")


def check_an_aggregate_edge_carries_nothing():
    assert access.carriers_of(COPY_GRAPH, "raw.patient.mrn") == ()


def check_a_column_with_no_outgoing_edge_has_no_carrier():
    assert access.carriers_of(COPY_GRAPH, "raw.patient.nothing_downstream") == ()


def check_a_join_key_edge_is_not_a_carrier():
    g = Graph(edges=(
        _edge("raw.patient.patient_id", "raw.encounter.patient_id", EdgeKind.JOIN_KEY),))
    assert access.carriers_of(g, "raw.patient.patient_id") == ()


def check_the_carrier_walk_survives_a_cycle():
    g = Graph(edges=(
        _edge("a.t.c", "b.t.c", EdgeKind.COPY),
        _edge("b.t.c", "a.t.c", EdgeKind.COPY),
    ))
    assert access.carriers_of(g, "a.t.c") == ("b.t.c",)


# Reachability through grants


def _people():
    grants = [Grant("bi", "mart.daily"), Grant("eng", "raw.patient")]
    members = [RoleMember("dana", "bi"), RoleMember("sam", "eng")]
    return grants, members


def check_a_grant_on_the_columns_own_table_is_a_direct_reader():
    grants, members = _people()
    e = access.exposure("raw.patient.postal_code", grants, members,
                        [QueryEvent("q1", "sam", D(2026, 6, 1), ("raw.patient",),
                                    select_star=True)],
                        COPY_GRAPH)
    assert e.direct == ("sam",)


def check_a_grant_on_a_carrier_table_reaches_the_value_without_a_grant_on_the_source():
    grants, members = _people()
    e = access.exposure("raw.patient.postal_code", grants, members,
                        [QueryEvent("q1", "sam", D(2026, 6, 1), ("raw.patient",),
                                    select_star=True)],
                        COPY_GRAPH)
    assert e.through_a_carrier == ("dana",)
    assert set(e.could_have_read) == {"dana", "sam"}


def check_without_a_graph_only_the_direct_grant_counts():
    grants, members = _people()
    e = access.exposure("raw.patient.postal_code", grants, members, [], None)
    assert e.direct == ("sam",)
    assert e.through_a_carrier == ()
    assert e.carriers == ()


def check_a_user_holding_both_grants_is_counted_once_as_direct():
    grants = [Grant("bi", "mart.daily"), Grant("eng", "raw.patient")]
    members = [RoleMember("dana", "bi"), RoleMember("dana", "eng")]
    e = access.exposure("raw.patient.postal_code", grants, members, [], COPY_GRAPH)
    assert e.direct == ("dana",)
    assert e.through_a_carrier == ()
    assert e.could_have_read == ("dana",)


def check_a_grant_of_all_privileges_reads():
    grants = [Grant("bi", "mart.daily", Privilege.ALL)]
    members = [RoleMember("dana", "bi")]
    e = access.exposure("raw.patient.postal_code", grants, members, [], COPY_GRAPH)
    assert e.through_a_carrier == ("dana",)
    assert Privilege.ALL in access.READS
    assert Grant("bi", "t", Privilege.ALL).reads


def check_readable_tables_lists_only_that_roles_grants():
    grants, _members = _people()
    assert access.readable_tables("bi", grants) == ("mart.daily",)
    assert access.readable_tables("nobody", grants) == ()


# Use, from the query log


def _log():
    return [
        QueryEvent("q1", "sam", D(2026, 6, 10), ("raw.patient",),
                   columns=("raw.patient.postal_code",)),
        QueryEvent("q2", "dana", D(2026, 6, 11), ("mart.daily",),
                   columns=("mart.daily.postal_code",)),
        QueryEvent("q3", "kim", D(2026, 6, 12), ("mart.daily",), select_star=True),
        QueryEvent("q4", "zoe", D(2026, 6, 13), ("mart.daily",),
                   columns=("mart.daily.patients",)),
    ]


def _all_roles():
    grants = [Grant("bi", "mart.daily"), Grant("eng", "raw.patient")]
    members = [RoleMember("sam", "eng"), RoleMember("dana", "bi"),
               RoleMember("kim", "bi"), RoleMember("zoe", "bi")]
    return grants, members


def check_naming_the_column_is_a_read():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH)
    assert e.named_the_column == ("sam",)


def check_naming_a_carrier_is_a_read_of_the_source_value():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH)
    assert e.named_a_carrier == ("dana",)
    assert set(e.did_read) == {"dana", "sam"}


def check_naming_an_aggregate_of_the_column_is_not_a_read_of_it():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH)
    assert "zoe" not in e.did_read
    assert "zoe" not in e.named_a_carrier


def check_a_star_query_is_a_ceiling_and_not_a_read():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH)
    assert e.star_over_a_holder == ("kim",)
    assert "kim" not in e.did_read
    assert "kim" in e.did_read_at_most
    assert e.only_by_star == ("kim",)


def check_reading_through_a_carrier_with_no_direct_grant_is_the_finding():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH)
    assert e.read_without_a_direct_grant == ("dana",)


def check_a_grant_nobody_used_is_reported():
    grants = [Grant("bi", "mart.daily"), Grant("eng", "raw.patient")]
    members = [RoleMember("sam", "eng"), RoleMember("idle", "bi")]
    e = access.exposure("raw.patient.postal_code", grants, members,
                        [QueryEvent("q1", "sam", D(2026, 6, 10), ("raw.patient",),
                                    columns=("raw.patient.postal_code",))],
                        COPY_GRAPH)
    assert e.granted_and_never_used == ("idle",)


# The date range


def check_the_range_excludes_a_query_before_it():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH,
                        D(2026, 6, 11), D(2026, 6, 30))
    assert e.named_the_column == ()


def check_the_range_excludes_a_query_after_it():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH,
                        D(2026, 6, 1), D(2026, 6, 10))
    assert e.named_the_column == ("sam",)
    assert e.named_a_carrier == ()


def check_the_range_is_inclusive_at_both_ends():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH,
                        D(2026, 6, 10), D(2026, 6, 11))
    assert set(e.did_read) == {"dana", "sam"}


def check_a_backwards_range_raises():
    grants, members = _all_roles()
    assert _raises(lambda: access.exposure(
        "raw.patient.postal_code", grants, members, _log(), COPY_GRAPH,
        D(2026, 6, 30), D(2026, 6, 1)), ValueError)


def check_a_range_of_one_day_is_legal():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH,
                        D(2026, 6, 10), D(2026, 6, 10))
    assert e.did_read == ("sam",)


# The event record itself


def check_an_event_naming_no_table_raises():
    assert _raises(lambda: QueryEvent("q", "u", D(2026, 1, 1), ()), ValueError)


def check_a_star_query_that_also_names_columns_raises():
    assert _raises(lambda: QueryEvent("q", "u", D(2026, 1, 1), ("t",),
                                      columns=("t.c",), select_star=True), ValueError)


# Aggregates over the report


def check_star_share_counts_in_range_only():
    assert access.star_share(_log()) == (1, 4)
    assert access.star_share(_log(), D(2026, 6, 13), D(2026, 6, 13)) == (0, 1)


def check_unused_grants_needs_every_column_untouched():
    grants = [Grant("bi", "mart.daily"), Grant("eng", "raw.patient")]
    members = [RoleMember("sam", "eng"), RoleMember("idle", "bi")]
    events = [QueryEvent("q1", "sam", D(2026, 6, 10), ("raw.patient",),
                         columns=("raw.patient.postal_code",))]
    exps = access.report(["raw.patient.postal_code", "raw.patient.mrn"],
                         grants, members, events, COPY_GRAPH)
    assert access.unused_grants(exps) == ("idle",)


def check_unused_grants_over_no_exposures_is_empty():
    assert access.unused_grants([]) == ()


def check_widening_names_only_the_columns_that_widen():
    grants, members = _all_roles()
    exps = access.report(["raw.patient.postal_code", "raw.patient.mrn"],
                         grants, members, _log(), COPY_GRAPH)
    lines = access.widening(exps)
    assert len(lines) == 1
    assert lines[0].startswith("raw.patient.postal_code")


def check_users_by_role_groups_and_sorts():
    got = access.users_by_role([RoleMember("b", "r"), RoleMember("a", "r")])
    assert got == {"r": ("a", "b")}


# The generated substrate. The report is measured against it in scripts/access_probe.py
# and these only check it is the shape the report expects.


def check_the_generated_log_only_reads_tables_the_user_was_granted():
    grants, members, events = corpus.generate_access(n_queries=120)
    reachable = {}
    for m in members:
        for g in grants:
            if g.role == m.role:
                reachable.setdefault(m.user, set()).add(g.table)
    for e in events:
        for t in e.tables:
            assert t in reachable[e.user], "{} read {} unpermitted".format(e.user, t)


def check_the_generated_log_is_reproducible_from_the_seed():
    _g1, _m1, e1 = corpus.generate_access(n_queries=40)
    _g2, _m2, e2 = corpus.generate_access(n_queries=40)
    assert [x.query_id for x in e1] == [x.query_id for x in e2]
    assert [x.user for x in e1] == [x.user for x in e2]
    assert [x.columns for x in e1] == [x.columns for x in e2]


def check_a_different_seed_gives_a_different_log():
    _g1, _m1, e1 = corpus.generate_access(seed=1, n_queries=40)
    _g2, _m2, e2 = corpus.generate_access(seed=2, n_queries=40)
    assert [x.user for x in e1] != [x.user for x in e2]


def check_the_generated_log_holds_both_star_and_named_queries():
    _g, _m, events = corpus.generate_access(n_queries=200)
    assert any(e.select_star for e in events)
    assert any(e.columns for e in events)


def check_an_empty_query_log_is_refused():
    assert _raises(lambda: corpus.generate_access(n_queries=0), ValueError)


def check_a_backwards_log_window_is_refused():
    assert _raises(lambda: corpus.generate_access(
        start=D(2026, 9, 1), end=D(2026, 6, 1)), ValueError)


def check_the_log_stays_inside_the_window_it_was_given():
    _g, _m, events = corpus.generate_access(
        n_queries=60, start=D(2026, 7, 1), end=D(2026, 7, 31))
    assert min(e.at for e in events) >= D(2026, 7, 1)
    assert max(e.at for e in events) <= D(2026, 7, 31)


def check_summarise_access_counts_what_is_there():
    grants, members, events = corpus.generate_access(n_queries=100)
    s = corpus.summarise_access(grants, members, events)
    assert s["queries"] == 100
    assert s["star_queries"] == sum(1 for e in events if e.select_star)
    assert s["column_references"] == sum(len(e.columns) for e in events)
    assert s["users"] == len({m.user for m in members})


# The gaps a mutation pass found. Each of these failed to fail before it was written.


def check_the_records_are_frozen():
    # `frozen=True` on four dataclasses and nothing asserted any of them, so a pass
    # flipping all four to False was invisible. An Exposure that can be edited after the
    # fact is an audit record that can be edited after the fact.
    g = Grant("bi", "mart.daily")
    m = RoleMember("dana", "bi")
    q = QueryEvent("q", "dana", D(2026, 6, 1), ("mart.daily",), select_star=True)
    e = access.exposure("raw.patient.postal_code", [g], [m], [q], COPY_GRAPH)
    for obj, field, value in ((g, "role", "other"), (m, "user", "other"),
                              (q, "user", "other"), (e, "address", "other")):
        assert _raises(lambda o=obj, f=field, v=value: setattr(o, f, v)), (
            "{} is not frozen".format(type(obj).__name__))


def check_a_star_query_over_an_unrelated_table_is_not_a_read():
    # The holder test is `in`, and with every star query in the other checks pointed at a
    # holder, flipping it to `not in` changed nothing. A star over a table that holds
    # neither the column nor a carrier must not appear anywhere in the report.
    grants = [Grant("bi", "mart.daily"), Grant("other", "raw.unrelated")]
    members = [RoleMember("dana", "bi"), RoleMember("far", "other")]
    events = [QueryEvent("q1", "far", D(2026, 6, 1), ("raw.unrelated",),
                         select_star=True)]
    e = access.exposure("raw.patient.postal_code", grants, members, events, COPY_GRAPH)
    assert e.star_over_a_holder == ()
    assert e.did_read_at_most == ()


def check_an_omitted_start_falls_back_to_the_first_query_in_the_log():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH)
    assert e.start == D(2026, 6, 10)


def check_an_omitted_end_falls_back_to_the_last_query_in_the_log():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH)
    assert e.end == D(2026, 6, 13)


def check_a_given_range_is_reported_rather_than_the_logs_own():
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, _log(), COPY_GRAPH,
                        D(2026, 5, 1), D(2026, 7, 1))
    assert e.start == D(2026, 5, 1)
    assert e.end == D(2026, 7, 1)


def check_an_empty_log_with_no_range_still_reports_a_range():
    # The fallback takes a default rather than raising on `min` of nothing. A report over
    # a log with no rows in it is a legitimate answer and it has to carry some range.
    grants, members = _all_roles()
    e = access.exposure("raw.patient.postal_code", grants, members, [], COPY_GRAPH)
    assert e.start == dt.date.min
    assert e.end == dt.date.max


def check_widening_names_the_carrier_it_widened_through():
    grants, members = _all_roles()
    exps = access.report(["raw.patient.postal_code"], grants, members, _log(),
                         COPY_GRAPH)
    assert "mart.daily.postal_code" in access.widening(exps)[0]


def check_a_column_with_no_read_and_no_star_is_answerable():
    # Answerable and the answer is nobody. Distinct from the case below, where the floor
    # is nobody and the ceiling is not.
    grants = [Grant("eng", "raw.patient")]
    members = [RoleMember("sam", "eng")]
    e = access.exposure("raw.patient.postal_code", grants, members, [], COPY_GRAPH)
    assert access.unanswerable([e]) == ()


def check_a_column_reached_only_by_a_star_cannot_be_answered():
    grants = [Grant("bi", "mart.daily")]
    members = [RoleMember("kim", "bi")]
    events = [QueryEvent("q", "kim", D(2026, 6, 1), ("mart.daily",), select_star=True)]
    e = access.exposure("raw.patient.postal_code", grants, members, events, COPY_GRAPH)
    assert access.unanswerable([e]) == ("raw.patient.postal_code",)


def check_a_column_somebody_named_is_answerable_even_with_stars_around_it():
    grants, members = _all_roles()
    exps = access.report(["raw.patient.postal_code"], grants, members, _log(),
                         COPY_GRAPH)
    assert access.unanswerable(exps) == ()


# A transform carries the value for the purpose of asking who read it, and does not carry
# a granularity claim. `VALUE_PRESERVING` answers the second question and this module asks
# the first, so the two sets differ by exactly `TRANSFORM`.


TRANSFORM_GRAPH = Graph(edges=(
    _edge("raw.patient.last_name", "mart.people.surname", EdgeKind.TRANSFORM),
    _edge("mart.people.surname", "mart.people.initial", EdgeKind.TRANSFORM),
    _edge("raw.patient.ssn", "mart.people.n_ids", EdgeKind.AGGREGATE),
))


def check_a_transform_carries_the_value_for_exposure():
    assert access.carriers_of(TRANSFORM_GRAPH, "raw.patient.last_name") == (
        "mart.people.initial", "mart.people.surname")


def check_an_aggregate_still_ends_the_walk():
    assert access.carriers_of(TRANSFORM_GRAPH, "raw.patient.ssn") == ()


def check_the_carrier_set_is_wider_than_the_label_propagation_set():
    from pii.lineage import VALUE_PRESERVING
    assert EdgeKind.TRANSFORM in access.CARRIES_A_VALUE
    assert EdgeKind.TRANSFORM not in VALUE_PRESERVING
    assert set(VALUE_PRESERVING) < set(access.CARRIES_A_VALUE)
    assert EdgeKind.AGGREGATE not in access.CARRIES_A_VALUE
    assert EdgeKind.JOIN_KEY not in access.CARRIES_A_VALUE


def check_the_narrower_set_can_still_be_asked_for():
    from pii.lineage import VALUE_PRESERVING
    assert access.carriers_of(TRANSFORM_GRAPH, "raw.patient.last_name",
                              kinds=VALUE_PRESERVING) == ()


def check_a_transformed_column_reaches_a_reader_with_no_grant_on_the_source():
    grants = [Grant("bi", "mart.people"), Grant("eng", "raw.patient")]
    members = [RoleMember("dana", "bi"), RoleMember("sam", "eng")]
    events = [QueryEvent("q1", "dana", D(2026, 6, 1), ("mart.people",),
                         columns=("mart.people.surname",))]
    e = access.exposure("raw.patient.last_name", grants, members, events,
                        TRANSFORM_GRAPH)
    assert e.through_a_carrier == ("dana",)
    assert e.read_without_a_direct_grant == ("dana",)


def check_the_real_warehouse_has_no_transform_edge_so_no_figure_moved():
    # The reason this fix is invisible in every published number, asserted rather than
    # claimed. If a transform edge ever appears here, this check fails and the person
    # reading it is told to re-capture the access figures.
    from pii import lineage, schema
    cols = [c.name for c in
            schema.tables_by_fqn()["analytics.encounter_daily"].columns]
    g = lineage.read_insert_select(
        schema.DERIVED_SQL["analytics.encounter_daily"], cols)
    kinds = {e.kind for e in g.edges}
    assert EdgeKind.TRANSFORM not in kinds, (
        "a transform edge exists now, so the access figures need re-capturing")
