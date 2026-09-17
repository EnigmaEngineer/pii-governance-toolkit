from __future__ import annotations

from pii.taxonomy import (
    CATEGORIES,
    TAXONOMY,
    Category,
    Granularity,
    Identifiability,
    Regime,
    Taxonomy,
    at_least_as_fine,
    granularity_family,
)


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def check_every_category_key_is_unique_and_lowercase():
    keys = [c.key for c in CATEGORIES]
    assert len(keys) == len(set(keys)), "duplicate category key"
    for k in keys:
        assert k == k.lower(), k
        assert " " not in k, k


def check_a_duplicate_key_is_refused():
    a = Category("dup", "One", Identifiability.DIRECT)
    b = Category("dup", "Two", Identifiability.DIRECT)
    assert _raises(lambda: Taxonomy((a, b)), ValueError)


def check_an_empty_taxonomy_is_refused():
    assert _raises(lambda: Taxonomy(()), ValueError)


def check_a_category_claiming_a_regime_while_identifying_nobody_is_refused():
    assert _raises(
        lambda: Category("x", "X", Identifiability.NONE, frozenset({Regime.HIPAA})),
        ValueError,
    )


def check_a_none_category_carrying_a_granularity_threshold_is_refused():
    assert _raises(
        lambda: Category("x", "X", Identifiability.NONE,
                        identifies_at=Granularity.DAY),
        ValueError,
    )


def check_an_empty_key_and_an_empty_label_are_both_refused():
    assert _raises(lambda: Category("", "X", Identifiability.DIRECT), ValueError)
    assert _raises(lambda: Category("x", "", Identifiability.DIRECT), ValueError)


def check_an_upper_case_key_and_a_spaced_key_are_refused():
    assert _raises(lambda: Category("Xy", "X", Identifiability.DIRECT), ValueError)
    assert _raises(lambda: Category("x y", "X", Identifiability.DIRECT), ValueError)


def check_granularity_ordering_is_tested_on_both_sides_of_each_limit():
    # A threshold checked well away from its boundary does not pin the boundary. Every
    # adjacent pair in each family is asserted in both directions.
    temporal = (Granularity.YEAR, Granularity.MONTH, Granularity.DAY, Granularity.SECOND)
    for i in range(len(temporal) - 1):
        coarse, fine = temporal[i], temporal[i + 1]
        assert at_least_as_fine(fine, coarse) is True, (fine, coarse)
        assert at_least_as_fine(coarse, fine) is False, (coarse, fine)
        assert at_least_as_fine(coarse, coarse) is True, coarse

    postal = (Granularity.POSTAL_3, Granularity.POSTAL_5, Granularity.POSTAL_FULL)
    for i in range(len(postal) - 1):
        coarse, fine = postal[i], postal[i + 1]
        assert at_least_as_fine(fine, coarse) is True, (fine, coarse)
        assert at_least_as_fine(coarse, fine) is False, (coarse, fine)


def check_comparing_across_granularity_families_raises_rather_than_answering():
    assert _raises(
        lambda: at_least_as_fine(Granularity.DAY, Granularity.POSTAL_5), ValueError)
    assert _raises(
        lambda: at_least_as_fine(Granularity.POSTAL_3, Granularity.YEAR), ValueError)


def check_not_applicable_has_no_family():
    assert granularity_family(Granularity.NOT_APPLICABLE) is None
    assert granularity_family(Granularity.DAY) == "temporal"
    assert granularity_family(Granularity.POSTAL_5) == "postal"


def _message(fn) -> str:
    try:
        fn()
    except Exception as exc:
        return str(exc)
    raise AssertionError("expected a raise and got none")


def check_one_unorderable_side_raises_on_its_own_and_not_only_when_both_are():
    # The guard reads "either side has no family". Every other check hands it two real
    # granularities, so the half where only one side is unorderable was untested.
    #
    # Asserting the type alone is not enough here and that is the interesting part. Turn
    # the `or` into an `and` and the call still raises ValueError, because the cross
    # family guard one line below catches it and says something else entirely. A guard
    # rescued by the layer under it needs the message asserted, not the exception.
    assert "has no ordering" in _message(
        lambda: at_least_as_fine(Granularity.NOT_APPLICABLE, Granularity.DAY))
    assert "has no ordering" in _message(
        lambda: at_least_as_fine(Granularity.DAY, Granularity.NOT_APPLICABLE))
    assert "has no ordering" in _message(
        lambda: at_least_as_fine(Granularity.NOT_APPLICABLE, Granularity.NOT_APPLICABLE))
    # And the neighbouring guard still says its own thing, so the two are not one message.
    assert "different families" in _message(
        lambda: at_least_as_fine(Granularity.DAY, Granularity.POSTAL_5))


def check_a_birth_date_identifies_at_day_and_not_at_year():
    b = TAXONOMY.get("birth_date")
    assert b.identifies(Granularity.YEAR) is False
    assert b.identifies(Granularity.MONTH) is False
    assert b.identifies(Granularity.DAY) is True
    assert b.identifies(Granularity.SECOND) is True


def check_a_postal_code_identifies_at_five_digits_and_not_at_three():
    p = TAXONOMY.get("postal_code")
    assert p.identifies(Granularity.POSTAL_3) is False
    assert p.identifies(Granularity.POSTAL_5) is True
    assert p.identifies(Granularity.POSTAL_FULL) is True


def check_a_category_with_no_threshold_ignores_the_granularity_it_is_handed():
    e = TAXONOMY.get("email")
    assert e.identifies_at is None
    assert e.identifies(Granularity.NOT_APPLICABLE) is True
    assert e.identifies(Granularity.YEAR) is True


def check_not_personal_never_identifies_anything():
    n = TAXONOMY.get("not_personal")
    for g in Granularity:
        assert n.identifies(g) is False, g


def check_the_two_axes_are_independent_in_the_shipped_taxonomy():
    # If every HIPAA category were also direct, the identifiability axis would be a
    # restatement of the regime axis and the taxonomy would have one axis wearing two
    # names. Both of these have to be non empty for the split to be earning its place.
    hipaa = set(c.key for c in TAXONOMY.under_regime(Regime.HIPAA))
    direct = set(c.key for c in TAXONOMY.with_identifiability(Identifiability.DIRECT))
    assert hipaa - direct, "every HIPAA category is direct, so the second axis is free"
    assert direct - hipaa, "every direct category is HIPAA, so the regime axis is free"


def check_gdpr_special_and_hipaa_are_not_the_same_set():
    gdpr = set(c.key for c in TAXONOMY.under_regime(Regime.GDPR_SPECIAL))
    hipaa = set(c.key for c in TAXONOMY.under_regime(Regime.HIPAA))
    assert gdpr, "no category is a GDPR special category"
    assert gdpr != hipaa
    assert gdpr - hipaa, "GDPR special is a subset of HIPAA, which would make it redundant"


def check_there_is_at_least_one_category_of_each_identifiability():
    for level in Identifiability:
        assert TAXONOMY.with_identifiability(level), level


def check_the_identifiability_filter_returns_that_level_and_partitions_the_taxonomy():
    # Asserting the result is non empty passes just as happily against a filter that
    # returns everything except the level asked for. A mutant flipping `is` to `is not`
    # survived exactly that. Both halves are pinned now.
    total = 0
    for level in Identifiability:
        got = TAXONOMY.with_identifiability(level)
        for c in got:
            assert c.identifiability is level, (c.key, level)
        total += len(got)
    assert total == len(TAXONOMY)


def check_the_regime_filter_returns_only_categories_carrying_that_regime():
    for regime in Regime:
        for c in TAXONOMY.under_regime(regime):
            assert regime in c.regimes, (c.key, regime)


def check_the_fingerprint_is_stable_across_two_calls():
    assert TAXONOMY.fingerprint() == TAXONOMY.fingerprint()


def check_the_fingerprint_moves_when_any_field_moves():
    base = Category("a", "A", Identifiability.QUASI, frozenset({Regime.HIPAA}),
                    Granularity.DAY, "note")
    original = Taxonomy((base,)).fingerprint()

    variants = [
        Category("a", "B", Identifiability.QUASI, frozenset({Regime.HIPAA}),
                 Granularity.DAY, "note"),
        Category("a", "A", Identifiability.DIRECT, frozenset({Regime.HIPAA}), None, "note"),
        Category("a", "A", Identifiability.QUASI, frozenset({Regime.PCI}),
                 Granularity.DAY, "note"),
        Category("a", "A", Identifiability.QUASI, frozenset({Regime.HIPAA}),
                 Granularity.SECOND, "note"),
        Category("a", "A", Identifiability.QUASI, frozenset({Regime.HIPAA}),
                 Granularity.DAY, "different"),
    ]
    for v in variants:
        assert Taxonomy((v,)).fingerprint() != original, v


def check_the_fingerprint_does_not_move_when_the_category_order_moves():
    a = Category("a", "A", Identifiability.DIRECT)
    b = Category("b", "B", Identifiability.QUASI)
    assert Taxonomy((a, b)).fingerprint() == Taxonomy((b, a)).fingerprint()


def check_the_fingerprint_is_twelve_hex_characters():
    fp = TAXONOMY.fingerprint()
    assert len(fp) == 12, fp
    int(fp, 16)


def check_get_raises_on_a_missing_key_rather_than_returning_none():
    assert _raises(lambda: TAXONOMY.get("no_such_category"), KeyError)
    assert "no_such_category" not in TAXONOMY
    assert "email" in TAXONOMY


def check_every_category_carries_a_label_and_the_keys_come_back_sorted():
    for c in TAXONOMY:
        assert c.label.strip(), c.key
    assert list(TAXONOMY.keys()) == sorted(TAXONOMY.keys())
    assert len(TAXONOMY.keys()) == len(TAXONOMY)


def check_a_category_is_frozen():
    c = TAXONOMY.get("email")
    try:
        c.key = "other"
    except Exception:
        return
    raise AssertionError("Category is not frozen")
