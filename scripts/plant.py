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

from pii.corpus import generate, summarise  # noqa: E402
from pii.schema import PLANTED, TABLES, check_planting_is_total  # noqa: E402
from pii.taxonomy import TAXONOMY  # noqa: E402

ROWS_FOR = {
    "raw.patient": "patients",
    "raw.encounter": "encounters",
    "raw.claim": "claims",
    "raw.device_reading": "readings",
}


def ddl_for(table) -> str:
    cols = []
    for c in table.columns:
        line = "  {} {}".format(c.name, c.sql_type)
        if not c.nullable:
            line += " NOT NULL"
        cols.append(line)
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
        loaded = {}
        for t in TABLES:
            con.execute("DROP TABLE IF EXISTS {}".format(t.fqn))
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

        con.execute("""
            INSERT INTO analytics.encounter_daily
            SELECT
                CAST(e.admitted_at AS DATE)                     AS day,
                e.department                                    AS department,
                p.postal_code                                   AS postal_code,
                count(*)                                        AS encounters,
                avg(date_diff('minute', e.admitted_at,
                              e.discharged_at) / 60.0)          AS mean_length_of_stay_h
            FROM raw.encounter e
            JOIN raw.patient p ON p.patient_id = e.patient_id
            GROUP BY 1, 2, 3
        """)
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

    problem = check_planting_is_total()
    if problem is not None:
        print("refusing to build: {}".format(problem))
        return 2

    corpus = generate(n_patients=args.patients, seed=args.seed)

    s = summarise(corpus)
    print("generated, all of it synthetic")
    for k in ("counts", "distinct_postal", "distinct_birth_date", "age_min",
              "age_median", "age_max", "patients_with_no_encounter",
              "max_encounters_per_patient"):
        print("  {:<28} {}".format(k, s[k]))

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
