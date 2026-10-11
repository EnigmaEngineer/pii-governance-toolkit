"""A table of columns the predicate set was never going to find, and the grading of it.

`ot-077` asked for one thing and it is specific. A column deliberately formatted outside
the predicate set, graded against what a sampling arm would have found. The sample
warehouse cannot answer it, because I wrote the corpus and the predicates together and
every format in it is one I thought of.

So this is a separate fixture and it is labelled as one. Four columns in their own schema,
none of them in `pii/schema.py`, so the declared fingerprint does not move and the forty
two column figures the rest of the repo publishes still describe the same warehouse.

What the fixture is for is the mechanism and not the coverage. A sampling arm that found
these four because I chose four shapes a shape detector finds would say nothing. The claim
worth making is narrower. The shape rule holds no rule for a passport, a card format with
slashes in it or a trace id, and it still separates three of these from nothing at all. The
fourth is there because it cannot, and the one that is not personal is there because the
arm flags it anyway.

    outside_rule      what it holds                      sampling should
    travel_doc_ref    a passport in AA9999999 form       find it
    payer_ref         a card number with slashes         find it and name it
    site_patient_ref  four site formats at a quarter     miss it
    request_ref       a trace id, not personal           flag it and be wrong

The honest reading of the last two rows is the point. A fixture of only the first two is a
demonstration and not a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from pii import ablation, classify, sample
from pii.rng import stream
from pii.sample import luhn_ok
from pii.schema import Column, PlantedColumn, Table

BENCH_SCHEMA = "probe"
BENCH_TABLE = "outside_rule"
BENCH_FQN = "{}.{}".format(BENCH_SCHEMA, BENCH_TABLE)

BENCH: Table = Table(BENCH_SCHEMA, BENCH_TABLE, (
    Column("row_id", "BIGINT", nullable=False, is_key=True),
    Column("travel_doc_ref", "VARCHAR"),
    Column("payer_ref", "VARCHAR"),
    Column("site_patient_ref", "VARCHAR"),
    Column("request_ref", "VARCHAR"),
))

# The answer key for the fixture, in the same shape the warehouse uses. These are my labels
# and they grade the arm against my judgement and nothing else, which is the same caveat
# `pii/schema.py` carries for the planted warehouse.
BENCH_PLANTED: Tuple[PlantedColumn, ...] = (
    PlantedColumn(BENCH_FQN, "row_id", "not_personal",
                  why="surrogate key, declared as a key so the structure arm skips it"),
    PlantedColumn(BENCH_FQN, "travel_doc_ref", "national_id",
                  why="a passport number, which is a national identifier with no "
                       "predicate behind it"),
    PlantedColumn(BENCH_FQN, "payer_ref", "payment_card",
                  why="a real card number in a format the digit count predicate cannot "
                       "reach"),
    PlantedColumn(BENCH_FQN, "site_patient_ref", "medical_record_number",
                  why="one record number per site format, so no shape dominates"),
    PlantedColumn(BENCH_FQN, "request_ref", "not_personal",
                  why="a trace id. Fixed shape and unique per row and about nobody"),
)

N_ROWS = 400

_LETTERS = "ABCDEFGHJKLMNPRSTVWXYZ"


def _passport(rng) -> str:
    """Two letters and seven digits. No predicate in the repo describes this."""
    return "{}{}{:07d}".format(rng.choice(_LETTERS), rng.choice(_LETTERS),
                               rng.randrange(10 ** 7))


def _card(rng) -> str:
    """A Luhn valid sixteen digit number written in groups of four with slashes.

    The separator is the whole point. `VALUE_PREDICATES` strips spaces and hyphens before
    counting digits and a slash survives that, so the digit count predicate sees a string
    with non digits in it and returns false.
    """
    body = [rng.randrange(10) for _ in range(15)]
    for check in range(10):
        digits = "".join(str(d) for d in body + [check])
        if luhn_ok(digits):
            break
    return "/".join(digits[i:i + 4] for i in range(0, 16, 4))


def _site_ref(rng, which: int) -> str:
    """One record number in one of four site formats, chosen round robin.

    Round robin rather than random, so the share of each format is exactly a quarter and
    the miss is a property of the fixture rather than of a draw. A quarter is under the
    shape concentration threshold and under the value arm's match floor, which is why the
    ten digit variant does not fire the telephone predicate on its own.
    """
    if which == 0:
        return "P-{:05d}".format(rng.randrange(10 ** 5))
    if which == 1:
        return "{:010d}".format(rng.randrange(10 ** 10))
    if which == 2:
        return "{}{}/{:02d}/{:04d}".format(rng.choice(_LETTERS), rng.choice(_LETTERS),
                                           rng.randrange(100), rng.randrange(10 ** 4))
    return "X{:012d}".format(rng.randrange(10 ** 12))


def _trace(rng) -> str:
    return "{}{:04d}{}".format(
        "".join(rng.choice(_LETTERS) for _ in range(4)),
        rng.randrange(10 ** 4),
        "".join(rng.choice(_LETTERS) for _ in range(4)))


def rows(n: int = N_ROWS, seed: int = 20261010) -> Tuple[Dict[str, object], ...]:
    """The fixture rows. One named stream per column, so adding a column moves no other."""
    doc = stream(seed, "travel_doc_ref")
    card = stream(seed, "payer_ref")
    site = stream(seed, "site_patient_ref")
    trace = stream(seed, "request_ref")
    out: List[Dict[str, object]] = []
    for i in range(n):
        out.append({
            "row_id": i + 1,
            "travel_doc_ref": _passport(doc),
            "payer_ref": _card(card),
            "site_patient_ref": _site_ref(site, i % 4),
            "request_ref": _trace(trace),
        })
    return tuple(out)


def ddl() -> str:
    cols = []
    for c in BENCH.columns:
        line = "  {} {}".format(c.name, c.sql_type)
        if not c.nullable:
            line += " NOT NULL"
        cols.append(line)
    cols.append("  PRIMARY KEY (row_id)")
    return "CREATE TABLE {} (\n{}\n)".format(BENCH_FQN, ",\n".join(cols))


def load(con, n: int = N_ROWS, seed: int = 20261010) -> int:
    """Write the fixture into a connection and return the count read back out of it."""
    con.execute("CREATE SCHEMA IF NOT EXISTS {}".format(BENCH_SCHEMA))
    con.execute("DROP TABLE IF EXISTS {}".format(BENCH_FQN))
    con.execute(ddl())
    names = [c.name for c in BENCH.columns]
    con.executemany(
        "INSERT INTO {} VALUES ({})".format(BENCH_FQN, ", ".join("?" for _ in names)),
        [tuple(r[n] for n in names) for r in rows(n, seed)],
    )
    return con.execute("SELECT count(*) FROM {}".format(BENCH_FQN)).fetchone()[0]


def planted_key_for_bench(result) -> str:
    """The planted category for a classification of a fixture column."""
    for p in BENCH_PLANTED:
        if p.table == result.table and p.column == result.column:
            return p.category_key
    raise KeyError("{} is not a fixture column".format(result.address))


@dataclass(frozen=True)
class Outcome:
    """One fixture column read twice, with the planted answer beside both readings."""

    address: str
    planted: str
    before_band: str
    before_category: str
    before_confidence: float
    after_band: str
    after_category: str
    after_confidence: float

    @property
    def was_invisible(self) -> bool:
        return self.before_band == "ignore"

    @property
    def now_flagged(self) -> bool:
        return self.after_band != "ignore"

    @property
    def verdict(self) -> str:
        """What the pair of readings means for this column.

        Four outcomes and they are not symmetric. `recovered` is a column in scope that
        nothing saw before. `false alarm` is a column out of scope that something sees now.
        `still missed` is the cost that remains. `unchanged` is everything else and it is
        the commonest answer on a real warehouse.
        """
        personal = self.planted != "not_personal"
        if personal and self.was_invisible and self.now_flagged:
            return "recovered"
        if personal and not self.now_flagged:
            return "still missed"
        if not personal and self.now_flagged and self.was_invisible:
            return "false alarm"
        return "unchanged"


def grade(before: Sequence, after: Sequence,
          planted_for) -> Tuple[Outcome, ...]:
    """Pair the two readings of each column up by address and attach the planted answer."""
    first = {r.address: r for r in before}
    out: List[Outcome] = []
    for r in after:
        b = first[r.address]
        out.append(Outcome(
            address=r.address,
            planted=planted_for(r),
            before_band=b.band.value,
            before_category=b.category_key,
            before_confidence=b.confidence,
            after_band=r.band.value,
            after_category=r.category_key,
            after_confidence=r.confidence,
        ))
    return tuple(sorted(out, key=lambda o: o.address))


def counts(outcomes: Sequence[Outcome]) -> Dict[str, int]:
    out = {"recovered": 0, "still missed": 0, "false alarm": 0, "unchanged": 0}
    for o in outcomes:
        out[o.verdict] += 1
    return out



# The concentrations to sweep. 0.90 is the shipped value and 0.25 is the share each of the
# four site formats holds in `site_patient_ref`, which is the column the arm misses. A sweep
# that stops above 0.25 cannot reach the thing it is meant to answer.
CONCENTRATIONS: Tuple[float, ...] = (
    0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.00)


@dataclass(frozen=True)
class FixtureSweepRow:
    """One concentration read against the four column fixture."""

    concentration: float
    signals: int
    recovered: int
    still_missed: int
    false_alarms: int
    unchanged: int
    person_linked: bool

    @property
    def net(self) -> int:
        """Columns the arm got right minus columns it got wrong, at this threshold.

        A single number for a sweep row so a cliff is visible in one column of the table.
        It is not a score and nothing is optimised against it. A recovered medical record
        number and a false alarm on a trace id are not the same size of mistake and this
        arithmetic says they are.
        """
        return self.recovered - self.false_alarms


@dataclass(frozen=True)
class WarehouseSweepRow:
    """One concentration read against the shipped warehouse through the ablation harness."""

    concentration: float
    signals: int
    found: int
    masked: int
    false_alarms: int
    bands_moved: int


def sweep_fixture(profiles: Sequence, samples: Sequence,
                  concentrations: Sequence[float] = CONCENTRATIONS
                  ) -> Tuple[FixtureSweepRow, ...]:
    """Re-read the fixture at each concentration, with everything else held still.

    The samples are taken once and reused. A shape count does not depend on the threshold,
    so re-sampling per row would re-read the same values eleven times for no new
    information, which is the opposite of what this module is careful about.
    """
    out: List[FixtureSweepRow] = []
    for c in concentrations:
        thresholds = sample.Thresholds(concentration=c)
        with_samples = sample.attach(profiles, samples, thresholds)
        before = classify.classify_warehouse(with_samples, arms=classify.ALL_ARMS)
        after = classify.classify_warehouse(with_samples,
                                            arms=classify.ARMS_WITH_SAMPLING)
        tally = counts(grade(before, after, planted_key_for_bench))
        out.append(FixtureSweepRow(
            concentration=c,
            signals=sum(1 for p in with_samples if p.sampled_signals),
            recovered=tally["recovered"],
            still_missed=tally["still missed"],
            false_alarms=tally["false alarm"],
            unchanged=tally["unchanged"],
            person_linked=classify.table_is_person_linked(after),
        ))
    return tuple(out)


def sweep_warehouse(profiles: Sequence, samples: Sequence, upstream_tables,
                    scope, planted_key_for,
                    concentrations: Sequence[float] = CONCENTRATIONS
                    ) -> Tuple[WarehouseSweepRow, ...]:
    """The same sweep against the shipped warehouse, through `pii.ablation`.

    Through the committed harness rather than a second diff written here, for the reason
    `scripts/sample_probe.py` gives. An arm set is an arm set and the ablation already
    knows how to read two of them against a planted answer key.
    """
    arm_sets = (("declaration only", classify.ALL_ARMS),
                ("with sampling", classify.ARMS_WITH_SAMPLING))
    out: List[WarehouseSweepRow] = []
    for c in concentrations:
        thresholds = sample.Thresholds(concentration=c)
        with_samples = sample.attach(profiles, samples, thresholds)
        report = ablation.ablate(
            with_samples,
            upstream_tables=upstream_tables,
            scope=scope,
            planted_key_for=planted_key_for,
            arm_sets=arm_sets,
        )
        reading = report.by_label("with sampling")
        out.append(WarehouseSweepRow(
            concentration=c,
            signals=sum(1 for p in with_samples if p.sampled_signals),
            found=reading.found,
            masked=reading.masked,
            false_alarms=reading.false_alarms,
            bands_moved=reading.bands_moved,
        ))
    return tuple(out)


def cliff(rows: Sequence) -> float:
    """The highest concentration below the shipped one that reads differently.

    Coming down from the shipped value, this is where something first changes. The first
    version walked up from the bottom and returned the lowest value that differed, which is
    0.20 on this sweep and says nothing, because 0.25 differs too and 0.20 is just the end
    of the list. The cliff is the interesting edge and the end of the range is not.

    Read off the sweep rather than typed beside it, which is the convention
    `scripts/classify_probe.py` set for the band thresholds. Returns 0.0 when nothing below
    the shipped value differs, and a caller printing that should say nothing moved rather
    than print a threshold of zero.
    """
    shipped = [r for r in rows if r.concentration == sample.SHAPE_CONCENTRATION]
    if not shipped:
        return 0.0
    baseline = shipped[0]
    below = [r for r in rows if r.concentration < baseline.concentration]
    for r in sorted(below, key=lambda x: -x.concentration):
        if (r.signals, r.false_alarms) != (baseline.signals, baseline.false_alarms):
            return r.concentration
    return 0.0
