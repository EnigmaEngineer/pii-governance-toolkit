"""Answer "who could have read this column, and did they" and show where it goes wrong.

    python3 scripts/access_probe.py --db /tmp/pii.duckdb

Four sections and the last two are the ones worth reading.

One restates the grants, which is what a table level access review produces.
Two follows the value through lineage and reports the users the first section missed.
Three sweeps the date range, because the answer to "does this role still need its grant"
turns out to depend on how much history you have.
Four counts what a query log cannot tell you, which is every column returned by a
`SELECT *`.

The grants and the query log are generated in `pii/corpus.py` and not here, because a
module that manufactures the data it then measures has measured nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import access, classify, corpus, crawl, lineage, profile, schema  # noqa: E402

RULE = "-" * 96

WINDOW_END = dt.date(2026, 9, 21)


def rule(title):
    print("\n{}\n{}".format(title, RULE))


def build_graph(con):
    cols = [c.name for c in
            schema.tables_by_fqn()["analytics.encounter_daily"].columns]
    derived = lineage.read_insert_select(
        schema.DERIVED_SQL["analytics.encounter_daily"], cols)
    keys = lineage.Graph(edges=lineage.foreign_key_edges(con, crawl.ENGINE_SCHEMAS))
    return derived.merge(keys)


def section_grants(grants, members):
    rule("WHAT THE GRANTS SAY, WHICH IS WHAT AN ACCESS REVIEW PRODUCES")
    by_role = access.users_by_role(members)
    for role in sorted({g.role for g in grants}):
        tables = access.readable_tables(role, grants)
        print("  {:<20} {:<28} {}".format(
            role, ", ".join(u for u in by_role.get(role, ())), ", ".join(tables)))
    print("\n{} grants over {} roles, {} memberships".format(
        len(grants), len({g.role for g in grants}), len(members)))


def section_widening(exposures):
    rule("WHAT THE GRANTS MISS, ONCE THE VALUE IS FOLLOWED")
    lines = access.widening(exposures)
    if not lines:
        print("  nothing widens, which means no flagged column has a carrier")
    for line in lines:
        print("  " + line)

    print("\nper column, the users who read the value and hold no grant on its table")
    any_finding = False
    for e in exposures:
        if not e.read_without_a_direct_grant:
            continue
        any_finding = True
        print("  {:<42} {}".format(
            e.address, ", ".join(e.read_without_a_direct_grant)))
    if not any_finding:
        print("  none")

    # The module's actual question, printed as the two numbers it is made of. This was
    # missing from the first version of the probe, which printed the widening and the use
    # and never the answer they are components of.
    print("\n{:>7}  {:>5}  {:>7}  {}".format("could", "did", "unused", "column"))
    for e in exposures:
        print("  {:>5}  {:>5}  {:>7}  {}".format(
            len(e.could_have_read), len(e.did_read),
            len(e.granted_and_never_used), e.address))
    print("\n  unused is per column and it is the cost of a revocation. A user in that")
    print("  count could read this column over the range and did not touch it.")


def section_window(flagged, grants, members, events, graph):
    rule("THE SAME QUESTION OVER DIFFERENT AMOUNTS OF HISTORY")
    print("  the retention window an access review runs against changes its answer")
    print()
    print("  {:>7}  {:>8}  {:>6}  {:>14}  {:>14}".format(
        "window", "queries", "stars", "unused grants", "star only"))
    for days in (3, 7, 14, 30, 60, 113):
        start = WINDOW_END - dt.timedelta(days=days - 1)
        exps = access.report(flagged, grants, members, events, graph, start, WINDOW_END)
        starred, total = access.star_share(events, start, WINDOW_END)
        star_only = sum(len(e.only_by_star) for e in exps)
        print("  {:>6}d  {:>8}  {:>6}  {:>14}  {:>14}".format(
            days, total, starred, len(access.unused_grants(exps)), star_only))
    print()
    print("  unused grants is the count a least privilege review would act on. At three")
    print("  days it recommends revoking from users who are simply on holiday, and the")
    print("  recommendation disappears as the window grows. The number is a property of")
    print("  the log's length and reads like a property of the users.")


def section_the_star_floor(events, exposures):
    rule("WHAT A QUERY LOG CANNOT TELL YOU")
    starred, total = access.star_share(events)
    print("  {} of {} queries named a table and no column".format(starred, total))
    print("  {} column references across the rest".format(
        sum(len(e.columns) for e in events)))
    print()
    print("  {:>6}  {:>10}  {:>14}  {}".format(
        "did", "at most", "only by star", "column"))
    for e in exposures:
        print("  {:>6}  {:>10}  {:>14}  {}".format(
            len(e.did_read), len(e.did_read_at_most),
            len(e.only_by_star), e.address))
    cannot_say = access.unanswerable(exposures)
    print()
    print("  {} of {} columns cannot be answered either way".format(
        len(cannot_say), len(exposures)))
    for address in cannot_say:
        print("    {}".format(address))
    print()
    print("  did_read is a floor and did_read_at_most is a ceiling. Nothing here is the")
    print("  count, and a report printing one number for this would be picking an end of")
    print("  the interval and hoping. Closing the gap needs the star expanded against a")
    print("  catalog as it stood on the query's date, and this warehouse keeps no such")
    print("  history, so expanding against today's catalog would attribute a column to a")
    print("  query that ran before the column existed.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--queries", type=int, default=400)
    args = ap.parse_args()

    import duckdb

    con = duckdb.connect(args.db, read_only=True)
    try:
        crawled = crawl.crawl(con)
        profiles = profile.profile_crawl(con, crawled)
        graph = build_graph(con)
        upstream = graph.upstream_table_map(sorted({p.table for p in profiles}))
        results = classify.classify_warehouse(profiles, upstream_tables=upstream)
        grants, members, events = corpus.generate_access(n_queries=args.queries)

        summary = corpus.summarise_access(grants, members, events)
        print("query log {} to {}, {} queries, {} of them stars".format(
            summary["first_query"], summary["last_query"],
            summary["queries"], summary["star_queries"]))

        flagged = [r.address for r in results if r.flagged]
        print("{} flagged columns to report on".format(len(flagged)))

        start = WINDOW_END - dt.timedelta(days=112)
        exposures = access.report(flagged, grants, members, events, graph,
                                  start, WINDOW_END)

        section_grants(grants, members)
        section_widening(exposures)
        section_window(flagged, grants, members, events, graph)
        section_the_star_floor(events, exposures)
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
