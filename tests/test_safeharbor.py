from __future__ import annotations

from pii import safeharbor
from pii.safeharbor import CLASSES, SafeHarborClass
from pii.taxonomy import TAXONOMY, Regime


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def check_there_are_eighteen_clauses_lettered_a_to_r():
    assert len(CLASSES) == 18, len(CLASSES)
    assert safeharbor.letters() == tuple("ABCDEFGHIJKLMNOPQR")


def check_every_clause_carries_its_text():
    for c in CLASSES:
        assert c.clause.strip(), c.letter


def check_a_clause_mapping_to_an_unknown_category_is_refused():
    assert _raises(
        lambda: SafeHarborClass("Z", "text", ("not_a_real_category",)), ValueError)


def check_a_clause_repeating_a_category_is_refused():
    assert _raises(lambda: SafeHarborClass("Z", "text", ("email", "email")), ValueError)


def check_a_clause_with_no_mapping_and_no_reason_is_refused():
    assert _raises(lambda: SafeHarborClass("Z", "text", ()), ValueError)


def check_a_clause_that_both_maps_and_claims_a_gap_is_refused():
    assert _raises(
        lambda: SafeHarborClass("Z", "text", ("email",), gap_reason="because"), ValueError)


def check_a_bad_letter_is_refused_in_both_directions():
    assert _raises(lambda: SafeHarborClass("ZZ", "text", ("email",)), ValueError)
    assert _raises(lambda: SafeHarborClass("z", "text", ("email",)), ValueError)


def check_the_two_known_gaps_are_e_and_r_and_both_say_why():
    gaps = safeharbor.gaps()
    assert tuple(g.letter for g in gaps) == ("E", "R"), [g.letter for g in gaps]
    for g in gaps:
        assert len(g.gap_reason) > 40, g.letter


def check_every_hipaa_tagged_category_is_reachable_from_a_clause():
    # This is the reading that found the real defect in this file. Clause B names street
    # address and postal code in one sentence and clause C names birth dates and event
    # dates in another, and the first version of the mapping carried one key each, so
    # street_address and birth_date were tagged HIPAA on the strength of nothing. Running
    # the table clause first hides that completely.
    unreached = safeharbor.hipaa_keys_not_reached()
    assert unreached == (), "tagged HIPAA with no clause behind it: {}".format(unreached)


def check_the_reverse_reading_can_actually_fail():
    # A control for the check above. If nothing can make it fire, it is decoration.
    from pii.taxonomy import Category, Identifiability, Taxonomy

    orphan = Category("orphan", "Orphan", Identifiability.DIRECT,
                      frozenset({Regime.HIPAA}))
    tagged = {c.key for c in Taxonomy((orphan,)).under_regime(Regime.HIPAA)}
    reached = set(safeharbor.mapped_keys())
    assert tagged - reached == {"orphan"}


def check_clause_b_reaches_both_the_address_and_the_postal_code():
    b = next(c for c in CLASSES if c.letter == "B")
    assert set(b.maps_to) == {"street_address", "postal_code"}


def check_clause_c_reaches_both_the_birth_date_and_the_event_date():
    c = next(x for x in CLASSES if x.letter == "C")
    assert set(c.maps_to) == {"birth_date", "event_date"}


def check_mapped_keys_are_deduplicated_and_sorted():
    keys = safeharbor.mapped_keys()
    assert list(keys) == sorted(set(keys))
    for k in keys:
        assert k in TAXONOMY, k


def check_no_clause_maps_onto_a_category_that_identifies_nobody():
    for c in CLASSES:
        for key in c.maps_to:
            cat = TAXONOMY.get(key)
            assert cat.identifiability.value != "none", (c.letter, key)


def check_the_population_floor_refuses_an_empty_population():
    assert _raises(lambda: safeharbor.postal_3_floor(()), ValueError)


def check_the_population_floor_fires_below_the_limit_and_clears_above_it():
    # Both sides of the boundary, and the boundary itself. The clause says more than
    # 20,000 people, so exactly 20,000 does not clear it and 20,001 does.
    at = tuple(["10101"] * safeharbor.POSTAL_3_POPULATION_FLOOR)
    over = at + ("10102",)  # different prefix, so the first group is still exactly at

    r_at = safeharbor.postal_3_floor(at)
    assert r_at.groups == 1
    assert r_at.groups_meeting_floor == 0
    assert r_at.applies is False
    assert "does not apply" in r_at.why_not()

    just_over = tuple(["10101"] * (safeharbor.POSTAL_3_POPULATION_FLOOR + 1))
    r_over = safeharbor.postal_3_floor(just_over)
    assert r_over.groups_meeting_floor == 1
    assert r_over.applies is True
    assert r_over.why_not() == ""

    # And a mixed population where one group clears and one does not must not apply.
    mixed = just_over + ("10999",)
    r_mixed = safeharbor.postal_3_floor(mixed)
    assert r_mixed.groups == 2
    assert r_mixed.groups_meeting_floor == 1
    assert r_mixed.applies is False
    assert len(over) == safeharbor.POSTAL_3_POPULATION_FLOOR + 1


def check_the_floor_groups_by_three_digits_and_not_by_the_whole_value():
    r = safeharbor.postal_3_floor(("10101", "10199", "10234"))
    assert r.groups == 2, r.groups
    assert r.smallest_group == 1
    assert r.largest_group == 2


def check_the_floor_is_a_parameter_and_moving_it_moves_the_verdict():
    values = tuple(["10101"] * 50)
    assert safeharbor.postal_3_floor(values, floor=49).applies is True
    assert safeharbor.postal_3_floor(values, floor=50).applies is False


def check_the_population_floor_constant_is_the_number_the_clause_names():
    # Asserted as a literal rather than as its own name. Every check above spells the
    # limit `safeharbor.POSTAL_3_POPULATION_FLOOR`, so a mutant moving it moves both sides
    # of every comparison and survives the lot. Twenty thousand is what clause B says. If
    # that ever changes, this check is supposed to fail.
    assert safeharbor.POSTAL_3_POPULATION_FLOOR == 20000


def check_a_report_over_zero_groups_does_not_report_that_the_allowance_applies():
    # Zero groups meeting a floor out of zero groups satisfies the equality, so without
    # the first term this reports that the clause applies to a population that does not
    # exist. The empty input is refused upstream, which is exactly why the guard needs
    # reaching directly rather than through the builder.
    vacuous = safeharbor.FloorReport(groups=0, smallest_group=0, largest_group=0,
                                     groups_meeting_floor=0, floor=20000)
    assert vacuous.applies is False


def check_the_shortfall_in_the_message_is_the_count_of_areas_that_miss_the_floor():
    # Assert the value the report prints and not only that it printed something. Four
    # areas, one of them over the floor, so three fall short.
    values = tuple(["10101"] * 5 + ["10201"] * 5 + ["10301"] * 5 + ["10401"] * 30)
    r = safeharbor.postal_3_floor(values, floor=20)
    assert r.groups == 4
    assert r.groups_meeting_floor == 1
    assert r.applies is False
    assert r.why_not().startswith("3 of 4 three digit areas")
    assert "smallest 5" in r.why_not()


def check_the_dataclasses_here_are_frozen():
    frozen_cases = [
        (SafeHarborClass("Z", "text", ("email",)), "letter", "Y"),
        (safeharbor.postal_3_floor(("10101",)), "groups", 99),
    ]
    for obj, attr, value in frozen_cases:
        try:
            setattr(obj, attr, value)
        except Exception:
            continue
        raise AssertionError("{} is not frozen".format(type(obj).__name__))
