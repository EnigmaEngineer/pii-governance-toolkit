"""How many people a set of quasi identifiers picks out on its own.

The arithmetic lives here rather than in the probe because it decides a published claim,
and a rule in a report script cannot be falsified by a mutant.

Two answers, deliberately reported side by side.

`measured_unique_share` counts rows whose quasi identifier tuple is unique in the data.
That is a fact about the corpus and therefore about my generator.

`uniform_unique_share` predicts the same quantity assuming every combination is drawn
independently and uniformly from the product of the column cardinalities. That is a fact
about combinatorics and it knows nothing about my generator at all.

Neither number alone is worth much. The gap between them is, because it is the part that
comes from the data having a shape. If the two agreed exactly I would have built a corpus
that satisfies the prediction by construction and measured nothing, which is why the
generator skews its postal codes.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Sequence, Tuple


@dataclass(frozen=True)
class Uniqueness:
    n_rows: int
    n_columns: int
    distinct_combinations: int
    unique_rows: int
    k_anonymity: int
    measured_unique_share: float
    uniform_unique_share: float
    cardinality_product: int

    @property
    def shape_effect(self) -> float:
        """Measured minus predicted. Positive means the data's own shape makes people
        easier to pick out than a flat draw over the same cardinalities would."""
        return self.measured_unique_share - self.uniform_unique_share


def cardinalities(rows: Sequence[dict], columns: Sequence[str]) -> Dict[str, int]:
    if not columns:
        raise ValueError("no columns given, so there is nothing to be unique on")
    missing = [c for c in columns if rows and c not in rows[0]]
    if missing:
        raise KeyError("rows have no column {}".format(missing[0]))
    return {c: len({r[c] for r in rows}) for c in columns}


def uniform_unique_share(n_rows: int, product: int) -> float:
    """Expected share of rows sitting alone in their cell, under a uniform draw.

    A given row is alone when none of the other n-1 rows landed in its cell, which is
    (1 - 1/K) ** (n - 1). No approximation and no constant chosen by anybody.
    """
    if n_rows < 0:
        raise ValueError("n_rows must not be negative")
    if product < 1:
        raise ValueError("cardinality product must be at least 1")
    # Only the empty case needs a branch. A single row falls through to an exponent of
    # zero, which is 1.0 for any base, so the special case it used to carry was two lines
    # restating what the expression already does. A mutant proved it by surviving.
    if n_rows == 0:
        return 0.0
    return math.pow(1.0 - 1.0 / product, n_rows - 1)


def measure(rows: Sequence[dict], columns: Sequence[str]) -> Uniqueness:
    if not rows:
        raise ValueError("cannot measure uniqueness over zero rows")
    card = cardinalities(rows, columns)
    product = 1
    for c in columns:
        product *= card[c]

    counts = Counter(tuple(r[c] for c in columns) for r in rows)
    unique_rows = sum(n for n in counts.values() if n == 1)

    return Uniqueness(
        n_rows=len(rows),
        n_columns=len(columns),
        distinct_combinations=len(counts),
        unique_rows=unique_rows,
        k_anonymity=min(counts.values()),
        measured_unique_share=unique_rows / len(rows),
        uniform_unique_share=uniform_unique_share(len(rows), product),
        cardinality_product=product,
    )


def sweep(rows: Sequence[dict], columns: Sequence[str]) -> Tuple[Uniqueness, ...]:
    """Add one column at a time, in the order given.

    A single number for the full combination invites the reading that you have to hand
    over every column to be identifiable. The sweep is what shows how few it takes.
    """
    out = []
    for i in range(1, len(columns) + 1):
        out.append(measure(rows, columns[:i]))
    return tuple(out)


def generalise_postal(value: str, digits: int) -> str:
    """Truncate a postal code, which is the masking Safe Harbor actually permits.

    Refuses to widen a value rather than silently returning it unchanged, because a
    generaliser that quietly does nothing is how a masking policy reports success on a
    column it never touched.
    """
    if digits < 1:
        raise ValueError("digits must be at least 1")
    if digits > len(value):
        raise ValueError(
            "cannot generalise {} to {} digits, it has {}".format(
                value, digits, len(value))
        )
    return value[:digits] + "*" * (len(value) - digits)


def coarsen_date_to_year(value) -> int:
    """The other permitted generalisation. Year is not an identifier, a date is."""
    return value.year
