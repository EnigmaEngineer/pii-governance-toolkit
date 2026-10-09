"""Checks for the arm ablation harness.

Nothing here touches a database. The arms read a `ColumnProfile`, which is a bag of
integers, so the whole ablation is checkable from the standard library. That is the point
of the module existing. The measurement it produces on the real warehouse is re-derived by
`tests/test_ablation_warehouse.py`, which does need duckdb, and these checks are what say
the harness itself is sound before anybody trusts that number.
"""

from __future__ import annotations

from pii import ablation
from pii.ablation import ARM_SETS, Report, ablate
from pii.classify import ALL_ARMS, Arm, Band, ColumnProfile, column_signals


def _profile(column, sql_type="VARCHAR", rows=100, non_null=100, distinct=90,
             mean_length=8.0, hits=None, is_key=False, nullable=True,
             table="raw.patient"):
    return ColumnProfile(
        table=table, column=column, sql_type=sql_type, nullable=nullable,
        is_key=is_key, rows=rows, non_null=non_null, distinct=distinct,
        mean_length=mean_length, pattern_hits=hits or {},
    )


def _warehouse():
    """Two tables. One names people outright and one is a mart derived from it.

    The mart column is named `day` and typed DATE, which is the shape the structure arm is
    the only arm that can reach. `day` is not a temporal suffix the name arm knows, so the
    catalog type is the column's entire evidence.
    """
    return (
        _profile("first_name", table="raw.patient"),
        _profile("email", hits={"email": 99}, table="raw.patient"),
        _profile("day", sql_type="DATE", table="analytics.daily"),
        _profile("encounters", sql_type="BIGINT", table="analytics.daily"),
    )


def _upstream():
    return {"analytics.daily": ("raw.patient",)}


def check_naming_no_arm_produces_no_signal_at_all():
    assert column_signals(_profile("ssn"), arms=()) == ()


def check_each_arm_contributes_only_its_own_signals():
    profile = _profile("email", sql_type="TIMESTAMP", hits={"email": 99})
    for arm in ALL_ARMS:
        got = column_signals(profile, arms=(arm,))
        assert got, "{} produced nothing on a profile built to trigger it".format(arm)
        assert {s.arm for s in got} == {arm}


def check_the_arm_set_is_the_union_of_its_members():
    profile = _profile("email", sql_type="TIMESTAMP", hits={"email": 99})
    parts = sum((len(column_signals(profile, arms=(a,))) for a in ALL_ARMS))
    assert len(column_signals(profile, arms=ALL_ARMS)) == parts


def check_the_default_arm_set_is_every_arm():
    profile = _profile("email", sql_type="TIMESTAMP", hits={"email": 99})
    assert column_signals(profile) == column_signals(profile, arms=ALL_ARMS)


def check_the_baseline_row_moves_nothing_by_construction():
    report = ablate(_warehouse(), upstream_tables=_upstream())
    assert report.by_label("all three").bands_moved == 0


def check_every_arm_set_in_the_table_gets_a_reading():
    report = ablate(_warehouse(), upstream_tables=_upstream())
    assert len(report.readings) == len(ARM_SETS)
    assert [r.label for r in report.readings] == [label for label, _ in ARM_SETS]


def check_removing_the_structure_arm_drops_the_one_column_only_it_reaches():
    """The whole reason the harness exists, on a four column fixture.

    `analytics.daily.day` is rescued by round two and scores on its catalog type alone. Take
    the structure arm away and there is nothing left to score, so the column stops reaching
    a reviewer. A count of bands moved reports this as a 1 and says nothing about which
    column or which direction, which is how it got read as a rounding detail.
    """
    report = ablate(_warehouse(), upstream_tables=_upstream())
    reading = report.by_label("without structure")
    assert reading.moved_addresses == ("analytics.daily.day",)
    move = reading.moves[0]
    assert move.from_band is Band.REVIEW
    assert move.to_band is Band.IGNORE
    assert move.to_category == "not_personal"


def check_a_move_keeps_both_readings_and_not_just_the_count():
    report = ablate(_warehouse(), upstream_tables=_upstream())
    move = report.by_label("without structure").moves[0]
    assert move.from_confidence > move.to_confidence
    assert move.from_category != move.to_category
    assert "review" in move.describe() and "ignore" in move.describe()


def check_decides_nothing_reads_the_removal_row_and_not_the_only_row():
    """The distinction the old print loop could not express.

    On this fixture the structure arm alone finds nothing, because no mart column survives
    without a table that names somebody. It is still the only arm holding `day` up. An arm
    is free to delete when its removal costs nothing, which is a different row of the table
    from what the arm can do by itself.
    """
    report = ablate(_warehouse(), upstream_tables=_upstream())
    assert report.by_label("structure only").found == 0
    assert report.by_label("without structure").bands_moved == 1
    assert Arm.STRUCTURE not in report.decides_nothing()


def check_an_arm_whose_removal_costs_nothing_is_named():
    """A fixture where one arm really does decide nothing, so the positive case is covered.

    Every column here is reached by its name. The structure arm has no temporal column to
    speak about and the value arm has no pattern hit, so removing either moves no band and
    both are reported as free.
    """
    profiles = (
        _profile("first_name", table="raw.patient"),
        _profile("mrn", table="raw.patient"),
    )
    report = ablate(profiles)
    assert set(report.decides_nothing()) == {Arm.VALUE, Arm.STRUCTURE}
    assert Arm.NAME not in report.decides_nothing()


def check_the_arm_set_reaches_the_table_verdict_and_not_only_the_signals():
    """What a late filter of the signal list would have got wrong.

    The second pass asks whether the table names anybody and then deletes temporal evidence
    where it does not. That verdict is read off the classifications, so an arm removed after
    the fact would still have voted on it.

    The fixture is built so the only direct identifier is one the name arm alone can see. A
    bare `first_name` with no matching values is reached by no predicate and by no catalog
    rule. Take the name arm away and the table stops naming anybody, which is a different
    verdict rather than a shorter signal list, and `admitted_at` loses its temporal evidence
    as a result.
    """
    from pii.classify import classify_warehouse, table_is_person_linked
    profiles = (
        _profile("first_name", table="raw.patient"),
        _profile("admitted_at", sql_type="TIMESTAMP", table="raw.patient"),
    )
    full = [r for r in classify_warehouse(profiles)]
    without = [r for r in classify_warehouse(profiles, arms=(Arm.VALUE, Arm.STRUCTURE))]
    assert table_is_person_linked(full)
    assert not table_is_person_linked(without)

    admitted_full = [r for r in full if r.column == "admitted_at"][0]
    admitted_without = [r for r in without if r.column == "admitted_at"][0]
    assert admitted_full.band is Band.REVIEW
    assert admitted_without.category_key == "not_personal"


def check_the_moves_are_sorted_so_the_manifest_diffs_cleanly():
    report = ablate(_warehouse(), upstream_tables=_upstream())
    for reading in report.readings:
        addresses = list(reading.moved_addresses)
        assert addresses == sorted(addresses)


def check_the_manifest_holds_a_row_per_arm_set_and_the_verdict():
    report = ablate(_warehouse(), upstream_tables=_upstream())
    manifest = report.manifest()
    assert [row["label"] for row in manifest["arm_sets"]] == [l for l, _ in ARM_SETS]
    assert manifest["decides_nothing"] == [
        a.value for a in report.decides_nothing()]


def check_the_manifest_records_bands_and_not_confidences():
    """Deliberate. A manifest that fails on a rounding change stops being read."""
    report = ablate(_warehouse(), upstream_tables=_upstream())
    for row in report.manifest()["arm_sets"]:
        for move in row["moves"]:
            assert set(move) == {"address", "from_band", "to_band"}


def check_the_manifest_bands_moved_agrees_with_the_moves_it_lists():
    report = ablate(_warehouse(), upstream_tables=_upstream())
    for row in report.manifest()["arm_sets"]:
        assert row["bands_moved"] == len(row["moves"])


def check_the_table_prints_one_row_per_arm_set_plus_a_header():
    report = ablate(_warehouse(), upstream_tables=_upstream())
    lines = report.table().splitlines()
    assert len(lines) == len(ARM_SETS) + 1
    assert lines[0].split()[0] == "arms"
    for line, (label, _) in zip(lines[1:], ARM_SETS):
        assert line.startswith(label)


def check_an_ablation_with_no_answer_key_still_reports_the_moves():
    """The scope and the planted key are both optional. The moves do not need either.

    A move is the classifier compared against itself. That is why these checks can run with
    no planted warehouse to read. It is also the honest boundary of the harness, which can
    say a decision changed and cannot say the decision was right.
    """
    report = ablate(_warehouse(), upstream_tables=_upstream())
    reading = report.by_label("without structure")
    assert reading.in_scope == 0
    assert reading.found == 0
    assert reading.bands_moved == 1


def check_an_answer_key_fills_the_grading_columns():
    scope = frozenset({"person_name", "email"})

    def planted(result):
        return {"first_name": "person_name", "email": "email"}.get(
            result.column, "not_personal")

    report = ablate(_warehouse(), upstream_tables=_upstream(),
                    scope=scope, planted_key_for=planted)
    baseline = report.by_label("all three")
    assert baseline.in_scope == 2
    assert baseline.found == 2


def check_asking_for_a_label_that_is_not_there_raises():
    report = ablate(_warehouse(), upstream_tables=_upstream())
    try:
        report.by_label("without everything")
    except KeyError:
        return
    raise AssertionError("by_label accepted a label no arm set carries")


def check_an_empty_warehouse_produces_a_reading_per_arm_set_and_no_moves():
    report = ablate(())
    assert len(report.readings) == len(ARM_SETS)
    assert all(r.bands_moved == 0 for r in report.readings)
    # Every removal moves nothing, so every arm reads as free. True and useless, and the
    # harness should say it rather than special case it.
    assert set(report.decides_nothing()) == set(ALL_ARMS)


def check_every_removal_label_names_an_arm_set_that_exists():
    labels = {label for label, _ in ARM_SETS}
    for arm in ALL_ARMS:
        assert ablation.REMOVAL_LABEL[arm] in labels


def check_each_removal_set_is_every_arm_but_one():
    sets = dict(ARM_SETS)
    for arm in ALL_ARMS:
        kept = set(sets[ablation.REMOVAL_LABEL[arm]])
        assert kept == set(ALL_ARMS) - {arm}
