"""What round two of the classifier costs, measured rather than assumed.

`classify.classify_warehouse` runs two rounds when it is given a lineage map. Round one
classifies every table on its own columns. Round two re-decides the derived tables that
named nobody but came out of tables that did. `ot-085` asked what the second round costs
on a warehouse wider than this one and `ot-138` asked the same question about the grain
measurement that feeds it. Both are here because they are one question asked twice.

The two costs are different in kind and that is the finding. Round two's own cost is CPU
and it was a doubled call into the arms. The grain's cost is database statements and it was
paid for every table in the warehouse whether or not round two was ever going to look at
it. One of those is a constant factor on work the process was doing anyway. The other grows
with the size of somebody else's warehouse and buys nothing for most of it.

What the unit is. Wall time is reported and statement counts are the number that transfers.
DuckDB is in process, so a statement here is a function call rather than a network hop, and
`pii/scale.py` makes the same point about the profiler for the same reason. A warehouse
across a wire charges per statement and this machine cannot say what that charge is. So the
count is the measurement and the milliseconds are context.

What is deliberately not measured. Profiling. Building the `ColumnProfile` objects is one
query per column or one per table and day 3 measured both in `pii/scale.py`. The fixtures
below build profiles in Python so that the profiler's cost does not sit on top of round
two's and get read as part of it.

`reprofiling_warehouse` is the shape `classify_warehouse` had before 2026-10-13, kept so
the memoised version has something to be timed against and something to agree with. It is
not a second production path. Nothing in `pii/` calls it and a check holds the two to
identical output.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from pii import classify, scale
from pii.classify import (ALL_ARMS, REVIEW_AT, Arm, Classification, ColumnProfile,
                          classify_column, classify_table, settle_table,
                          table_is_person_linked)

BENCH_SCHEMA = "grainbench"

# Table counts to measure. 5 is the shipped warehouse, so the first row of the curve is
# comparable with every other figure this repo publishes. 400 is the number `ot-138` names
# and the reason it names it is that the tool's premise is being pointed at a warehouse
# somebody else owns.
TABLE_COUNTS: Tuple[int, ...] = (5, 25, 100, 400)

# How many of those tables round two rescues. Held constant on purpose. A warehouse does
# not grow its rescued set in proportion to its size, it grows its ordinary tables, and a
# fixture where both grow together cannot tell the two costs apart.
RESCUED = 3

BENCH_ROWS = 200


class CountingConnection:
    """A connection that counts what is sent through it.

    Only `execute` is wrapped and everything else is forwarded, because `execute` is the
    only thing in this repo that reaches a database. The statements are kept rather than
    just counted so a reader can see WHICH statements the eager path was sending, which is
    what turned the per-table claim in `ot-138` into a per-table-and-a-half one.
    """

    def __init__(self, con):
        self._con = con
        self.statements: List[str] = []

    def execute(self, sql, *args, **kwargs):
        self.statements.append(sql if isinstance(sql, str) else str(sql))
        return self._con.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._con, name)

    @property
    def count(self) -> int:
        return len(self.statements)

    def reset(self) -> None:
        self.statements = []


def reprofiling_warehouse(
    profiles: Sequence[ColumnProfile],
    evidence_bar: float = REVIEW_AT,
    upstream_tables: Optional[Dict[str, Sequence[str]]] = None,
    arms: Sequence[Arm] = ALL_ARMS,
    grain_confidence: Optional[Dict[str, float]] = None,
) -> Tuple[Classification, ...]:
    """`classify_warehouse` as it was before the memoisation, for timing against.

    The only difference is the one line that matters. Round two calls `classify_table`,
    which runs every arm over every column of the rescued table a second time and throws
    the first answer away. The grain is a map and has to be complete before this is
    called, which is the other half of what was wrong.
    """
    grouped: Dict[str, List[ColumnProfile]] = {}
    for p in profiles:
        grouped.setdefault(p.table, []).append(p)

    first: Dict[str, Tuple[Classification, ...]] = {}
    for table in sorted(grouped):
        first[table] = classify_table(grouped[table], evidence_bar, arms=arms)

    if upstream_tables is None:
        return tuple(r for table in sorted(first) for r in first[table])

    linked = {t: table_is_person_linked(rs, evidence_bar) for t, rs in first.items()}
    out: List[Classification] = []
    for table in sorted(grouped):
        if linked[table]:
            out.extend(first[table])
            continue
        sources = upstream_tables.get(table, ())
        if any(linked.get(s, False) for s in sources):
            discount = None if grain_confidence is None else grain_confidence.get(table)
            out.extend(classify_table(grouped[table], evidence_bar,
                                      person_linked=True, arms=arms,
                                      grain_confidence=discount))
        else:
            out.extend(first[table])
    return tuple(out)


@dataclass(frozen=True)
class ArmCost:
    """How many times the arms run over a column, per shape.

    Counted rather than timed. A wall time on two cores says something about this machine
    and the call count says something about the algorithm, and the second one is what
    survives being read on another machine.
    """

    columns: int
    rescued_tables: int
    rescued_columns: int

    @property
    def reprofiling(self) -> int:
        return self.columns + self.rescued_columns

    @property
    def memoised(self) -> int:
        return self.columns

    @property
    def wasted_share(self) -> float:
        if self.reprofiling == 0:
            return 0.0
        return self.rescued_columns / self.reprofiling


def arm_cost(profiles: Sequence[ColumnProfile],
             upstream_tables: Dict[str, Sequence[str]],
             evidence_bar: float = REVIEW_AT,
             arms: Sequence[Arm] = ALL_ARMS) -> ArmCost:
    """The call counts, derived by running round one and reading which tables rescue.

    Round one has to run to know the rescued set, so this is not free and is not meant to
    be. It is the same work `classify_warehouse` does and the point is to report the shape
    rather than to avoid it.
    """
    grouped: Dict[str, List[ColumnProfile]] = {}
    for p in profiles:
        grouped.setdefault(p.table, []).append(p)
    first = {t: tuple(classify_column(p, arms) for p in ps)
             for t, ps in grouped.items()}
    settled = {t: settle_table(grouped[t], first[t], evidence_bar) for t in grouped}
    linked = {t: table_is_person_linked(rs, evidence_bar) for t, rs in settled.items()}
    rescued = [t for t in sorted(grouped)
               if not linked[t]
               and any(linked.get(s, False) for s in upstream_tables.get(t, ()))]
    return ArmCost(
        columns=len(profiles),
        rescued_tables=len(rescued),
        rescued_columns=sum(len(grouped[t]) for t in rescued),
    )


@dataclass(frozen=True)
class GrainCost:
    """Statements the grain measurement sends, eagerly and lazily.

    `eager_tables` and `lazy_tables` are table counts and the statement counts are what
    came back from a counting connection. The two are not in proportion, because a table
    the lineage graph says was grouped costs an extra statement checking that the weight
    column still exists and an ungrouped one does not.
    """

    eager_tables: int
    lazy_tables: int
    eager_statements: int
    lazy_statements: int

    @property
    def saved(self) -> int:
        return self.eager_statements - self.lazy_statements

    @property
    def per_eager_table(self) -> float:
        if self.eager_tables == 0:
            return 0.0
        return self.eager_statements / self.eager_tables


def grain_cost(con, graph, tables: Sequence[str],
               rescued: Sequence[str]) -> GrainCost:
    """Count both ways on the same connection and the same graph.

    The eager side is what `grain.classify_shipped` built until 2026-10-13, a confidence
    for every table in the warehouse. The lazy side is the rescued set, which is what
    round two is the only consumer of.
    """
    from pii import grain

    counter = CountingConnection(con)
    grain.confidence_map(counter, graph, tables)
    eager = counter.count

    counter.reset()
    grain.confidence_map(counter, graph, rescued)
    lazy = counter.count

    return GrainCost(eager_tables=len(tables), lazy_tables=len(rescued),
                     eager_statements=eager, lazy_statements=lazy)


@dataclass(frozen=True)
class Timing:
    """One median wall time. Two fields, because the caller already knows what it timed.

    An earlier version carried a label, a table count and a column count and nothing read
    any of the three. A record that holds what its caller already has is padding and the
    audit deleted it.
    """

    seconds: float

    @property
    def ms(self) -> float:
        return self.seconds * 1000.0


def time_call(fn, passes: int = 5) -> Timing:
    """Median of `passes` runs. The median because one slow pass is the machine and not
    the code, and a mean lets that pass into the published number.

    `scale.median` rather than a second copy of four lines. It was written for day 3's
    profiler bench and nothing graded it until today, which a mutation pass found by
    moving the index of the hand rolled copy that used to be here and surviving.
    """
    samples = []
    for _ in range(passes):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return Timing(seconds=scale.median(samples))


def _person_table() -> str:
    return "{}.person".format(BENCH_SCHEMA)


def _mart_table(i: int) -> str:
    return "{}.mart_{:03d}".format(BENCH_SCHEMA, i)


def _ref_table(i: int) -> str:
    return "{}.ref_{:03d}".format(BENCH_SCHEMA, i)


def table_names(total: int, rescued: int = RESCUED) -> Tuple[str, ...]:
    """Every table in a fixture of this size, person table first.

    `total` counts the person table and the marts, so the plain reference tables fill the
    rest. A total smaller than one plus the rescued count is refused rather than clamped,
    because a clamped fixture reports a curve against a size it did not build.
    """
    if total < 1 + rescued:
        raise ValueError(
            "a fixture of {} tables cannot hold a person table and {} marts".format(
                total, rescued))
    names = [_person_table()]
    names += [_mart_table(i) for i in range(rescued)]
    names += [_ref_table(i) for i in range(total - 1 - rescued)]
    return tuple(names)


def build(con, total: int, rescued: int = RESCUED, rows: int = BENCH_ROWS) -> int:
    """Create the fixture tables. Returns how many were made.

    The marts are grouped off the person table and carry a row count column, which is what
    the graph below points `mask.weight_for` at. The reference tables are derived from
    nothing and name nobody, so they are neither linked nor rescued and they are the cost
    the eager grain map was paying for.
    """
    names = table_names(total, rescued)
    con.execute("CREATE SCHEMA IF NOT EXISTS {}".format(BENCH_SCHEMA))
    con.execute("DROP TABLE IF EXISTS {}".format(_person_table()))
    con.execute(
        "CREATE TABLE {} AS SELECT i AS person_id, "
        "'user' || i || '@example.org' AS email, "
        "DATE '2024-01-01' + cast(i % 90 AS INTEGER) AS admitted_at "
        "FROM range({}) t(i)".format(_person_table(), rows))
    for name in names[1:]:
        con.execute("DROP TABLE IF EXISTS {}".format(name))
        if ".mart_" in name:
            con.execute(
                "CREATE TABLE {} AS SELECT admitted_at AS day, count(*) AS encounters "
                "FROM {} GROUP BY admitted_at".format(name, _person_table()))
        else:
            con.execute(
                "CREATE TABLE {} AS SELECT 'K' || lpad(cast(i AS VARCHAR), 6, '0') AS code, "
                "i AS amount FROM range({}) t(i)".format(name, rows))
    return len(names)


def profiles_for(total: int, rescued: int = RESCUED,
                 rows: int = BENCH_ROWS) -> Tuple[ColumnProfile, ...]:
    """Profiles matching what `build` created, written in Python on purpose.

    Running the real profiler here would add one query per column to every number below
    and profiling is day 3's measurement, not this one. The counts are plausible rather
    than exact and nothing in this module reads them for anything but the arms, which
    only care about the name, the type and the pattern hits.
    """
    out: List[ColumnProfile] = []
    person = _person_table()
    out.append(ColumnProfile(person, "person_id", "BIGINT", False, True,
                             rows, rows, rows, 3.0))
    out.append(ColumnProfile(person, "email", "VARCHAR", False, False,
                             rows, rows, rows, 22.0, {"email": rows}))
    out.append(ColumnProfile(person, "admitted_at", "DATE", False, False,
                             rows, rows, 90, 10.0))
    for name in table_names(total, rescued)[1:]:
        if ".mart_" in name:
            out.append(ColumnProfile(name, "day", "DATE", False, False, 90, 90, 90, 10.0))
            out.append(ColumnProfile(name, "encounters", "BIGINT", False, False,
                                     90, 90, 12, 2.0))
        else:
            out.append(ColumnProfile(name, "code", "VARCHAR", False, False,
                                     rows, rows, rows, 7.0))
            out.append(ColumnProfile(name, "amount", "BIGINT", False, False,
                                     rows, rows, rows, 3.0))
    return tuple(out)


def graph_for(total: int, rescued: int = RESCUED):
    """A lineage graph for the fixture, built as edges rather than parsed out of SQL.

    The parser in `pii/lineage.py` reads the sample warehouse's own statements and this
    fixture has no statements worth parsing. What the measurement needs from a graph is
    two facts per mart: that its day came from the person table without the value
    changing, and that its row count is a count. Those are one GROUPED edge and one
    AGGREGATE edge off the star, which is what `mask.weight_candidates` looks for.
    """
    from pii.lineage import ColumnRef, Edge, EdgeKind, Graph

    person = _person_table()
    edges: List[Edge] = []
    for i in range(rescued):
        mart = _mart_table(i)
        edges.append(Edge(ColumnRef(person, "admitted_at"), ColumnRef(mart, "day"),
                          EdgeKind.GROUPED, "GROUP BY admitted_at"))
        edges.append(Edge(ColumnRef(person, "*"), ColumnRef(mart, "encounters"),
                          EdgeKind.AGGREGATE, "count(*)"))
    return Graph(edges=tuple(edges))


def upstream_for(total: int, rescued: int = RESCUED) -> Dict[str, Tuple[str, ...]]:
    return graph_for(total, rescued).upstream_table_map(list(table_names(total, rescued)))


def disagreements(profiles: Sequence[ColumnProfile],
                  upstream_tables: Dict[str, Sequence[str]],
                  grain_confidence: Optional[Dict[str, float]] = None,
                  evidence_bar: float = REVIEW_AT) -> Tuple[str, ...]:
    """Addresses where the memoised path and the reprofiling one differ.

    This is the check that makes the timing comparison mean anything. Two shapes that
    disagree are not two timings of one thing. Returned as addresses rather than as a
    boolean so a failure says which column moved.
    """
    new = {r.address: (r.category_key, r.confidence)
           for r in classify.classify_warehouse(
               profiles, evidence_bar, upstream_tables,
               grain_confidence=grain_confidence)}
    old = {r.address: (r.category_key, r.confidence)
           for r in reprofiling_warehouse(
               profiles, evidence_bar, upstream_tables,
               grain_confidence=grain_confidence)}
    bad = [a for a in sorted(set(new) | set(old)) if new.get(a) != old.get(a)]
    return tuple(bad)
