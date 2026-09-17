"""The HIPAA Safe Harbor identifier list, as an external answer key.

45 CFR 164.514(b)(2) enumerates eighteen classes of identifier that have to be removed
before a data set counts as de-identified under the Safe Harbor method. I did not write
this list and I cannot quietly widen it to match whatever my classifier happens to find,
which is the entire reason it is in the repo.

Every other answer key available to this repo would have been one I authored. The planted
labels on my own sample schema are mine. The regexes are mine. The column name heuristics
are mine. Grading any of those against each other measures nothing. This list is the one
input here that was decided by somebody else.

The `maps_to` field is my mapping and it is the part that can be wrong. It is separated
from the clause text on purpose so a reader can see which half is published and which half
is a judgement I made.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from pii.taxonomy import TAXONOMY


@dataclass(frozen=True)
class SafeHarborClass:
    """One lettered clause of the Safe Harbor list.

    `maps_to` is a tuple of taxonomy keys, empty where this taxonomy has no category for
    the clause. An empty tuple is a real answer and it is reported rather than hidden. A
    mapping table with no gaps in it is usually a table whose author widened the mapping
    until the gaps went away.

    It is a tuple rather than one key because the first version of this file used one key
    and that was wrong. Clause B names street address and city and postal code in a single
    sentence, and clause C names birth dates and admission dates and discharge dates in
    another. Writing one key each silently dropped `street_address` and `birth_date` off
    the list of things the regulation covers. Nothing looked wrong from the clause side.
    The reverse reading below is what found it.
    """

    letter: str
    clause: str
    maps_to: Tuple[str, ...] = ()
    gap_reason: str = ""

    def __post_init__(self) -> None:
        if len(self.letter) != 1 or not self.letter.isupper():
            raise ValueError("letter must be a single upper case character")
        if not self.clause:
            raise ValueError("clause {} must carry its text".format(self.letter))
        for key in self.maps_to:
            if key not in TAXONOMY:
                raise ValueError(
                    "clause {} maps to {} which is not in the taxonomy".format(
                        self.letter, key
                    )
                )
        if len(set(self.maps_to)) != len(self.maps_to):
            raise ValueError("clause {} repeats a category".format(self.letter))
        if not self.maps_to and not self.gap_reason:
            raise ValueError(
                "clause {} has no mapping and no stated reason".format(self.letter)
            )
        if self.maps_to and self.gap_reason:
            raise ValueError(
                "clause {} maps to a category and also states a gap reason".format(
                    self.letter
                )
            )


CLASSES: Tuple[SafeHarborClass, ...] = (
    SafeHarborClass("A", "Names", ("person_name",)),
    SafeHarborClass(
        "B",
        "Geographic subdivisions smaller than a state, except the first three digits "
        "of a postal code above a population floor",
        ("street_address", "postal_code"),
    ),
    SafeHarborClass(
        "C",
        "All elements of dates except year, for dates directly related to an "
        "individual, and all ages over 89",
        ("birth_date", "event_date"),
    ),
    SafeHarborClass("D", "Telephone numbers", ("phone",)),
    SafeHarborClass(
        "E",
        "Fax numbers",
        gap_reason="Folded into the phone category. A fax number and a telephone number "
                   "have the same shape and this taxonomy cannot separate them, so a "
                   "separate category would be a label nothing could ever assign.",
    ),
    SafeHarborClass("F", "Email addresses", ("email",)),
    SafeHarborClass("G", "Social security numbers", ("national_id",)),
    SafeHarborClass("H", "Medical record numbers", ("medical_record_number",)),
    SafeHarborClass("I", "Health plan beneficiary numbers", ("health_plan_id",)),
    SafeHarborClass("J", "Account numbers", ("account_number",)),
    SafeHarborClass("K", "Certificate and licence numbers", ("licence_number",)),
    SafeHarborClass(
        "L", "Vehicle identifiers and serial numbers including plate numbers",
        ("vehicle_id",)
    ),
    SafeHarborClass("M", "Device identifiers and serial numbers", ("device_id",)),
    SafeHarborClass("N", "Web URLs", ("web_url",)),
    SafeHarborClass("O", "Internet Protocol addresses", ("ip_address",)),
    SafeHarborClass("P", "Biometric identifiers including finger and voice prints",
                    ("biometric",)),
    SafeHarborClass("Q", "Full face photographs and comparable images", ("face_photo",)),
    SafeHarborClass(
        "R",
        "Any other unique identifying number, characteristic or code",
        gap_reason="Not a category. It is a catch all whose membership depends on the "
                   "data rather than on the column, so nothing in a metadata driven "
                   "taxonomy can represent it. Named here so the gap is on the record.",
    ),
)


def letters() -> Tuple[str, ...]:
    return tuple(c.letter for c in CLASSES)


def mapped_keys() -> Tuple[str, ...]:
    """Taxonomy keys reachable from the Safe Harbor list, deduplicated and sorted."""
    keys = set()
    for c in CLASSES:
        keys.update(c.maps_to)
    return tuple(sorted(keys))


def gaps() -> Tuple[SafeHarborClass, ...]:
    """Clauses this taxonomy has no category for."""
    return tuple(c for c in CLASSES if not c.maps_to)


# Clause B lets you keep the first three digits of a postal code only where the area those
# three digits cover holds more than this many people. The allowance is conditional and
# almost everybody quotes it as though it were not.
POSTAL_3_POPULATION_FLOOR = 20000


@dataclass(frozen=True)
class FloorReport:
    """Whether a population is large enough for clause B's three digit allowance to apply.

    This exists because of a number this repo nearly published. The probe measured what
    happens to re-identification after truncating a postal code to three digits, and was
    about to report it as what Safe Harbor permits. The corpus has a thousand people spread
    over a handful of three digit areas, which is two orders of magnitude under the floor
    the clause names, so the allowance does not apply to it at all. The measurement is
    correct arithmetic about the corpus and it is not evidence about the regulation.
    """

    groups: int
    smallest_group: int
    largest_group: int
    groups_meeting_floor: int
    floor: int

    @property
    def applies(self) -> bool:
        return self.groups > 0 and self.groups_meeting_floor == self.groups

    def why_not(self) -> str:
        if self.applies:
            return ""
        return (
            "{} of {} three digit areas hold fewer than {} people, smallest {}. "
            "The clause B allowance does not apply to this population.".format(
                self.groups - self.groups_meeting_floor, self.groups, self.floor,
                self.smallest_group)
        )


def postal_3_floor(values: Tuple[str, ...],
                   floor: int = POSTAL_3_POPULATION_FLOOR) -> FloorReport:
    if not values:
        raise ValueError("cannot judge a population floor over zero values")
    counts = {}
    for v in values:
        prefix = str(v)[:3]
        counts[prefix] = counts.get(prefix, 0) + 1
    sizes = sorted(counts.values())
    return FloorReport(
        groups=len(counts),
        smallest_group=sizes[0],
        largest_group=sizes[-1],
        groups_meeting_floor=sum(1 for s in sizes if s > floor),
        floor=floor,
    )


def hipaa_keys_not_reached() -> Tuple[str, ...]:
    """Categories tagged HIPAA that no Safe Harbor clause maps onto.

    Runs the comparison in the direction nobody runs it. A mapping table is normally read
    as "does every clause have a home", and the other reading is "does every category
    claiming this regime have a clause behind it". A category tagged HIPAA with no clause
    is a claim I made up.
    """
    from pii.taxonomy import Regime

    reached = set(mapped_keys())
    tagged = {c.key for c in TAXONOMY.under_regime(Regime.HIPAA)}
    return tuple(sorted(tagged - reached))
