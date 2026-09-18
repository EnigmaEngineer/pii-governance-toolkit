from __future__ import annotations

import datetime as dt

from pii.corpus import (
    NULL_RATE,
    POSTAL_CODES,
    POSTAL_WEIGHTS,
    generate,
    null_counts,
    nullable_columns,
    summarise,
)


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
    # The age figures are over the patients whose birth date is recorded, and the summary
    # says how many that was rather than leaving a reader to assume it was all of them.
    assert s["ages_known"] <= s["counts"]["raw.patient"]
    assert s["ages_known"] > 0


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
        if b is None:
            continue
        ages.append(today.year - b.year - ((today.month, today.day) < (b.month, b.day)))
    span = max(ages) - min(ages) + 1
    near_mode = sum(1 for a in ages if 36 <= a <= 56)
    uniform_expectation = 21.0 / span
    assert near_mode / len(ages) > 1.5 * uniform_expectation, (
        near_mode / len(ages), uniform_expectation)


def check_a_discharge_never_precedes_its_admission():
    # A null discharge is a patient still admitted, which is a legal row and not a
    # violation of the ordering. The check says so instead of skipping quietly, and it
    # asserts that the skip really fires, because an ordering check that silently
    # excluded every row would report the same clean pass.
    judged = 0
    for e in SMALL.encounters:
        if e["discharged_at"] is None:
            continue
        judged += 1
        assert e["discharged_at"] > e["admitted_at"], e["encounter_id"]
    assert judged > 0
    assert judged < len(SMALL.encounters), "no encounter is still admitted"


def check_a_claim_is_never_submitted_before_its_encounter_ended():
    by_id = {e["encounter_id"]: e for e in SMALL.encounters}
    judged = 0
    for c in SMALL.claims:
        e = by_id[c["encounter_id"]]
        if c["submitted_on"] is None or e["discharged_at"] is None:
            continue
        judged += 1
        assert c["submitted_on"] >= e["discharged_at"].date(), c["claim_id"]
    assert judged > 0


def check_only_a_paid_claim_carries_a_paid_amount():
    # Three cases now, and the third one is the whole reason this file changed. A claim
    # whose status is null cannot be judged against a rule keyed on the status, and a
    # paid amount of null is not a paid amount of zero. Both are counted so that a
    # version of this check which judged nothing would fail.
    judged = 0
    unjudgeable = 0
    for c in SMALL.claims:
        if c["claim_status"] is None or c["paid_amount"] is None:
            unjudgeable += 1
            continue
        judged += 1
        if c["claim_status"] != "paid":
            assert c["paid_amount"] == 0.0, c["claim_id"]
        else:
            assert c["billed_amount"] is None or (
                0 < c["paid_amount"] <= c["billed_amount"]), c["claim_id"]
    assert judged > 0
    assert unjudgeable > 0, "no claim is missing a status or an amount"


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
    # Over every value the column carries rather than over row zero. Row zero used to be
    # enough and it is not any more: the first patient may be missing any of these, and a
    # check reading one row would pass or crash depending on which draw landed there.
    for p in SMALL.patients:
        if p["email"] is not None:
            assert "@" in p["email"] and p["email"].endswith(".org"), p["patient_id"]
        if p["ssn"] is not None:
            assert p["ssn"].count("-") == 2 and len(p["ssn"]) == 11, p["patient_id"]
        if p["phone"] is not None:
            assert p["phone"].startswith("+1555"), p["patient_id"]
        if p["birth_date"] is not None:
            assert isinstance(p["birth_date"], dt.date), p["patient_id"]
        assert isinstance(p["created_at"], dt.datetime), p["patient_id"]
    for r in SMALL.readings:
        if r["source_ip"] is None:
            continue
        assert r["source_ip"].startswith("10."), r["reading_id"]
        assert r["source_ip"].count(".") == 3, r["reading_id"]


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


# ot-069. Nullability was declared on 09-17 and exercised by nothing, because the
# generator never wrote a null anywhere. The suite could check one direction of it, that a
# NOT NULL column holds no null, and that direction is the easy one. These are the other
# direction, and they are the reason the corpus moved.


def check_no_not_null_column_ever_holds_a_null():
    # The direction that already worked, kept and widened to every table rather than
    # asserted on a sample. This is the one the database itself also enforces, so it is
    # belt and braces, and it is the check that fails first if a flag is flipped by hand.
    from pii.schema import tables_by_fqn

    from pii.corpus import ROWS_FOR

    by_fqn = tables_by_fqn()
    checked = 0
    for fqn, attr in sorted(ROWS_FOR.items()):
        rows = getattr(SMALL, attr)
        for c in by_fqn[fqn].columns:
            if c.nullable:
                continue
            checked += 1
            for r in rows:
                assert r[c.name] is not None, "{}.{}".format(fqn, c.name)
    assert checked > 0, "no column in the generated tables is declared NOT NULL"


def check_every_nullable_column_really_holds_a_null():
    # The direction that could not fire before. A nullable flag is a claim that the
    # column may be empty, and until something in the data is empty the claim has no
    # evidence behind it either way.
    counts = null_counts(generate(n_patients=1000, seed=20260917))
    assert set(counts) == {"{}.{}".format(t, c) for t, c in nullable_columns()}
    never = sorted(k for k, v in counts.items() if v == 0)
    assert not never, "declared nullable and never null: {}".format(never)


def check_the_observed_null_share_is_near_the_rate_it_was_asked_for():
    # Not an assertion about the exact count, which would be transcription. A rate of
    # 0.02 over 1,000 draws has a standard deviation of about 0.44 percent, so a window
    # of plus or minus 1.5 points is about three sigma and it would catch a rate applied
    # to the wrong denominator or applied twice.
    c = generate(n_patients=1000, seed=20260917)
    counts = null_counts(c)
    rows = {"raw.patient": 1000, "raw.encounter": len(c.encounters),
            "raw.claim": len(c.claims), "raw.device_reading": len(c.readings)}
    for address, n in counts.items():
        table = address.rsplit(".", 1)[0]
        share = n / rows[table]
        assert abs(share - NULL_RATE) < 0.015, (address, share)


# Digests of the default corpus as it stood on 2026-09-17, before nulls existed, taken
# off the tree at commit 599f817 and pinned here. They are what makes the post pass claim
# checkable: turning the rate off has to give back that corpus and not merely a corpus
# with no nulls in it. Without them the check below would be satisfied by any generator
# that happens not to emit a null, including one whose draw had shifted underneath.
BEFORE_NULLS = {
    "patients": "42985250dfe8",
    "encounters": "bc53e2ef0a0a",
    "claims": "1bb46f2ceb91",
    "readings": "6e284904a972",
}


def _digest(rows):
    import hashlib
    import json

    payload = [{k: str(v) for k, v in r.items()} for r in rows]
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def check_a_zero_rate_gives_back_the_corpus_that_existed_before_nulls():
    # The control for the whole change. If this fails then the draw shifted, and every
    # figure that moved on 09-18 moved for two reasons at once rather than one.
    a = generate(null_rate=0.0)
    for name, expected in sorted(BEFORE_NULLS.items()):
        assert _digest(getattr(a, name)) == expected, name


def check_the_default_corpus_is_not_that_corpus_any_more():
    # The other half, and it is the half that stops the pin above from being vacuous. If
    # the nulls were never applied, both checks would pass and the pair would say nothing.
    a = generate()
    moved = [name for name, expected in BEFORE_NULLS.items()
             if _digest(getattr(a, name)) != expected]
    assert sorted(moved) == sorted(BEFORE_NULLS), moved


def check_nulling_one_column_does_not_move_another_columns_nulls():
    # Per column streams, which is the same argument pii/rng.py makes for naming streams
    # rather than numbering them. Adding a column to the schema must not shift the nulls
    # in any column beside it. Approximated here by checking that two columns drawn in the
    # same pass have independent null positions rather than the same ones.
    c = generate(n_patients=400, seed=11)
    email = {p["patient_id"] for p in c.patients if p["email"] is None}
    phone = {p["patient_id"] for p in c.patients if p["phone"] is None}
    assert email and phone
    assert email != phone, "two columns got identical null positions"


def check_the_null_rate_is_refused_outside_its_range():
    assert _raises(lambda: generate(n_patients=10, null_rate=-0.1), ValueError)
    assert _raises(lambda: generate(n_patients=10, null_rate=1.0), ValueError)
    assert _raises(lambda: generate(n_patients=10, null_rate=2.0), ValueError)


def check_a_summary_statistic_is_over_the_values_that_exist():
    # A null is not a zero. If the age figures folded nulls in as year zero the minimum
    # would be about 2,026 and the median would move, so the cheap tell is that the
    # summary of a corpus with nulls and the summary of one without agree on the shape.
    with_nulls = summarise(generate(n_patients=600, seed=77))
    without = summarise(generate(n_patients=600, seed=77, null_rate=0.0))
    assert with_nulls["age_min"] >= without["age_min"]
    assert with_nulls["age_max"] <= without["age_max"]
    assert abs(with_nulls["age_median"] - without["age_median"]) <= 2
    assert with_nulls["ages_known"] < without["ages_known"]
    assert without["nulls_total"] == 0
    assert with_nulls["nulls_total"] > 0


# Written against named survivors of the 09-18 mutation pass. Each one killed a mutant
# that had been green, and the mutant is named so that deleting the check later is a
# visible decision rather than a tidy-up.


def check_one_patient_is_a_legal_population():
    # Two survivors sat on the guard boundary. `n_patients < 1` mutated to `< 2` and to
    # `<= 1`, and both left the suite green because nothing ever asked for one patient.
    # The zero case was tested and the boundary beside it was not.
    one = generate(n_patients=1, seed=3, null_rate=0.0)
    assert len(one.patients) == 1
    assert one.patients[0]["patient_id"] == 1
    assert len(one.encounters) >= 1


def check_the_summary_uses_the_date_the_corpus_was_drawn_against():
    # summarise carried its own copy of 2026-09-17 while generate took the same date as a
    # parameter, so three mutants could move one of them and nothing noticed. The date is
    # on the corpus now and this is the check that says so.
    # The first version of this asserted that a later date gives larger ages, and that is
    # wrong about the generator. `_draw_birth_date` draws an age and subtracts it from the
    # year, so moving the date moves every birth date by the same amount and the ages come
    # out identical. The age is invariant to the date by construction.
    #
    # What a hardcoded date in the summary would really do is report the difference
    # between the two. Generate ten years out and the ages are still 0 to 95, unless the
    # summary is measuring against 2026, in which case a third of them come out negative.
    later = generate(n_patients=300, seed=8, today=dt.date(2036, 9, 17), null_rate=0.0)
    earlier = generate(n_patients=300, seed=8, today=dt.date(2026, 9, 17), null_rate=0.0)
    assert later.as_of == dt.date(2036, 9, 17)
    assert [p["birth_date"] for p in later.patients] != [
        p["birth_date"] for p in earlier.patients]

    s = summarise(later)
    assert s["age_min"] >= 0, s["age_min"]
    assert s["age_max"] <= 95, s["age_max"]
    # And the two summaries agree on the ages, which is the invariance stated as a claim
    # rather than left as a thing a reader has to work out from the generator.
    assert s["age_min"] == summarise(earlier)["age_min"]
    assert s["age_max"] == summarise(earlier)["age_max"]
    assert s["age_median"] == summarise(earlier)["age_median"]


def check_an_age_is_the_whole_years_between_the_birth_date_and_the_as_of_date():
    # Two survivors flipped the sign and the comparison inside the age arithmetic and the
    # existing bounds were loose enough to absorb both. Recomputed here against the dates
    # rather than against another copy of the same expression, because a check that
    # repeats the formula cannot catch a wrong formula.
    c = generate(n_patients=200, seed=51, null_rate=0.0)
    s = summarise(c)
    ages = []
    for p in c.patients:
        b = p["birth_date"]
        full_years = c.as_of.year - b.year
        if (c.as_of.month, c.as_of.day) < (b.month, b.day):
            full_years -= 1
        ages.append(full_years)
        assert 0 <= full_years <= 95, (p["patient_id"], b)
    assert s["age_min"] == min(ages)
    assert s["age_max"] == max(ages)
    assert s["age_median"] == sorted(ages)[len(ages) // 2]


def check_the_median_age_sits_in_the_middle_and_not_at_a_third():
    # `sorted(ages)[len(ages) // 2]` mutated to `// 3` and survived, because the only
    # assertion on the median was that it fell between the minimum and the maximum. The
    # median of this draw is near the mode, and a third of the way up is not.
    c = generate(n_patients=800, seed=17, null_rate=0.0)
    s = summarise(c)
    ages = sorted(c.as_of.year - p["birth_date"].year for p in c.patients)
    third = ages[len(ages) // 3]
    assert s["age_median"] > third, (s["age_median"], third)
    below = sum(1 for a in ages if a < s["age_median"])
    assert abs(below / len(ages) - 0.5) < 0.1, below / len(ages)


def check_a_share_of_the_population_cannot_exceed_the_population():
    # postal_head_share had a lower bound and no upper one, so dividing by the row count
    # mutated to multiplying by it and the check still passed. A share is bounded at both
    # ends and the upper end is the one that catches a wrong denominator.
    s = summarise(generate(n_patients=400, seed=23, null_rate=0.0))
    assert 0.0 < s["postal_head_share"] <= 1.0, s["postal_head_share"]
    c = generate(n_patients=400, seed=23, null_rate=0.0)
    head = max(sum(1 for p in c.patients if p["postal_code"] == pc)
               for pc in {p["postal_code"] for p in c.patients})
    assert abs(s["postal_head_share"] - head / len(c.patients)) < 1e-9


def check_the_encounter_tallies_are_the_counts_and_not_the_counts_plus_one():
    # Two survivors here. The default in per_patient.get moved from 0 to 1, inflating
    # every tally, and the subtraction computing patients with no encounter flipped to an
    # addition. Neither was asserted against anything but an inequality.
    c = generate(n_patients=250, seed=91, null_rate=0.0)
    s = summarise(c)
    tally = {}
    for e in c.encounters:
        tally[e["patient_id"]] = tally.get(e["patient_id"], 0) + 1
    assert s["max_encounters_per_patient"] == max(tally.values())
    assert s["max_encounters_per_patient"] <= 5, "the draw tops out at five"
    assert s["patients_with_no_encounter"] == len(c.patients) - len(tally)
    assert 0 <= s["patients_with_no_encounter"] <= len(c.patients)


def check_the_distinct_counts_count_the_values_and_not_the_nulls():
    # The null filter in the distinct_birth_date comprehension mutated from `is not None`
    # to `is None`, which makes the answer 1 for any corpus with a null in it, and
    # nothing asserted the figure at all.
    c = generate(n_patients=500, seed=64)
    s = summarise(c)
    assert s["distinct_birth_date"] == len(
        {p["birth_date"] for p in c.patients if p["birth_date"] is not None})
    assert s["distinct_birth_date"] > 1
    assert None not in {p["birth_date"] for p in c.patients
                        if p["birth_date"] is not None}
    assert s["distinct_sex"] == len(
        {p["sex"] for p in c.patients if p["sex"] is not None})
    assert s["distinct_postal"] == len(
        {p["postal_code"] for p in c.patients if p["postal_code"] is not None})
    # And the corpus really does carry nulls in all three, or the filters are being
    # graded against columns that have nothing for them to filter.
    for column in ("birth_date", "sex", "postal_code"):
        assert any(p[column] is None for p in c.patients), column


def check_the_corpus_refuses_to_have_its_fields_rebound():
    # frozen=True mutated to frozen=False and survived, because nothing depended on the
    # dataclass refusing assignment. The lists inside are still mutable and the null pass
    # relies on that, so what this pins is only that the corpus cannot be pointed at a
    # different set of rows after the fact.
    c = generate(n_patients=10, seed=1)
    assert _raises(lambda: setattr(c, "patients", []), Exception)
    assert _raises(lambda: setattr(c, "as_of", dt.date(2000, 1, 1)), Exception)


def check_the_published_reference_date_is_the_one_the_readme_quotes():
    # Two mutants moved the month and the day of generate's default date and both
    # survived, because every check that reads the ages recomputes them from the same
    # as_of the summary used, so the pair move together and agree. The date is a published
    # input and it gets pinned like one.
    assert generate(n_patients=5, seed=1).as_of == dt.date(2026, 9, 17)


def check_a_summary_of_a_population_with_no_encounters_does_not_raise():
    # The `else 0` guard in max_encounters_per_patient is unreachable through generate,
    # because every patient gets at least one encounter. Removing it would let max() raise
    # out of a function nothing expects to raise, so per the 08-11 rule it stays and gets
    # a stubbed test rather than being deleted as unreachable.
    from pii.corpus import Corpus

    hollow = Corpus(patients=[dict(p) for p in SMALL.patients], encounters=[],
                    claims=[], readings=[], as_of=SMALL.as_of)
    s = summarise(hollow)
    assert s["max_encounters_per_patient"] == 0
    assert s["patients_with_no_encounter"] == len(SMALL.patients)


def check_the_head_share_is_published_at_the_precision_it_claims():
    # round(..., 6) mutated to 7 and survived. The figure goes in the README, so the
    # precision is part of what is published.
    s = summarise(generate(n_patients=300, seed=44, null_rate=0.0))
    share = s["postal_head_share"]
    assert round(share, 6) == share, share
    assert len(str(share).split(".")[-1]) <= 6, share
