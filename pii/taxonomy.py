"""The classification taxonomy.

Two axes, not one. Most PII tooling I have used carries a single label per column, some
variation on EMAIL or SSN or NONE, and then hangs the masking policy off that label. That
works right up until you try to write the policy down, at which point the label turns out
not to contain the thing the policy needs.

The two axes here are:

`Identifiability` says what the column does on its own. A direct identifier names a person
by itself. A quasi identifier names nobody alone and names somebody in combination with
other quasi identifiers. A sensitive attribute is not an identifier at all, it is the thing
you are trying to keep private once somebody has been identified. Those three need
different masking and a single label cannot express the difference.

`Regime` says who is asking. A column can be a HIPAA identifier and not a GDPR special
category, or both, or neither. These are not points on one scale and there is no honest way
to sort them into an ordering, so the taxonomy stores a set rather than a level.

The third piece is granularity. Some categories are only identifiers above a threshold. A
date is an identifier at day precision and is not one at year precision. A postal code is
an identifier at five digits and is not one at three. A taxonomy with no granularity field
has to answer yes or no to `zip_code` and both answers are wrong.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Dict, FrozenSet, Optional, Tuple


class Identifiability(Enum):
    """What a column does on its own."""

    DIRECT = "direct"
    QUASI = "quasi"
    SENSITIVE = "sensitive"
    NONE = "none"


class Regime(Enum):
    """Which rulebook claims the column.

    Deliberately a membership set rather than a severity level. HIPAA and GDPR disagree
    about what matters and neither is a superset of the other.
    """

    HIPAA = "hipaa"
    GDPR_SPECIAL = "gdpr_special"
    PCI = "pci"


class Granularity(Enum):
    """The unit a value is recorded in, where that decides identifiability.

    NOT_APPLICABLE is the honest answer for most categories. An email address has no
    coarser form that stops being an email address.
    """

    NOT_APPLICABLE = "not_applicable"
    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    SECOND = "second"
    POSTAL_3 = "postal_3"
    POSTAL_5 = "postal_5"
    POSTAL_FULL = "postal_full"


# Ordered coarsest to finest, per family. A category carrying a threshold is an identifier
# at or finer than that threshold and is not one above it. Two families never compare.
_GRANULARITY_ORDER: Dict[str, Tuple[Granularity, ...]] = {
    "temporal": (
        Granularity.YEAR,
        Granularity.MONTH,
        Granularity.DAY,
        Granularity.SECOND,
    ),
    "postal": (
        Granularity.POSTAL_3,
        Granularity.POSTAL_5,
        Granularity.POSTAL_FULL,
    ),
}


def granularity_family(g: Granularity) -> Optional[str]:
    for name, members in _GRANULARITY_ORDER.items():
        if g in members:
            return name
    return None


def at_least_as_fine(value: Granularity, threshold: Granularity) -> bool:
    """True when `value` carries at least as much detail as `threshold`.

    Raises on a cross family comparison rather than returning False. A postal code is
    neither finer nor coarser than a timestamp and a function that answers that question
    with a bool is lying to whoever called it.
    """
    vf = granularity_family(value)
    tf = granularity_family(threshold)
    if vf is None or tf is None:
        raise ValueError(
            "granularity {} or {} has no ordering".format(value.value, threshold.value)
        )
    if vf != tf:
        raise ValueError(
            "cannot compare {} against {}, different families".format(
                value.value, threshold.value
            )
        )
    order = _GRANULARITY_ORDER[vf]
    return order.index(value) >= order.index(threshold)


@dataclass(frozen=True)
class Category:
    """One entry in the taxonomy.

    `identifies_at` is the coarsest granularity at which this category is still an
    identifier. None means granularity does not enter into it.
    """

    key: str
    label: str
    identifiability: Identifiability
    regimes: FrozenSet[Regime] = field(default_factory=frozenset)
    identifies_at: Optional[Granularity] = None
    note: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("category key must not be empty")
        if self.key != self.key.lower():
            raise ValueError("category key must be lowercase: {}".format(self.key))
        if " " in self.key:
            raise ValueError("category key must not contain a space: {}".format(self.key))
        if not self.label:
            raise ValueError("category {} must carry a label".format(self.key))
        if self.identifiability is Identifiability.NONE and self.regimes:
            raise ValueError(
                "category {} is identifiability NONE and claims a regime, "
                "which is a contradiction".format(self.key)
            )
        if self.identifies_at is not None:
            if granularity_family(self.identifies_at) is None:
                raise ValueError(
                    "category {} has an unorderable threshold {}".format(
                        self.key, self.identifies_at.value
                    )
                )
            if self.identifiability is Identifiability.NONE:
                raise ValueError(
                    "category {} carries a granularity threshold and is "
                    "identifiability NONE".format(self.key)
                )

    def identifies(self, observed: Granularity) -> bool:
        """Whether a column of this category, recorded at `observed`, is an identifier.

        A category with no threshold ignores the argument. That is not an oversight, it is
        the case where granularity is not the deciding variable.
        """
        if self.identifiability is Identifiability.NONE:
            return False
        if self.identifies_at is None:
            return True
        return at_least_as_fine(observed, self.identifies_at)


class Taxonomy:
    """An immutable set of categories addressed by key."""

    def __init__(self, categories: Tuple[Category, ...]):
        if not categories:
            raise ValueError("a taxonomy with no categories cannot classify anything")
        seen = {}
        for c in categories:
            if c.key in seen:
                raise ValueError("duplicate category key: {}".format(c.key))
            seen[c.key] = c
        self._by_key: Dict[str, Category] = seen
        self._categories = tuple(categories)

    def __len__(self) -> int:
        return len(self._categories)

    def __iter__(self):
        return iter(self._categories)

    def __contains__(self, key: object) -> bool:
        return key in self._by_key

    def get(self, key: str) -> Category:
        if key not in self._by_key:
            raise KeyError("no such category: {}".format(key))
        return self._by_key[key]

    def keys(self) -> Tuple[str, ...]:
        return tuple(sorted(self._by_key))

    def with_identifiability(self, level: Identifiability) -> Tuple[Category, ...]:
        return tuple(c for c in self._categories if c.identifiability is level)

    def under_regime(self, regime: Regime) -> Tuple[Category, ...]:
        return tuple(c for c in self._categories if regime in c.regimes)

    def fingerprint(self) -> str:
        """Content hash over the whole taxonomy.

        Lineage propagation and policy generation both read a classification back
        later. Both need to say which taxonomy they were built against, and a version
        number is a thing somebody forgets to bump. Truncated to 12 hex characters, which
        is enough to name a document and short enough to sit in a report line.
        """
        payload = []
        for c in sorted(self._categories, key=lambda x: x.key):
            row = {}
            for f in fields(Category):
                v = getattr(c, f.name)
                if isinstance(v, Enum):
                    row[f.name] = v.value
                elif isinstance(v, frozenset):
                    row[f.name] = sorted(r.value for r in v)
                else:
                    row[f.name] = v
            payload.append(row)
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _c(key, label, ident, regimes=(), identifies_at=None, note=""):
    return Category(
        key=key,
        label=label,
        identifiability=ident,
        regimes=frozenset(regimes),
        identifies_at=identifies_at,
        note=note,
    )


# The shipped taxonomy, chosen so that almost every HIPAA Safe Harbor clause has somewhere
# to land and so that the quasi identifiers a re-identification attack actually uses are
# present as their own entries rather than folded into NONE. The count is not written here
# on purpose. A count in a comment is a number nothing re-derives, and `len(TAXONOMY)` is
# one call away.
CATEGORIES: Tuple[Category, ...] = (
    # Direct identifiers. Each one names a person without help.
    _c("person_name", "Person name", Identifiability.DIRECT, [Regime.HIPAA]),
    _c("email", "Email address", Identifiability.DIRECT, [Regime.HIPAA]),
    _c("phone", "Telephone number", Identifiability.DIRECT, [Regime.HIPAA]),
    _c("street_address", "Street address", Identifiability.DIRECT, [Regime.HIPAA]),
    _c("national_id", "National identification number", Identifiability.DIRECT,
       [Regime.HIPAA]),
    _c("medical_record_number", "Medical record number", Identifiability.DIRECT,
       [Regime.HIPAA]),
    _c("health_plan_id", "Health plan beneficiary number", Identifiability.DIRECT,
       [Regime.HIPAA]),
    _c("account_number", "Account number", Identifiability.DIRECT, [Regime.HIPAA]),
    _c("payment_card", "Payment card number", Identifiability.DIRECT, [Regime.PCI]),
    _c("licence_number", "Certificate or licence number", Identifiability.DIRECT,
       [Regime.HIPAA]),
    _c("vehicle_id", "Vehicle identifier or plate", Identifiability.DIRECT, [Regime.HIPAA]),
    _c("device_id", "Device serial number", Identifiability.DIRECT, [Regime.HIPAA]),
    _c("web_url", "Personal URL", Identifiability.DIRECT, [Regime.HIPAA]),
    _c("ip_address", "IP address", Identifiability.DIRECT, [Regime.HIPAA]),
    _c("biometric", "Biometric identifier", Identifiability.DIRECT,
       [Regime.HIPAA, Regime.GDPR_SPECIAL]),
    _c("face_photo", "Full face photograph", Identifiability.DIRECT,
       [Regime.HIPAA, Regime.GDPR_SPECIAL]),

    # Quasi identifiers. None of these names anybody. Together they name almost everybody,
    # which is the whole reason the axis exists.
    _c("birth_date", "Date of birth", Identifiability.QUASI, [Regime.HIPAA],
       identifies_at=Granularity.DAY,
       note="A birth year alone is not an identifier. A birth date is."),
    _c("postal_code", "Postal code", Identifiability.QUASI, [Regime.HIPAA],
       identifies_at=Granularity.POSTAL_5,
       note="Safe Harbor permits the first three digits above a population floor."),
    _c("event_date", "Date tied to an individual", Identifiability.QUASI, [Regime.HIPAA],
       identifies_at=Granularity.DAY,
       note="Admission, discharge, service and death dates. Year is permitted."),
    _c("sex", "Sex or gender", Identifiability.QUASI),
    _c("occupation", "Occupation", Identifiability.QUASI),

    # Sensitive attributes. Not identifiers. These are what an attacker wants once the
    # quasi identifiers have told them which row is yours.
    _c("health_condition", "Diagnosis or condition", Identifiability.SENSITIVE,
       [Regime.GDPR_SPECIAL]),
    _c("free_text_clinical", "Free text clinical note", Identifiability.SENSITIVE,
       [Regime.GDPR_SPECIAL],
       note="Unstructured, so its contents are not knowable from the schema."),

    # The absence of a finding. Present so that a classification is always a category and
    # never a None the caller has to remember to handle.
    _c("not_personal", "Not personal data", Identifiability.NONE),
)

TAXONOMY = Taxonomy(CATEGORIES)
