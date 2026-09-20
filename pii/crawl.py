"""Recover the warehouse schema from a live catalog.

This module exists to break a circularity the README named. `pii/schema.py`
is a hand written description of the warehouse `scripts/plant.py` then builds, so any check
that the two agree is a check that one of them wrote the other. Nothing was learned by
running it.

The crawler reads the database instead. It asks the catalog what tables exist and what
columns they hold. It asks each column for its type, its nullability and whether it is a
key. Then it builds `Table` and `Column` objects out of the answers. Those are the same objects
`pii/schema.py` declares, so the two can be compared field by field, and `schema.fingerprint()`
is pinned to `1501a19ca3d8`, which gives the comparison a golden value to hit rather than a
vibe.

Recovering the key flag took a change to the warehouse rather than to the crawler. The DDL
`plant.py` emitted carried no primary key at all, so four of the five fields in that hash
were in the catalog and the fifth was nowhere, and the pinned value was unreachable for a
reason that had nothing to do with reading it. The keys are declared now.

Three things it does not do, stated here because each one is a limit somebody will otherwise
assume away.

It cannot recover a planted label. The labels are a judgement about personal data and no
catalog holds one. `PlantedColumn` stays out of here on purpose and the classifier that
arrives next is what reads the recovered metadata.

It does not read a row. Nothing in this module runs a `SELECT` against user data. Column
names, types and flags all come out of the catalog, which matters for a tool that is going
to be pointed at a warehouse whose contents are the thing being governed.

The nullability it recovers is only ever as good as the DDL that was loaded. `plant.py`
writes that DDL out of `pii/schema.py`, so a recovered `nullable=True` is my own declaration
having made a round trip. That is why `grade_nullability` exists and why it reads data: a
declaration is a claim about what the column may hold, and only the rows can say whether
anything ever exercised it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from pii.schema import Column, Table, fingerprint, tables_by_fqn

# Catalog schemas DuckDB ships with. Everything in them belongs to the engine and none of
# it is warehouse metadata, so the crawl skips them by name. Listed rather than inferred
# from `internal`, because `duckdb_schemas().internal` is a property of the schema and a
# user schema in an attached read only database is a case this has not been tested against.
ENGINE_SCHEMAS = ("information_schema", "main", "pg_catalog", "temp")


@dataclass(frozen=True)
class Difference:
    """One disagreement between the declared schema and the crawled one."""

    where: str
    field: str
    declared: object
    crawled: object

    def __str__(self) -> str:
        return "{:<42} {:<10} declared {!r:<18} crawled {!r}".format(
            self.where, self.field, self.declared, self.crawled)


@dataclass(frozen=True)
class RecoveredReference:
    """One declared foreign key, read out of the catalog.

    The referenced table arrives from `duckdb_constraints()` as a bare name with no schema
    attached, and this resolves it inside the referencing table's own schema. That is
    correct for DuckDB and only for DuckDB, which refuses a cross schema foreign key with
    a binder error rather than storing one. Any engine that allows one makes this
    resolution a guess, so the rule is written down here instead of being buried in a
    format string.
    """

    table: str
    column: str
    references_table: str
    references_column: str

    def __str__(self) -> str:
        return "{}.{} -> {}.{}".format(
            self.table, self.column, self.references_table, self.references_column)


@dataclass(frozen=True)
class Crawl:
    tables: Tuple[Table, ...]
    key_columns: Tuple[Tuple[str, str], ...]
    engine: str
    engine_version: str
    references: Tuple[RecoveredReference, ...] = ()

    @property
    def fqns(self) -> Tuple[str, ...]:
        return tuple(t.fqn for t in self.tables)

    def by_fqn(self) -> Dict[str, Table]:
        return {t.fqn: t for t in self.tables}

    @property
    def n_columns(self) -> int:
        return sum(len(t.columns) for t in self.tables)

    def fingerprint(self) -> str:
        """The same content hash `pii/schema.py` publishes, over what was crawled.

        Deliberately computed by the declared module's own function rather than by a copy
        of its body living here. A second implementation of a hash is a second answer, and
        the two would agree right up until one of them was edited.
        """
        return _fingerprint_over(self.tables)


def _fingerprint_over(tables: Sequence[Table]) -> str:
    import pii.schema as declared

    original = declared.TABLES
    try:
        declared.TABLES = tuple(tables)
        return declared.fingerprint()
    finally:
        declared.TABLES = original


def _rows(con, sql: str, params: Sequence = ()) -> List[tuple]:
    return con.execute(sql, list(params)).fetchall()


def _quote(identifier: str) -> str:
    """Quote a catalog identifier for use in SQL.

    A table or column name cannot be a bound parameter, so the only two options are
    interpolation and quoting. The first version of `grade_nullability` interpolated the
    bare name, which is fine on this corpus and wrong in principle for a tool whose whole
    premise is being pointed at a warehouse somebody else owns. A reserved word, a space,
    or a name containing a quote would produce broken SQL at best.

    Doubling an embedded quote is the ANSI escape and DuckDB follows it. A name containing
    a null byte is refused outright rather than escaped, because there is no escape for it
    and a governance tool should not be the thing that guesses.
    """
    if "\x00" in identifier:
        raise ValueError("identifier contains a null byte: {!r}".format(identifier))
    return '"{}"'.format(identifier.replace('"', '""'))


def crawl(con) -> Crawl:
    """Build the metadata model from a catalog, given an open DuckDB connection.

    Takes a connection rather than a path so the caller decides whether it is read only.
    A governance tool that opens its own writable handle to the warehouse it is auditing
    has made that decision for somebody who would have wanted to make it themselves.
    """
    version = _rows(con, "SELECT version()")[0][0]

    placeholders = ", ".join("?" for _ in ENGINE_SCHEMAS)
    column_rows = _rows(con, """
        SELECT schema_name, table_name, column_name, column_index, data_type, is_nullable
        FROM duckdb_columns()
        WHERE schema_name NOT IN ({})
        ORDER BY schema_name, table_name, column_index
    """.format(placeholders), ENGINE_SCHEMAS)

    # Keys come from the constraint catalog, not from a naming convention. A crawler that
    # decides `patient_id` is a key because it ends in `_id` is guessing, and it would call
    # `raw.encounter.patient_id` a key too, which it is not.
    key_rows = _rows(con, """
        SELECT schema_name, table_name, constraint_column_names
        FROM duckdb_constraints()
        WHERE constraint_type = 'PRIMARY KEY'
          AND schema_name NOT IN ({})
    """.format(placeholders), ENGINE_SCHEMAS)

    keys = set()
    for schema_name, table_name, columns in key_rows:
        for name in columns:
            keys.add(("{}.{}".format(schema_name, table_name), name))

    # The same catalog view carries the referential half and the first version of this
    # function read none of it. `raw.encounter.patient_id` pointed at `raw.patient.patient_id`
    # in the DDL and nothing downstream could see the relationship, so every question about
    # which table a date belonged to was answered by a heuristic over column names instead.
    reference_rows = _rows(con, """
        SELECT schema_name, table_name, constraint_column_names,
               referenced_table, referenced_column_names
        FROM duckdb_constraints()
        WHERE constraint_type = 'FOREIGN KEY'
          AND schema_name NOT IN ({})
        ORDER BY schema_name, table_name
    """.format(placeholders), ENGINE_SCHEMAS)

    references = []
    for schema_name, table_name, columns, ref_table, ref_columns in reference_rows:
        for local, remote in zip(columns, ref_columns):
            references.append(RecoveredReference(
                table="{}.{}".format(schema_name, table_name),
                column=local,
                references_table="{}.{}".format(schema_name, ref_table),
                references_column=remote,
            ))

    grouped: Dict[Tuple[str, str], List[Column]] = {}
    for schema_name, table_name, column_name, _index, data_type, is_nullable in column_rows:
        fqn = "{}.{}".format(schema_name, table_name)
        grouped.setdefault((schema_name, table_name), []).append(Column(
            name=column_name,
            sql_type=data_type,
            nullable=bool(is_nullable),
            is_key=(fqn, column_name) in keys,
        ))

    tables = tuple(
        Table(schema=schema_name, name=table_name, columns=tuple(cols))
        for (schema_name, table_name), cols in sorted(grouped.items())
    )
    return Crawl(
        tables=tables,
        key_columns=tuple(sorted(keys)),
        engine="duckdb",
        engine_version=str(version),
        references=tuple(sorted(
            references, key=lambda r: (r.table, r.column))),
    )


def compare_references(crawled: Crawl) -> Tuple[Difference, ...]:
    """Declared references against recovered ones, both directions like `compare`.

    Kept out of `compare` on purpose. That function walks columns and its result feeds the
    fingerprint story, and a reference is not one of the five fields in the hash. Folding
    these in would make a green `compare` mean two different things.
    """
    # Read off the module rather than off a name bound at import time, the same way
    # `_fingerprint_over` reaches for `declared.TABLES`, so a test that swaps the declared
    # schema swaps its references with it.
    import pii.schema as declared

    declared_refs = {
        (fk.table, fk.column): (fk.references_table, fk.references_column)
        for fk in declared.FOREIGN_KEYS
    }
    found = {
        (r.table, r.column): (r.references_table, r.references_column)
        for r in crawled.references
    }
    out: List[Difference] = []
    for key in sorted(set(declared_refs) | set(found)):
        where = "{}.{}".format(*key)
        if key not in found:
            out.append(Difference(where, "reference", declared_refs[key], None))
        elif key not in declared_refs:
            out.append(Difference(where, "reference", None, found[key]))
        elif declared_refs[key] != found[key]:
            out.append(Difference(where, "reference", declared_refs[key], found[key]))
    return tuple(out)


def compare(crawled: Crawl) -> Tuple[Difference, ...]:
    """Field by field against the declared schema, in both directions.

    Both directions because one direction is the easy half. Walking the declared schema and
    looking each column up in the crawl finds anything the crawl missed and is blind to
    anything the warehouse holds that nobody declared, which on a real warehouse is most of
    what a governance tool is for. The same mistake the clause mapping made, where every
    clause had a category and not every category had a clause.
    """
    declared = tables_by_fqn()
    found = crawled.by_fqn()
    out: List[Difference] = []

    for fqn in sorted(set(declared) | set(found)):
        if fqn not in found:
            out.append(Difference(fqn, "table", "declared", None))
            continue
        if fqn not in declared:
            out.append(Difference(fqn, "table", None, "crawled"))
            continue

        d_cols = {c.name: c for c in declared[fqn].columns}
        f_cols = {c.name: c for c in found[fqn].columns}
        for name in sorted(set(d_cols) | set(f_cols)):
            where = "{}.{}".format(fqn, name)
            if name not in f_cols:
                out.append(Difference(where, "column", "declared", None))
                continue
            if name not in d_cols:
                out.append(Difference(where, "column", None, "crawled"))
                continue
            d, f = d_cols[name], f_cols[name]
            for field in ("sql_type", "nullable", "is_key"):
                if getattr(d, field) != getattr(f, field):
                    out.append(Difference(where, field,
                                          getattr(d, field), getattr(f, field)))

        # Reported separately rather than folded in, because a reordered table is a
        # different thing from a changed one and only one of them breaks a `SELECT *`.
        #
        # The fingerprint is asymmetric here and it is worth knowing which way. It sorts
        # the tables, so reversing the table order leaves the hash identical. It does not
        # sort the columns, so swapping two columns inside a table moves it. `sort_keys`
        # in `schema.fingerprint` sorts the keys of each row dict and has no opinion about
        # the order of the list. Measured both ways after a check written on the
        # opposite assumption failed.
        d_order = [c.name for c in declared[fqn].columns]
        f_order = [c.name for c in found[fqn].columns]
        if d_order != f_order and sorted(d_order) == sorted(f_order):
            out.append(Difference(fqn, "order", tuple(d_order), tuple(f_order)))

    return tuple(out)


def recovers_declared_schema(crawled: Crawl) -> bool:
    """The whole point, as one boolean, and it is the fingerprint that decides it."""
    return crawled.fingerprint() == fingerprint()


@dataclass(frozen=True)
class NullabilityVerdict:
    """What the data says about one recovered nullable flag.

    `exercised` is the only field here that is evidence. The rest is bookkeeping.
    """

    table: str
    column: str
    crawled_nullable: bool
    nulls_observed: int
    rows: int

    @property
    def address(self) -> str:
        return "{}.{}".format(self.table, self.column)

    @property
    def contradicted(self) -> bool:
        """A NOT NULL column holding a null. The database should make this impossible."""
        return not self.crawled_nullable and self.nulls_observed > 0

    @property
    def exercised(self) -> bool:
        """A nullable column that really does hold a null somewhere."""
        return self.crawled_nullable and self.nulls_observed > 0

    @property
    def unexercised(self) -> bool:
        """Declared nullable and nothing has ever put a null in it.

        Not a defect. It is the absence of evidence, and the reason it is named is that
        26 columns were in this state at once and the repo read as though it had been checked.
        """
        return self.crawled_nullable and self.nulls_observed == 0


def grade_nullability(con, crawled: Crawl) -> Tuple[NullabilityVerdict, ...]:
    """Count nulls per crawled column and judge the recovered flag against them.

    This is the one function here that reads rows, and it reads counts rather than values.
    A `count(*) - count(column)` per column returns no personal data, which matters because
    the whole point of the tool is that somebody can point it at data they are not allowed
    to look at.

    Both directions, which is the half that could not be done before the corpus wrote a null. A NOT NULL column holding a
    null contradicts the schema. A nullable column holding no null does not contradict
    anything, and saying so is the difference between a declaration and a claim about the
    data.
    """
    out: List[NullabilityVerdict] = []
    for t in crawled.tables:
        exprs = ", ".join(
            "count(*) - count({})".format(_quote(c.name)) for c in t.columns
        )
        row = _rows(con, "SELECT count(*), {} FROM {}.{}".format(
            exprs, _quote(t.schema), _quote(t.name)))[0]
        total = row[0]
        for c, nulls in zip(t.columns, row[1:]):
            out.append(NullabilityVerdict(
                table=t.fqn,
                column=c.name,
                crawled_nullable=c.nullable,
                nulls_observed=int(nulls),
                rows=int(total),
            ))
    return tuple(out)


def unexercised_nullable(verdicts: Sequence[NullabilityVerdict]) -> Tuple[str, ...]:
    return tuple(v.address for v in verdicts if v.unexercised)


def contradictions(verdicts: Sequence[NullabilityVerdict]) -> Tuple[str, ...]:
    return tuple(v.address for v in verdicts if v.contradicted)


# `first_difference` used to live here, returning compare()[0] or None. Nothing called it.
# The probe prints every difference and the suite asserts on the whole tuple, so it was a
# convenience for a caller that does not exist, and a mutant changing which element it
# returned survived because no test could reach it. Deleted rather than wired up, since
# there is nothing to wire it to. Twenty second day of finding one of these.
