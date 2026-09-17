from __future__ import annotations

import datetime as dt

from pii.corpus import POSTAL_CODES, POSTAL_WEIGHTS, generate, summarise


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


SMALL = generate(n_patients=200, seed=99)


def check_the_same_seed_reproduces_the_corpus_row_for_row():
    a = generate(n_patients=50, seed=5)
    b = generate(n_patients=50, seed=5)
    assert a.patients == b.patients
    assert a.encounters == b.encounters
    assert a.claims == b.claims
    assert a.readings == b.readings


def check_a_different_seed_changes_it():
    a = generate(n_patients=50, seed=5)
    b = generate(n_patients=50, seed=6)
    assert a.patients != b.patients


def check_zero_patients_is_refused():
    assert _raises(lambda: generate(n_patients=0), ValueError)


def check_the_population_is_not_degenerate():
    # A generator can look correct line by line and produce a population that could not
    # exist. These are the summary facts that would have caught that, and they are
    # asserted rather than printed.
    s = summarise(SMALL)
    assert s["counts"]["raw.patient"] == 200
    assert s["counts"]["raw.encounter"] >= 200, "somebody has no encounter at all"
    assert s["distinct_sex"] >= 2
    assert 1 < s["distinct_postal"] <= len(POSTAL_CODES)
    assert 0 <= s["age_min"] < s["age_median"] < s["age_max"] <= 95
    assert s["max_encounters_per_patient"] > 1, "every patient has exactly one encounter"


def check_the_postal_draw_is_skewed_rather_than_flat():
    # If it were flat the uniform prediction would be exact by construction and the
    # measurement would be measuring its own assumption.
    s = summarise(SMALL)
    flat = 1.0 / s["distinct_postal"]
    assert s["postal_head_share"] > 2 * flat, (s["postal_head_share"], flat)


def check_the_postal_weights_sum_to_one_and_decay():
    assert abs(sum(POSTAL_WEIGHTS) - 1.0) < 1e-12
    assert POSTAL_WEIGHTS[0] > POSTAL_WEIGHTS[1] > POSTAL_WEIGHTS[-1]
    assert len(POSTAL_WEIGHTS) == len(POSTAL_CODES)


def check_every_postal_code_is_five_characters():
    for pc in POSTAL_CODES:
        assert len(pc) == 5, pc
    assert len(set(POSTAL_CODES)) == len(POSTAL_CODES)


def check_the_age_draw_is_not_uniform():
    # The first version of this compared the median against the midpoint of the range,
    # which cannot separate the two at all, because the mode sits near the centre and a
    # symmetric triangular has its median there too. Measured 45 against a midpoint of 46
    # and the check failed for being wrong rather than for finding anything.
    #
    # What does separate them is concentration. A band twenty years wide around the mode
    # holds about 22 percent of a uniform draw over this range and about 40 percent of
    # this one.
    today = dt.date(2026, 9, 17)
    ages = []
    for p in SMALL.patients:
        b = p["birth_date"]
        ages.append(today.year - b.year - ((today.month, today.day) < (b.month, b.day)))
    span = max(ages) - min(ages) + 1
    near_mode = sum(1 for a in ages if 36 <= a <= 56)
    uniform_expectation = 21.0 / span
    assert near_mode / len(ages) > 1.5 * uniform_expectation, (
        near_mode / len(ages), uniform_expectation)


def check_a_discharge_never_precedes_its_admission():
    for e in SMALL.encounters:
        assert e["discharged_at"] > e["admitted_at"], e["encounter_id"]


def check_a_claim_is_never_submitted_before_its_encounter_ended():
    by_id = {e["encounter_id"]: e for e in SMALL.encounters}
    for c in SMALL.claims:
        e = by_id[c["encounter_id"]]
        assert c["submitted_on"] >= e["discharged_at"].date(), c["claim_id"]


def check_only_a_paid_claim_carries_a_paid_amount():
    for c in SMALL.claims:
        if c["claim_status"] != "paid":
            assert c["paid_amount"] == 0.0, c["claim_id"]
        else:
            assert 0 < c["paid_amount"] <= c["billed_amount"], c["claim_id"]


def check_every_child_row_points_at_a_parent_that_exists():
    patients = {p["patient_id"] for p in SMALL.patients}
    encounters = {e["encounter_id"] for e in SMALL.encounters}
    for e in SMALL.encounters:
        assert e["patient_id"] in patients
    for c in SMALL.claims:
        assert c["encounter_id"] in encounters
    for r in SMALL.readings:
        assert r["patient_id"] in patients


def check_the_planted_values_really_look_like_what_they_are_planted_as():
    # The corpus is the input to a value based classifier later, so a column planted as
    # an email whose values are not emails would break that silently.
    p = SMALL.patients[0]
    assert "@" in p["email"] and p["email"].endswith(".org")
    assert p["ssn"].count("-") == 2 and len(p["ssn"]) == 11
    assert p["phone"].startswith("+1555")
    assert isinstance(p["birth_date"], dt.date)
    assert isinstance(p["created_at"], dt.datetime)
    r = SMALL.readings[0]
    assert r["source_ip"].startswith("10.") and r["source_ip"].count(".") == 3


def check_a_reading_is_taken_inside_a_plausible_window_of_its_encounter():
    starts = {}
    for e in SMALL.encounters:
        starts.setdefault(e["patient_id"], []).append(e["admitted_at"])
    for r in SMALL.readings:
        assert any(s <= r["taken_at"] for s in starts[r["patient_id"]]), r["reading_id"]


def check_ids_are_dense_and_start_at_one():
    assert [p["patient_id"] for p in SMALL.patients] == list(range(1, 201))
    assert [e["encounter_id"] for e in SMALL.encounters] == list(
        range(1, len(SMALL.encounters) + 1))


def check_growing_the_population_does_not_change_the_rows_already_drawn():
    # Patients are drawn one at a time off one stream, so a larger run has to extend the
    # smaller one rather than produce a different population. A generator that fails this
    # makes every figure a function of the row count somebody happened to pass.
    small = generate(n_patients=20, seed=5)
    large = generate(n_patients=40, seed=5)
    assert large.patients[:20] == small.patients
