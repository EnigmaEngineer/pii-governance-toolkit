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
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Tuple

from pii.rng import stream

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


def generate(n_patients: int = 1000, seed: int = 20260917,
             today: dt.date = dt.date(2026, 9, 17)) -> Corpus:
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
            stay_h = max(1, int(e_rnd.triangular(1, 260, 30)))
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

    return Corpus(patients=patients, encounters=encounters, claims=claims,
                  readings=readings)


def summarise(c: Corpus) -> Dict[str, object]:
    """Distributional facts about the corpus, computed rather than asserted.

    This exists because of a mistake I have watched happen: a generator that looks correct
    line by line and produces a population that could not exist, where the tell was one
    line of a summary nobody had asked for. Run it before building anything on the rows.
    """
    ages = []
    today = dt.date(2026, 9, 17)
    for p in c.patients:
        b = p["birth_date"]
        ages.append(today.year - b.year - ((today.month, today.day) < (b.month, b.day)))
    per_patient: Dict[int, int] = {}
    for e in c.encounters:
        per_patient[e["patient_id"]] = per_patient.get(e["patient_id"], 0) + 1

    return {
        "counts": c.counts(),
        "distinct_postal": len({p["postal_code"] for p in c.patients}),
        "distinct_birth_date": len({p["birth_date"] for p in c.patients}),
        "distinct_sex": len({p["sex"] for p in c.patients}),
        "age_min": min(ages),
        "age_max": max(ages),
        "age_median": sorted(ages)[len(ages) // 2],
        "patients_with_no_encounter": len(c.patients) - len(per_patient),
        "max_encounters_per_patient": max(per_patient.values()) if per_patient else 0,
        "postal_head_share": round(
            max(
                sum(1 for p in c.patients if p["postal_code"] == pc)
                for pc in {p["postal_code"] for p in c.patients}
            ) / len(c.patients), 6),
    }
