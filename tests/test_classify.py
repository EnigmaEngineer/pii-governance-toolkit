"""Checks for the classifier, its confidence, and its bands.

Nothing here touches a database. The arms take a `ColumnProfile`, which is a bag of
integers, so the whole scoring path is testable from the standard library and only the
profiler needs duckdb.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

from pii import classify
from pii.classify import (ACCEPT_AT, REVIEW_AT, Arm, Band, ColumnProfile, Signal,
                          classify_column, classify_table, classify_warehouse,
                          name_signals, structure_signals, table_is_person_linked,
                          tokenise, value_signals)
from pii.taxonomy import Identifiability


def _profile(column, sql_type="VARCHAR", rows=100, non_null=100, distinct=90,
             mean_length=8.0, hits=None, is_key=False, nullable=True,
             table="raw.patient"):
    return ColumnProfile(
        table=table, column=column, sql_type=sql_type, nullable=nullable,
        is_key=is_key, rows=rows, non_null=non_null, distinct=distinct,
        mean_length=mean_length, pattern_hits=hits or {},
    )


def check_tokenise_splits_on_underscore_and_camel_case():
    assert tokenise("first_name") == ("first", "name")
    assert tokenise("patientMRN") == ("patient", "mrn")
    assert tokenise("source-ip addr") == ("source", "ip", "addr")
    assert tokenise("") == ()


def check_a_token_is_not_a_substring():
    # The floor matches substrings and says `ip` is inside `disposition`. That is the
    # specific failure this arm exists to avoid, so it is asserted rather than described.
    assert "disposition" in "disposition"
    assert name_signals("disposition") == ()
    assert name_signals("recipient_kind") == ()
    assert [s.category_key for s in name_signals("source_ip")] == ["ip_address"]


def check_a_bare_name_token_scores_lower_than_a_qualified_one():
    # `first_name` fires both rules, since it carries the qualified form and the bare
    # token, and they agree so they combine. Take the strongest from each side rather
    # than the only one, because assuming a single signal is assuming the rules do not
    # overlap and they do.
    bare = max(s.strength for s in name_signals("payer_name")
               if s.category_key == "person_name")
    qualified = max(s.strength for s in name_signals("first_name")
                    if s.category_key == "person_name")
    assert bare < qualified
    # And the gap has to straddle the accept threshold or the distinction buys nothing.
    assert bare < ACCEPT_AT <= qualified
    # The bare form on its own must still reach a human rather than be dropped.
    assert classify_column(_profile("payer_name")).band is Band.REVIEW


def check_a_temporal_suffix_fires_and_stays_weak():
    for name in ("admitted_at", "submitted_on", "event_date", "created_at"):
        keys = [s.category_key for s in name_signals(name)]
        assert "event_date" in keys, name
    weak = [s for s in name_signals("created_at") if s.category_key == "event_date"]
    assert weak[0].strength < ACCEPT_AT


def check_the_value_arm_reports_the_denominator_it_used():
    p = _profile("contact", rows=100, non_null=40, distinct=40, hits={"email": 40})
    sig = [s for s in value_signals(p) if s.category_key == "email"][0]
    # Every value present matched, so the rate is 1.0 and the support is what carries the
    # sixty missing values into the answer. A weight equal to the strength would mean the
    # nulls had been silently dropped, which is what the floor does.
    assert sig.strength == 0.90
    assert abs(sig.support - 0.40) < 1e-9
    assert abs(sig.weight - 0.36) < 1e-9
    assert "40 of 40" in sig.detail
    assert "40% of the column is populated" in sig.detail


def check_a_column_with_no_values_produces_no_value_evidence():
    p = _profile("contact", rows=100, non_null=0, distinct=0, hits={"email": 0})
    assert value_signals(p) == ()
    assert p.support == 0.0
    # And an empty table, where the rate would be a division by zero rather than a zero.
    assert _profile("contact", rows=0, non_null=0, distinct=0).support == 0.0


def check_a_match_rate_under_the_floor_is_not_evidence():
    below = _profile("contact", non_null=100, hits={"email": 59})
    above = _profile("contact", non_null=100, hits={"email": 60})
    assert value_signals(below) == ()
    assert [s.category_key for s in value_signals(above)] == ["email"]


def check_the_structure_arm_ignores_a_key():
    stamp = _profile("x", sql_type="TIMESTAMP")
    key = _profile("x", sql_type="TIMESTAMP", is_key=True, nullable=False)
    assert [s.category_key for s in structure_signals(stamp)] == ["event_date"]
    assert structure_signals(key) == ()


def check_the_structure_arm_reads_the_type_without_its_precision():
    assert structure_signals(_profile("x", sql_type="TIMESTAMP(6)")) != ()
    assert structure_signals(_profile("x", sql_type="decimal(12,2)")) == ()


def check_two_arms_agreeing_beat_either_one_alone():
    name_only = classify_column(_profile("email"))
    both = classify_column(_profile("email", non_null=100, hits={"email": 100}))
    assert name_only.category_key == both.category_key == "email"
    assert both.confidence > name_only.confidence
    assert both.confidence <= 1.0


def check_combining_evidence_cannot_exceed_one():
    assert classify._combine([0.9, 0.9, 0.9, 0.9]) < 1.0
    assert classify._combine([]) == 0.0
    assert classify._combine([1.0, 0.5]) == 1.0


def check_two_categories_disagreeing_lowers_confidence():
    # A ten digit licence number matches the telephone predicate. The name arm says
    # licence and the value arm says phone, and the answer should arrive less certain than
    # it would have on the name alone. Both are direct identifiers, which is why the
    # penalty cannot be keyed on identifiability.
    alone = classify_column(_profile("attending_npi", non_null=100))
    conflicted = classify_column(
        _profile("attending_npi", non_null=100, hits={"phone": 100}))
    assert alone.category_key == conflicted.category_key == "licence_number"
    assert alone.identifiability is conflicted.identifiability
    assert conflicted.runner_up == "phone"
    assert conflicted.confidence < alone.confidence
    # And far enough to matter. A penalty that leaves the column in the same band is a
    # penalty nothing acts on.
    assert alone.band is Band.ACCEPT and conflicted.band is Band.REVIEW


def check_two_readings_of_one_fact_are_not_a_conflict():
    # `birth_date` scores on birth_date and on the temporal suffix. Both carry a threshold
    # in the temporal family, so one is the other read coarser and no penalty applies.
    r = classify_column(_profile("birth_date", sql_type="DATE"))
    assert r.category_key == "birth_date"
    assert r.runner_up == "event_date"
    assert classify.two_readings_of_one_fact("birth_date", "event_date")
    best = classify._combine([s.weight for s in r.signals
                              if s.category_key == "birth_date"])
    assert abs(r.confidence - round(best, 4)) < 1e-9


def check_the_waiver_needs_both_sides_to_carry_a_threshold():
    # Both directions, and the pair that shares an identifiability level without sharing
    # a family, which is the case the first version of this rule got wrong.
    assert classify.two_readings_of_one_fact("postal_code", "postal_code")
    assert not classify.two_readings_of_one_fact("licence_number", "phone")
    assert not classify.two_readings_of_one_fact("postal_code", "birth_date")
    assert not classify.two_readings_of_one_fact("email", "phone")
    assert not classify.two_readings_of_one_fact("birth_date", "person_name")


def check_a_column_with_no_evidence_is_not_personal_rather_than_unknown():
    r = classify_column(_profile("billed_amount", sql_type="DECIMAL(12,2)"))
    assert r.category_key == "not_personal"
    assert r.confidence == 0.0
    assert r.band is Band.IGNORE
    assert not r.flagged


def check_the_bands_sit_where_the_thresholds_say():
    assert REVIEW_AT < ACCEPT_AT
    p = _profile("x")
    for confidence, expected in ((ACCEPT_AT, Band.ACCEPT),
                                 (ACCEPT_AT - 0.0001, Band.REVIEW),
                                 (REVIEW_AT, Band.REVIEW),
                                 (REVIEW_AT - 0.0001, Band.IGNORE)):
        r = classify.Classification(p.table, p.column, "email", confidence, ())
        assert r.band is expected, (confidence, r.band)
    # Both boundaries from both sides, because a validator checked away from its limit
    # does not pin the limit.
    assert classify.Classification("t", "c", "email", 1.0, ()).band is Band.ACCEPT
    assert classify.Classification("t", "c", "email", 0.0, ()).band is Band.IGNORE


def check_not_personal_is_ignored_whatever_its_confidence():
    r = classify.Classification("t", "c", "not_personal", 0.99, ())
    assert r.band is Band.IGNORE
    assert not r.flagged and not r.auto_masked


def check_a_signal_must_name_a_real_category():
    for bad in ({"category_key": "not_a_category"}, {"strength": 1.5},
                {"strength": -0.1}, {"support": 2.0}, {"detail": ""}):
        kwargs = {"arm": Arm.NAME, "category_key": "email", "strength": 0.5,
                  "detail": "x"}
        kwargs.update(bad)
        try:
            Signal(**kwargs)
        except ValueError:
            continue
        raise AssertionError("Signal accepted {}".format(bad))


def check_a_profile_refuses_counts_that_cannot_be_true():
    # Assert the message and not the type. A negative row count with a positive non null
    # count trips the "more non null values than rows" guard as well, so the first draft
    # of this check passed with the negative count guard removed entirely. It was right
    # about the exception and wrong about which line raised it.
    cases = (
        ({"non_null": 101}, "more non null values than rows"),
        ({"distinct": 101}, "more distinct values than non null"),
        ({"rows": -1, "non_null": -1, "distinct": -1}, "negative row count"),
        ({"rows": -1, "non_null": 0, "distinct": 0}, "negative row count"),
        ({"rows": 0, "non_null": -1, "distinct": -1}, "negative row count"),
    )
    for bad, expected in cases:
        kwargs = dict(table="t", column="c", sql_type="VARCHAR", nullable=True,
                      is_key=False, rows=100, non_null=100, distinct=100,
                      mean_length=1.0)
        kwargs.update(bad)
        try:
            ColumnProfile(**kwargs)
        except ValueError as exc:
            assert expected in str(exc), (bad, str(exc))
            continue
        raise AssertionError("ColumnProfile accepted {}".format(bad))


def check_the_records_cannot_be_edited_after_they_are_made():
    # A classification is the thing a masking policy and an audit trail both read back.
    # A caller that can quietly raise a confidence after the fact is a governance hole and
    # nothing was asserting the records are frozen.
    p = _profile("email", non_null=100, hits={"email": 100})
    r = classify_column(p)
    # The type is the right assertion here rather than the message, because
    # `FrozenInstanceError` is raised by exactly one thing and the language does not
    # produce it for any other reason. The message is asserted too, so a mutant that
    # blocks the wrong field cannot pass.
    for obj, attribute, value in ((p, "rows", 0), (r, "confidence", 1.0),
                                  (r.signals[0], "strength", 1.0)):
        try:
            setattr(obj, attribute, value)
        except FrozenInstanceError as exc:
            assert attribute in str(exc), str(exc)
            continue
        raise AssertionError("{} allowed {} to be reassigned".format(
            type(obj).__name__, attribute))


def check_distinct_ratio_divides_and_survives_an_empty_column():
    p = _profile("x", rows=100, non_null=80, distinct=20)
    assert abs(p.distinct_ratio - 0.25) < 1e-9
    # Both sides of the zero guard, since the guard and the division are two sites.
    assert _profile("x", rows=0, non_null=0, distinct=0).distinct_ratio == 0.0
    assert _profile("x", rows=5, non_null=1, distinct=1).distinct_ratio == 1.0


def check_the_free_text_rule_sits_exactly_on_both_of_its_limits():
    # Two thresholds in one condition, so four fixtures: each one on its boundary and each
    # one a hair under. A rule checked away from its limits does not pin them.
    def signals(mean_length, distinct, non_null=100):
        return [s.category_key for s in value_signals(
            _profile("body", mean_length=mean_length, non_null=non_null,
                     distinct=distinct))]

    assert signals(40.0, 2) == ["free_text_clinical"]
    assert signals(39.9, 2) == []
    assert signals(40.0, 1) == []
    assert signals(60.0, 1) == []
    assert signals(60.0, 2) == ["free_text_clinical"]


def check_a_tie_between_two_categories_is_broken_by_name_and_not_by_score():
    # Two categories reaching exactly the same score. Without a deterministic tie break
    # the answer depends on dictionary ordering, which is a classification that changes
    # when somebody adds a rule somewhere else in the file.
    p = _profile("x")
    signals = (
        Signal(Arm.NAME, "phone", 0.60, "one"),
        Signal(Arm.NAME, "email", 0.60, "two"),
    )
    first = classify._decide(p, signals)
    second = classify._decide(p, tuple(reversed(signals)))
    assert first.category_key == second.category_key == "email"
    assert first.runner_up == second.runner_up == "phone"
    assert first.confidence == second.confidence


def check_the_confidence_is_rounded_to_four_places():
    # The published number. A fifth decimal in a report is noise and a third loses a
    # comparison the threshold sweep depends on.
    p = _profile("x")
    r = classify._decide(p, (Signal(Arm.NAME, "email", 0.123456, "x"),))
    assert r.confidence == 0.1235
    assert len(str(r.confidence).split(".")[1]) <= 4
    conflicted = classify._decide(p, (Signal(Arm.NAME, "email", 0.7654321, "x"),
                                      Signal(Arm.VALUE, "birth_date", 0.333333, "y")))
    assert conflicted.confidence == round(conflicted.confidence, 4)
    assert conflicted.confidence != round(conflicted.confidence, 3)
    # The runner up score is rounded too, and it is published: `explain` prints it and so
    # does the probe. A field that reaches a report needs the same pin as the headline.
    assert conflicted.runner_up == "birth_date"
    assert conflicted.runner_up_score == 0.3333
    assert conflicted.runner_up_score != round(0.333333, 5)


def check_the_context_bar_includes_a_column_sitting_exactly_on_it():
    # `>=` against `>`. A direct identifier landing precisely on the bar has to count,
    # or the bar means one thing in the docstring and another in the code.
    on_the_bar = classify.Classification("t", "c", "email", REVIEW_AT, ())
    under = classify.Classification("t", "c", "email", REVIEW_AT - 0.0001, ())
    assert table_is_person_linked((on_the_bar,))
    assert not table_is_person_linked((under,))
    assert table_is_person_linked((on_the_bar,), evidence_bar=REVIEW_AT)
    assert not table_is_person_linked((on_the_bar,), evidence_bar=ACCEPT_AT)


def check_a_quasi_identifier_does_not_make_a_table_about_people():
    # Only a direct identifier counts, and the mart is the case that depends on it. A
    # postal code column is a quasi identifier and every row of the mart carries one.
    quasi = classify.Classification("analytics.daily", "postal_code", "postal_code",
                                    0.99, ())
    assert quasi.identifiability is Identifiability.QUASI
    assert not table_is_person_linked((quasi,))


def check_the_temporal_detail_names_the_suffix_that_fired():
    # The detail is what a reviewer reads to decide whether to trust the answer, so a
    # mutant that reports the wrong token is worse than one inside the arithmetic.
    for name, suffix in (("admitted_at", "at"), ("submitted_on", "on"),
                         ("service_date", "date")):
        sig = [s for s in name_signals(name)
               if s.arm is Arm.NAME and "reads as a date" in s.detail][0]
        assert repr(suffix) in sig.detail, (name, sig.detail)


def check_rule_coverage_reports_both_arms_per_category():
    coverage_map = classify.rule_coverage()
    from pii.taxonomy import TAXONOMY
    assert set(coverage_map) == set(TAXONOMY.keys())
    # email carries a token rule and a predicate. person_name carries only a token rule.
    # not_personal carries neither and is the absence of a finding rather than a gap.
    assert coverage_map["email"] == (True, True)
    assert coverage_map["person_name"] == (True, False)
    assert coverage_map["biometric"] == (False, False)
    assert coverage_map["not_personal"] == (False, False)
    # The two arms that produce a category without a table entry, which a map derived
    # from the tables alone would miss.
    assert coverage_map["event_date"][0] is True
    assert coverage_map["free_text_clinical"][1] is True


def check_undetectable_names_only_the_categories_no_arm_can_return():
    undetectable = classify.undetectable_categories()
    coverage_map = classify.rule_coverage()
    assert undetectable == tuple(sorted(undetectable))
    assert "not_personal" not in undetectable
    for key in undetectable:
        assert coverage_map[key] == (False, False), key
    for key, (by_name, by_value) in coverage_map.items():
        if key == "not_personal":
            continue
        if by_name or by_value:
            assert key not in undetectable, key
        else:
            assert key in undetectable, key
    # `payment_card` has rules and no column in the sample warehouse, which is a
    # different state and the one worth a fixture rather than a note.
    assert "payment_card" not in undetectable
    assert "biometric" in undetectable


def check_a_profile_carries_no_value_from_the_column():
    # The invariant the whole design rests on, asserted by walking the dataclass rather
    # than by trusting the docstring that claims it. A field added later that holds a
    # sample fails here.
    allowed = {"table", "column", "sql_type", "nullable", "is_key",
               "rows", "non_null", "distinct", "mean_length", "pattern_hits"}
    assert {f.name for f in fields(ColumnProfile)} == allowed


def check_table_context_removes_a_temporal_signal_and_adds_nothing():
    # A mart with no direct identifier in it. The date is a grain rather than a date
    # belonging to anybody.
    mart = [
        _profile("day", sql_type="DATE", table="analytics.daily"),
        _profile("department", table="analytics.daily", distinct=5),
        _profile("encounters", sql_type="BIGINT", table="analytics.daily", distinct=3),
    ]
    alone = classify_column(mart[0])
    with_context = classify_table(mart)[0]
    assert alone.flagged
    assert not with_context.flagged
    assert not table_is_person_linked(tuple(classify_column(p) for p in mart))


def check_table_context_keeps_a_temporal_signal_where_somebody_is_named():
    rows = [
        _profile("email", table="raw.p", non_null=100, hits={"email": 100}),
        _profile("admitted_at", sql_type="TIMESTAMP", table="raw.p"),
    ]
    results = classify_table(rows)
    assert results[1].flagged
    assert results[1].category_key == "event_date"


def check_the_context_bar_sits_at_the_review_floor_and_the_cliff_is_real():
    # The defect this default exists for. `member_number` scores 0.70, which is a direct
    # identifier in the review band, and at a bar of ACCEPT_AT the table reads as being
    # about nobody. A different column then loses its only evidence.
    rows = [
        _profile("member_number", table="raw.claim"),
        _profile("submitted_on", sql_type="DATE", table="raw.claim"),
    ]
    assert classify_column(rows[0]).confidence < ACCEPT_AT
    assert classify_column(rows[0]).identifiability is Identifiability.DIRECT
    shipped = classify_table(rows)
    strict = classify_table(rows, evidence_bar=ACCEPT_AT)
    assert shipped[1].flagged
    assert not strict[1].flagged


def check_table_context_is_called_with_no_keyword_at_all():
    # The production default is what every caller who has not read the source gets, and a
    # helper that names the argument every time leaves it untested.
    rows = [_profile("member_number", table="raw.claim"),
            _profile("submitted_on", sql_type="DATE", table="raw.claim")]
    assert classify_table(rows)[1].flagged
    assert classify_warehouse(rows)[1].flagged
    assert table_is_person_linked((classify_column(rows[0]),))


def check_the_second_pass_never_flags_something_the_first_pass_did_not():
    rows = [
        _profile("day", sql_type="DATE", table="analytics.daily"),
        _profile("department", table="analytics.daily", distinct=5),
        _profile("email", table="raw.p", non_null=100, hits={"email": 100}),
        _profile("admitted_at", sql_type="TIMESTAMP", table="raw.p"),
    ]
    independent = {p.column: classify_column(p).flagged for p in rows}
    with_context = {r.column: r.flagged for r in classify_warehouse(rows)}
    for column, flagged in with_context.items():
        assert not (flagged and not independent[column]), column


def check_classify_table_refuses_two_tables():
    rows = [_profile("a", table="raw.one"), _profile("b", table="raw.two")]
    try:
        classify_table(rows)
    except ValueError as exc:
        assert "one table at a time" in str(exc)
        return
    raise AssertionError("classify_table accepted two tables")


def check_an_empty_warehouse_produces_nothing_rather_than_raising():
    assert classify_warehouse(()) == ()
    assert classify_table(()) == ()
    assert classify.band_counts(()) == {"accept": 0, "review": 0, "ignore": 0}


def check_band_counts_add_up():
    rows = [_profile("email", non_null=100, hits={"email": 100}),
            _profile("city"), _profile("billed_amount", sql_type="DOUBLE")]
    results = classify_warehouse(rows)
    counts = classify.band_counts(results)
    assert sum(counts.values()) == len(results) == 3
    assert counts["accept"] == 1 and counts["review"] == 1 and counts["ignore"] == 1


def check_explain_names_every_signal_that_produced_the_answer():
    r = classify_column(_profile("email", non_null=100, hits={"email": 100}))
    text = r.explain()
    for s in r.signals:
        assert s.detail in text
        assert s.arm.value in text
    assert "{:.4f}".format(r.confidence) in text


def check_explain_shows_the_precision_the_band_is_decided_at():
    # Two columns a ten thousandth apart, in different bands. Printed at two decimals
    # they are the same number, which reads to a reviewer as a tool contradicting itself.
    just_over = classify.Classification("t", "a", "email", ACCEPT_AT, ())
    just_under = classify.Classification("t", "b", "email", ACCEPT_AT - 0.0001, ())
    assert just_over.band is Band.ACCEPT and just_under.band is Band.REVIEW
    over, under = just_over.explain(), just_under.explain()
    assert "{:.4f}".format(ACCEPT_AT) in over
    assert "{:.4f}".format(ACCEPT_AT - 0.0001) in under
    assert over.split("at ")[1] != under.split("at ")[1]


def check_the_phone_predicate_does_not_read_a_date_as_a_telephone_number():
    # The floor's pattern is digits and dashes of about the right length, which is the
    # shape of an ISO date, and that is why a column of dates came back as telephone
    # numbers. The predicate here is SQL so the check is on the pattern's intent: strip
    # the separators and a date has eight digits where a phone number has at least ten.
    import re
    expr = dict((k, e) for k, e, _s, _w in classify.VALUE_PREDICATES)["phone"]
    assert "10,15" in expr
    stripped = re.compile(r"^\+?[0-9]{10,15}$")
    assert not stripped.match("20260105")
    assert stripped.match("15551234567")
    assert stripped.match("1234567890")


def check_only_a_predicate_that_earns_it_can_mask_a_column_on_its_own():
    # A single arm reaching the accept threshold means one piece of evidence decides that
    # nobody looks at the column. Some predicates deserve that and the short vocabulary
    # does not, so the split is asserted rather than left to whoever edits the table next.
    strengths = {k: s for k, _e, s, _w in classify.VALUE_PREDICATES}
    assert strengths["email"] >= ACCEPT_AT
    assert strengths["national_id"] >= ACCEPT_AT
    assert strengths["sex"] < ACCEPT_AT
    assert strengths["phone"] < ACCEPT_AT
    for key, strength in strengths.items():
        assert strength > REVIEW_AT, key


def check_every_value_predicate_names_a_category_in_the_taxonomy():
    from pii.taxonomy import TAXONOMY
    for key, expr, strength, why in classify.VALUE_PREDICATES:
        assert key in TAXONOMY, key
        assert "{c}" in expr, key
        assert 0.0 < strength <= 1.0, key
        assert why
    for needles, key, strength, why in classify.TOKEN_RULES:
        assert key in TAXONOMY, key
        assert needles and all(n == n.lower() for n in needles), key
        assert 0.0 < strength <= 1.0, key
        assert why


def check_no_two_token_rules_claim_the_same_needle():
    seen = {}
    for needles, key, _strength, _why in classify.TOKEN_RULES:
        for n in needles:
            assert n not in seen, "{} claimed by {} and {}".format(n, seen.get(n), key)
            seen[n] = key
