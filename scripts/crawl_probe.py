"""Crawl the warehouse catalog and report what was recovered.

    python3 scripts/plant.py --db /tmp/pii.duckdb
    python3 scripts/crawl_probe.py --db /tmp/pii.duckdb

Needs DuckDB, because the whole point is that it reads a real catalog. Four sections.

The first is the recovery. Tables, columns, and whether the content hash over what came out
of the catalog matches the one `pii/schema.py` publishes. That hash is the day's result: it
is the first thing in this repo that agrees with the declared schema without having been
written from it.

The second grades the recovered nullability against the rows. Recovering a flag from a
catalog that was built from the same module that declares the flag is a round trip, and the
only thing that can grade it is data.

The third is the null effect on the uniqueness figures, which is uncomfortable and is the
reason it is printed. A null lands in a cell of its own and lowers every unique share,
which reads as though people got safer.

The fourth is controls. The recovery check would pass against a crawler that returned the
declared schema verbatim, so there is a case here where the right answer is known and is
not the one a broken crawler would give.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import crawl as crawler  # noqa: E402
from pii import reidentify  # noqa: E402
from pii.corpus import NULL_RATE, generate  # noqa: E402
from pii.schema import Column, Table, fingerprint  # noqa: E402


def rule(title):
    print("\n" + title)
    print("-" * len(title))


def section_recovery(crawled):
    rule("what came out of the catalog")

    print("engine                     {} {}".format(
        crawled.engine, crawled.engine_version))
    print("tables recovered           {}".format(len(crawled.tables)))
    print("columns recovered          {}".format(crawled.n_columns))
    print("key columns recovered      {}".format(len(crawled.key_columns)))
    for fqn, col in crawled.key_columns:
        print("  {}.{}".format(fqn, col))

    print("\ndeclared fingerprint       {}".format(fingerprint()))
    print("crawled fingerprint        {}".format(crawled.fingerprint()))
    matches = crawler.recovers_declared_schema(crawled)
    print("recovered the schema       {}".format("yes" if matches else "NO"))

    diffs = crawler.compare(crawled)
    print("differences                {}".format(len(diffs)))
    for d in diffs:
        print("  {}".format(d))
    return matches and not diffs


def section_nullability(verdicts):
    rule("what the rows say about the nullable flags")

    exercised = [v for v in verdicts if v.exercised]
    unexercised = crawler.unexercised_nullable(verdicts)
    broken = crawler.contradictions(verdicts)

    print("columns graded             {}".format(len(verdicts)))
    print("declared not null          {}".format(
        sum(1 for v in verdicts if not v.crawled_nullable)))
    print("not null holding a null    {}".format(", ".join(broken) or "none"))
    print("nullable and exercised     {}".format(len(exercised)))
    print("nullable, never null       {}".format(", ".join(unexercised) or "none"))

    print("\n{:<42} {:>7} {:>8} {:>9}".format("column", "rows", "nulls", "nullable"))
    for v in sorted(verdicts, key=lambda x: (-x.nulls_observed, x.address)):
        if v.nulls_observed == 0 and not v.crawled_nullable:
            continue
        print("{:<42} {:>7} {:>8} {:>9}".format(
            v.address, v.rows, v.nulls_observed, str(v.crawled_nullable)))

    return not broken


RATES = (0.0, 0.01, 0.02, 0.05, 0.10, 0.20, 0.40, 0.60)


def section_null_effect():
    rule("what the nulls do to the uniqueness figures")

    print("A null is missing data and it is not anonymity. Nobody is protected by this")
    print("warehouse failing to record their postal code, because somebody holding that")
    print("postal code from elsewhere is not stopped by a gap in this table. Every")
    print("uniqueness metric here disagrees, because a null is a value to a Counter.")
    print("\nWhich way that bends the number is not obvious and was measured rather than")
    print("argued. Shipped rate is {}.".format(NULL_RATE))

    pair = ["sex", "postal_code"]
    triple = ["sex", "postal_code", "birth_date"]
    collected = {}

    for cols in (pair, triple):
        print("\n{}".format(" + ".join(cols)))
        print("{:>6} {:>10} {:>11} {:>11} {:>11}".format(
            "rate", "complete", "nulls as a", "complete", "honest minus"))
        print("{:>6} {:>10} {:>11} {:>11} {:>11}".format(
            "", "rows", "value", "rows only", "naive"))
        effects = []
        for rate in RATES:
            corpus = generate(null_rate=rate)
            effect = reidentify.measure_with_null_effect(corpus.patients, cols)
            effects.append(effect)
            print("{:>6.2f} {:>10} {:>11.4f} {:>11.4f} {:>+11.4f}".format(
                rate, effect.n_complete,
                effect.all_rows.measured_unique_share,
                effect.complete_only.measured_unique_share,
                effect.flattery))
        collected[" + ".join(cols)] = effects

    flips = {k: reidentify.flattery_changes_sign(v) for k, v in collected.items()}
    print("\nthe sign of that last column flips with the rate")
    for k, v in flips.items():
        print("  {:<30} {}".format(k, "yes" if v else "no"))
    print("A rare null is an uncommon value, so it isolates a row rather than pooling it")
    print("and the naive figure reads LESS safe. A common null is a crowded cell, so it")
    print("pools, and the naive figure reads safer. On a combination already at 1.0000")
    print("the honest figure cannot rise, so only pooling is left and the sign never")
    print("flips. Dropping the incomplete rows is not a free fix either. It shrinks the")
    print("population, and uniqueness depends on how many people are in it.")
    return collected


def section_controls(con, crawled):
    rule("controls")

    ok = True

    # The recovery check would pass against a crawler that ignored the catalog and handed
    # back pii/schema.py. Feed the comparison a crawl that is the declared schema with one
    # field changed and confirm it reports exactly that field. Without this, the whole
    # first section is satisfied by a function returning a constant.
    broken = crawler.Crawl(
        tables=tuple(
            Table(t.schema, t.name, tuple(
                Column(c.name, c.sql_type, nullable=not c.nullable, is_key=c.is_key)
                if c.name == "mrn" else c
                for c in t.columns))
            for t in crawled.tables
        ),
        key_columns=crawled.key_columns,
        engine=crawled.engine,
        engine_version=crawled.engine_version,
    )
    diffs = crawler.compare(broken)
    one_field = len(diffs) == 1 and diffs[0].field == "nullable"
    print("one flipped flag is caught           {:<8} expected True".format(
        str(one_field)))
    print("and the fingerprint moves with it    {:<8} expected True".format(
        str(broken.fingerprint() != fingerprint())))
    ok = ok and one_field and broken.fingerprint() != fingerprint()

    # A dropped table has to be caught from the declared side, and an extra one from the
    # crawled side. One direction is the easy half and it is the half that passes by
    # accident, which is the mistake the clause mapping made.
    short = crawler.Crawl(crawled.tables[1:], crawled.key_columns,
                          crawled.engine, crawled.engine_version)
    missing = [d for d in crawler.compare(short) if d.field == "table"]
    print("a missing table is caught            {:<8} expected 1".format(len(missing)))
    ok = ok and len(missing) == 1

    extra = crawler.Crawl(
        crawled.tables + (Table("raw", "invented", (Column("x", "BIGINT"),)),),
        crawled.key_columns, crawled.engine, crawled.engine_version)
    undeclared = [d for d in crawler.compare(extra)
                  if d.field == "table" and d.declared is None]
    print("an undeclared table is caught        {:<8} expected 1".format(
        len(undeclared)))
    ok = ok and len(undeclared) == 1

    # The nullability grader must be reading the rows rather than the flag. An empty table
    # gives it nothing to find, and reporting "no contradictions" there is the same
    # failure as a checker printing clean over zero files.
    con.execute("CREATE SCHEMA IF NOT EXISTS probe")
    con.execute("DROP TABLE IF EXISTS probe.empty")
    con.execute("CREATE TABLE probe.empty (a BIGINT NOT NULL, b VARCHAR)")
    empty_crawl = crawler.crawl(con)
    graded = [v for v in crawler.grade_nullability(con, empty_crawl)
              if v.table == "probe.empty"]
    zero_rows = all(v.rows == 0 for v in graded)
    none_exercised = not any(v.exercised for v in graded)
    print("an empty table exercises nothing     {:<8} expected True".format(
        str(zero_rows and none_exercised)))
    ok = ok and zero_rows and none_exercised
    con.execute("DROP TABLE probe.empty")

    # And the generalisers have to refuse a null rather than raising whatever the
    # language happens to raise on None. Assert the message, not the type.
    for fn, arg, word in ((lambda: reidentify.generalise_postal(None, 3), None, "null"),
                          (lambda: reidentify.coarsen_date_to_year(None), None, "null")):
        try:
            fn()
            said = "did not raise"
        except ValueError as exc:
            said = str(exc)
        except Exception as exc:
            said = "{}: {}".format(type(exc).__name__, exc)
        hit = word in said
        print("a null is refused by name            {:<8} expected True".format(str(hit)))
        ok = ok and hit

    print("\ncontrols {}".format("clean" if ok else "FAILED"))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Crawl the sample warehouse catalog.")
    ap.add_argument("--db", default="/tmp/pii.duckdb")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("no database at {}".format(args.db))
        print("build it first: python3 scripts/plant.py --db {}".format(args.db))
        return 2

    import duckdb

    # Read only, and the tool says so. A governance tool that opens a writable handle to
    # the warehouse it is auditing has made a decision somebody else wanted to make.
    con = duckdb.connect(args.db, read_only=True)
    try:
        crawled = crawler.crawl(con)
        recovered = section_recovery(crawled)
        nullable_ok = section_nullability(crawler.grade_nullability(con, crawled))
    finally:
        con.close()

    section_null_effect()

    # The controls need to create a table, so they get their own writable connection
    # rather than widening the one above.
    con = duckdb.connect(args.db)
    try:
        controls_ok = section_controls(con, crawler.crawl(con))
    finally:
        con.close()

    rule("read this before quoting anything above")
    print("The recovered schema is a fact about the catalog. The nullable flags in it")
    print("came from DDL this repo generated, so the round trip proves the crawl and")
    print("not the declaration. Only the null counts are evidence about the data.")

    return 0 if (recovered and nullable_ok and controls_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
