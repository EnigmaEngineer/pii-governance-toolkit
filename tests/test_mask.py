from __future__ import annotations

from pii import mask
from pii.classify import Band, Classification, Signal, Arm
from pii.taxonomy import TAXONOMY, Granularity, Identifiability


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def _result(table, column, key, confidence, signals=None):
    """A classification with a signal attached unless one is asked for explicitly.

    The default carries one signal, because a result with an empty signal tuple means
    something specific in this module and a helper that produced it by accident would make
    half these checks test the wrong branch.
    """
    if signals is None:
        signals = (Signal(Arm.NAME, key, 0.9, "stub"),)
    return Classification(table, column, key, confidence, tuple(signals))


# Generation


def check_a_direct_identifier_is_redacted():
    p = mask.policy_for(_result("raw.patient", "email", "email", 0.99))
    assert p.action is mask.Action.REDACT
    assert p.target is None
    assert p.changes_the_value


def check_a_quasi_identifier_with_a_threshold_is_generalised_to_the_clause_target():
    p = mask.policy_for(_result("raw.patient", "birth_date", "birth_date", 0.96))
    assert p.action is mask.Action.GENERALISE
    assert p.target is Granularity.YEAR


def check_a_postal_code_generalises_to_three_digits():
    p = mask.policy_for(_result("raw.patient", "postal_code", "postal_code", 0.90))
    assert p.target is Granularity.POSTAL_3


def check_a_quasi_identifier_with_no_coarser_form_is_retained_and_still_counts():
    # `sex` carries no granularity threshold, so there is nothing between keeping it and
    # destroying it. It is retained and it stays in the quasi set, which is the whole point.
    p = mask.policy_for(_result("raw.patient", "sex", "sex", 0.95))
    assert p.action is mask.Action.RETAIN
    assert not p.evidence_only
    assert TAXONOMY.get(p.category_key).identifiability is Identifiability.QUASI


def check_a_sensitive_attribute_is_retained_rather_than_destroyed():
    p = mask.policy_for(_result("raw.encounter", "primary_diagnosis",
                                "health_condition", 0.80))
    assert p.action is mask.Action.RETAIN
    assert "not an identifier" in p.reason


def check_the_review_band_gets_a_review_and_not_a_mask():
    r = _result("raw.patient", "city", "postal_code", 0.60)
    assert r.band is Band.REVIEW
    assert mask.policy_for(r).action is mask.Action.REVIEW


def check_the_review_band_beats_the_category():
    """A direct identifier under the accept threshold is still a review.

    The band is checked before the category on purpose. Reading the category first would
    auto mask anything the classifier guessed was a name, at any confidence.
    """
    p = mask.policy_for(_result("raw.claim", "member_number", "health_plan_id", 0.70))
    assert p.action is mask.Action.REVIEW


def check_a_column_nothing_fired_on_is_marked_as_retained_on_absence():
    p = mask.policy_for(_result("raw.claim", "claim_id", "not_personal", 0.0, signals=()))
    assert p.action is mask.Action.RETAIN
    assert p.evidence_only
    assert "absence of evidence" in p.reason


def check_a_column_cleared_on_real_signals_is_not_marked_evidence_only():
    p = mask.policy_for(_result("raw.claim", "payer_name", "not_personal", 0.10))
    assert p.action is mask.Action.RETAIN
    assert not p.evidence_only


def check_retained_on_absence_lists_only_the_evidence_only_retains():
    policies = (
        mask.policy_for(_result("t", "a", "not_personal", 0.0, signals=())),
        mask.policy_for(_result("t", "b", "not_personal", 0.1)),
        mask.policy_for(_result("t", "c", "email", 0.99)),
    )
    assert mask.retained_on_absence(policies) == ("t.a",)


def check_generate_refuses_an_empty_result_set():
    assert _raises(lambda: mask.generate([]), ValueError)


def check_action_counts_covers_every_action_even_at_zero():
    counts = mask.action_counts((mask.policy_for(_result("t", "a", "email", 0.99)),))
    assert set(counts) == {a.value for a in mask.Action}
    assert counts["redact"] == 1
    assert counts["review"] == 0


# The policy object's own rules


def check_a_generalise_with_no_target_is_refused():
    assert _raises(lambda: mask.Policy("t", "c", "birth_date", mask.Action.GENERALISE,
                                       0.9, "r"), ValueError)


def check_a_target_on_something_that_does_not_generalise_is_refused():
    assert _raises(lambda: mask.Policy("t", "c", "email", mask.Action.REDACT, 0.9, "r",
                                       target=Granularity.YEAR), ValueError)


def check_evidence_only_on_a_non_retain_is_refused():
    assert _raises(lambda: mask.Policy("t", "c", "email", mask.Action.REDACT, 0.9, "r",
                                       evidence_only=True), ValueError)


def check_a_policy_with_no_reason_is_refused():
    assert _raises(lambda: mask.Policy("t", "c", "email", mask.Action.REDACT, 0.9, ""),
                   ValueError)


def check_a_policy_cannot_be_edited_after_it_is_generated():
    """A generated policy is a record of a decision and not a working variable.

    The validation in `__post_init__` runs once. If a caller can set `action` afterwards,
    a REDACT becomes a RETAIN with the redaction's reason still attached and nothing
    re-checks it. Unfreezing the class survived a mutation pass, which is how this got
    written.
    """
    p = mask.policy_for(_result("t", "email", "email", 0.99))
    assert _raises(lambda: setattr(p, "action", mask.Action.RETAIN), Exception)
    assert _raises(lambda: setattr(p, "reason", "changed"), Exception)


def check_a_residual_cannot_be_edited_after_it_is_measured():
    r = mask.Residual("m", ("a",), k=1, groups=2, singleton_groups=1, population=3)
    assert _raises(lambda: setattr(r, "k", 20), Exception)


# The clause list against the taxonomy's own ordering


def check_the_taxonomy_and_the_clause_list_disagree_about_dates():
    """Pins a real disagreement rather than asserting they match.

    The taxonomy says a temporal category identifies at DAY, which makes MONTH non
    identifying by its own ordering. Clause C removes every element of a date except the
    year, so a month does not survive either. The clause wins and the gap is reported. If
    this check ever fails, one of the two moved and the README says something wrong.
    """
    gaps = mask.threshold_disagreements()
    assert len(gaps) == 1
    assert gaps[0].startswith("temporal:")
    assert "month" in gaps[0] and "year" in gaps[0]


def check_the_postal_family_agrees():
    assert mask.derived_target("postal") is mask.SAFE_HARBOR_TARGET["postal"]


def check_a_family_with_no_threshold_is_refused():
    assert _raises(lambda: mask.derived_target("nonsense"), Exception)


# Application


def check_redaction_drops_the_value():
    p = mask.policy_for(_result("t", "email", "email", 0.99))
    assert mask.masking_expression(p) == "NULL"


def check_a_date_generalises_to_its_year():
    p = mask.policy_for(_result("t", "birth_date", "birth_date", 0.96))
    assert mask.masking_expression(p) == 'year("birth_date")'


def check_a_postal_code_generalises_to_a_prefix():
    p = mask.policy_for(_result("t", "postal_code", "postal_code", 0.90))
    assert mask.masking_expression(p) == 'substr(cast("postal_code" as varchar), 1, 3)'


def check_a_retained_column_is_quoted_rather_than_interpolated():
    p = mask.policy_for(_result("t", "sex", "sex", 0.95))
    assert mask.masking_expression(p) == '"sex"'


def check_masking_a_review_raises_rather_than_publishing_the_raw_value():
    """The failure worth crashing on.

    Returning the column untouched here would put the unmasked value into whatever the
    caller built, and it would look like the policy ran.
    """
    p = mask.policy_for(_result("t", "city", "postal_code", 0.60))
    assert _raises(lambda: mask.masking_expression(p), ValueError)


def check_a_target_with_no_expression_behind_it_is_refused():
    p = mask.Policy("t", "c", "birth_date", mask.Action.GENERALISE, 0.9, "r",
                    target=Granularity.MONTH)
    assert _raises(lambda: mask.masking_expression(p), ValueError)


# Residual risk


def _mart_policies():
    return (
        mask.policy_for(_result("m", "day", "event_date", 0.80)),
        mask.policy_for(_result("m", "postal_code", "postal_code", 0.90)),
        mask.policy_for(_result("m", "encounters", "not_personal", 0.0, signals=())),
    )


def check_the_table_name_is_quoted_part_by_part():
    """A qualified name is two identifiers and not one.

    Quoting the whole string produces a table called `raw.patient` rather than `patient`
    in schema `raw`. Every column here was quoted and the table was not, which is the
    defect rather than the dot being harmless.
    """
    assert mask._quote_fqn("raw.patient") == '"raw"."patient"'
    assert mask._quote_fqn("patient") == '"patient"'
    assert _raises(lambda: mask._quote_fqn(""), ValueError)


def check_the_residual_statement_names_the_table_in_quotes():
    sql = mask.residual_sql("analytics.encounter_daily", _mart_policies())
    assert 'FROM "analytics"."encounter_daily"' in sql


def check_the_residual_statement_groups_on_the_masked_quasi_set_only():
    sql = mask.residual_sql("m", _mart_policies())
    assert 'GROUP BY year("day"), substr(cast("postal_code" as varchar), 1, 3)' in sql
    assert '"encounters"' not in sql


def check_the_residual_statement_selects_no_user_value():
    """The no values rule, enforced rather than described.

    Everything in the select list is an aggregate over group sizes. A column name appearing
    outside the GROUP BY would mean a value coming back to the process.
    """
    sql = mask.residual_sql("m", _mart_policies())
    head = sql.split("GROUP BY")[0]
    assert '"day"' not in head
    assert '"postal_code"' not in head
    assert "min(s)" in head and "count(*)" in head


def check_a_weight_column_counts_people_rather_than_rows():
    sql = mask.residual_sql("m", _mart_policies(), weight="encounters")
    assert 'sum("encounters") AS s' in sql
    assert "count(*) AS s" not in sql


def check_measuring_around_an_unresolved_review_is_refused():
    policies = (
        mask.policy_for(_result("m", "city", "postal_code", 0.60)),
        mask.policy_for(_result("m", "postal_code", "postal_code", 0.90)),
    )
    assert _raises(lambda: mask.residual_sql("m", policies), ValueError)


def check_a_table_with_no_quasi_column_has_no_k_to_report():
    policies = (mask.policy_for(_result("m", "email", "email", 0.99)),)
    assert _raises(lambda: mask.residual_sql("m", policies), ValueError)


def check_a_residual_knows_whether_it_meets_a_target():
    r = mask.Residual("m", ("a",), k=1, groups=10, singleton_groups=9, population=20)
    assert not r.meets(2)
    assert r.meets(1)
    assert r.alone_share == 0.45


def check_a_k_target_under_one_is_refused():
    r = mask.Residual("m", ("a",), k=1, groups=1, singleton_groups=0, population=1)
    assert _raises(lambda: r.meets(0), ValueError)


def check_compliant_but_identifiable_picks_out_the_tables_under_target():
    safe = mask.Residual("s", ("a",), k=20, groups=6, singleton_groups=0, population=100)
    risky = mask.Residual("r", ("a",), k=1, groups=99, singleton_groups=98, population=100)
    assert mask.compliant_but_identifiable([safe, risky]) == (risky,)


def check_the_default_k_target_is_two_and_not_some_other_number():
    """Pins the default rather than only the behaviour either side of it.

    A table at exactly k of 2 is the only one that can tell 2 from 3, and without it the
    default is a number no check can see. A mutant moving it survived.
    """
    pair = mask.Residual("p", ("a",), k=2, groups=50, singleton_groups=0, population=100)
    assert mask.compliant_but_identifiable([pair]) == ()
    assert mask.compliant_but_identifiable([pair], k_target=3) == (pair,)


# The reviewer's decision


def _queued():
    return mask.policy_for(_result("raw.patient", "city", "postal_code", 0.60))


def check_a_reviewer_confirming_the_category_turns_a_review_into_a_mask():
    settled = mask.resolve_review(_queued(), True)
    assert settled.action is mask.Action.GENERALISE
    assert settled.target is Granularity.POSTAL_3
    assert settled.column == "city"


def check_a_reviewer_declining_turns_it_into_a_retain_and_says_who_decided():
    settled = mask.resolve_review(_queued(), False)
    assert settled.action is mask.Action.RETAIN
    assert settled.category_key == "not_personal"
    assert "a reviewer decided" in settled.reason


def check_the_two_reviewer_answers_do_not_produce_the_same_policy():
    """The guard against a decision argument nothing reads.

    A `resolve_review` that ignored `personal` would pass both checks above only if they
    happened to agree, and this is what makes that impossible.
    """
    yes = mask.resolve_review(_queued(), True)
    no = mask.resolve_review(_queued(), False)
    assert yes.action is not no.action


def check_a_resolved_policy_carries_the_reviewers_reason_and_the_original_reason():
    settled = mask.resolve_review(_queued(), True)
    assert settled.reason.startswith("a reviewer confirmed the category, then ")
    assert "postal_3" in settled.reason


def check_resolving_something_that_is_not_in_the_review_band_is_refused():
    masked = mask.policy_for(_result("t", "email", "email", 0.99))
    assert _raises(lambda: mask.resolve_review(masked, True), ValueError)


def check_resolving_the_same_policy_twice_is_refused():
    settled = mask.resolve_review(_queued(), True)
    assert _raises(lambda: mask.resolve_review(settled, True), ValueError)


def check_a_reviewers_decision_does_not_invent_a_confidence():
    """The classifier's number survives the review.

    Stamping the accept threshold onto the returned policy would report 0.75 for a column
    the evidence scored 0.60, which is a reviewer's judgement wearing a measurement's
    clothes.
    """
    queued = _queued()
    settled = mask.resolve_review(queued, True)
    assert settled.confidence == queued.confidence == 0.60


# measure_residual against a stub connection, so the arithmetic is reachable without a
# database. The database checks live in tests/test_mask_applied.py and they are slower and
# fewer, which is the wrong place for a rule that decides which columns enter the set.


class _StubCon:
    def __init__(self, row):
        self.row = row
        self.sql = None

    def execute(self, sql):
        self.sql = sql
        return self

    def fetchone(self):
        return self.row


def check_measure_residual_reports_the_quasi_columns_and_not_the_others():
    policies = _mart_policies()
    con = _StubCon((1, 1444, 1386, 1504))
    r = mask.measure_residual(con, "m", policies)
    assert r.columns == ("day", "postal_code")
    assert r.k == 1 and r.groups == 1444
    assert r.singleton_groups == 1386 and r.population == 1504
    assert abs(r.alone_share - 0.9215) < 0.0001


def check_measure_residual_passes_the_weight_through_to_the_statement():
    con = _StubCon((20, 6, 0, 1504))
    mask.measure_residual(con, "m", _mart_policies(), weight="encounters")
    assert 'sum("encounters")' in con.sql
