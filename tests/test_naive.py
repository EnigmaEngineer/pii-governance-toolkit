from __future__ import annotations

from pii import naive


def check_a_listed_substring_fires_and_an_unlisted_one_does_not():
    assert naive.classify_by_name("customer_email") == "email"
    assert naive.classify_by_name("EMAIL_ADDRESS") == "email"
    assert naive.classify_by_name("contact_handle") is None


def check_the_name_heuristic_returns_the_first_hit_and_that_is_the_documented_behaviour():
    # "address" and "ip_address" both match a column called ip_address, and the list order
    # decides which wins. Pinning it means a reordering of the list cannot change the
    # answer without something failing.
    assert naive.classify_by_name("ip_address") == "street_address"
    assert naive.classify_by_name("source_ip") is None


def check_classify_falls_through_to_not_personal_rather_than_none():
    assert naive.classify("widget_count") == "not_personal"
    assert naive.flags_as_personal("widget_count") is False
    assert naive.flags_as_personal("customer_email") is True


def check_the_value_arm_needs_a_two_thirds_majority():
    emails = tuple("a{}@b.org".format(i) for i in range(10))
    assert naive.classify_by_value(emails) == "email"

    # Exactly two thirds clears it and one below does not. Both sides of the limit,
    # because a floor tested well away from its boundary is not tested.
    at = tuple(list(emails[:6]) + ["x", "y", "z"])
    assert naive.classify_by_value(at) == "email"
    under = tuple(list(emails[:5]) + ["v", "w", "x", "y"])
    assert naive.classify_by_value(under) is None


def check_the_value_arm_ignores_nulls_and_empties_rather_than_counting_them():
    sample = ("a@b.org", "c@d.org", None, "", "e@f.org")
    assert naive.classify_by_value(sample) == "email"


def check_an_empty_sample_returns_none_rather_than_a_category():
    assert naive.classify_by_value(()) is None
    assert naive.classify_by_value((None, "", None)) is None


def check_one_email_in_a_free_text_column_does_not_make_it_an_email_column():
    notes = tuple(["patient reports chest tightness"] * 9 + ["a@b.org"])
    assert naive.classify_by_value(notes) is None


def check_the_name_arm_wins_over_the_value_arm():
    # classify reads the name first. If that ever flips, a column named email holding
    # phone numbers changes answer, so it is pinned rather than left to the reading order.
    phones = tuple("+15551234567" for _ in range(10))
    assert naive.classify_by_value(phones) == "phone"
    assert naive.classify("customer_email", phones) == "email"


def check_the_phone_regex_matches_an_iso_date_and_that_is_recorded_rather_than_patched():
    # This is a real defect in the naive scan and it is the reason it is here. An ISO date
    # is digits and dashes of the right length, so a column of dates comes back as a
    # column of telephone numbers. Fixing the regex would be improving the arm this repo
    # measures against, which is the one thing it must not do.
    assert naive.classify_by_value(("2026-03-14", "2025-11-02", "2024-01-30")) == "phone"


def check_a_ten_digit_identifier_also_reads_as_a_phone_number():
    assert naive.classify_by_value(("1300000123", "1300009999")) == "phone"


def check_each_regex_matches_its_own_family_and_not_its_neighbours():
    cases = {
        "email": ("a@b.co", "first.last9@example.org"),
        "national_id": ("123-45-6789", "899-99-9999"),
        "ip_address": ("10.0.0.1", "192.168.255.254"),
    }
    for expected, values in cases.items():
        assert naive.classify_by_value(values) == expected, expected


def check_a_malformed_email_is_not_matched_by_anything():
    assert naive.classify_by_value(("a@b", "c@d")) is None


def check_a_malformed_national_id_falls_through_to_the_phone_pattern():
    # Third time the loose phone pattern has turned up while writing these checks. The
    # first version of this asserted None, on the reasoning that a two digit first group
    # is not a valid identifier, and it is not. It is still digits and dashes of the right
    # length, so the phone pattern takes it. Asserting what it does rather than what I
    # assumed, because the arm is supposed to be naive and this is what naive looks like.
    ids = dict(naive.VALUE_PATTERNS)["national_id"]
    assert ids.match("12-345-6789") is None
    assert naive.classify_by_value(("12-345-6789", "12-345-6780")) == "phone"


def check_sample_column_is_bounded_and_survives_an_empty_table():
    rows = tuple({"a": i} for i in range(500))
    assert len(naive.sample_column(rows, "a")) == 50
    assert len(naive.sample_column(rows, "a", limit=7)) == 7
    assert naive.sample_column((), "a") == ()
    assert naive.sample_column(rows, "missing") == ()


def check_sample_column_reads_the_first_row_and_works_on_a_table_of_one():
    # A one row table is the fixture that pins which row the membership test reads. With
    # five hundred identical rows, reading the first or the second is the same answer and
    # a mutant moving the index survives everything.
    one = ({"a": 7},)
    assert naive.sample_column(one, "a") == (7,)
    assert naive.sample_column(one, "missing") == ()


def check_every_name_substring_maps_to_a_category_the_taxonomy_has():
    from pii.taxonomy import TAXONOMY
    for _, key in naive.NAME_SUBSTRINGS:
        assert key in TAXONOMY, key
    for key, _ in naive.VALUE_PATTERNS:
        assert key in TAXONOMY, key

