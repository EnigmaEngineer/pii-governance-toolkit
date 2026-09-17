from __future__ import annotations

from pii import coverage, safeharbor
from pii.corpus import generate
from pii.coverage import ColumnVerdict, Report
from pii.taxonomy import TAXONOMY, Identifiability


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


SMALL = generate(n_patients=120, seed=4242)
SAMPLES = {
    "raw.patient": tuple(SMALL.patients),
    "raw.encounter": tuple(SMALL.encounters),
    "raw.claim": tuple(SMALL.claims),
    "raw.device_reading": tuple(SMALL.readings),
}


def _flat(report, key):
    return Report(
        verdicts=tuple(
            ColumnVerdict(v.table, v.column, v.planted_key, key,
                          v.in_safe_harbor_scope, v.identifies_at_recorded_granularity)
            for v in report.verdicts
        ),
        taxonomy_fingerprint=report.taxonomy_fingerprint,
    )


def check_scope_is_decided_by_the_clause_list_and_not_by_the_planted_label():
    report = coverage.grade()
    mapped = set(safeharbor.mapped_keys())
    for v in report.verdicts:
        assert v.in_safe_harbor_scope == (v.planted_key in mapped), v.address


def check_something_is_in_scope_and_something_is_not():
    report = coverage.grade()
    assert report.in_scope, "nothing in scope, so recall has no denominator"
    assert len(report.in_scope) < report.n_columns, "everything in scope"


def check_a_scan_that_flags_nothing_scores_zero():
    blank = _flat(coverage.grade(), "not_personal")
    assert blank.recall == 0.0
    assert blank.missed == blank.in_scope
    assert blank.false_alarms == ()


def check_a_scan_that_flags_everything_scores_one_and_pays_for_it_in_false_alarms():
    everything = _flat(coverage.grade(), "person_name")
    assert everything.recall == 1.0
    assert everything.missed == ()
    assert len(everything.false_alarms) == len(
        coverage.columns_planted_as("not_personal"))


def check_recall_raises_rather_than_returning_zero_when_nothing_is_in_scope():
    # A ratio over an empty denominator has to be a refusal. Returning 0.0 would read as
    # a scan that found nothing rather than as a report with nothing to find.
    empty = Report(verdicts=(
        ColumnVerdict("t", "c", "not_personal", "not_personal", False, False),
    ), taxonomy_fingerprint="x")
    assert _raises(lambda: empty.recall, ValueError)


def check_the_value_arm_adds_something_and_the_two_arms_are_not_the_same_number():
    names_only = coverage.grade()
    with_values = coverage.grade(SAMPLES)
    assert with_values.recall > names_only.recall, (
        names_only.recall, with_values.recall)


def check_the_shipped_scan_is_neither_perfect_nor_useless():
    # A recall of exactly 0 or exactly 1 on the real arm would mean the grader is reading
    # something other than the scan.
    r = coverage.grade(SAMPLES).recall
    assert 0.0 < r < 1.0, r


def check_missing_a_column_and_flagging_it_as_the_wrong_thing_are_separate_verdicts():
    missed = ColumnVerdict("t", "c", "email", "not_personal", True, True)
    wrong = ColumnVerdict("t", "c", "email", "phone", True, True)
    right = ColumnVerdict("t", "c", "email", "email", True, True)

    assert missed.missed is True and missed.wrong_category is False
    assert wrong.missed is False and wrong.wrong_category is True
    assert right.missed is False and right.wrong_category is False
    assert right.naive_calls_it_personal is True


def check_a_false_alarm_needs_the_planted_label_to_be_not_personal():
    alarm = ColumnVerdict("t", "c", "not_personal", "phone", False, False)
    not_alarm = ColumnVerdict("t", "c", "phone", "phone", True, True)
    assert alarm.false_alarm is True
    assert not_alarm.false_alarm is False
    # And a wrong category is not a false alarm, because the column really is personal.
    assert not_alarm.wrong_category is False
    assert ColumnVerdict("t", "c", "phone", "email", True, True).false_alarm is False


def check_an_out_of_scope_column_can_never_be_missed():
    v = ColumnVerdict("t", "c", "health_condition", "not_personal", False, False)
    assert v.missed is False


def check_the_identifiability_split_covers_the_whole_in_scope_set():
    report = coverage.grade(SAMPLES)
    split = report.by_identifiability()
    assert sum(b["in_scope"] for b in split.values()) == len(report.in_scope)
    assert sum(b["found"] for b in split.values()) == sum(
        1 for v in report.in_scope if v.naive_calls_it_personal)


def check_the_split_separates_direct_from_quasi_rather_than_pooling_them():
    split = coverage.grade(SAMPLES).by_identifiability()
    assert "direct" in split and "quasi" in split
    assert split["direct"]["in_scope"] > 0
    assert split["quasi"]["in_scope"] > 0


def check_the_quasi_filter_only_returns_quasi_columns():
    report = coverage.grade(SAMPLES)
    qs = coverage.quasi_only_report(report)
    assert qs
    for v in qs:
        assert TAXONOMY.get(v.planted_key).identifiability is Identifiability.QUASI
        assert v.in_safe_harbor_scope is True


def check_grading_with_no_samples_is_the_name_arm_alone():
    a = coverage.grade()
    b = coverage.grade({})
    assert [v.naive_key for v in a.verdicts] == [v.naive_key for v in b.verdicts]


def check_the_report_carries_the_fingerprint_of_the_taxonomy_it_was_built_against():
    assert coverage.grade().taxonomy_fingerprint == TAXONOMY.fingerprint()


def check_the_planted_summary_adds_up_to_every_column():
    from pii.schema import PLANTED
    assert sum(coverage.planted_summary().values()) == len(PLANTED)


def check_columns_planted_as_finds_something_and_nothing():
    assert coverage.columns_planted_as("not_personal")
    assert coverage.columns_planted_as("biometric") == ()


def check_the_verdict_and_the_report_are_both_frozen():
    report = coverage.grade()
    cases = [(report.verdicts[0], "naive_key", "phone"),
             (report, "taxonomy_fingerprint", "x")]
    for obj, attr, value in cases:
        try:
            setattr(obj, attr, value)
        except Exception:
            continue
        raise AssertionError("{} is not frozen".format(type(obj).__name__))


def check_the_address_reads_as_a_fully_qualified_column():
    v = coverage.grade().verdicts[0]
    assert v.address == "{}.{}".format(v.table, v.column)
    assert v.address.count(".") >= 2
