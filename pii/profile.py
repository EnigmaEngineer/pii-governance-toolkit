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

That cost is now a number rather than a paragraph, and `pii/sample.py` is where it was
measured. Nothing in this module changed to get it. The sampling arm takes its own
connection and builds its own evidence, and the only mark it leaves here is the
`sampled_signals` field in the allowlist below. A profile this module produces still holds
no value and no shape, because this module never asks for one.

One query per column was the original shape and the reason given for it was wrong. The
docstring said a table-wide query names every predicate for every column, which is a
product growing quadratically in the width of the table. It is not. The predicate set is
fixed at six, so a table of W columns needs 9W expressions, which is linear. So the
answer to where one query per column stops being fine started with an arithmetic error
in the reason for not asking.

Both shapes are here now. `profile_column` is the reference implementation and
`profile_table` is the batched one, and `disagreements` is the check that holds them to
the same answer rather than a paragraph saying they agree. `pii/scale.py` times them.
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


def _profile_table_sql(table_schema: str, table_name: str,
                       columns: Sequence) -> Tuple[str, Tuple[str, ...], int]:
    """One statement covering every column of a table, and how to read the row back.

    Returns the SQL, the predicate keys in the order they appear per column, and the
    stride, which is how many of the returned values belong to each column. Three things
    rather than one because the row that comes back is flat and the only way to cut it
    into columns is to know the stride, and a stride computed at the call site is a second
    copy of a fact this function already decided.

    `count(*)` is named once and shared. Every column of a table has the same row count
    and the per-column path asks for it W times.
    """
    parts: List[str] = ["count(*)"]
    keys: List[str] = []
    for i, column in enumerate(columns):
        col = _quote(column.name)
        parts.append("count({})".format(col))
        parts.append("count(distinct {})".format(col))
        parts.append("avg(length(cast({} as varchar)))".format(col))
        for key, expr, _strength, _why in VALUE_PREDICATES:
            parts.append("count(*) filter (where {})".format(expr.format(c=col)))
            if i == 0:
                keys.append(key)
    stride = 3 + len(VALUE_PREDICATES)
    sql = "SELECT {} FROM {}.{}".format(
        ", ".join(parts), _quote(table_schema), _quote(table_name))
    return sql, tuple(keys), stride


def profile_table(con, table_schema: str, table_name: str,
                  columns: Sequence) -> Tuple[ColumnProfile, ...]:
    """Profile every column of one table in one round trip.

    The same numbers `profile_column` returns, obtained by naming the same expressions in
    one statement instead of W. `disagreements` is what holds that claim up.

    An empty column list returns nothing without touching the database. A table with no
    columns is not a thing DuckDB will produce, and `SELECT count(*) FROM t` with nothing
    to cut into would otherwise come back as a row this function has to decide what to do
    with.
    """
    if not columns:
        return ()
    sql, keys, stride = _profile_table_sql(table_schema, table_name, columns)
    row = con.execute(sql).fetchone()
    rows = row[0]
    out: List[ColumnProfile] = []
    for i, column in enumerate(columns):
        base = 1 + i * stride
        non_null, distinct, mean_length = row[base], row[base + 1], row[base + 2]
        hits = {}
        for key, value in zip(keys, row[base + 3:base + stride]):
            if value > non_null:
                raise ValueError(
                    "predicate {} matched {} of {} non null values in {}.{}.{}".format(
                        key, value, non_null, table_schema, table_name, column.name))
            hits[key] = int(value)
        out.append(ColumnProfile(
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
        ))
    return tuple(out)


def profile_crawl_batched(con, crawled: Crawl) -> Tuple[ColumnProfile, ...]:
    """`profile_crawl` at one statement per table instead of one per column."""
    out: List[ColumnProfile] = []
    for t in crawled.tables:
        out.extend(profile_table(con, t.schema, t.name, t.columns))
    return tuple(out)


def disagreements(first: Sequence[ColumnProfile],
                  second: Sequence[ColumnProfile]) -> Tuple[str, ...]:
    """Where two profile sets differ, field by field.

    This is the whole justification for the batched path existing. A faster query shape
    that returns a different number is not a faster query shape, it is a second classifier.
    Floats are compared with a tolerance because `avg` over a different expression order
    can land a bit apart, and integers are compared exactly because a count that moves is a
    defect rather than a rounding.
    """
    by_address = {p.address: p for p in second}
    bad: List[str] = []
    if len(first) != len(second):
        bad.append("{} profiles against {}".format(len(first), len(second)))
    for a in first:
        b = by_address.get(a.address)
        if b is None:
            bad.append("{} missing from the second set".format(a.address))
            continue
        for field in ("sql_type", "nullable", "is_key", "rows", "non_null", "distinct"):
            if getattr(a, field) != getattr(b, field):
                bad.append("{} {} is {} and {}".format(
                    a.address, field, getattr(a, field), getattr(b, field)))
        if abs(a.mean_length - b.mean_length) > 1e-9:
            bad.append("{} mean_length is {} and {}".format(
                a.address, a.mean_length, b.mean_length))
        if a.pattern_hits != b.pattern_hits:
            for key in sorted(set(a.pattern_hits) | set(b.pattern_hits)):
                if a.pattern_hits.get(key) != b.pattern_hits.get(key):
                    bad.append("{} hit {} is {} and {}".format(
                        a.address, key, a.pattern_hits.get(key),
                        b.pattern_hits.get(key)))
    return tuple(bad)


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
               "rows", "non_null", "distinct", "mean_length", "pattern_hits",
               # Allowed and not free. A sampled signal's detail is built from a shape and
               # two integers, so what it carries is bounded by the shape allowlist in
               # `pii/sample.py` and checked by `samples_hold_no_values` rather than here.
               # This check cannot reach the shape, since the sample object stays behind.
               "sampled_signals"}
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
