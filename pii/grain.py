"""How many people one row of a pre-aggregated table stands for.

`classify.classify_warehouse` round two keeps a derived table's temporal evidence when the
tables it came from name people. Being derived from a table full of patients is a necessary
condition for a mart's date to be one patient's date. It is not a sufficient one. A mart
grouped to a day and a department and a postal code can produce rows standing for one
encounter or rows standing for four hundred, and nothing in the catalog says which.

This module measures it. One statement per table, over the column the lineage graph says
holds the group size. Everything selected is an integer and no value from a user column
reaches this process, which is the same guarantee `pii/profile.py` keeps.

The number that ends up in the confidence is `singleton_share`, the share of ROWS whose
group holds one person. That is the quantity the rescue is actually claiming. A column of
dates is a column of people's dates to the extent that its rows are people, and the share
is that extent written as a number.

`k_anonymity` is reported and does not feed anything, and the reason is worth stating. k on
the grain is `min` over every group in the table, so one group of one takes it to 1 and the
other fourteen hundred stop mattering. It is a necessary condition wearing a measurement's
clothes, which is the same defect `ot-084` opened against the rescue itself. The
distribution has resolution and the minimum does not.

The population view of the same data already exists. `mask.Residual.alone_share` is
singleton groups over people rather than over rows, and it is the right number for
disclosure risk. It is not available here. `mask.residual_sql` refuses to run while any
quasi identifier of the table sits in the review band, and the column this measurement
exists to decide is the one in the review band. Measured 2026-10-12 on the shipped
warehouse: the refusal names `analytics.encounter_daily.day` by address. So the downstream
measurement cannot inform the upstream decision, and that is why this module is not a
wrapper around the one in `pii/mask.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from pii.classify import REVIEW_AT, TEMPORAL_TYPE_STRENGTH
from pii.mask import weight_for


def _quote_fqn(table: str) -> str:
    return ".".join('"{}"'.format(part.replace('"', '""')) for part in table.split("."))


def _quote(column: str) -> str:
    return '"{}"'.format(column.replace('"', '""'))


@dataclass(frozen=True)
class GrainWitness:
    """The group size distribution of one table, as four integers and a name.

    `rows` is how many rows the table has. `population` is how many people those rows
    cover, which is the sum of the group sizes. On a table that was never grouped the two
    are equal and the weight column is None.
    """

    table: str
    weight_column: Optional[str]
    rows: int
    population: int
    k_anonymity: int
    singleton_rows: int

    def __post_init__(self) -> None:
        if self.rows < 0:
            raise ValueError("{} cannot have negative rows".format(self.table))
        if self.singleton_rows > self.rows:
            raise ValueError(
                "{} reports more singleton rows than rows".format(self.table))
        if self.rows and self.population < self.rows:
            raise ValueError(
                "{} covers fewer people than it has rows, so the weight column is not a "
                "group size".format(self.table))
        if self.rows and self.k_anonymity < 1:
            raise ValueError(
                "{} reports a group of {} people, which is not a group".format(
                    self.table, self.k_anonymity))

    @property
    def singleton_share(self) -> float:
        """Share of rows standing for exactly one person.

        An empty table gets 0.0 rather than a division error. Zero is also the right
        answer for the rescue, because a table with no rows carries no evidence that
        anything in it is a person's date.
        """
        if self.rows == 0:
            return 0.0
        return self.singleton_rows / self.rows

    @property
    def people_per_row(self) -> float:
        if self.rows == 0:
            return 0.0
        return self.population / self.rows

    @property
    def was_grouped(self) -> bool:
        return self.weight_column is not None


def grain_sql(table: str, weight: Optional[str]) -> str:
    """The statement that measures the group size distribution of one table.

    `weight` names the column holding how many source rows each row stands for. Left out,
    every row counts once, which is the ungrouped case. Four integers come back and nothing
    else can.
    """
    size = "1" if weight is None else _quote(weight)
    return (
        "SELECT count(*), coalesce(sum({s}), 0), coalesce(min({s}), 1), "
        "coalesce(sum(CASE WHEN {s} = 1 THEN 1 ELSE 0 END), 0) FROM {t}".format(
            s=size, t=_quote_fqn(table))
    )


def measure_grain(con, table: str, weight: Optional[str]) -> GrainWitness:
    rows, population, k, singletons = con.execute(grain_sql(table, weight)).fetchone()
    return GrainWitness(
        table=table,
        weight_column=weight,
        rows=int(rows),
        population=int(population),
        k_anonymity=int(k),
        singleton_rows=int(singletons),
    )


def columns_of(con, table: str) -> Tuple[str, ...]:
    schema, name = table.split(".", 1)
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ?", [schema, name]).fetchall()
    return tuple(r[0] for r in rows)


def witness_for(con, graph, table: str) -> GrainWitness:
    """Measure one table, with the weight column read off the lineage graph.

    `mask.weight_for` raises when the graph offers two candidates. That refusal is passed
    through rather than caught, because a tool guessing which column is the group size is
    how a deduplicating GROUP BY gets reported as anonymisation.

    The graph and the database can disagree. A graph built from a statement that has since
    been rewritten names a column the table no longer carries, and sending that name down
    produces a binder error with the tool's own query in it. Found on 2026-10-12 by the
    obfuscated arm, which rebuilds the warehouse with every column renamed and leaves the
    graph in the original names. The refusal says which name is missing and where it came
    from, because that is a stale lineage read and not a bad table.
    """
    weight = weight_for(graph, table)
    if weight is not None and weight not in columns_of(con, table):
        raise ValueError(
            "the lineage graph says {}'s group size is in {} and the table has no such "
            "column. The graph is stale or it describes a different warehouse".format(
                table, weight))
    return measure_grain(con, table, weight)


def rescue_floor(strength: float = TEMPORAL_TYPE_STRENGTH,
                 review_at: float = REVIEW_AT) -> float:
    """The lowest `singleton_share` at which a rescued temporal column still queues.

    The attenuated signal is `strength` times the share, and `review_at` is where a column
    starts reaching a person. Below this share the rescue runs and the column lands in the
    ignore band anyway, which looks like the rescue failing and is the bands working.

    This is here because day 3 of this project found a threshold that could not move a
    verdict at any setting, for exactly this reason, and found it by sweeping it rather
    than by deriving it. Derived this time, and a check pins it against both constants.
    """
    if not 0.0 < strength <= 1.0:
        raise ValueError("a strength of {} cannot carry a floor".format(strength))
    return review_at / strength


def confidence_map(con,
                   graph,
                   tables: Sequence[str]) -> Dict[str, float]:
    """One grain confidence per table, ready for `classify_warehouse`.

    A table the graph says was never grouped gets 1.0. Its rows are people, so evidence
    that the table is about people transfers to its columns without discount.
    """
    out: Dict[str, float] = {}
    for table in sorted(tables):
        witness = witness_for(con, graph, table)
        out[table] = 1.0 if not witness.was_grouped else witness.singleton_share
    return out


def witnesses_for(con, graph, tables: Sequence[str]) -> Tuple[GrainWitness, ...]:
    """Every witness, kept so a report can print what the confidence came from."""
    return tuple(witness_for(con, graph, table) for table in sorted(tables))


@dataclass(frozen=True)
class GrainVariant:
    """The same mart regrouped, so the measurement has something to be wrong about.

    One warehouse produces one share, and a rule validated on one share is a rule nobody
    tested. These rebuild `analytics.encounter_daily` from the same source rows at coarser
    groupings. The column names do not change. Neither do the types or the arm set. The
    only thing that moves is how many encounters land in a group.

    The temporal key is cast back to DATE even when it is a month truncation. That is
    deliberate. The column is still a date and the structure arm still fires on it, so the
    comparison isolates the grain instead of also changing which arms speak.
    """

    table: str
    key_expressions: Tuple[str, ...]
    key_columns: Tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.key_expressions) != len(self.key_columns):
            raise ValueError(
                "{} names {} key expressions and {} key columns".format(
                    self.table, len(self.key_expressions), len(self.key_columns)))
        if "day" not in self.key_columns:
            raise ValueError(
                "{} has no temporal key, so there is no rescue to measure".format(
                    self.table))

    @property
    def columns(self) -> Tuple[str, ...]:
        return self.key_columns + ("encounters", "mean_length_of_stay_h")

    def create_sql(self) -> str:
        types = {"day": "DATE NOT NULL", "department": "VARCHAR",
                 "postal_code": "VARCHAR"}
        lines = ["  {} {}".format(c, types[c]) for c in self.key_columns]
        lines.append("  encounters BIGINT NOT NULL")
        lines.append("  mean_length_of_stay_h DOUBLE")
        return "CREATE TABLE {} (\n{}\n);".format(self.table, ",\n".join(lines))

    def insert_sql(self) -> str:
        """An INSERT SELECT in the shape `lineage.read_insert_select` parses.

        `count(*)` has to stay written as `count(*)` rather than as a count over a named
        column, because that star is how `mask.weight_candidates` tells a group size from
        an average over rows.
        """
        selects = ["            {:<55} AS {}".format(e, c)
                   for e, c in zip(self.key_expressions, self.key_columns)]
        selects.append("            {:<55} AS encounters".format("count(*)"))
        selects.append("            {:<55} AS mean_length_of_stay_h".format(
            "avg(date_diff('minute', e.admitted_at, e.discharged_at) / 60.0)"))
        return (
            "INSERT INTO {}\n        SELECT\n{}\n"
            "        FROM raw.encounter e\n"
            "        GROUP BY {}".format(
                self.table, ",\n".join(selects),
                ", ".join(str(i + 1) for i in range(len(self.key_columns)))))


# Two coarsenings, picked because they straddle the floor rather than to make a point.
# `day_dept` drops one key from the shipped grain and `month_dept` coarsens the date.
COARSER: Tuple[GrainVariant, ...] = (
    GrainVariant(
        table="analytics.encounter_day_dept",
        key_expressions=("CAST(e.admitted_at AS DATE)", "e.department"),
        key_columns=("day", "department"),
    ),
    GrainVariant(
        table="analytics.encounter_month_dept",
        key_expressions=("CAST(date_trunc('month', e.admitted_at) AS DATE)",
                         "e.department"),
        key_columns=("day", "department"),
    ),
)


def build_variant(con, variant: GrainVariant) -> None:
    """Create and fill one coarser mart. Dropped first, so a rerun measures the rebuild."""
    con.execute("DROP TABLE IF EXISTS {}".format(variant.table))
    con.execute(variant.create_sql().rstrip(";"))
    con.execute(variant.insert_sql())


@dataclass(frozen=True)
class RescueRow:
    """One table's grain and what the rescue did with it.

    `binary_confidence` is what the pre-2026-10-12 rescue gave, which is the undiscounted
    strength on every table it touched. Keeping both on one row is the only way the
    comparison is readable, because the claim being made is about the difference.
    """

    table: str
    rows: int
    population: int
    k_anonymity: int
    singleton_rows: int
    singleton_share: float
    confidence: float
    binary_confidence: float

    @property
    def band(self) -> str:
        return band_of(self.confidence)

    @property
    def binary_band(self) -> str:
        return band_of(self.binary_confidence)

    @property
    def moved(self) -> bool:
        return self.band != self.binary_band


def band_of(confidence: float) -> str:
    """The band name a confidence lands in.

    This reads `classify`'s own thresholds rather than carrying copies. A report that
    hard codes 0.35 is a report that disagrees with the tool the day somebody moves it.
    """
    from pii.classify import ACCEPT_AT

    if confidence >= ACCEPT_AT:
        return "accept"
    if confidence >= REVIEW_AT:
        return "review"
    return "ignore"


@dataclass(frozen=True)
class RescueReport:
    rows: Tuple[RescueRow, ...]

    def __post_init__(self) -> None:
        if not self.rows:
            raise ValueError("a rescue report over no tables grades nothing")

    @property
    def floor(self) -> float:
        return rescue_floor()

    def k_separates(self) -> bool:
        """Whether k on the grain tells the rescued tables from the refused ones.

        This is the question `ot-084` asked for by name and the answer here is no. k is
        `min` over every group, so a single group of one holds it at 1 whatever the other
        fourteen hundred groups do. Two tables that end in different bands can share a k,
        and when they do, k is a necessary condition and not a score. Reported as a boolean
        because the thread's premise is worth refuting out loud rather than quietly
        replacing.
        """
        by_k: Dict[int, set] = {}
        for row in self.rows:
            by_k.setdefault(row.k_anonymity, set()).add(row.band)
        return all(len(bands) == 1 for bands in by_k.values())

    def share_separates(self) -> bool:
        """The same question for the share, which is the statistic that replaced k."""
        ordered = sorted(self.rows, key=lambda r: r.singleton_share)
        bands = [r.band for r in ordered]
        return bands == sorted(bands, key=lambda b: ("ignore", "review", "accept").index(b))

    def manifest(self) -> Dict[str, object]:
        return {
            "floor": round(self.floor, 6),
            "review_at": REVIEW_AT,
            "temporal_strength": TEMPORAL_TYPE_STRENGTH,
            "k_separates_the_bands": self.k_separates(),
            "share_separates_the_bands": self.share_separates(),
            "tables": [
                {
                    "table": r.table,
                    "rows": r.rows,
                    "population": r.population,
                    "k_anonymity": r.k_anonymity,
                    "singleton_rows": r.singleton_rows,
                    "singleton_share": round(r.singleton_share, 6),
                    "confidence": round(r.confidence, 6),
                    "band": r.band,
                    "binary_confidence": round(r.binary_confidence, 6),
                    "binary_band": r.binary_band,
                }
                for r in sorted(self.rows, key=lambda r: r.table)
            ],
        }


def classify_shipped(con,
                     profiles,
                     graph=None,
                     evidence_bar: float = REVIEW_AT):
    """The classification the tool ships, with the grain measured rather than assumed.

    Six places used to build `warehouse_graph(con).upstream_table_map(...)` from the same
    profiles and agree by luck. `lineage.warehouse_graph` fixed half of that on 2026-10-09
    and left the map itself copied around. Adding a third thing every caller needs is what
    makes a seventh copy inevitable, so this is the one place it happens now.

    A caller wanting the undiscounted rescue, or an arm set other than the shipped three,
    calls `classify.classify_warehouse` directly. That is still how `pii/ablation.py` runs
    an arm set and how `scripts/lineage_probe.py` measures what propagation adds, and both
    have reasons written down where they do it. This function takes no arm set for that
    reason. A parameter no caller passes is a parameter no check can grade, and a mutation
    pass on 2026-10-12 proved it by flipping the default and surviving.
    """
    from pii import classify, lineage

    if graph is None:
        graph = lineage.warehouse_graph(con)
    tables = sorted({p.table for p in profiles})
    return classify.classify_warehouse(
        profiles,
        evidence_bar,
        graph.upstream_table_map(tables),
        classify.ALL_ARMS,
        confidence_map(con, graph, tables),
    )
