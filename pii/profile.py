"""Build a `ColumnProfile` by asking the database to count things.

The rule this module exists to enforce: no value from a user column is ever returned to
Python. Every predicate the classifier holds is pushed down as a `count(*) filter (...)`
and what comes back is an integer. A reviewer reading a profile learns that 847 of 976 non
null values in `raw.patient.email` match an email pattern, and learns nothing about who
those people are.

That is the whole reason the predicates live in `pii/classify.py` as SQL strings rather
than as Python regexes applied to a sample. A sample means the values are in the process,
in a traceback, and in whatever the caller does next.

The cost is real and it is worth naming rather than glossing. A classifier that can only
count the matches of patterns it already holds cannot find a pattern nobody wrote down. A
column of passport numbers in a format this repo has never heard of returns zero on every
predicate and falls to the name arm. Sampling would find it. Sampling would also read it.

One query per column rather than one per table. A table query would be fewer round trips
and it would have to name every predicate for every column, which is a product that grows
quadratically in the width of the table and produces SQL nobody can read in a log.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from pii.classify import VALUE_PREDICATES, ColumnProfile
from pii.crawl import Crawl, _quote


def _profile_sql(table_schema: str, table_name: str, column: str) -> Tuple[str, Tuple[str, ...]]:
    """The one statement per column, and the predicate keys its columns line up with.

    Returned together on purpose. The statement's shape and the order the hits come back in
    are one fact, and splitting them across two functions is how the second one goes stale.
    """
    col = _quote(column)
    parts = [
        "count(*)",
        "count({})".format(col),
        "count(distinct {})".format(col),
        "avg(length(cast({} as varchar)))".format(col),
    ]
    keys: List[str] = []
    for key, expr, _strength, _why in VALUE_PREDICATES:
        parts.append("count(*) filter (where {})".format(expr.format(c=col)))
        keys.append(key)
    sql = "SELECT {} FROM {}.{}".format(
        ", ".join(parts), _quote(table_schema), _quote(table_name))
    return sql, tuple(keys)


def profile_column(con, table_schema: str, table_name: str, column) -> ColumnProfile:
    sql, keys = _profile_sql(table_schema, table_name, column.name)
    row = con.execute(sql).fetchone()
    rows, non_null, distinct, mean_length = row[0], row[1], row[2], row[3]
    hits = {}
    for key, value in zip(keys, row[4:]):
        # A predicate counts over every row including the null ones, and a null fails every
        # test above, so the count is already over the populated values. Clamping it to the
        # non null count rather than trusting that would hide a predicate that somehow
        # matched a null, and that is a defect worth crashing on rather than smoothing.
        if value > non_null:
            raise ValueError(
                "predicate {} matched {} of {} non null values in {}.{}.{}".format(
                    key, value, non_null, table_schema, table_name, column.name))
        hits[key] = int(value)
    return ColumnProfile(
        table="{}.{}".format(table_schema, table_name),
        column=column.name,
        sql_type=column.sql_type,
        nullable=column.nullable,
        is_key=column.is_key,
        rows=int(rows),
        non_null=int(non_null),
        distinct=int(distinct),
        mean_length=float(mean_length) if mean_length is not None else 0.0,
        pattern_hits=hits,
    )


def profile_crawl(con, crawled: Crawl) -> Tuple[ColumnProfile, ...]:
    """Profile every column a crawl found, in catalog order."""
    out: List[ColumnProfile] = []
    for t in crawled.tables:
        for c in t.columns:
            out.append(profile_column(con, t.schema, t.name, c))
    return tuple(out)


def profiles_hold_no_values(profiles: Sequence[ColumnProfile]) -> Tuple[str, ...]:
    """Addresses of any profile carrying something that is not a number or metadata.

    A check rather than a comment. The fields a profile is allowed to hold are its address,
    its catalog metadata and integers, and the way this stops being true is somebody adding
    a `sample` or an `example_value` field six months from now because it would make the
    report easier to read.
    """
    allowed = {"table", "column", "sql_type", "nullable", "is_key",
               "rows", "non_null", "distinct", "mean_length", "pattern_hits"}
    bad: List[str] = []
    for p in profiles:
        extra = set(vars(p)) - allowed
        if extra:
            bad.append("{} carries {}".format(p.address, sorted(extra)))
        for value in p.pattern_hits.values():
            if not isinstance(value, int):
                bad.append("{} pattern hit is {}".format(p.address, type(value).__name__))
    return tuple(bad)


def null_rate_table(profiles: Sequence[ColumnProfile]) -> Dict[str, float]:
    """Null rate per column, for a reviewer deciding how much a value arm answer is worth.

    A column the value arm scored on a fifth of its rows and a column it scored on all of
    them arrive at the same place in the review queue, and this is what separates them.
    """
    return {p.address: 1.0 - p.support for p in profiles}
