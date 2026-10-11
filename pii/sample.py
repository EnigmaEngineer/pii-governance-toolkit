"""The arm that reads values, and the premise it gives up in order to run.

`pii/profile.py` opens by stating that no value from a user column is ever returned to
Python. Everything else in this repo rests on that. The predicates go down to the database
and integers come back, which means the classifier can only ever test a hypothesis it
already holds. A passport number in a format nobody wrote down returns zero on every
predicate and the column falls out of the results entirely.

This module is the measurement of what that costs, and it costs the premise. There is no
version of a sampling arm that keeps it. Values arrive in the process, in a traceback if
one is raised, and in whatever the caller does next. So the arm is off by default and
`classify.ALL_ARMS` does not contain it. Turning it on is a deliberate act by somebody who
has decided the discovery is worth more than the guarantee on the warehouse in front of
them, and the shipped tool never makes that decision for them.

What crosses the boundary once it is on is narrower than a value and wider than an integer.
A shape replaces every letter with `A` and every digit with `9` and keeps a separator from a
short allowlist, so `AB1234567` arrives as `AA9999999`. That is a projection and not a
redaction. A column of one distinct value arrives as one shape of the right length, and the
punctuation in it survives verbatim. `samples_hold_no_values` checks the allowlist rather
than trusting this paragraph, and the honest summary is that the no values property is now a
no values modulo a projection property. That is weaker and it is the price.

What the arm can do that a predicate cannot is say that a column holds a code without
holding a rule for that kind of code. Fixed shape, high cardinality, a length in the range
identifiers live in. That answer is `unknown_identifier` and it is deliberately the weakest
direct finding in the taxonomy. It reaches a reviewer and it can never auto mask, because a
tool that cannot name what it found has no business deciding what happens to it.

It also brings two hypotheses with it, which is worth admitting rather than burying. A Luhn
check is a payment card hypothesis. The date template list is a calendar hypothesis. A
genuinely hypothesis free arm was not reachable, because a timestamp cast to text is a fixed
shape high cardinality code by every structural test this module applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from pii.classify import REVIEW_AT, Arm, ColumnProfile, Signal
from pii.crawl import _quote

# Characters a shape is allowed to keep verbatim. Everything alphabetic becomes `A` and
# everything numeric becomes `9`. Anything outside this list becomes `?` rather than
# passing through, because the allowlist is the only thing bounding what a shape leaks and
# a default of pass through makes it unbounded.
SHAPE_SEPARATORS: str = "-./:+()@,#_ "
SHAPE_ALPHA = "A"
SHAPE_DIGIT = "9"
SHAPE_OTHER = "?"

# How much of the sample has to share one shape before the column counts as fixed format.
SHAPE_CONCENTRATION = 0.90

# A code is a code because almost every row differs. A fixed shape column of low
# cardinality is a categorical, and `raw.patient.sex` is the example that made this
# necessary rather than tidy.
IDENTIFIER_DISTINCT = 0.90

IDENTIFIER_MIN_LENGTH = 6
IDENTIFIER_MAX_LENGTH = 24

# A code with no letter in it has to be long enough that a year, a quantity or a small
# count cannot reach it. Eight digits is a date and four is a year, which is the argument
# for nine. `ot-129` is the thread that says an argument is not a sweep.
IDENTIFIER_DIGIT_FLOOR = 9

@dataclass(frozen=True)
class Thresholds:
    """The four numbers the shape rule turns on, in one object so a sweep can move them.

    `ot-129` is why this exists. Four module globals chosen by looking at one fixture, and
    `scripts/classify_probe.py` sweeps the band thresholds and publishes where the cliff
    sits while these got none of that. A sweep over a module global means rebinding it,
    which is the pattern day 1 removed from the ablation harness because a rebind cannot be
    asserted on and leaves the caller unsure which version ran.

    One parameter and not four. `ot-128` is the standing warning about widening a signature
    for a measurement, and the shape that respects it is a named object the default of which
    is the shipped configuration.
    """

    concentration: float = SHAPE_CONCENTRATION
    distinct: float = IDENTIFIER_DISTINCT
    min_length: int = IDENTIFIER_MIN_LENGTH
    max_length: int = IDENTIFIER_MAX_LENGTH
    digit_floor: int = IDENTIFIER_DIGIT_FLOOR

    def __post_init__(self) -> None:
        if not 0.0 < self.concentration <= 1.0:
            raise ValueError("concentration {} is not a share".format(self.concentration))
        if not 0.0 < self.distinct <= 1.0:
            raise ValueError("distinct {} is not a share".format(self.distinct))
        if self.min_length > self.max_length:
            raise ValueError("length range {}..{} is empty".format(
                self.min_length, self.max_length))


SHIPPED = Thresholds()


# Deliberately under `classify.ACCEPT_AT`. A finding the tool cannot name must reach a
# person and must never reach a masking policy on its own.
UNKNOWN_IDENTIFIER_STRENGTH = 0.55

def concentration_floor(strength: float = UNKNOWN_IDENTIFIER_STRENGTH,
                        review_at: float = REVIEW_AT) -> float:
    """The lowest shape concentration that can still change a decision.

    Found by sweeping the concentration on 2026-10-11 and watching nothing move. The
    concentration threshold decides whether a signal is emitted. The signal's strength is
    `UNKNOWN_IDENTIFIER_STRENGTH` times the share, and `classify.REVIEW_AT` decides whether
    a signal is heard. So a column whose dominant shape holds less than
    `review_at / strength` of the sample emits a signal that lands in the ignore band, which
    masks nothing and queues nobody. Lowering the threshold under this value produces
    evidence and no decisions.

    At the shipped 0.55 and 0.35 that is 0.6364. `probe.outside_rule.site_patient_ref` is
    the column `ot-129` said the threshold was the only thing standing between the arm and,
    and its dominant shape holds a quarter of the sample. A quarter is under the floor, so
    the threshold was never what was standing there.

    Derived rather than stored, because it is two other constants and a division and a
    fourth constant beside them is the fourth place a change has to be remembered.
    """
    if strength <= 0.0:
        raise ValueError("a strength of {} cannot reach any band".format(strength))
    return review_at / strength


LUHN_STRENGTH = 0.88
LUHN_CONCENTRATION = 0.90
LUHN_MIN_DIGITS = 13
LUHN_MAX_DIGITS = 19

SAMPLE_LIMIT = 500

# The calendar hypothesis, named in the module docstring. A timestamp cast to text passes
# every structural test for a fixed shape high cardinality code, so without this the arm
# returns an unnamed identifier for every date column in the warehouse. The catalog type
# guard below catches the declared ones and this catches a date held as text.
DATE_SHAPES: Tuple[str, ...] = (
    "9999-99-99",
    "9999/99/99",
    "99/99/9999",
    "99-99-9999",
    "99.99.9999",
    "9999-99-99 99:99:99",
    "9999-99-99 99:99:99.999999",
    "9999-99-99A99:99:99",
    "999999",
    "99999999",
)

TEMPORAL_TYPE_PREFIXES: Tuple[str, ...] = ("DATE", "TIME", "TIMESTAMP")


def shape(value: str) -> str:
    """The character class signature of one value.

    Letters and digits collapse. A separator on the allowlist stays. Anything else becomes
    a question mark, which is what keeps the output bounded for a column holding something
    this module did not anticipate.
    """
    out: List[str] = []
    for ch in value:
        if ch.isalpha():
            out.append(SHAPE_ALPHA)
        elif ch.isdigit():
            out.append(SHAPE_DIGIT)
        elif ch in SHAPE_SEPARATORS:
            out.append(ch)
        else:
            out.append(SHAPE_OTHER)
    return "".join(out)


def luhn_ok(digits: str) -> bool:
    """The payment card checksum, over a string of digits with nothing else in it.

    This is the one place the arm earns its keep against a predicate rather than against a
    missing predicate. The existing SQL counts digits and a digit count matches any long
    number. A checksum matches a number somebody issued.
    """
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True)
class ColumnSample:
    """What a sample leaves behind once the values are gone.

    Shapes and integers and catalog metadata. The values themselves never leave
    `sample_column`, which is the only function in this repo that holds one.
    """

    table: str
    column: str
    sql_type: str
    read: int
    non_null: int
    distinct: int
    shape_counts: Dict[str, int]
    luhn_candidates: int
    luhn_valid: int

    def __post_init__(self) -> None:
        if self.non_null > self.read:
            raise ValueError("{} sampled fewer rows than it read values".format(
                self.address))
        if self.distinct > self.non_null:
            raise ValueError("{} has more distinct values than non null ones".format(
                self.address))
        if sum(self.shape_counts.values()) != self.non_null:
            raise ValueError(
                "{} shape counts sum to {} and the non null count is {}".format(
                    self.address, sum(self.shape_counts.values()), self.non_null))

    @property
    def address(self) -> str:
        return "{}.{}".format(self.table, self.column)

    @property
    def support(self) -> float:
        if self.read == 0:
            return 0.0
        return self.non_null / self.read

    @property
    def distinct_ratio(self) -> float:
        if self.non_null == 0:
            return 0.0
        return self.distinct / self.non_null

    @property
    def dominant(self) -> Tuple[Optional[str], float]:
        """The most common shape and the share of the sample holding it."""
        if not self.shape_counts or self.non_null == 0:
            return None, 0.0
        best = max(sorted(self.shape_counts), key=lambda s: self.shape_counts[s])
        return best, self.shape_counts[best] / self.non_null

    @property
    def is_temporal_type(self) -> bool:
        base = self.sql_type.upper().split("(")[0].strip()
        return base.startswith(TEMPORAL_TYPE_PREFIXES)


def _window_sql(table_schema: str, table_name: str, column_name: str,
                limit: int = SAMPLE_LIMIT) -> str:
    """The limited window one column's values are read from."""
    return "SELECT cast({} as varchar) AS v FROM {}.{} LIMIT {}".format(
        _quote(column_name), _quote(table_schema), _quote(table_name), int(limit))


def sample_counts(con, table_schema: str, table_name: str, column,
                  limit: int = SAMPLE_LIMIT) -> Tuple[int, int, int]:
    """Rows read, non null values and distinct values over one column's window.

    One statement per column. `sample_counts_batched` is the same answer for a whole table
    in one, and `pii/scale.py` times the two against each other.
    """
    window = _window_sql(table_schema, table_name, column.name, limit)
    read, non_null, distinct = con.execute(
        "SELECT count(*), count(v), count(distinct v) FROM ({})".format(window)
    ).fetchone()
    return int(read), int(non_null), int(distinct)


def sample_counts_batched(con, table_schema: str, table_name: str,
                          columns: Sequence, limit: int = SAMPLE_LIMIT
                          ) -> Dict[str, Tuple[int, int, int]]:
    """The same counts for every column of a table in one statement, keyed by column name.

    The window is shared, which is the thing that makes this one statement rather than W.
    Every column is counted over the same first `limit` rows of the table instead of over
    its own `LIMIT` of the same table, and on an unordered scan those are the same rows. It
    is a different claim though, and the honest version of it is that this returns the
    counts for one window rather than the counts for W windows that happen to coincide.

    The value read is deliberately not batched. `SELECT c1, c2, ... FROM t LIMIT 500` would
    be one round trip and it would put 500 values of every column in the process at the same
    instant. This module exists to hold as few values as possible for as short a time as
    possible, so the shape that saves a round trip by holding W times as much is the wrong
    trade here even though it is the faster one. `pii/scale.py` measures what refusing it
    costs so the refusal is a number rather than a principle.
    """
    if not columns:
        return {}
    quoted = [_quote(c.name) for c in columns]
    inner = "SELECT {} FROM {}.{} LIMIT {}".format(
        ", ".join("cast({} as varchar) AS v{}".format(q, i)
                  for i, q in enumerate(quoted)),
        _quote(table_schema), _quote(table_name), int(limit))
    parts = ["count(*)"]
    for i in range(len(columns)):
        parts.append("count(v{})".format(i))
        parts.append("count(distinct v{})".format(i))
    row = con.execute("SELECT {} FROM ({})".format(", ".join(parts), inner)).fetchone()
    read = int(row[0])
    out: Dict[str, Tuple[int, int, int]] = {}
    for i, c in enumerate(columns):
        out[c.name] = (read, int(row[1 + i * 2]), int(row[2 + i * 2]))
    return out


def sample_column(con, table_schema: str, table_name: str, column,
                  limit: int = SAMPLE_LIMIT) -> ColumnSample:
    """Read up to `limit` values of one column and return everything except the values.

    Ordered rather than random, and that is a limitation and not a choice. An ordered
    sample of a clustered table sees one region of it, so a format used by one site only
    can be missed outright. A random sample costs a full scan on this warehouse and the
    honest version of this function says which one it did.

    Two statements rather than one. The distinct count comes back from the database over
    the same limited window instead of from a Python set over the values, which is one less
    place a value lives for the length of a call. The first version held every sampled value
    in a set to count them, which worked and kept 500 values alive in a frame inside the
    module whose subject is not doing that.

    Two statements per column is also why this module is the profiler's scaling question
    twice over. `sample_counts_batched` collapses the first of them for a whole table and
    deliberately leaves the second alone, and the reason is written there.
    """
    window = _window_sql(table_schema, table_name, column.name, limit)
    read, non_null, distinct = sample_counts(con, table_schema, table_name, column, limit)

    shape_counts: Dict[str, int] = {}
    luhn_candidates = 0
    luhn_valid = 0

    for (value,) in con.execute(window).fetchall():
        if value is None:
            continue
        sig = shape(value)
        shape_counts[sig] = shape_counts.get(sig, 0) + 1
        digits = "".join(c for c in value if c.isdigit())
        if LUHN_MIN_DIGITS <= len(digits) <= LUHN_MAX_DIGITS:
            luhn_candidates += 1
            if luhn_ok(digits):
                luhn_valid += 1

    # No value outlives this loop. `value` is rebound each pass and nothing above keeps a
    # reference to it, which is as strong as the guarantee gets once the arm is on at all.
    return ColumnSample(
        table="{}.{}".format(table_schema, table_name),
        column=column.name,
        sql_type=column.sql_type,
        read=int(read),
        non_null=int(non_null),
        distinct=int(distinct),
        shape_counts=shape_counts,
        luhn_candidates=luhn_candidates,
        luhn_valid=luhn_valid,
    )


def _looks_like_a_code(sample: ColumnSample, dominant: str,
                       thresholds: Thresholds = SHIPPED) -> bool:
    """Whether a fixed shape is the shape of an issued code rather than of something else.

    Three refusals. A declared temporal type is a date and the catalog already said so. A
    shape on the date template list is a date held as text. A shape with no digit in it is
    a word, and a column of words is a name or a category and both have arms of their own.
    """
    if sample.is_temporal_type:
        return False
    if dominant in DATE_SHAPES:
        return False
    if SHAPE_DIGIT not in dominant:
        return False
    if SHAPE_OTHER in dominant:
        return False
    if not (thresholds.min_length <= len(dominant) <= thresholds.max_length):
        return False
    if sample.distinct_ratio < thresholds.distinct:
        return False
    # A letter makes it unambiguous. Without one the shape has to be long enough that a
    # year, a quantity or a small count cannot reach it.
    digit_count = dominant.count(SHAPE_DIGIT)
    if SHAPE_ALPHA in dominant:
        return True
    return digit_count >= thresholds.digit_floor


def sample_signals(sample: ColumnSample,
                   thresholds: Thresholds = SHIPPED) -> Tuple[Signal, ...]:
    """Evidence from the shapes and the checksum. No category this module invented.

    Two rules. The checksum names a payment card because a checksum is evidence about what
    the number is. The shape rule names `unknown_identifier` because a shape is evidence
    that there is a code here and no evidence at all about what kind.
    """
    if sample.non_null == 0:
        return ()
    out: List[Signal] = []
    dominant, share = sample.dominant

    if sample.luhn_candidates:
        rate = sample.luhn_valid / sample.luhn_candidates
        if rate >= LUHN_CONCENTRATION:
            out.append(Signal(
                Arm.SAMPLE, "payment_card", LUHN_STRENGTH * rate,
                "{} of {} sampled values pass the card checksum".format(
                    sample.luhn_valid, sample.luhn_candidates),
                support=sample.support,
            ))

    if (dominant is not None and share >= thresholds.concentration
            and _looks_like_a_code(sample, dominant, thresholds)):
        out.append(Signal(
            Arm.SAMPLE, "unknown_identifier", UNKNOWN_IDENTIFIER_STRENGTH * share,
            "{} of {} sampled values have the shape {} and {:.0%} of them are "
            "distinct".format(sample.shape_counts[dominant], sample.non_null,
                              dominant, sample.distinct_ratio),
            support=sample.support,
        ))
    return tuple(out)


def samples_hold_no_values(samples: Sequence[ColumnSample]) -> Tuple[str, ...]:
    """Addresses of any sample carrying something a shape is not allowed to carry.

    The property this checks is weaker than the one `pii/profile.py` checks and that is the
    point of having it here rather than there. A shape may hold `A`, `9`, a separator on
    the allowlist and a question mark. Anything else means a value survived the projection.
    """
    allowed = set(SHAPE_ALPHA + SHAPE_DIGIT + SHAPE_OTHER + SHAPE_SEPARATORS)
    bad: List[str] = []
    for s in samples:
        for sig in s.shape_counts:
            leaked = sorted(set(sig) - allowed)
            if leaked:
                bad.append("{} shape {!r} holds {}".format(s.address, sig, leaked))
    return tuple(bad)


def attach(profiles: Sequence[ColumnProfile],
           samples: Sequence[ColumnSample],
           thresholds: Thresholds = SHIPPED) -> Tuple[ColumnProfile, ...]:
    """Put each sample's signals on its profile, leaving the sample itself behind.

    The signals ride on the profile rather than arriving as a second argument to the
    classifier. That was a shape decision and `ot-128` is the reason it went this way. The
    arm set parameter already says which arms a run considers, and a second parameter
    saying where one arm's evidence comes from would mean two things to keep in step. A
    profile carrying no sampled signals and an arm set naming the sampling arm produces
    nothing, which is the same answer as not asking for it.
    """
    by_address = {s.address: s for s in samples}
    out: List[ColumnProfile] = []
    for p in profiles:
        s = by_address.get(p.address)
        if s is None:
            out.append(p)
            continue
        out.append(p.with_sampled_signals(sample_signals(s, thresholds)))
    return tuple(out)
