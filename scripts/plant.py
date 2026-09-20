"""Build the sample warehouse and write the planted answer key beside it.

    python3 scripts/plant.py --db /tmp/pii.duckdb

Writes the five tables into a DuckDB database and prints the answer key as a table. The
metadata crawler that arrives next reads this database, so the schema here is the thing it
has to discover rather than a description of one.

DuckDB is the local stand in. The DDL this emits is the same shape a Snowflake account
would take, and the Snowflake dialect goes in beside it when there is an account to run it
against. Writing Snowflake SQL that has never executed and presenting it as verified is the
thing this project is not going to do.

The database goes under a path you give it. It must not go on a synced or network folder,
because DuckDB has to unlink its write ahead log to check point and some mounts refuse
that, which surfaces as a fatal error that reads like corruption.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii.corpus import ROWS_FOR, generate, null_counts, summarise  # noqa: E402
from pii.schema import (  # noqa: E402
    DERIVED_SQL, FOREIGN_KEYS, PLANTED, TABLES,
    check_foreign_keys_are_real, check_planting_is_total,
)
from pii.taxonomy import TAXONOMY  # noqa: E402


def ddl_for(table) -> str:
    """The DDL for one table, including its primary key and its references.

    The key used to be left out, and that made `Column.is_key` a flag only this repo knew
    about. It is one of the five fields in `schema.fingerprint()`, so a crawler reading the
    catalog back could recover four of them and had no way to recover the fifth, which
    means the pinned fingerprint was unreachable for a reason that had nothing to do with
    the crawler. Declaring the key puts it in the catalog where a crawl can find it.

    It is not free. A primary key in DuckDB is enforced, so the loader now refuses a
    duplicate id rather than accepting one, which is a stricter warehouse than the one the
    earlier figures came off. That is the right direction and it is a change in behaviour
    rather than a change in documentation.

    The foreign keys are the same move made a second time. A join key was a thing this repo
    knew and the catalog did not, so a crawler had no way to recover one. Declaring them
    puts them where `duckdb_constraints()` can answer for them. The same cost applies and
    it is larger: an enforced reference means the loader has to drop child tables before
    parents and insert parents before children, which is the ordering `load` now does
    on purpose rather than by luck.
    """
    cols = []
    for c in table.columns:
        line = "  {} {}".format(c.name, c.sql_type)
        if not c.nullable:
            line += " NOT NULL"
        cols.append(line)
    keys = [c.name for c in table.columns if c.is_key]
    if keys:
        cols.append("  PRIMARY KEY ({})".format(", ".join(keys)))
    for fk in FOREIGN_KEYS:
        if fk.table != table.fqn:
            continue
        cols.append("  FOREIGN KEY ({}) REFERENCES {} ({})".format(
            fk.column, fk.references_table, fk.references_column))
    return "CREATE TABLE {} (\n{}\n);".format(table.fqn, ",\n".join(cols))


def emit_ddl(path: str) -> int:
    body = ["-- The sample warehouse, generated from pii/schema.py.",
            "-- Edit that module rather than this file, which is overwritten.",
            ""]
    for schema in sorted({t.schema for t in TABLES}):
        body.append("CREATE SCHEMA IF NOT EXISTS {};".format(schema))
    body.append("")
    for t in TABLES:
        body.append(ddl_for(t))
        body.append("")
    text = "\n".join(body)
    with open(path, "w") as fh:
        fh.write(text)
    return len(TABLES)


def load(db_path: str, corpus) -> dict:
    import duckdb

    con = duckdb.connect(db_path)
    try:
        for schema in sorted({t.schema for t in TABLES}):
            con.execute("CREATE SCHEMA IF NOT EXISTS {}".format(schema))
        # Every drop first, children before parents, because an enforced reference makes
        # `DROP TABLE raw.patient` fail while `raw.encounter` still points at it. The old
        # loop dropped and created one table at a time and worked only because nothing
        # referenced anything. Reversing `TABLES` is enough here since the declaration
        # order is already parents first, and a real dependency sort is what this needs if
        # the schema ever stops being a straight line.
        for t in reversed(TABLES):
            con.execute("DROP TABLE IF EXISTS {}".format(t.fqn))

        loaded = {}
        for t in TABLES:
            con.execute(ddl_for(t).rstrip(";"))
            attr = ROWS_FOR.get(t.fqn)
            if attr is None:
                # analytics.encounter_daily is derived rather than generated, so it is
                # built by SQL out of what was just loaded. A mart nobody populates is a
                # mart the lineage work later cannot use.
                continue
            rows = getattr(corpus, attr)
            names = [c.name for c in t.columns]
            placeholders = ", ".join("?" for _ in names)
            con.executemany(
                "INSERT INTO {} VALUES ({})".format(t.fqn, placeholders),
                [tuple(r[n] for n in names) for r in rows],
            )
            loaded[t.fqn] = len(rows)

        con.execute(DERIVED_SQL["analytics.encounter_daily"])
        loaded["analytics.encounter_daily"] = con.execute(
            "SELECT count(*) FROM analytics.encounter_daily").fetchone()[0]

        # Read every count back out of the database rather than trusting the list length
        # that went in. A loader reporting what it intended to write is not a check.
        verified = {}
        for fqn in loaded:
            verified[fqn] = con.execute(
                "SELECT count(*) FROM {}".format(fqn)).fetchone()[0]
        return verified
    finally:
        con.close()


def print_answer_key() -> None:
    print("\n{:<26} {:<22} {:<22} {:<8} {}".format(
        "table", "column", "category", "identity", "identifies"))
    print("-" * 96)
    for p in PLANTED:
        cat = p.category
        print("{:<26} {:<22} {:<22} {:<8} {}".format(
            p.table, p.column, p.category_key,
            cat.identifiability.value[:8],
            "yes" if p.identifies() else "no"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the sample warehouse.")
    ap.add_argument("--db", default="/tmp/pii.duckdb",
                    help="where to write the DuckDB file")
    ap.add_argument("--patients", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260917)
    ap.add_argument("--ddl", default="schemas/warehouse.sql",
                    help="where to write the generated DDL")
    ap.add_argument("--key", action="store_true", help="print the planted answer key")
    args = ap.parse_args()

    for check in (check_planting_is_total, check_foreign_keys_are_real):
        problem = check()
        if problem is not None:
            print("refusing to build: {}".format(problem))
            return 2

    corpus = generate(n_patients=args.patients, seed=args.seed)

    s = summarise(corpus)
    print("generated, all of it synthetic")
    for k in ("counts", "distinct_postal", "distinct_birth_date", "age_min",
              "age_median", "age_max", "ages_known", "patients_with_no_encounter",
              "max_encounters_per_patient", "nullable_columns", "nulls_total"):
        print("  {:<28} {}".format(k, s[k]))

    # Every nullable column has to hold at least one null or its flag is still a
    # declaration nothing exercised, which was the state the whole corpus was in before.
    empty = [k for k, v in null_counts(corpus).items() if v == 0]
    print("  {:<28} {}".format("nullable, never null", ", ".join(empty) or "none"))

    n = emit_ddl(args.ddl)
    print("wrote {} table definitions to {}".format(n, args.ddl))

    verified = load(args.db, corpus)
    print("loaded into {}".format(args.db))
    for fqn in sorted(verified):
        print("  {:<30} {}".format(fqn, verified[fqn]))

    print("taxonomy {} over {} categories, {} planted columns".format(
        TAXONOMY.fingerprint(), len(TAXONOMY), len(PLANTED)))

    if args.key:
        print_answer_key()
    return 0


if __name__ == "__main__":
    sys.exit(main())
