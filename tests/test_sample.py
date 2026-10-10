"""Checks for the opt-in sampling arm that need no database.

The shape function and the two rules are the whole arm, and both rules are a set of
refusals with one acceptance at the end. So most of what is here is a refusal, which is the
right shape for a check suite over a rule whose failure mode is firing too often.
"""

from __future__ import annotations

from dataclasses import fields

from pii import sample
from pii.classify import ALL_ARMS, ARMS_WITH_SAMPLING, Arm, ColumnProfile, column_signals
from pii.sample import ColumnSample, luhn_ok, sample_signals, shape


def _sample(column="weird", sql_type="VARCHAR", shapes=None, read=100, non_null=100,
            distinct=100, luhn_candidates=0, luhn_valid=0):
    shapes = {"AA9999999": non_null} if shapes is None else shapes
    return ColumnSample(
        table="probe.t", column=column, sql_type=sql_type, read=read, non_null=non_null,
        distinct=distinct, shape_counts=shapes, luhn_candidates=luhn_candidates,
        luhn_valid=luhn_valid)


def check_shape_collapses_letters_and_digits_and_keeps_a_separator():
    assert shape("AB1234567") == "AA9999999"
    assert shape("123-45-6789") == "999-99-9999"
    assert shape("a.b@c.dd") == "A.A@A.AA"


def check_a_character_outside_the_allowlist_becomes_a_question_mark():
    # The bound on what a shape can leak. A default of pass through would make it the
    # column's own punctuation, which is unbounded.
    assert shape("a£b") == "A?A"
    assert shape("x\ty") == "A?A"


def check_a_non_ascii_letter_is_still_a_letter():
    # `str.isalpha` is true for these and that is the behaviour wanted. A shape that marked
    # an accented name as unknown punctuation would report a leak that is not one.
    assert shape("éü") == "AA"


def check_luhn_accepts_a_valid_card_and_refuses_a_bad_digit():
    assert luhn_ok("4539578763621486")
    assert not luhn_ok("4539578763621487")
    assert not luhn_ok("")
    assert not luhn_ok("4539-5787")


def check_a_sample_refuses_shape_counts_that_do_not_sum_to_its_non_null_count():
    # The one way this object can lie. Shapes are counted in the same loop as the non null
    # values and a mismatch means one of the two paths was edited alone.
    try:
        _sample(shapes={"AA9999999": 50})
    except ValueError as exc:
        assert "shape counts sum to 50" in str(exc), str(exc)
    else:
        raise AssertionError("a sample with mismatched shape counts was accepted")


def check_a_sample_holds_no_field_that_could_carry_a_value():
    allowed = {"table", "column", "sql_type", "read", "non_null", "distinct",
               "shape_counts", "luhn_candidates", "luhn_valid"}
    assert {f.name for f in fields(ColumnSample)} == allowed


def check_the_allowlist_check_names_a_shape_that_leaked():
    clean = _sample()
    assert sample.samples_hold_no_values([clean]) == ()
    dirty = _sample(shapes={"AA?999999": 100})
    assert sample.samples_hold_no_values([dirty]) == ()
    # A question mark is allowed because the shape function produces it. A letter that is
    # not `A` and a digit that is not `9` are what a leak looks like.
    leaked = ColumnSample(
        table="probe.t", column="c", sql_type="VARCHAR", read=1, non_null=1, distinct=1,
        shape_counts={"Smith": 1}, luhn_candidates=0, luhn_valid=0)
    hits = sample.samples_hold_no_values([leaked])
    assert len(hits) == 1 and "probe.t.c" in hits[0], hits


def check_a_fixed_shape_high_cardinality_code_returns_an_unnamed_identifier():
    signals = sample_signals(_sample())
    assert len(signals) == 1, signals
    s = signals[0]
    assert s.arm is Arm.SAMPLE
    assert s.category_key == "unknown_identifier"
    assert s.strength == sample.UNKNOWN_IDENTIFIER_STRENGTH


def check_the_unnamed_identifier_can_never_reach_the_accept_band():
    from pii.classify import ACCEPT_AT
    assert sample.UNKNOWN_IDENTIFIER_STRENGTH < ACCEPT_AT
    p = ColumnProfile("probe.t", "weird", "VARCHAR", True, False, 100, 100, 100, 9.0)
    p = p.with_sampled_signals(sample_signals(_sample()))
    from pii.classify import classify_column
    result = classify_column(p, ARMS_WITH_SAMPLING)
    assert result.band.value == "review", result.explain()


def check_a_declared_temporal_type_returns_nothing():
    # A timestamp cast to text is a fixed shape high cardinality code by every structural
    # test in the module, so without this guard the arm fires on every date column.
    s = _sample(sql_type="TIMESTAMP", shapes={"9999-99-99 99:99:99": 100})
    assert sample_signals(s) == ()


def check_a_date_held_as_text_returns_nothing():
    s = _sample(sql_type="VARCHAR", shapes={"9999-99-99": 100})
    assert sample_signals(s) == ()


def check_a_low_cardinality_fixed_shape_column_returns_nothing():
    # `raw.patient.sex` is the column that made this necessary. One shape, every row.
    s = _sample(shapes={"A": 100}, distinct=2)
    assert sample_signals(s) == ()


def check_a_shape_with_no_digit_in_it_returns_nothing():
    s = _sample(shapes={"AAAAAAAA": 100})
    assert sample_signals(s) == ()


def check_a_short_run_of_digits_with_no_letter_returns_nothing():
    # Eight digits is a date and a year is four. Without a letter the shape has to be long
    # enough that neither can reach it.
    assert sample_signals(_sample(shapes={"99999999": 100})) == ()
    assert sample_signals(_sample(shapes={"999999999": 100})) != ()


def check_a_mixed_format_column_returns_nothing():
    # The fixture column that stays missed, as a unit check. Four shapes at a quarter each
    # and none of them reaches the concentration threshold.
    s = _sample(shapes={"A-99999": 25, "9999999999": 25, "AA/99/9999": 25,
                        "A999999999999": 25})
    assert sample_signals(s) == ()


def check_the_checksum_rule_names_a_payment_card():
    s = _sample(shapes={"9999/9999/9999/9999": 100},
                luhn_candidates=100, luhn_valid=100)
    keys = {sig.category_key for sig in sample_signals(s)}
    assert "payment_card" in keys, keys


def check_the_checksum_rule_refuses_a_column_that_mostly_fails_it():
    s = _sample(shapes={"9999999999999999": 100},
                luhn_candidates=100, luhn_valid=50)
    keys = {sig.category_key for sig in sample_signals(s)}
    assert "payment_card" not in keys, keys


def check_an_empty_column_produces_nothing():
    s = _sample(shapes={}, read=10, non_null=0, distinct=0)
    assert sample_signals(s) == ()
    assert s.dominant == (None, 0.0)
    assert s.support == 0.0


def check_the_dominant_shape_is_stable_when_two_shapes_tie():
    # Sorted before the max, so a tie resolves the same way on every run. A dict order
    # dependent answer here would make the whole measurement unreproducible.
    s = _sample(shapes={"BBB": 50, "AAA": 50})
    assert s.dominant[0] == "AAA"


def check_attach_puts_the_signals_on_the_matching_profile_and_leaves_the_rest():
    hit = ColumnProfile("probe.t", "weird", "VARCHAR", True, False, 100, 100, 100, 9.0)
    miss = ColumnProfile("probe.t", "plain", "VARCHAR", True, False, 100, 100, 2, 1.0)
    out = sample.attach([hit, miss], [_sample(column="weird")])
    by_column = {p.column: p for p in out}
    assert by_column["weird"].sampled_signals
    assert by_column["plain"].sampled_signals == ()
    # The originals are frozen and untouched, so a caller holding the declaration-only
    # profiles still has them.
    assert hit.sampled_signals == ()


def check_sampled_evidence_contributes_nothing_to_the_shipped_arm_set():
    p = ColumnProfile("probe.t", "weird", "VARCHAR", True, False, 100, 100, 100, 9.0)
    p = p.with_sampled_signals(sample_signals(_sample()))
    assert column_signals(p, ALL_ARMS) == ()
    assert len(column_signals(p, ARMS_WITH_SAMPLING)) == 1

