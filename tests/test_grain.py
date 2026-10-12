"""The grain arithmetic, with no database anywhere in it.

`tests/test_grain_warehouse.py` is the other half and it runs the whole path against real
tables. This file is the part that can be wrong in a way a warehouse would hide, which is
mostly the boundaries. A share of exactly the floor, a group size of one, an empty table.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import grain  # noqa: E402
from pii.classify import (ACCEPT_AT, REVIEW_AT, TEMPORAL_TYPE_STRENGTH, Arm,  # noqa: E402
                          Band, ColumnProfile, Signal, attenuate_temporal,
                          classify_column, classify_table, classify_warehouse)


def _witness(**kw) -> grain.GrainWitness:
    base = dict(table="analytics.mart", weight_column="encounters",
                rows=10, population=14, k_anonymity=1, singleton_rows=8)
    base.update(kw)
    return grain.GrainWitness(**base)


def _profile(column, sql_type="VARCHAR", table="analytics.daily", hits=None, rows=100):
    return ColumnProfile(
        table=table, column=column, sql_type=sql_type, nullable=True, is_key=False,
        rows=rows, non_null=rows, distinct=rows, mean_length=8.0,
        pattern_hits=dict(hits or {}),
    )


def _mart():
    return (_profile("day", sql_type="DATE"), _profile("department"))


def _source():
    return (_profile("email", hits={"email": 0.99}, table="raw.patient"),
            _profile("admitted_at", sql_type="TIMESTAMP", table="raw.patient"))


UPSTREAM = {"analytics.daily": ("raw.patient",), "raw.patient": ()}


def check_the_share_is_singleton_rows_over_rows():
    assert _witness(rows=1444, population=1504, singleton_rows=1386).singleton_share == \
        1386 / 1444


def check_an_empty_table_has_a_share_of_zero_rather_than_an_error():
    w = _witness(rows=0, population=0, singleton_rows=0, k_anonymity=1)
    assert w.singleton_share == 0.0
    assert w.people_per_row == 0.0


def check_people_per_row_is_the_factor_the_group_by_deduplicated_by():
    assert _witness(rows=10, population=30, singleton_rows=0).people_per_row == 3.0


def check_no_weight_column_means_rows_are_people():
    assert _witness(weight_column=None, population=10).was_grouped is False
    assert _witness().was_grouped is True


def check_more_singletons_than_rows_is_refused():
    try:
        _witness(rows=5, population=9, singleton_rows=6)
    except ValueError as exc:
        assert "more singleton rows than rows" in str(exc)
        return
    raise AssertionError("a witness with more singletons than rows was accepted")


def check_fewer_people_than_rows_is_refused():
    # This is the shape of reading the wrong column as the weight. An average over rows
    # sums to less than the row count on most data and the error says so.
    try:
        _witness(rows=10, population=4)
    except ValueError as exc:
        assert "fewer people than it has rows" in str(exc)
        return
    raise AssertionError("a weight column summing under the row count was accepted")


def check_a_group_of_zero_people_is_refused():
    try:
        _witness(k_anonymity=0)
    except ValueError as exc:
        assert "which is not a group" in str(exc)
        return
    raise AssertionError("a group of zero people was accepted")


def check_an_empty_table_is_allowed_to_report_a_k_of_one():
    # The coalesce in `grain_sql` returns 1 for min over nothing, so the validation has to
    # let the empty case through or every empty table raises.
    assert _witness(rows=0, population=0, singleton_rows=0, k_anonymity=1).rows == 0


def check_the_sql_counts_rows_once_when_there_is_no_weight():
    sql = grain.grain_sql("raw.patient", None)
    assert "sum(1)" in sql
    assert '"raw"."patient"' in sql


def check_the_sql_names_the_weight_column_and_quotes_both_names():
    sql = grain.grain_sql("analytics.encounter_daily", "encounters")
    assert 'sum("encounters")' in sql
    assert 'min("encounters")' in sql
    assert '"analytics"."encounter_daily"' in sql


def check_the_sql_selects_no_user_column():
    # The no values property, asserted rather than described. Only the weight column and
    # aggregates over it appear, and the weight column is an integer the database computed.
    sql = grain.grain_sql("analytics.encounter_daily", "encounters")
    for forbidden in ("day", "department", "postal_code", "*"):
        assert forbidden not in sql.replace("count(*)", ""), forbidden


def check_the_floor_comes_out_of_the_two_constants_it_depends_on():
    assert grain.rescue_floor() == REVIEW_AT / TEMPORAL_TYPE_STRENGTH


def check_the_floor_is_a_real_boundary_and_not_a_printed_number():
    floor = grain.rescue_floor()
    profiles = _mart() + _source()
    at_floor = classify_warehouse(profiles, upstream_tables=UPSTREAM,
                                  grain_confidence={"analytics.daily": floor})
    below = classify_warehouse(profiles, upstream_tables=UPSTREAM,
                               grain_confidence={"analytics.daily": floor - 0.01})
    day_at = [r for r in at_floor if r.address == "analytics.daily.day"][0]
    day_below = [r for r in below if r.address == "analytics.daily.day"][0]
    assert day_at.band is Band.REVIEW
    assert day_below.band is Band.IGNORE


def check_a_strength_of_zero_cannot_carry_a_floor():
    try:
        grain.rescue_floor(strength=0.0)
    except ValueError as exc:
        assert "cannot carry a floor" in str(exc)
        return
    raise AssertionError("a floor was derived from a zero strength")


def check_a_strength_over_one_cannot_carry_a_floor():
    try:
        grain.rescue_floor(strength=1.5)
    except ValueError as exc:
        assert "cannot carry a floor" in str(exc)
        return
    raise AssertionError("a floor was derived from a strength over one")


def check_band_of_reads_the_classifier_thresholds_rather_than_copies_of_them():
    assert grain.band_of(ACCEPT_AT) == "accept"
    assert grain.band_of(ACCEPT_AT - 0.0001) == "review"
    assert grain.band_of(REVIEW_AT) == "review"
    assert grain.band_of(REVIEW_AT - 0.0001) == "ignore"


def check_attenuation_moves_the_temporal_signal_and_nothing_else():
    signals = (
        Signal(Arm.STRUCTURE, "event_date", 0.45, "catalog type DATE is temporal"),
        Signal(Arm.NAME, "postal_code", 0.90, "names a postal code"),
    )
    got = attenuate_temporal(signals, 0.5)
    assert got[0].strength == 0.225
    assert got[1].strength == 0.90
    assert got[1].detail == signals[1].detail


def check_attenuation_says_in_the_detail_what_it_discounted_by():
    signals = (Signal(Arm.STRUCTURE, "event_date", 0.45, "catalog type DATE is temporal"),)
    detail = attenuate_temporal(signals, 0.9598)[0].detail
    assert "0.9598" in detail
    assert "catalog type DATE is temporal" in detail


def check_attenuation_refuses_a_factor_that_is_not_a_share():
    for bad in (-0.1, 1.1):
        try:
            attenuate_temporal((), bad)
        except ValueError as exc:
            assert "is not a share" in str(exc)
            continue
        raise AssertionError("attenuation accepted a factor of {}".format(bad))


def check_leaving_the_grain_out_is_the_behaviour_from_before_the_discount_existed():
    # The regression pin. Every caller in the repo that does not pass a grain gets the
    # binary rescue, and this is the check that fails if a default is ever added.
    profiles = _mart() + _source()
    plain = classify_warehouse(profiles, upstream_tables=UPSTREAM)
    day = [r for r in plain if r.address == "analytics.daily.day"][0]
    assert day.confidence == 0.45
    assert day.band is Band.REVIEW
    assert day.confidence == classify_column(_mart()[0]).confidence


def check_a_share_of_one_leaves_the_rescued_score_exactly_where_it_was():
    profiles = _mart() + _source()
    plain = [r for r in classify_warehouse(profiles, upstream_tables=UPSTREAM)
             if r.address == "analytics.daily.day"][0]
    whole = [r for r in classify_warehouse(profiles, upstream_tables=UPSTREAM,
                                           grain_confidence={"analytics.daily": 1.0})
             if r.address == "analytics.daily.day"][0]
    assert whole.confidence == plain.confidence


def check_a_share_of_zero_deletes_the_evidence_rather_than_shrinking_it():
    profiles = _mart() + _source()
    got = [r for r in classify_warehouse(profiles, upstream_tables=UPSTREAM,
                                         grain_confidence={"analytics.daily": 0.0})
           if r.address == "analytics.daily.day"][0]
    assert got.confidence == 0.0
    assert got.band is Band.IGNORE


def check_a_grain_for_a_table_round_two_never_rescued_changes_nothing():
    # `raw.patient` names people in its own columns, so nothing about its verdict was
    # borrowed and there is nothing to discount. A discount reaching it would be the
    # classifier punishing a table for being pre-aggregated.
    profiles = _mart() + _source()
    plain = {r.address: r.confidence
             for r in classify_warehouse(profiles, upstream_tables=UPSTREAM)}
    discounted = {r.address: r.confidence
                  for r in classify_warehouse(profiles, upstream_tables=UPSTREAM,
                                              grain_confidence={"raw.patient": 0.0})}
    assert plain["raw.patient.admitted_at"] == discounted["raw.patient.admitted_at"]


def check_a_missing_table_in_the_grain_map_falls_back_to_the_binary():
    profiles = _mart() + _source()
    got = [r for r in classify_warehouse(profiles, upstream_tables=UPSTREAM,
                                         grain_confidence={"ref.other": 0.1})
           if r.address == "analytics.daily.day"][0]
    assert got.confidence == 0.45


def check_classify_table_takes_the_grain_directly_for_a_caller_holding_one_table():
    got = classify_table(_mart(), person_linked=True, grain_confidence=0.5)
    day = [r for r in got if r.column == "day"][0]
    assert day.confidence == round(0.45 * 0.5, 4)


def _row(table, k, share, confidence):
    """A row whose share carries more than six decimals on purpose.

    The manifest rounds to six places. A fixture built on 0.96 rounds identically at six
    and at seven, so three mutants that moved the rounding survived the pure suite. 1444
    and 1386 are the shipped mart's own figures and they do not.
    """
    return grain.RescueRow(table=table, rows=1444, population=1504, k_anonymity=k,
                           singleton_rows=round(share * 1444), singleton_share=share,
                           confidence=confidence, binary_confidence=0.45)


def check_k_does_not_separate_two_tables_that_share_it_and_land_apart():
    report = grain.RescueReport(rows=(
        _row("a", 1, 1386 / 1444, 0.4319),
        _row("b", 1, 469 / 894, 0.2361),
    ))
    assert report.k_separates() is False


def check_k_does_separate_when_every_k_carries_one_band():
    report = grain.RescueReport(rows=(
        _row("a", 1, 1386 / 1444, 0.4319),
        _row("b", 9, 101 / 1444, 0.0450),
    ))
    assert report.k_separates() is True


def check_the_share_separates_when_the_bands_rise_with_it():
    report = grain.RescueReport(rows=(
        _row("a", 1, 1386 / 1444, 0.4319),
        _row("b", 1, 469 / 894, 0.2361),
        _row("c", 2, 0.0, 0.0000),
    ))
    assert report.share_separates() is True


def check_the_share_does_not_separate_when_a_lower_share_scores_higher():
    report = grain.RescueReport(rows=(
        _row("a", 1, 1386 / 1444, 0.2000),
        _row("b", 1, 469 / 894, 0.4000),
    ))
    assert report.share_separates() is False


def check_a_report_over_no_tables_is_refused():
    try:
        grain.RescueReport(rows=())
    except ValueError as exc:
        assert "grades nothing" in str(exc)
        return
    raise AssertionError("an empty rescue report was accepted")


def check_the_manifest_carries_the_floor_and_both_separation_answers():
    manifest = grain.RescueReport(rows=(_row("a", 1, 1386 / 1444, 0.4319),)).manifest()
    assert manifest["floor"] == round(REVIEW_AT / TEMPORAL_TYPE_STRENGTH, 6)
    assert manifest["review_at"] == REVIEW_AT
    assert manifest["temporal_strength"] == TEMPORAL_TYPE_STRENGTH
    assert "k_separates_the_bands" in manifest
    assert "share_separates_the_bands" in manifest
    assert manifest["tables"][0]["band"] == "review"


def check_every_figure_in_the_manifest_is_rounded_where_the_committed_file_rounds():
    # Three mutants moved the rounding on these three fields and survived, because the
    # only check reading the manifest was reading its band and its floor. A figure
    # rounded one place further is a manifest diff on every regeneration.
    # Every figure here carries more than six decimals, which is what makes the rounding
    # observable. A fixture rounding identically at six and seven places asserts nothing.
    share = 1386 / 1444
    row = grain.RescueRow(table="a", rows=1444, population=1504, k_anonymity=1,
                          singleton_rows=1386, singleton_share=share,
                          confidence=share * 0.45, binary_confidence=1 / 3)
    table = grain.RescueReport(rows=(row,)).manifest()["tables"][0]
    assert table["singleton_share"] == round(share, 6)
    assert table["confidence"] == round(share * 0.45, 6)
    assert table["binary_confidence"] == round(1 / 3, 6)
    for field in ("singleton_share", "confidence", "binary_confidence"):
        assert len(str(table[field]).split(".")[1]) <= 6, field


def check_a_variant_with_no_temporal_key_is_refused():
    try:
        grain.GrainVariant(table="analytics.x", key_expressions=("e.department",),
                           key_columns=("department",))
    except ValueError as exc:
        assert "no temporal key" in str(exc)
        return
    raise AssertionError("a variant with nothing temporal in it was accepted")


def check_a_variant_with_mismatched_keys_is_refused():
    try:
        grain.GrainVariant(table="analytics.x", key_expressions=("a", "b"),
                           key_columns=("day",))
    except ValueError as exc:
        assert "key expressions" in str(exc)
        return
    raise AssertionError("a variant naming two expressions for one column was accepted")


def check_every_variant_writes_its_group_size_as_a_literal_star_count():
    # `mask.weight_candidates` tells a group size from an average over rows by looking for
    # a `count(*)`. A variant that wrote `count(e.encounter_id)` would be measured by row
    # count and reported safer than it is.
    for variant in grain.COARSER:
        assert "count(*)" in variant.insert_sql()
        assert "AS encounters" in variant.insert_sql()


def check_every_variant_declares_the_columns_it_selects():
    for variant in grain.COARSER:
        for column in variant.columns:
            assert "AS {}".format(column) in variant.insert_sql(), column


def check_every_object_this_module_hands_around_is_immutable():
    # These travel from a probe into a report and into a manifest. A mutable one lets a
    # caller rewrite a measurement after it was taken, and a mutation pass on 2026-10-12
    # flipped `frozen` on all four of them and nothing failed.
    frozen = (
        _witness(),
        grain.GrainVariant(table="analytics.x", key_expressions=("CAST(e.a AS DATE)",),
                           key_columns=("day",)),
        _row("a", 1, 1386 / 1444, 0.4319),
        grain.RescueReport(rows=(_row("a", 1, 1386 / 1444, 0.4319),)),
    )
    for obj in frozen:
        try:
            setattr(obj, "table", "rewritten")
        except Exception:
            continue
        raise AssertionError("{} let a field be rewritten".format(type(obj).__name__))


def check_a_variant_groups_by_every_key_it_selects_and_by_nothing_else():
    # The GROUP BY is built by counting the keys, and an off by one there produces SQL
    # that either groups by the wrong column or does not parse. Two mutants survived here
    # until this check existed, and neither is visible without running the statement.
    for variant in grain.COARSER:
        want = "GROUP BY {}".format(
            ", ".join(str(i + 1) for i in range(len(variant.key_columns))))
        sql = variant.insert_sql()
        assert sql.rstrip().endswith(want), (variant.table, sql[-40:])


def check_moved_is_true_only_when_the_two_bands_differ():
    same = _row("a", 1, 1386 / 1444, 0.4319)
    apart = _row("b", 1, 469 / 894, 0.2361)
    assert same.band == same.binary_band
    assert same.moved is False
    assert apart.band != apart.binary_band
    assert apart.moved is True
