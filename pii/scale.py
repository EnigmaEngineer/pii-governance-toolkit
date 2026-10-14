"""Where one query per column stops being fine, measured rather than argued.

The profiler docstring used to answer that question with a guess. The guess
had an arithmetic error in it, so the measurement is worth more than usual.

What is being compared. `profile.profile_column` issues one statement per column, so a
table of W columns is W round trips and W full scans. `profile.profile_table` names the
same expressions once and issues one statement, so it is one round trip and one scan. The
numbers that matter are wall time, round trips and the character length of the SQL, because
the last one is the cost the original docstring was worried about.

The fixture is synthetic on purpose. The shipped warehouse is 42 columns across five tables
and its widest table is 13, which cannot answer a question about several hundred columns in
one table. The values cycle through templates that some predicates match and some do not,
because a column no predicate touches and a column every predicate scans are different
amounts of work and a fixture of only one of them measures the wrong thing.

What this does not measure. DuckDB is in process, so a round trip here is a function call
and not a network hop. Every number below understates the per-column shape against a
warehouse across a wire, and by how much is not something this machine can say. That is the
honest limit of the measurement and it points the same direction either way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from pii import profile, sample
from pii.schema import Column

BENCH_SCHEMA = "bench"
BENCH_TABLE = "wide"
BENCH_FQN = "{}.{}".format(BENCH_SCHEMA, BENCH_TABLE)

# Widths to measure. 42 is the shipped warehouse's column count, so the first row of the
# curve is comparable with every other figure this repo publishes. The rest double.
#
# 672 is deliberately not in the default. The batched shape at 672 columns is 6048 aggregate
# expressions in one statement and it was killed by the OOM killer on both machines, at 5000
# rows and again at 10, so a default that reached it would take the run down rather than
# report a number. Measure it on purpose with `--widths 672 --only batched` and expect
# exit 137.
WIDTHS: Tuple[int, ...] = (42, 84, 168, 336)
CEILING_WIDTH = 672

BENCH_ROWS = 5000

# Value templates, one per generated column, cycled. Each one is chosen for what it does
# to the predicate set rather than for looking realistic.
#   email   matches the email predicate on every row
#   card    matches the payment_card predicate and is Luhn valid
#   code    matches nothing and is the case a predicate set scans and rejects
#   sex     matches the sex predicate, low cardinality, so the structure arm sees it too
#   null    is empty, which is the case an avg returns null for
VALUE_TEMPLATES: Tuple[Tuple[str, str, str], ...] = (
    ("email", "VARCHAR", "'user' || i || '@example.org'"),
    ("card", "VARCHAR", "'4111111111111111'"),
    ("code", "VARCHAR", "'K' || lpad(cast(i as varchar), 8, '0')"),
    ("sex", "VARCHAR", "case when i % 2 = 0 then 'M' else 'F' end"),
    ("null", "VARCHAR", "cast(null as varchar)"),
)


def column_name(index: int) -> str:
    kind = VALUE_TEMPLATES[index % len(VALUE_TEMPLATES)][0]
    return "c{}_{}".format(index, kind)


def columns_for(width: int) -> Tuple[Column, ...]:
    """The catalog metadata the profiler needs, built without reading the catalog.

    A crawl would also work and it would make the bench depend on the crawler being
    correct at 672 columns, which is a separate question from the one being asked.
    """
    return tuple(
        Column(column_name(i), VALUE_TEMPLATES[i % len(VALUE_TEMPLATES)][1],
               nullable=True, is_key=False)
        for i in range(width)
    )


def build(con, width: int, rows: int = BENCH_ROWS) -> int:
    """Create the wide table and return the row count the database reports.

    One `CREATE TABLE AS SELECT` over `range`. An executemany of 672 columns by 5000 rows
    would be the thing being timed rather than the setup.
    """
    exprs = ["cast(i as bigint) AS row_id"]
    for i in range(width):
        _kind, _sql_type, expr = VALUE_TEMPLATES[i % len(VALUE_TEMPLATES)]
        exprs.append("{} AS {}".format(expr, column_name(i)))
    con.execute("CREATE SCHEMA IF NOT EXISTS {}".format(BENCH_SCHEMA))
    con.execute("DROP TABLE IF EXISTS {}".format(BENCH_FQN))
    con.execute("CREATE TABLE {} AS SELECT {} FROM range(0, {}) t(i)".format(
        BENCH_FQN, ", ".join(exprs), int(rows)))
    return con.execute("SELECT count(*) FROM {}".format(BENCH_FQN)).fetchone()[0]


@dataclass(frozen=True)
class Timing:
    """One shape at one width.

    `statements` and `sql_chars` are counted rather than timed, so they are the same on
    any machine and the wall time is the only figure that is this machine's.
    """

    shape: str
    columns: int
    rows: int
    statements: int
    sql_chars: int
    seconds: float

    @property
    def per_column_ms(self) -> float:
        if self.columns == 0:
            return 0.0
        return self.seconds * 1000.0 / self.columns


def _sql_cost_per_column(columns: Sequence[Column]) -> Tuple[int, int]:
    lengths = [len(profile._profile_sql(BENCH_SCHEMA, BENCH_TABLE, c.name)[0])
               for c in columns]
    return len(lengths), sum(lengths)


def time_per_column(con, columns: Sequence[Column], rows: int) -> Tuple[Timing, tuple]:
    statements, sql_chars = _sql_cost_per_column(columns)
    start = time.perf_counter()
    profiles = tuple(profile.profile_column(con, BENCH_SCHEMA, BENCH_TABLE, c)
                     for c in columns)
    elapsed = time.perf_counter() - start
    return Timing("per column", len(columns), rows, statements, sql_chars, elapsed), profiles


def time_batched(con, columns: Sequence[Column], rows: int) -> Tuple[Timing, tuple]:
    sql, _keys, _stride = profile._profile_table_sql(BENCH_SCHEMA, BENCH_TABLE, columns)
    start = time.perf_counter()
    profiles = profile.profile_table(con, BENCH_SCHEMA, BENCH_TABLE, columns)
    elapsed = time.perf_counter() - start
    return Timing("batched", len(columns), rows, 1, len(sql), elapsed), profiles


def time_sample_counts(con, columns: Sequence[Column], rows: int) -> Tuple[Timing, Timing]:
    """The second caller. `pii/sample.py` issues two statements per column, not one.

    Only the count statement is timed against a batched version, and the reason is in
    `sample.sample_counts_batched`. Batching the value read would put `rows_read` values of
    every column in the process at the same instant, and holding W times as many values is
    the wrong direction for the module whose subject is holding as few as possible.
    """
    start = time.perf_counter()
    for c in columns:
        sample.sample_counts(con, BENCH_SCHEMA, BENCH_TABLE, c)
    per = time.perf_counter() - start

    start = time.perf_counter()
    sample.sample_counts_batched(con, BENCH_SCHEMA, BENCH_TABLE, columns)
    batched = time.perf_counter() - start

    return (Timing("sample counts per column", len(columns), rows, len(columns), 0, per),
            Timing("sample counts batched", len(columns), rows, 1, 0, batched))


DEFAULT_PASSES = 3


def median(values: Sequence[float]) -> float:
    """Middle value of a sorted copy, averaging the two middles on an even count.

    Written out rather than imported from `statistics`, which is one more import for four
    lines, and the four lines are the part a reader of a benchmark wants to see.
    """
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


@dataclass(frozen=True)
class Repeated:
    """One shape at one width, timed more than once.

    The first version of this bench timed each shape once. The profiler result was so one
    sided that a single pass could not have been wrong about the direction, but the sampling
    arm's count statement came back at 0.94x on one run and 1.29x on the next, which is a
    37 percent swing on a two core machine and is not a measurement. A median and a range
    says which of the two kinds of result is in front of you.
    """

    shape: str
    columns: int
    rows: int
    statements: int
    sql_chars: int
    seconds: Tuple[float, ...]

    @property
    def middle(self) -> float:
        return median(self.seconds)

    @property
    def low(self) -> float:
        return min(self.seconds) if self.seconds else 0.0

    @property
    def high(self) -> float:
        return max(self.seconds) if self.seconds else 0.0

    @property
    def spread(self) -> float:
        """High over low. One means every pass agreed and two means one took twice as long."""
        if not self.seconds or self.low == 0.0:
            return 0.0
        return self.high / self.low

    @property
    def per_column_ms(self) -> float:
        if self.columns == 0:
            return 0.0
        return self.middle * 1000.0 / self.columns

    def as_timing(self) -> Timing:
        """The median pass as a single `Timing`, for the ratio and exponent helpers."""
        return Timing(self.shape, self.columns, self.rows, self.statements,
                      self.sql_chars, self.middle)


def repeat(fn, con, columns: Sequence, rows: int,
           passes: int = DEFAULT_PASSES) -> Tuple[Repeated, tuple]:
    """Run one timing function several times and keep every pass.

    The profiles come back from the last pass. They are identical across passes by
    construction, since nothing in the table changes between them, and keeping one set
    rather than all of them is the only thing this discards.
    """
    seconds: List[float] = []
    timing = profiles = None
    for _ in range(max(1, passes)):
        timing, profiles = fn(con, columns, rows)
        seconds.append(timing.seconds)
    return Repeated(timing.shape, timing.columns, timing.rows, timing.statements,
                    timing.sql_chars, tuple(seconds)), profiles


def speedups(timings: Sequence[Timing]) -> List[Tuple[int, float, float]]:
    """Width, the ratio of the two profiler shapes, and the ratio of their SQL lengths."""
    by_width: dict = {}
    for t in timings:
        by_width.setdefault(t.columns, {})[t.shape] = t
    out: List[Tuple[int, float, float]] = []
    for width in sorted(by_width):
        pair = by_width[width]
        if "per column" not in pair or "batched" not in pair:
            continue
        per, batch = pair["per column"], pair["batched"]
        time_ratio = per.seconds / batch.seconds if batch.seconds else 0.0
        sql_ratio = per.sql_chars / batch.sql_chars if batch.sql_chars else 0.0
        out.append((width, time_ratio, sql_ratio))
    return out


def growth_exponent(timings: Sequence[Timing], shape: str) -> float:
    """The exponent of SQL length against width for one shape, fitted on the endpoints.

    The profiler docstring claimed the batched shape grows quadratically in the width of
    the table. An exponent is the cheapest way to say whether that was true, and two
    endpoints are enough when the only candidates on the table are 1 and 2.
    """
    points = sorted((t.columns, t.sql_chars) for t in timings if t.shape == shape)
    points = [(w, c) for w, c in points if w > 0 and c > 0]
    if len(points) < 2:
        return 0.0
    (w0, c0), (w1, c1) = points[0], points[-1]
    import math
    if w1 == w0:
        return 0.0
    return math.log(c1 / c0) / math.log(w1 / w0)
