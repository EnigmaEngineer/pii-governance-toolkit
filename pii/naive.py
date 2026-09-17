"""The obvious classifier, built on purpose so there is a floor to measure against.

This is not the classifier this project ships. It is the thing almost every in-house PII
scan turns out to be when you read it: a list of substrings matched against column names,
plus a handful of regexes matched against values. It exists here for one reason, which is
that a score for a real classifier means nothing without the score of doing the obvious
thing printed beside it.

Keeping it in the library rather than in a report script is deliberate. A rule that decides
a published comparison has to sit somewhere a mutation pass can reach it.

The name substrings were written the way somebody writes them on a Tuesday afternoon: from
memory, covering what came to mind. That is the point. Do not extend this list to close a
gap the measurement finds, because the gap is the measurement.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

NAME_SUBSTRINGS: Tuple[Tuple[str, str], ...] = (
    ("email", "email"),
    ("e_mail", "email"),
    ("phone", "phone"),
    ("mobile", "phone"),
    ("ssn", "national_id"),
    ("social_security", "national_id"),
    ("first_name", "person_name"),
    ("last_name", "person_name"),
    ("full_name", "person_name"),
    ("surname", "person_name"),
    ("address", "street_address"),
    ("postal", "postal_code"),
    ("zip", "postal_code"),
    ("dob", "birth_date"),
    ("birth", "birth_date"),
    ("mrn", "medical_record_number"),
    ("card_number", "payment_card"),
    ("ip_address", "ip_address"),
)

VALUE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("email", re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")),
    ("national_id", re.compile(r"^\d{3}-\d{2}-\d{4}$")),
    ("phone", re.compile(r"^\+?\d[\d\-\s]{7,}\d$")),
    ("ip_address", re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")),
    ("payment_card", re.compile(r"^\d{13,19}$")),
)


def classify_by_name(column_name: str) -> Optional[str]:
    """First substring hit, or None. First hit rather than best hit, because that is what
    a hand rolled scan does and because "best" needs a scoring rule nobody writes."""
    low = column_name.lower()
    for needle, key in NAME_SUBSTRINGS:
        if needle in low:
            return key
    return None


def classify_by_value(sample: Tuple[object, ...]) -> Optional[str]:
    """Majority vote over a sample, with a two thirds floor.

    A floor rather than any hit, because one email address in a free text column should not
    make the whole column an email column. Two thirds is a number I chose and it is not
    measured, which is said out loud here rather than left for a reader to assume otherwise.
    """
    values = [str(v) for v in sample if v is not None and str(v) != ""]
    if not values:
        return None
    for key, pattern in VALUE_PATTERNS:
        hits = sum(1 for v in values if pattern.match(v))
        if hits >= (2 * len(values)) / 3:
            return key
    return None


def classify(column_name: str, sample: Tuple[object, ...] = ()) -> str:
    """Name first, values second, `not_personal` when neither fires.

    Returning a category rather than None keeps every caller off the "did you remember the
    null case" path.
    """
    by_name = classify_by_name(column_name)
    if by_name is not None:
        return by_name
    by_value = classify_by_value(sample)
    if by_value is not None:
        return by_value
    return "not_personal"


def flags_as_personal(column_name: str, sample: Tuple[object, ...] = ()) -> bool:
    return classify(column_name, sample) != "not_personal"


def sample_column(rows: Tuple[dict, ...], column: str, limit: int = 50) -> Tuple[object, ...]:
    # The first row decides whether the column exists, which is fine for warehouse rows
    # and would be wrong for ragged dicts. Said out loud because the alternative is a
    # scan over every row to answer a question the schema already answers.
    if not rows or column not in rows[0]:
        return ()
    return tuple(r[column] for r in rows[:limit])

