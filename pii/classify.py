"""Classify a column, and say how sure the answer is.

`pii/naive.py` is the floor. It returns a category and nothing else, which means every
answer it gives arrives with the same authority. A column whose name is `ssn` and a column
that merely happens to hold ten digits come back looking identical to whoever reads the
output. A masking policy built on that either masks both or masks neither.

So this module returns a confidence with every answer, and the confidence is what decides
whether a column is masked automatically or queued for a person to look at. Three bands.
Accept, review, ignore. The review band is the point of the whole exercise.

Three arms feed it.

The name arm reads the column name as tokens rather than as a bag of substrings. First hit
wins in the naive version, which is how `attending_npi` came back as a telephone number:
the value regex for a phone matched ten digits and nothing outranked it. Here the arms
produce weighted evidence and the strongest category wins, so a name that says `npi`
outvotes a value shape that says phone.

The value arm never sees a value. It sends predicates down to the database and gets counts
back, so what crosses the boundary is `847 of 976 non null values match this pattern` and
never the 847 values. That is a real constraint rather than a decoration. It means this
classifier can only test a hypothesis it already holds and cannot discover a pattern nobody
thought of. The alternative is a scanner that pulls personal data out of the warehouse it
was pointed at in order to decide whether that data is personal, and I would rather give up
discovery than ship that.

The null rate enters the confidence explicitly. The floor drops nulls before taking its
majority, so a column that is sixty percent null and forty percent valid email addresses
comes back as an email column on exactly the same evidence as one that is complete. Both
readings are here. The match rate is over the non null values because that is the question
the pattern answers, and the support is carried beside it because a claim about forty
percent of a column is a weaker claim about the column.

The structure arm reads the catalog. Type, cardinality, key flag. It is the only arm that
can reach a column whose name says nothing and whose values match no pattern, which on the
sample warehouse is most of the timestamps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from pii.taxonomy import TAXONOMY, Identifiability, granularity_family

# Hand chosen, not fitted. Both are swept in scripts/classify_probe.py and the sweep is
# published, because a headline defined by a threshold is a fact about the threshold until
# somebody moves it and looks.
ACCEPT_AT = 0.75
REVIEW_AT = 0.35


class Band(Enum):
    """What happens to the column next."""

    ACCEPT = "accept"
    REVIEW = "review"
    IGNORE = "ignore"


class Arm(Enum):
    NAME = "name"
    VALUE = "value"
    STRUCTURE = "structure"


@dataclass(frozen=True)
class Signal:
    """One piece of evidence pointing at one category.

    `strength` is how much this arm alone believes it, in 0 to 1. `support` is the share of
    the column the arm got to look at. For the name and structure arms that is always 1,
    because a column name is fully observed. For the value arm it is the non null share.
    """

    arm: Arm
    category_key: str
    strength: float
    detail: str
    support: float = 1.0

    def __post_init__(self) -> None:
        if self.category_key not in TAXONOMY:
            raise ValueError("signal names {} which is not in the taxonomy".format(
                self.category_key))
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError("strength out of range: {}".format(self.strength))
        if not 0.0 <= self.support <= 1.0:
            raise ValueError("support out of range: {}".format(self.support))
        if not self.detail:
            raise ValueError("a signal with no detail cannot be reviewed by anybody")

    @property
    def weight(self) -> float:
        """Strength discounted by how much of the column the arm actually saw."""
        return self.strength * self.support


@dataclass(frozen=True)
class ColumnProfile:
    """Counts about one column. Deliberately holds no value from that column.

    Everything here is either catalog metadata or an integer the database computed. A
    reviewer reading a profile learns the shape of the column and learns nothing about any
    person in it. `tests/test_classify.py` asserts the no values property by walking the
    dataclass rather than by trusting this paragraph.
    """

    table: str
    column: str
    sql_type: str
    nullable: bool
    is_key: bool
    rows: int
    non_null: int
    distinct: int
    mean_length: float
    pattern_hits: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.rows < 0 or self.non_null < 0:
            raise ValueError("negative row count for {}".format(self.address))
        if self.non_null > self.rows:
            raise ValueError(
                "{} has more non null values than rows".format(self.address))
        if self.distinct > self.non_null:
            raise ValueError(
                "{} has more distinct values than non null ones".format(self.address))

    @property
    def address(self) -> str:
        return "{}.{}".format(self.table, self.column)

    @property
    def support(self) -> float:
        """Share of the column a value arm gets to read.

        Zero rows gives zero support rather than a division error, and zero support means
        the value arm produces nothing at all. A pattern that matched every value of an
        empty column is the shape of a check that passes on nothing.
        """
        if self.rows == 0:
            return 0.0
        return self.non_null / self.rows

    @property
    def distinct_ratio(self) -> float:
        if self.non_null == 0:
            return 0.0
        return self.distinct / self.non_null


# Tokens, matched against the column name split on underscores and camel case boundaries.
# Written to cover the sample warehouse and the obvious neighbours of each entry, which
# means it is the same kind of hand list the floor carries. The difference is that a token
# here can be weak on purpose. `name` sits at 0.4 because `payer_name` is the name of an
# insurance company, and a rule that cannot tell those apart should say so in its number
# rather than by being left out.
TOKEN_RULES: Tuple[Tuple[Tuple[str, ...], str, float, str], ...] = (
    (("ssn", "socialsecurity"), "national_id", 0.95, "names a national identifier"),
    (("email", "mail"), "email", 0.90, "names an email address"),
    (("mrn",), "medical_record_number", 0.90, "names a medical record number"),
    (("dob", "birthdate", "birthday"), "birth_date", 0.90, "names a birth date"),
    (("postal", "postcode", "zip", "zipcode"), "postal_code", 0.90, "names a postal code"),
    (("phone", "mobile", "telephone", "fax"), "phone", 0.85, "names a telephone number"),
    (("address", "street"), "street_address", 0.85, "names a street address"),
    (("sex", "gender"), "sex", 0.85, "names a sex or gender column"),
    (("card", "pan", "ccn"), "payment_card", 0.85, "names a payment card"),
    (("diagnosis", "dx", "condition"), "health_condition", 0.80, "names a diagnosis"),
    (("ip", "ipaddr"), "ip_address", 0.80, "names an IP address"),
    (("npi", "licence", "license"), "licence_number", 0.80, "names a licence number"),
    (("serial", "imei", "device"), "device_id", 0.75, "names a device"),
    (("member", "subscriber", "beneficiary"), "health_plan_id", 0.70,
     "names a health plan membership"),
    (("note", "notes", "comment", "remarks"), "free_text_clinical", 0.60,
     "names free text"),
    (("city", "town", "locality"), "postal_code", 0.60,
     "names a geographic subdivision smaller than a state"),
    (("birth",), "birth_date", 0.60, "carries a birth token"),
    # A qualified name is a person's name and a bare one is not. `payer_name` holds the
    # name of an insurance company and `first_name` holds a person, and the only thing
    # separating them is the word in front. These are the joined forms, which the matcher
    # compares against the tokens run together, so `first_name` and `firstName` both land.
    (("firstname", "lastname", "givenname", "familyname", "middlename", "fullname",
      "maidenname", "surname", "legalname", "patientname"), "person_name", 0.90,
     "names a person"),
    (("name",), "person_name", 0.40,
     "carries a bare name token, which companies have too"),
)

# A trailing token that makes a column temporal by name. Weak on purpose. Four columns in
# the sample warehouse end this way and one of them is the row's load time, which is not a
# date tied to any individual. No reading of the name separates them.
TEMPORAL_SUFFIXES: Tuple[str, ...] = ("at", "on", "date", "time", "ts", "timestamp")
TEMPORAL_NAME_STRENGTH = 0.50

TEMPORAL_TYPES: Tuple[str, ...] = ("DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE",
                                   "TIMESTAMP_NS", "TIMESTAMP_MS", "TIMESTAMP_S")
TEMPORAL_TYPE_STRENGTH = 0.45

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenise(column_name: str) -> Tuple[str, ...]:
    """Split a column name into lower case tokens.

    Underscores, hyphens, spaces and camel case boundaries. Tokens rather than substrings
    because a substring scan says `ip` is inside `disposition` and `recipient`, and the
    floor's first hit rule then hands the whole column to the wrong category with nothing
    recording that it was close.
    """
    spaced = _CAMEL.sub(" ", column_name)
    return tuple(t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t)


def name_signals(column_name: str) -> Tuple[Signal, ...]:
    """Evidence from the column name alone.

    Every rule that fires produces a signal. They are not collapsed here, because two rules
    naming two categories is the disagreement the confidence is supposed to record and
    picking a winner this early throws it away.
    """
    tokens = tokenise(column_name)
    joined = "".join(tokens)
    out: List[Signal] = []
    for needles, key, strength, why in TOKEN_RULES:
        hit = next((n for n in needles if n in tokens or n == joined), None)
        if hit is not None:
            out.append(Signal(Arm.NAME, key, strength,
                              "token {!r} {}".format(hit, why)))
    if tokens and tokens[-1] in TEMPORAL_SUFFIXES:
        out.append(Signal(Arm.NAME, "event_date", TEMPORAL_NAME_STRENGTH,
                          "ends in {!r}, which reads as a date or a time".format(
                              tokens[-1])))
    return tuple(out)


# Each entry is a SQL boolean over one column. The database evaluates it and counts it and
# returns an integer. The expression takes the quoted column name twice at most and never
# returns a row.
VALUE_PREDICATES: Tuple[Tuple[str, str, float, str], ...] = (
    ("email",
     r"regexp_matches(cast({c} as varchar), '^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{{2,}}$')",
     0.90, "matches an email address"),
    ("national_id",
     r"regexp_matches(cast({c} as varchar), '^[0-9]{{3}}-[0-9]{{2}}-[0-9]{{4}}$')",
     0.90, "matches a national identifier"),
    ("ip_address",
     r"regexp_matches(cast({c} as varchar), '^[0-9]{{1,3}}(\.[0-9]{{1,3}}){{3}}$')",
     0.85, "matches an IPv4 address"),
    # The floor's phone pattern is digits and dashes of roughly the right length, which is
    # also the shape of an ISO date. Stripping the separators and counting the digits is
    # what makes the two different: a date has eight and a phone number has ten or more.
    ("phone",
     r"regexp_matches(regexp_replace(cast({c} as varchar), '[-. ()]', '', 'g'),"
     r" '^\+?[0-9]{{10,15}}$')",
     0.70, "has ten to fifteen digits once separators are removed"),
    ("payment_card",
     r"regexp_matches(regexp_replace(cast({c} as varchar), '[- ]', '', 'g'),"
     r" '^[0-9]{{13,19}}$')",
     0.75, "has the digit count of a payment card"),
    # A vocabulary the classifier brings with it rather than one it learns. That is the
    # cost of never reading a value: the question has to be asked before the answer can
    # come back, so a coding this list does not hold is invisible.
    #
    # It sits under the accept threshold on purpose, which is a deliberate difference from
    # every other predicate here. Matching six short strings is much weaker evidence than
    # matching an email address, because any single letter categorical column matches it.
    # A fixture of M and F under a column called `v` was being masked with no human in the
    # loop on this one signal. At 0.70 it still reaches a reviewer and no longer decides
    # anything by itself, and a column that is also named `sex` clears the bar easily.
    ("sex",
     "lower(cast({c} as varchar)) in ('m', 'f', 'x', 'male', 'female', 'other',"
     " 'unknown')",
     0.70, "holds values from a sex vocabulary"),
)

VALUE_FLOOR = 0.60


def value_signals(profile: ColumnProfile) -> Tuple[Signal, ...]:
    """Evidence from the counts the database returned.

    The match rate is over the non null values, because `does this value look like an email
    address` has no answer for a value that is not there. The support then carries the null
    share into the confidence separately, so the two facts stay apart. Folding them into one
    number by counting nulls as failures would mean a column of perfectly formed email
    addresses that happens to be half empty scores below the floor and is never flagged at
    all, which is the wrong direction for a tool whose job is to find personal data.
    """
    if profile.non_null == 0:
        return ()
    out: List[Signal] = []
    for key, _expr, strength, why in VALUE_PREDICATES:
        hits = profile.pattern_hits.get(key)
        if hits is None:
            continue
        rate = hits / profile.non_null
        if rate < VALUE_FLOOR:
            continue
        out.append(Signal(
            Arm.VALUE, key, strength * rate,
            "{} of {} non null values {} ({:.0%} of the column is populated)".format(
                hits, profile.non_null, why, profile.support),
            support=profile.support,
        ))
    if profile.mean_length >= 40 and profile.distinct_ratio >= 0.02:
        out.append(Signal(
            Arm.VALUE, "free_text_clinical", 0.55,
            "mean value length {:.0f} characters, which is prose rather than a code"
            .format(profile.mean_length),
            support=profile.support,
        ))
    return tuple(out)


def structure_signals(profile: ColumnProfile) -> Tuple[Signal, ...]:
    """Evidence from the catalog.

    One rule, and it is the only thing in here that reaches a column whose name says
    nothing and whose values match no pattern. A surrogate key is excluded because a
    primary key of a table is the warehouse's own invention rather than a fact about a
    person, and every one in the sample schema is planted as not personal.
    """
    if profile.is_key:
        return ()
    base = profile.sql_type.upper().split("(")[0].strip()
    if base in TEMPORAL_TYPES:
        return (Signal(Arm.STRUCTURE, "event_date", TEMPORAL_TYPE_STRENGTH,
                       "catalog type {} is temporal".format(profile.sql_type)),)
    return ()


def _combine(strengths: Sequence[float]) -> float:
    """Noisy or over independent evidence for one category.

    Two arms agreeing should be worth more than either alone and must not exceed one.
    Adding and clipping does both of those and hides which side the ceiling came from, so
    this uses the complement of the product instead. The independence assumption is not
    true here, since a column named `email` is also likely to hold email addresses, and the
    effect is that agreement is worth slightly more than it should be. Stated rather than
    corrected, because correcting it needs a correlation nobody has measured.
    """
    remaining = 1.0
    for s in strengths:
        remaining *= (1.0 - s)
    return 1.0 - remaining


CONFLICT_PENALTY = 0.5


def two_readings_of_one_fact(first: str, second: str) -> bool:
    """Whether two categories are the same finding at two coarsenesses.

    `birth_date` and `event_date` both fire on a column of birth dates, and there is
    nothing for a reviewer to arbitrate between them: one is the other read at a coarser
    grain and both are quasi identifiers recorded to the day. Penalising that pair would
    charge the classifier for agreeing with itself.

    The first version asked whether the two shared an identifiability level, which is the
    wrong test and a check caught it. `licence_number` and `phone` are both direct, so a
    ten digit clinician number matching the telephone predicate was waved through with no
    penalty at all. Two direct identifiers are not two readings of one fact. They are two
    different categories carrying two different masking policies, which is the disagreement
    a review queue exists to settle. The granularity family is the real test, because only
    a category carrying a threshold has a coarser form to be confused with.
    """
    a = TAXONOMY.get(first).identifies_at
    b = TAXONOMY.get(second).identifies_at
    if a is None or b is None:
        return False
    return granularity_family(a) == granularity_family(b)


@dataclass(frozen=True)
class Classification:
    """The answer for one column, with everything that produced it."""

    table: str
    column: str
    category_key: str
    confidence: float
    signals: Tuple[Signal, ...]
    runner_up: Optional[str] = None
    runner_up_score: float = 0.0

    @property
    def address(self) -> str:
        return "{}.{}".format(self.table, self.column)

    @property
    def band(self) -> Band:
        if self.category_key == "not_personal":
            return Band.IGNORE
        if self.confidence >= ACCEPT_AT:
            return Band.ACCEPT
        if self.confidence >= REVIEW_AT:
            return Band.REVIEW
        return Band.IGNORE

    @property
    def flagged(self) -> bool:
        """Anything a person or a policy will act on. Both bands above ignore."""
        return self.band is not Band.IGNORE

    @property
    def auto_masked(self) -> bool:
        return self.band is Band.ACCEPT

    @property
    def identifiability(self) -> Identifiability:
        return TAXONOMY.get(self.category_key).identifiability

    def explain(self) -> str:
        """The answer and everything that produced it, for whoever works the queue.

        Four decimals rather than two, because the band turns on the fourth. At two a
        column on 0.7499 and one on 0.7500 both print as 0.75 and land in different
        bands, and the reviewer reading that concludes the tool is broken. The precision
        a number is shown at has to be at least the precision it is decided at.
        """
        lines = ["{} -> {} at {:.4f} ({})".format(
            self.address, self.category_key, self.confidence, self.band.value)]
        for s in self.signals:
            lines.append("    {:<9} {:<22} {:.4f}  {}".format(
                s.arm.value, s.category_key, s.weight, s.detail))
        if self.runner_up:
            lines.append("    runner up {} at {:.4f}".format(
                self.runner_up, self.runner_up_score))
        return "\n".join(lines)


def classify_column(profile: ColumnProfile) -> Classification:
    """One column, on its own evidence, with no knowledge of its neighbours."""
    signals = (
        name_signals(profile.column)
        + value_signals(profile)
        + structure_signals(profile)
    )
    return _decide(profile, signals)


def _decide(profile: ColumnProfile, signals: Tuple[Signal, ...]) -> Classification:
    if not signals:
        return Classification(profile.table, profile.column, "not_personal", 0.0, ())

    by_category: Dict[str, List[float]] = {}
    for s in signals:
        by_category.setdefault(s.category_key, []).append(s.weight)

    scored = sorted(
        ((key, _combine(weights)) for key, weights in by_category.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    best_key, best_score = scored[0]
    runner_up, runner_up_score = (scored[1] if len(scored) > 1 else (None, 0.0))

    # Two categories both scoring well is the case a review queue exists for. The penalty
    # is proportional to the runner up rather than fixed, so a close second costs more than
    # a distant one, and it is waived only where the two are the same finding read at two
    # grains. See `two_readings_of_one_fact` for why that is the test.
    confidence = best_score
    if runner_up is not None and not two_readings_of_one_fact(best_key, runner_up):
        confidence = best_score * (1.0 - CONFLICT_PENALTY * runner_up_score)

    return Classification(
        table=profile.table,
        column=profile.column,
        category_key=best_key,
        confidence=round(confidence, 4),
        signals=signals,
        runner_up=runner_up,
        runner_up_score=round(runner_up_score, 4),
    )


def table_is_person_linked(results: Sequence[Classification],
                           evidence_bar: float = REVIEW_AT) -> bool:
    """Whether anything in this table names a person outright.

    A temporal column is a Safe Harbor identifier when the date is tied to an individual
    and is not one otherwise, and no property of the column itself carries that. What the
    table holds is the cheapest thing standing in for it: a table with a direct identifier
    in it is describing people, and a table without one may be describing anything.

    The bar defaults to the review floor rather than the accept floor, and that default is
    the whole argument. Set at accept, `raw.claim` came back as a table about nobody,
    because its only direct identifier is a health plan membership number scoring 0.70,
    which is five hundredths under. One column sitting in the review band then deleted a
    different column's only evidence and `submitted_on` fell out of the results entirely.
    Demanding certainty that a table is about people before conceding that it is runs the
    wrong way for a governance tool. `scripts/classify_probe.py` sweeps the bar and prints
    where the cliff sits.

    This reads the classifier's own output, so it inherits whatever the classifier got
    wrong about the direct identifiers. That is a real dependency and not a hedge. It is
    also strictly weaker than following the column back to its source, which is what would
    actually answer the question.
    """
    return any(
        r.identifiability is Identifiability.DIRECT and r.confidence >= evidence_bar
        for r in results
    )


def classify_table(profiles: Sequence[ColumnProfile],
                   evidence_bar: float = REVIEW_AT) -> Tuple[Classification, ...]:
    """Two passes. Columns alone, then the temporal ones again with the table in view.

    The second pass only ever removes evidence. A temporal signal in a table that names
    nobody is dropped, and nothing is added anywhere, so a column this function flags is a
    column `classify_column` flagged too.
    """
    if not profiles:
        return ()
    tables = {p.table for p in profiles}
    if len(tables) != 1:
        raise ValueError(
            "classify_table takes one table at a time, got {}".format(sorted(tables)))

    first = tuple(classify_column(p) for p in profiles)
    if table_is_person_linked(first, evidence_bar):
        return first

    out: List[Classification] = []
    for profile, result in zip(profiles, first):
        temporal = tuple(s for s in result.signals if s.category_key == "event_date")
        if not temporal:
            out.append(result)
            continue
        kept = tuple(s for s in result.signals if s.category_key != "event_date")
        out.append(_decide(profile, kept))
    return tuple(out)


def classify_warehouse(
    profiles: Sequence[ColumnProfile],
    evidence_bar: float = REVIEW_AT,
) -> Tuple[Classification, ...]:
    """Every column, grouped by table so the second pass has something to read."""
    grouped: Dict[str, List[ColumnProfile]] = {}
    for p in profiles:
        grouped.setdefault(p.table, []).append(p)
    out: List[Classification] = []
    for table in sorted(grouped):
        out.extend(classify_table(grouped[table], evidence_bar))
    return tuple(out)


def band_counts(results: Sequence[Classification]) -> Dict[str, int]:
    counts = {b.value: 0 for b in Band}
    for r in results:
        counts[r.band.value] += 1
    return counts


def rule_coverage() -> Dict[str, Tuple[bool, bool]]:
    """Per category, whether any arm can ever return it.

    A taxonomy entry with no rule behind it is a category this classifier cannot produce
    under any input. That is a fair thing for a taxonomy to hold, since its job is to name
    what a governance tool has to be able to express and not what this one detects today.
    It stops being fair the moment nobody says which is which, and a reader counting
    categories has no way to tell an entry that is waiting for a rule from one that is
    covered.

    So the tool reports it. Two booleans per category, derived from the rule tables rather
    than from a list somebody maintains beside them, because a hand written list covers
    what its author remembered.
    """
    name_keys = {key for _needles, key, _s, _w in TOKEN_RULES}
    value_keys = {key for key, _expr, _s, _w in VALUE_PREDICATES}
    name_keys.add("event_date")
    value_keys.add("free_text_clinical")
    return {key: (key in name_keys, key in value_keys) for key in TAXONOMY.keys()}


def undetectable_categories() -> Tuple[str, ...]:
    """Categories no arm can return. Not a defect, and not something to leave silent."""
    return tuple(sorted(
        key for key, (by_name, by_value) in rule_coverage().items()
        if not by_name and not by_value and key != "not_personal"
    ))
