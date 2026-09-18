"""Generated rows for the sample schema.

Every number this repo publishes about the corpus is a number about a generator I wrote.
That is stated here, it is stated in the README, and it is printed by the probe next to
each figure rather than parked in a caveat two screens down. There is no real patient data
available to this project and there never will be, which is the point of planting it.

The one thing the generator deliberately does NOT do is draw quasi identifiers uniformly.
A uniform draw would make the re-identification arithmetic exact by construction, and an
exact agreement between a prediction and a measurement over data built to satisfy the
prediction measures nothing at all. Postal codes follow a decaying weight and birth dates
follow an age curve, so the measured uniqueness and the uniform prediction are two
different numbers and the gap between them is the informative part.

It also writes nulls, which it did not before 09-18. Every column declared nullable in
`pii/schema.py` used to carry a declaration nothing had ever exercised, so the suite could
only ever check one direction of it. A check that a NOT NULL column holds no null is
evidence. A check that a nullable column may hold one cannot fire against a corpus with no
null in it anywhere. The nulls arrive in a post pass, after every value has been drawn and
every derived value computed, for two reasons. The generator body never has to handle a
missing input, and the surviving values stay byte identical to the ones drawn before this
existed, so anything that moves in a published figure moved because of the nulls and not
because the draw shifted.

One consequence is stated rather than hidden. `raw.claim.submitted_on` is derived from the
encounter discharge date, so nulling a discharge date afterwards leaves the claim carrying a
date derived from a value the warehouse no longer holds. The corpus does not pretend the
claim was filed without a discharge.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Tuple

from pii.rng import stream
from pii.schema import tables_by_fqn

# Small vocabularies. Invented, obviously. Real generators pull from census name files and
# this one cannot, so the names are short lists and the README says so.
FIRST_NAMES = (
    "Aisha", "Bruno", "Carmen", "Dae", "Elif", "Farid", "Grace", "Hana", "Idris",
    "Jonah", "Kavi", "Lena", "Mira", "Noor", "Omar", "Petra", "Quinn", "Rosa",
    "Samir", "Tomas", "Uma", "Viktor", "Wren", "Yara", "Zane",
)
LAST_NAMES = (
    "Abara", "Baptiste", "Cheng", "Dlamini", "Erdogan", "Ferraro", "Gjoni",
    "Haddad", "Ibarra", "Jelinek", "Kowalski", "Lindqvist", "Moreau", "Nakamura",
    "Okafor", "Pereira", "Quintana", "Rasmussen", "Sokolov", "Tanaka", "Uddin",
    "Varga", "Wexler", "Yilmaz", "Zubair",
)
CITIES = ("Bellmont", "Cragside", "Downe End", "Elderfield", "Fairholt", "Gorsehill")
DEPARTMENTS = ("cardiology", "emergency", "oncology", "orthopaedics", "paediatrics")
DIAGNOSES = ("I10", "E11.9", "J45.909", "M54.5", "K21.9", "F41.1", "N39.0")
DISPOSITIONS = ("home", "transferred", "observation", "left_ama")
PAYERS = ("Northwind Health", "Coastal Mutual", "Pinebrook Plan", "Statewide Care")
CLAIM_STATUSES = ("submitted", "paid", "denied", "pending")
METRICS = ("heart_rate", "spo2", "systolic_bp", "temperature_c")
SEXES = ("F", "M", "X")

# Sixty postal codes. Enough that the quasi identifier combination has real spread and few
# enough that a thousand patients do not make every one of them unique, which would have
# made the day's measurement trivially 1.0 and said nothing.
POSTAL_CODES = tuple("1{:04d}".format(101 + 7 * i) for i in range(60))

# Which generated table feeds which schema table. One definition, imported by the loader,
# because a mapping written out twice is a second place for the answer to live and the
# copies drift the first time a table is added.
ROWS_FOR: Dict[str, str] = {
    "raw.patient": "patients",
    "raw.encounter": "encounters",
    "raw.claim": "claims",
    "raw.device_reading": "readings",
}

# Share of rows given a null in each nullable column. One rate for every column, which is
# a number chosen rather than measured, and it is low enough that no aggregate moves much
# and high enough that a thousand rows cannot miss a column by chance. The probability of
# a 1,000 row column receiving none at this rate is about 2e-9, and the suite asserts the
# observed count rather than trusting that arithmetic.
NULL_RATE = 0.02

NOTE_TEMPLATES = (
    "patient reports {sym} for {n} days, no fever",
    "follow up on {sym}, tolerating treatment",
    "seen for {sym}. discussed plan with family",
    "{sym} improving. discharge advice given",
)
SYMPTOMS = ("chest tightness", "shortness of breath", "lower back pain", "palpitations")


@dataclass(frozen=True)
class Corpus:
    patients: List[dict]
    encounters: List[dict]
    claims: List[dict]
    readings: List[dict]
    # The date the ages were drawn against. It used to live only in `generate`'s signature
    # while `summarise` carried its own copy of the same literal, so the two could drift
    # and a mutant moving one of them was invisible. Carried on the corpus instead, which
    # is the same fix as deriving the uniqueness sweep's column list from the taxonomy
    # rather than writing it out a second time.
    #
    # Required rather than defaulted. It had a default of 2026-09-17 for about an hour and
    # `generate` is the only thing that builds a Corpus and always passes the date
    # explicitly, so the default was a third copy of the same literal that nothing could
    # reach. Three mutants moved it and all three survived.
    as_of: dt.date

    def counts(self) -> Dict[str, int]:
        return {
            "raw.patient": len(self.patients),
            "raw.encounter": len(self.encounters),
            "raw.claim": len(self.claims),
            "raw.device_reading": len(self.readings),
        }


def _postal_weights(n: int) -> Tuple[float, ...]:
    """A decaying weight per postal code.

    1/(i+1) rather than anything fitted. The shape matters and the exact curve does not,
    because nothing downstream reads the weights. What matters is that it is not flat.
    """
    raw = [1.0 / (i + 1) for i in range(n)]
    total = sum(raw)
    return tuple(r / total for r in raw)


POSTAL_WEIGHTS = _postal_weights(len(POSTAL_CODES))


def _draw_birth_date(rnd, today: dt.date) -> dt.date:
    """An age in a plausible band, then a day inside that year.

    Ages cluster in middle life rather than spreading evenly over 0 to 95. A triangular
    draw is the cheapest thing that is not uniform and it is honest about being a shape I
    picked rather than one I measured.
    """
    age = int(rnd.triangular(0, 95, 46))
    year = today.year - age
    day_of_year = rnd.randint(1, 365)
    return dt.date(year, 1, 1) + dt.timedelta(days=day_of_year - 1)


def nullable_columns() -> Tuple[Tuple[str, str], ...]:
    """Every generated column the schema says may hold a null, as (table fqn, column).

    Derived from `pii/schema.py` rather than listed here. A hand written copy of this list
    would go stale the moment a column changed its flag, and it would go stale silently,
    which is the failure the flags already had before they were exercised at all.
    """
    by_fqn = tables_by_fqn()
    out = []
    for fqn in sorted(ROWS_FOR):
        for c in by_fqn[fqn].columns:
            if c.nullable:
                out.append((fqn, c.name))
    return tuple(out)


def _apply_nulls(corpus: "Corpus", seed: int, rate: float) -> None:
    """Null out a share of each nullable column, in place.

    One stream per column rather than one stream for the whole pass. With a single stream
    the draws are consumed in whatever order the columns are visited, so adding a column
    to the schema would move the nulls in every column after it. Per column streams mean a
    new column cannot touch an existing one, which is the same argument `pii/rng.py` makes
    for naming streams instead of numbering them.
    """
    if not 0.0 <= rate < 1.0:
        raise ValueError("null rate must be at least 0 and below 1, got {}".format(rate))
    if rate == 0.0:
        return
    for fqn, column in nullable_columns():
        rows = getattr(corpus, ROWS_FOR[fqn])
        rnd = stream(seed, "nulls:{}.{}".format(fqn, column))
        for r in rows:
            if rnd.random() < rate:
                r[column] = None


def null_counts(c: "Corpus") -> Dict[str, int]:
    """Observed nulls per column, counted off the rows.

    Counted rather than tallied during generation. A generator reporting how many nulls it
    meant to write is not a measurement of how many are there, which is the same reason
    the loader reads its row counts back out of the database.
    """
    out: Dict[str, int] = {}
    for fqn, column in nullable_columns():
        rows = getattr(c, ROWS_FOR[fqn])
        out["{}.{}".format(fqn, column)] = sum(1 for r in rows if r[column] is None)
    return out


def generate(n_patients: int = 1000, seed: int = 20260917,
             today: dt.date = dt.date(2026, 9, 17),
             null_rate: float = NULL_RATE) -> Corpus:
    if n_patients < 1:
        raise ValueError("n_patients must be at least 1")

    p_rnd = stream(seed, "patient")
    e_rnd = stream(seed, "encounter")
    c_rnd = stream(seed, "claim")
    d_rnd = stream(seed, "device_reading")

    patients = []
    for pid in range(1, n_patients + 1):
        first = p_rnd.choice(FIRST_NAMES)
        last = p_rnd.choice(LAST_NAMES)
        postal = p_rnd.choices(POSTAL_CODES, weights=POSTAL_WEIGHTS, k=1)[0]
        patients.append({
            "patient_id": pid,
            "mrn": "MRN{:08d}".format(4100000 + pid * 13),
            "first_name": first,
            "last_name": last,
            "email": "{}.{}{}@example.org".format(first.lower(), last.lower(), pid % 97),
            "phone": "+1555{:07d}".format(1000000 + pid * 37 % 8999999),
            "street_address": "{} {} Street".format(
                p_rnd.randint(1, 480), p_rnd.choice(LAST_NAMES)),
            "city": p_rnd.choice(CITIES),
            "postal_code": postal,
            "birth_date": _draw_birth_date(p_rnd, today),
            "sex": p_rnd.choices(SEXES, weights=(0.49, 0.49, 0.02), k=1)[0],
            "ssn": "{:03d}-{:02d}-{:04d}".format(
                p_rnd.randint(100, 899), p_rnd.randint(10, 99), p_rnd.randint(1000, 9999)),
            "created_at": dt.datetime(2026, 1, 1) + dt.timedelta(
                seconds=p_rnd.randint(0, 220 * 86400)),
        })

    encounters = []
    eid = 0
    for p in patients:
        # Most patients have one encounter. A few have several, which is what puts more
        # than one row under the same quasi identifier combination.
        for _ in range(e_rnd.choices((1, 2, 3, 5), weights=(0.70, 0.20, 0.07, 0.03), k=1)[0]):
            eid += 1
            admitted = dt.datetime(2026, 1, 1) + dt.timedelta(
                seconds=e_rnd.randint(0, 250 * 86400))
            # No clamp. It was max(1, ...) and the clamp was dead, because the triangular
            # draw has a lower bound of 1 so the int is never below it. A mutant moving
            # the clamp to 2 survived, which is what pointed at it. Deleting it leaves
            # every generated value byte identical.
            stay_h = int(e_rnd.triangular(1, 260, 30))
            sym = e_rnd.choice(SYMPTOMS)
            encounters.append({
                "encounter_id": eid,
                "patient_id": p["patient_id"],
                "admitted_at": admitted,
                "discharged_at": admitted + dt.timedelta(hours=stay_h),
                "department": e_rnd.choice(DEPARTMENTS),
                "attending_npi": "{:010d}".format(1300000000 + e_rnd.randint(0, 9999)),
                "primary_diagnosis": e_rnd.choice(DIAGNOSES),
                "clinical_note": e_rnd.choice(NOTE_TEMPLATES).format(
                    sym=sym, n=e_rnd.randint(2, 14)),
                "disposition": e_rnd.choice(DISPOSITIONS),
            })

    claims = []
    for i, e in enumerate(encounters, start=1):
        billed = round(c_rnd.uniform(180, 42000), 2)
        status = c_rnd.choices(CLAIM_STATUSES, weights=(0.18, 0.56, 0.12, 0.14), k=1)[0]
        paid = round(billed * c_rnd.uniform(0.35, 0.92), 2) if status == "paid" else 0.0
        claims.append({
            "claim_id": i,
            "encounter_id": e["encounter_id"],
            "member_number": "MB{:09d}".format(770000000 + e["patient_id"] * 91),
            "payer_name": c_rnd.choice(PAYERS),
            "billed_amount": billed,
            "paid_amount": paid,
            "claim_status": status,
            "submitted_on": (e["discharged_at"] + dt.timedelta(
                days=c_rnd.randint(0, 21))).date(),
        })

    readings = []
    rid = 0
    for e in encounters:
        for _ in range(d_rnd.randint(0, 4)):
            rid += 1
            readings.append({
                "reading_id": rid,
                "patient_id": e["patient_id"],
                "device_serial": "DEV-{:06X}".format(d_rnd.randint(0, 0xFFFFFF)),
                "taken_at": e["admitted_at"] + dt.timedelta(
                    minutes=d_rnd.randint(0, 1440)),
                "metric": d_rnd.choice(METRICS),
                "reading_value": round(d_rnd.uniform(35.0, 190.0), 2),
                "source_ip": "10.{}.{}.{}".format(
                    d_rnd.randint(0, 31), d_rnd.randint(0, 255), d_rnd.randint(1, 254)),
            })

    corpus = Corpus(patients=patients, encounters=encounters, claims=claims,
                    readings=readings, as_of=today)
    _apply_nulls(corpus, seed, null_rate)
    return corpus


def summarise(c: Corpus) -> Dict[str, object]:
    """Distributional facts about the corpus, computed rather than asserted.

    This exists because of a mistake I have watched happen: a generator that looks correct
    line by line and produces a population that could not exist, where the tell was one
    line of a summary nobody had asked for. Run it before building anything on the rows.
    """
    # Every statistic below is over the rows that carry a value. A null is not a zero and
    # it is not a category, and a summary that quietly folded one into either would be the
    # first thing to mislead somebody reading this corpus. The null counts are reported
    # separately so the denominator each figure used is visible rather than implied.
    ages = []
    today = c.as_of
    for p in c.patients:
        b = p["birth_date"]
        if b is None:
            continue
        ages.append(today.year - b.year - ((today.month, today.day) < (b.month, b.day)))
    per_patient: Dict[int, int] = {}
    for e in c.encounters:
        per_patient[e["patient_id"]] = per_patient.get(e["patient_id"], 0) + 1

    postals = [p["postal_code"] for p in c.patients if p["postal_code"] is not None]

    return {
        "counts": c.counts(),
        "distinct_postal": len(set(postals)),
        "distinct_birth_date": len({p["birth_date"] for p in c.patients
                                    if p["birth_date"] is not None}),
        "distinct_sex": len({p["sex"] for p in c.patients if p["sex"] is not None}),
        "age_min": min(ages),
        "age_max": max(ages),
        "age_median": sorted(ages)[len(ages) // 2],
        "ages_known": len(ages),
        "patients_with_no_encounter": len(c.patients) - len(per_patient),
        "max_encounters_per_patient": max(per_patient.values()) if per_patient else 0,
        "postal_head_share": round(
            max(postals.count(pc) for pc in set(postals)) / len(postals), 6),
        "nulls_total": sum(null_counts(c).values()),
        "nullable_columns": len(nullable_columns()),
    }
