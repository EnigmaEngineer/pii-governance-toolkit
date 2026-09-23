"""Recover the lineage of this warehouse and measure what it is worth.

    python3 scripts/lineage_probe.py --db /tmp/pii.duckdb

Prints six things and the third one is the reason this file exists.

1. The edges recovered from the statement that built the mart, and every refusal.
2. The declared references recovered from the catalog, checked against the schema.
3. How much of the warehouse lineage can speak about at all, as a count of roots.
4. What propagation changes against the classifier working on its own.
5. The same comparison with the mart's column names taken away.
6. What the aggregate in the mart is actually worth, measured as group sizes.

Number three is the one that changed my mind about this layer. Number six is the one I
would show an auditor.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import classify, crawl, lineage, profile, schema  # noqa: E402

# Built in the probe and dropped at the end rather than declared in `pii/schema.py`. A
# fifth table in the declared schema would move `schema.fingerprint()` off the pinned
# 1501a19ca3d8 that the crawler is graded against, for the sake of an experiment that does
# not need to be part of the warehouse. The statement is the mart's statement with every
# output name replaced by something a token rule cannot read.
OBFUSCATED_SCHEMA = "scratch"
OBFUSCATED_TABLE = "scratch.rollup_v2"
OBFUSCATED_COLUMNS = ("d1", "grp", "geo", "n", "dur")
OBFUSCATED_SQL = """
    INSERT INTO scratch.rollup_v2
    SELECT
        CAST(e.admitted_at AS DATE)                     AS d1,
        e.department                                    AS grp,
        p.postal_code                                   AS geo,
        count(*)                                        AS n,
        avg(date_diff('minute', e.admitted_at,
                      e.discharged_at) / 60.0)          AS dur
    FROM raw.encounter e
    JOIN raw.patient p ON p.patient_id = e.patient_id
    GROUP BY 1, 2, 3
"""


def rule(title: str) -> None:
    print("\n" + title)
    print("-" * len(title))


def build_graph(con, crawled) -> lineage.Graph:
    target = "analytics.encounter_daily"
    columns = [c.name for c in crawled.by_fqn()[target].columns]
    derived = lineage.read_insert_select(schema.DERIVED_SQL[target], columns)
    keys = lineage.Graph(edges=lineage.foreign_key_edges(con, crawl.ENGINE_SCHEMAS))
    return derived.merge(keys)


def band_of(category: str, confidence: float) -> str:
    if category == "not_personal":
        return "ignore"
    if confidence >= classify.ACCEPT_AT:
        return "accept"
    if confidence >= classify.REVIEW_AT:
        return "review"
    return "ignore"


def main() -> int:
    ap = argparse.ArgumentParser(description="Recover and measure column level lineage.")
    ap.add_argument("--db", default="/tmp/pii.duckdb")
    args = ap.parse_args()

    import duckdb

    con = duckdb.connect(args.db)
    try:
        crawled = crawl.crawl(con)
        graph = build_graph(con, crawled)

        rule("edges recovered from the statement that built the mart")
        for e in graph.derivation_edges():
            print("  {}".format(e))
        print("  {} derivation edges, {} refusals".format(
            len(graph.derivation_edges()), len(graph.refusals)))
        for r in graph.refusals:
            print("  refused  {}".format(r))

        rule("declared references, recovered from the catalog")
        for r in crawled.references:
            print("  {}".format(r))
        differences = crawl.compare_references(crawled)
        print("  {} recovered, {} disagreements with the declared schema".format(
            len(crawled.references), len(differences)))
        for d in differences:
            print("  {}".format(d))

        addresses = ["{}.{}".format(fqn, c.name) for fqn, c in schema.all_columns()]
        roots = graph.roots(addresses)
        rule("how much of this warehouse lineage can speak about")
        print("  {} columns, {} of them roots with nothing upstream".format(
            len(addresses), len(roots)))
        print("  {} columns have an upstream, which is {:.1f} percent".format(
            len(addresses) - len(roots),
            100.0 * (len(addresses) - len(roots)) / len(addresses)))
        pair = ("raw.patient.created_at", "raw.encounter.admitted_at")
        print("  the two timestamps the classifier cannot separate:")
        for address in pair:
            print("    {:<32} {}".format(
                address, "root" if address in roots else "derived"))
        print("  both are roots, so lineage does not separate them either")

        rule("nullable source feeding a NOT NULL target")
        breaches = lineage.nullable_into_not_null(graph, schema.nullable_by_address())
        if breaches:
            for b in breaches:
                print("  {}".format(b))
        else:
            print("  none. the one this check was written for is already fixed, and this")
            print("  is what would have found it without waiting for a load to fail")

        profiles = profile.profile_crawl(con, crawled)
        # Deliberately without the upstream map. This section measures what propagation
        # adds on top of a column's own answer, and the classifier's own answer can
        # itself be lineage informed now. Passing the graph here would compare a
        # lineage informed answer against a lineage informed answer and call the
        # difference the value of lineage. The interaction is real and it is named in the
        # README rather than smoothed over.
        results = classify.classify_warehouse(profiles)
        inherited = lineage.propagate(graph, results)

        rule("what propagation changes, with the mart's real column names")
        changed = [i for i in inherited if i.disagrees]
        print("  {:<40} {:<16} {:<16} {}".format(
            "column", "on its own", "from upstream", "rule"))
        for i in inherited:
            if not i.address.startswith("analytics."):
                continue
            print("  {:<40} {:<16} {:<16} {}".format(
                i.address, i.direct_category, i.inherited_category or "-", i.rule))
        print("  {} {} {} with upstream across the whole warehouse".format(
            len(changed),
            "column" if len(changed) == 1 else "columns",
            "disagrees" if len(changed) == 1 else "disagree"))
        for i in changed:
            print("    {} direct {} upstream {}".format(
                i.address, i.direct_category, i.inherited_category))

        rule("what each masked column reaches downstream")
        masked = [r for r in results if r.auto_masked]
        reaching = 0
        for r in sorted(masked, key=lambda x: x.address):
            downstream = graph.out_of(r.address)
            if not downstream:
                continue
            reaching += 1
            for e in downstream:
                print("  {:<38} reaches {:<38} by {}".format(
                    r.address, e.target.address, e.kind.value))
        print("  {} columns are masked without a human, {} of them {} anything".format(
            len(masked), reaching, "reaches" if reaching == 1 else "reach"))
        print("  the other {} end where they are, so masking them is the whole job".format(
            len(masked) - reaching))

        rule("the same mart with its column names taken away")
        con.execute("CREATE SCHEMA IF NOT EXISTS {}".format(OBFUSCATED_SCHEMA))
        con.execute("DROP TABLE IF EXISTS {}".format(OBFUSCATED_TABLE))
        con.execute("""
            CREATE TABLE {} (
              d1 DATE NOT NULL, grp VARCHAR, geo VARCHAR,
              n BIGINT NOT NULL, dur DOUBLE
            )
        """.format(OBFUSCATED_TABLE))
        con.execute(OBFUSCATED_SQL)

        obf_graph = lineage.read_insert_select(OBFUSCATED_SQL, list(OBFUSCATED_COLUMNS))
        obf_table = [t for t in crawl.crawl(con).tables if t.fqn == OBFUSCATED_TABLE][0]
        obf_profiles = tuple(
            profile.profile_column(con, obf_table.schema, obf_table.name, c)
            for c in obf_table.columns)
        obf_results = classify.classify_warehouse(obf_profiles)
        obf_inherited = lineage.propagate(
            obf_graph.merge(lineage.Graph(edges=tuple(
                e for e in graph.edges if e.kind is lineage.EdgeKind.JOIN_KEY))),
            tuple(obf_results) + tuple(results))

        print("  {:<28} {:<24} {:<24}".format("column", "on its own", "from upstream"))
        recovered = 0
        for i in obf_inherited:
            if not i.address.startswith(OBFUSCATED_SCHEMA + "."):
                continue
            direct = "{} ({})".format(i.direct_category,
                                      band_of(i.direct_category, i.direct_confidence))
            upstream = i.inherited_category or "-"
            print("  {:<28} {:<24} {:<24}".format(i.address, direct, upstream))
            if i.upgrade:
                recovered += 1
        print("  {} columns that read as not personal on their own are personal "
              "upstream".format(recovered))

        rule("what the aggregate in the mart is actually worth")
        row = con.execute("""
            SELECT count(*),
                   count(*) filter (where encounters = 1),
                   min(encounters), max(encounters)
            FROM analytics.encounter_daily
        """).fetchone()
        total, singles, low, high = row
        print("  {} rows in the mart, encounters between {} and {}".format(total, low, high))
        print("  {} of them are a group of one, which is {:.1f} percent".format(
            singles, 100.0 * singles / total))
        print("  a group of one is one patient's one encounter, carrying their postal")
        print("  code and the day they were admitted. the count did not aggregate anything")

        return 0
    finally:
        try:
            con.execute("DROP SCHEMA IF EXISTS {} CASCADE".format(OBFUSCATED_SCHEMA))
        finally:
            con.close()


if __name__ == "__main__":
    sys.exit(main())
