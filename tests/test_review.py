from __future__ import annotations

import datetime as dt

from pii import mask, review
from pii.classify import ACCEPT_AT, Arm, Band, Classification, Signal
from pii.lineage import ColumnRef, Edge, EdgeKind, Graph
from pii.mask import Action
from pii.review import Decision

D = dt.date


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def _result(table, column, key, confidence, signals=None, runner_up=None,
            runner_up_score=0.0):
    if signals is None:
        signals = (Signal(Arm.NAME, key, confidence, "stub"),)
    return Classification(table, column, key, confidence, tuple(signals),
                          runner_up=runner_up, runner_up_score=runner_up_score)


def _edge(src, tgt, kind):
    st, sc = src.rsplit(".", 1)
    tt, tc = tgt.rsplit(".", 1)
    return Edge(ColumnRef(st, sc), ColumnRef(tt, tc), kind)


# One table with two reviews in it and one table with a single review, so the sole blocker
# rule has both cases to separate.
def _fixture():
    results = (
        _result("raw.patient", "city", "postal_code", 0.60),
        _result("raw.patient", "created_at", "event_date", 0.725),
        _result("raw.device", "taken_at", "event_date", 0.725),
        _result("raw.patient", "email", "email", 0.99),
    )
    return results, mask.generate(results)


# Building the queue


def check_only_the_review_band_reaches_the_queue():
    results, policies = _fixture()
    q = review.build(results, policies)
    assert len(q) == 3
    assert all(i.policy.action is Action.REVIEW for i in q)
    assert "raw.patient.email" not in [i.address for i in q]


def check_a_policy_with_no_classification_behind_it_raises():
    results, policies = _fixture()
    assert _raises(lambda: review.build(results[:1], policies), ValueError)


def check_an_item_pairing_mismatched_addresses_raises():
    results, policies = _fixture()
    reviews = [p for p in policies if p.action is Action.REVIEW]
    assert _raises(lambda: review.QueueItem(
        classification=results[0], policy=reviews[1]), ValueError)


def check_an_item_built_over_a_non_review_policy_raises():
    results, policies = _fixture()
    redact = [p for p in policies if p.action is Action.REDACT][0]
    email = [r for r in results if r.column == "email"][0]
    assert _raises(lambda: review.QueueItem(
        classification=email, policy=redact), ValueError)


def check_an_empty_queue_is_legal():
    results = (_result("raw.patient", "email", "email", 0.99),)
    assert review.build(results, mask.generate(results)) == ()


# Blocking and ordering


def check_a_review_blocks_the_table_it_lives_in():
    results, policies = _fixture()
    q = review.build(results, policies)
    by = {i.address: i for i in q}
    assert by["raw.patient.city"].tables_blocked == ("raw.patient",)


def check_the_only_review_on_a_table_is_its_sole_blocker():
    results, policies = _fixture()
    q = review.build(results, policies)
    by = {i.address: i for i in q}
    assert by["raw.device.taken_at"].sole_blocker_of == ("raw.device",)


def check_one_of_two_reviews_on_a_table_is_not_its_sole_blocker():
    results, policies = _fixture()
    q = review.build(results, policies)
    by = {i.address: i for i in q}
    assert by["raw.patient.city"].sole_blocker_of == ()
    assert by["raw.patient.created_at"].sole_blocker_of == ()


def check_an_item_that_frees_a_table_sorts_first():
    results, policies = _fixture()
    q = review.build(results, policies)
    assert q[0].address == "raw.device.taken_at"


def check_frees_a_table_is_shorter_than_the_queue():
    results, policies = _fixture()
    q = review.build(results, policies)
    short = review.frees_a_table(q)
    assert len(short) == 1
    assert len(short) < len(q)


def check_a_carrier_puts_the_downstream_table_in_the_blocked_set():
    results = (
        _result("raw.patient", "created_at", "event_date", 0.725),
        _result("mart.daily", "day", "event_date", 0.45),
    )
    g = Graph(edges=(_edge("raw.patient.created_at", "mart.daily.day", EdgeKind.CAST),))
    q = review.build(results, mask.generate(results), g)
    by = {i.address: i for i in q}
    assert by["raw.patient.created_at"].tables_blocked == ("mart.daily", "raw.patient")
    assert by["raw.patient.created_at"].carried_into == ("mart.daily.day",)


def check_the_lineage_trace_says_where_a_derived_column_came_from():
    results = (
        _result("raw.patient", "created_at", "event_date", 0.725),
        _result("mart.daily", "day", "event_date", 0.45),
    )
    g = Graph(edges=(_edge("raw.patient.created_at", "mart.daily.day", EdgeKind.CAST),))
    q = review.build(results, mask.generate(results), g)
    by = {i.address: i for i in q}
    assert by["mart.daily.day"].came_from == ("raw.patient.created_at",)


def check_without_a_graph_an_item_carries_no_trace():
    results, policies = _fixture()
    q = review.build(results, policies)
    assert all(i.came_from == () and i.carried_into == () for i in q)


def check_a_direct_identifier_candidate_outranks_a_quasi_one_at_the_same_breadth():
    results = (
        _result("t.a", "c1", "postal_code", 0.60),
        _result("t.b", "c2", "person_name", 0.60),
    )
    q = review.build(results, mask.generate(results))
    assert q[0].address == "t.b.c2", [i.address for i in q]


def check_the_near_miss_is_taken_before_the_middle_of_the_band():
    results = (
        _result("t.a", "early", "event_date", 0.40),
        _result("t.b", "late", "event_date", 0.72),
    )
    q = review.build(results, mask.generate(results))
    assert q[0].address == "t.b.late"


def check_distance_to_accept_is_measured_from_the_threshold():
    item = review.build(*_fixture())[0]
    assert item.distance_to_accept == round(ACCEPT_AT - 0.725, 4)


# The brief a reviewer reads


def check_the_brief_carries_the_evidence_and_the_question():
    results, policies = _fixture()
    q = review.build(results, policies)
    text = q[0].brief()
    assert "is this column personal data" in text
    assert "evidence" in text
    assert "raw.device.taken_at" in text


def check_the_brief_says_what_the_decision_frees():
    results, policies = _fixture()
    q = review.build(results, policies)
    by = {i.address: i for i in q}
    assert "on its own" in by["raw.device.taken_at"].brief()
    assert "waits with others" in by["raw.patient.city"].brief()


def check_the_brief_reports_a_runner_up_when_there_is_one():
    results = (_result("t.a", "npi", "licence_number", 0.52,
                       runner_up="phone", runner_up_score=0.686),)
    q = review.build(results, mask.generate(results))
    assert "runner up" in q[0].brief()
    assert "phone" in q[0].brief()


# Decisions


def check_a_decision_with_no_reviewer_raises():
    assert _raises(lambda: Decision("t.a.c", True, "", D(2026, 9, 22), "why"), ValueError)


def check_a_decision_with_no_note_raises():
    assert _raises(lambda: Decision("t.a.c", True, "me", D(2026, 9, 22), "  "),
                   ValueError)


def check_a_decision_that_the_column_is_personal_produces_a_masking_action():
    results, policies = _fixture()
    q = review.build(results, policies)
    d = Decision("raw.patient.city", True, "me", D(2026, 9, 22), "it is a city")
    resolved, _open = review.apply(q, [d])
    assert resolved[0].action is Action.GENERALISE
    assert resolved[0].target is not None


def check_a_decision_that_the_column_is_not_personal_retains_it():
    results, policies = _fixture()
    q = review.build(results, policies)
    d = Decision("raw.patient.city", False, "me", D(2026, 9, 22), "a ward name")
    resolved, _open = review.apply(q, [d])
    assert resolved[0].action is Action.RETAIN
    assert resolved[0].category_key == "not_personal"


def check_a_decision_for_a_column_not_in_the_queue_raises():
    results, policies = _fixture()
    q = review.build(results, policies)
    d = Decision("raw.patient.email", True, "me", D(2026, 9, 22), "obviously")
    assert _raises(lambda: review.apply(q, [d]), ValueError)


def check_two_decisions_for_one_column_raise_rather_than_the_later_winning():
    results, policies = _fixture()
    q = review.build(results, policies)
    ds = [Decision("raw.patient.city", True, "a", D(2026, 9, 22), "yes"),
          Decision("raw.patient.city", False, "b", D(2026, 9, 23), "no")]
    assert _raises(lambda: review.apply(q, ds), ValueError)


def check_undecided_items_come_back_as_still_open():
    results, policies = _fixture()
    q = review.build(results, policies)
    d = Decision("raw.patient.city", True, "me", D(2026, 9, 22), "it is a city")
    _resolved, still_open = review.apply(q, [d])
    assert "raw.patient.city" not in still_open
    assert len(still_open) == 2


def check_applying_no_decisions_leaves_the_whole_queue_open():
    results, policies = _fixture()
    q = review.build(results, policies)
    resolved, still_open = review.apply(q, [])
    assert resolved == ()
    assert len(still_open) == len(q)


# Unblocking


def check_deciding_the_sole_blocker_unblocks_its_table():
    results, policies = _fixture()
    q = review.build(results, policies)
    d = Decision("raw.device.taken_at", True, "me", D(2026, 9, 22), "a reading time")
    assert review.unblocks(q, [d]) == ("raw.device",)


def check_deciding_one_of_two_blockers_unblocks_nothing():
    results, policies = _fixture()
    q = review.build(results, policies)
    d = Decision("raw.patient.city", True, "me", D(2026, 9, 22), "it is a city")
    assert review.unblocks(q, [d]) == ()


def check_deciding_both_blockers_unblocks_the_table():
    results, policies = _fixture()
    q = review.build(results, policies)
    ds = [Decision("raw.patient.city", True, "me", D(2026, 9, 22), "city"),
          Decision("raw.patient.created_at", False, "me", D(2026, 9, 22), "load time")]
    assert review.unblocks(q, ds) == ("raw.patient",)


def check_worked_share_counts_decided_against_the_queue():
    results, policies = _fixture()
    q = review.build(results, policies)
    d = Decision("raw.patient.city", True, "me", D(2026, 9, 22), "city")
    assert review.worked_share(q, [d]) == (1, 3)
    assert review.worked_share(q, []) == (0, 3)


# The trail


def check_the_trail_carries_the_reviewer_the_date_and_the_reason():
    d = Decision("raw.patient.city", True, "s.hussain", D(2026, 9, 22),
                 "a geographic subdivision")
    line = review.trail([d])[0]
    assert "2026-09-22" in line
    assert "s.hussain" in line
    assert "a geographic subdivision" in line
    assert "personal" in line


def check_the_trail_distinguishes_a_not_personal_decision():
    d = Decision("t.a.c", False, "me", D(2026, 9, 22), "a ward code")
    assert "not personal" in review.trail([d])[0]


def check_the_trail_is_ordered_oldest_first():
    ds = [Decision("t.a.b", True, "me", D(2026, 9, 22), "later"),
          Decision("t.a.a", True, "me", D(2026, 9, 20), "earlier")]
    lines = review.trail(ds)
    assert lines[0].startswith("2026-09-20")
    assert lines[1].startswith("2026-09-22")


def check_the_trail_is_stable_across_two_calls():
    ds = [Decision("t.a.b", True, "me", D(2026, 9, 22), "x"),
          Decision("t.a.a", True, "me", D(2026, 9, 22), "y")]
    assert review.trail(ds) == review.trail(list(reversed(ds)))


def check_an_empty_trail_is_empty_rather_than_a_header():
    assert review.trail([]) == ()


# The gaps a mutation pass found. Each of these failed to fail before it was written.


def check_a_quasi_candidate_outranks_a_sensitive_one():
    # The rank table had DIRECT against QUASI covered and nothing below it, so a pass
    # moving the quasi and sensitive entries onto each other was invisible.
    results = (
        _result("t.a", "note", "free_text_clinical", 0.60),
        _result("t.b", "city", "postal_code", 0.60),
    )
    q = review.build(results, mask.generate(results))
    assert q[0].address == "t.b.city", [i.address for i in q]


def check_a_sensitive_candidate_still_reaches_the_queue():
    # It ranks last and it is not excluded. `policy_for` tests the review band before it
    # tests identifiability, so a sensitive column under the accept threshold is a review
    # like any other even though the action it resolves to is a retain either way.
    results = (_result("t.a", "note", "free_text_clinical", 0.60),)
    q = review.build(results, mask.generate(results))
    assert len(q) == 1
    assert q[0].candidate_key == "free_text_clinical"


def check_the_distance_is_shown_at_the_precision_the_band_is_decided_at():
    # Four decimals, for the reason `Classification.explain` gives: the band turns on the
    # fourth. A distance printed at five would disagree with the confidence beside it.
    item = review.build(*_fixture())[0]
    assert item.distance_to_accept == 0.025
    noisy = (_result("t.a", "c", "postal_code", 0.4444444),)
    q = review.build(noisy, mask.generate(noisy))
    assert q[0].distance_to_accept == 0.3056


def check_the_brief_renders_the_lineage_trace_rather_than_only_carrying_it():
    results = (
        _result("raw.patient", "created_at", "event_date", 0.725),
        _result("mart.daily", "day", "event_date", 0.45),
    )
    g = Graph(edges=(_edge("raw.patient.created_at", "mart.daily.day", EdgeKind.CAST),))
    q = review.build(results, mask.generate(results), g)
    by = {i.address: i for i in q}
    assert "raw.patient.created_at" in by["mart.daily.day"].brief()
    assert "mart.daily.day" in by["raw.patient.created_at"].brief()


def check_an_item_blocking_something_does_not_claim_to_block_nothing():
    results, policies = _fixture()
    q = review.build(results, policies)
    for item in q:
        assert "nothing measurable" not in item.brief(), item.address


def check_an_item_blocking_nothing_says_so():
    # Only reachable by building the item directly, which is what a caller holding a
    # queue from somewhere other than `build` would do.
    results, policies = _fixture()
    reviews = [p for p in policies if p.action is Action.REVIEW]
    pol = [p for p in reviews if p.address == "raw.patient.city"][0]
    cls = [r for r in results if r.address == "raw.patient.city"][0]
    item = review.QueueItem(classification=cls, policy=pol)
    assert "nothing measurable" in item.brief()


def check_the_queue_item_and_the_decision_are_frozen():
    # An audit trail whose entries can be edited after the fact is not an audit trail.
    item = review.build(*_fixture())[0]
    d = Decision("t.a.c", True, "me", D(2026, 9, 22), "why")
    assert _raises(lambda: setattr(item, "policy", None))
    assert _raises(lambda: setattr(d, "personal", False))
    assert _raises(lambda: setattr(d, "reviewer", "somebody else"))
