"""Produce the compliance report for the sample warehouse.

    python3 scripts/compliance_report.py --db /tmp/pii.duckdb
    python3 scripts/compliance_report.py --db /tmp/pii.duckdb --out docs/sample-compliance-report.md

Everything printed comes out of `pii/compliance.py`, which in turn composes the modules
that have checks behind them. This file drives, writes and controls. It does no arithmetic
of its own, which is deliberate: a number computed in a script cannot be reached by a
mutant, so a script that computes anything is a number nothing grades.

The last thing it does before writing is ask `compliance.undelivered` whether every refusal
the report holds survived into the text. A report whose refusals are silently dropped by a
renderer is worse than one that refuses nothing, because it reads as complete.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import (access, classify, compliance, corpus, coverage, crawl,  # noqa: E402
                 lineage, mask, profile, schema)

COMMAND = "python3 scripts/compliance_report.py --db /tmp/pii.duckdb"

# The same window the access probe reports over. Named here rather than defaulted to today
# because a report whose date range moves with the wall clock produces a different document
# every run and nothing can then tell a real change from the calendar.
WINDOW_END = dt.date(2026, 9, 21)
WINDOW_DAYS = 113


def build_graph(con):
    cols = [c.name for c in
            schema.tables_by_fqn()["analytics.encounter_daily"].columns]
    derived = lineage.read_insert_select(
        schema.DERIVED_SQL["analytics.encounter_daily"], cols)
    keys = lineage.Graph(edges=lineage.foreign_key_edges(con, crawl.ENGINE_SCHEMAS))
    return derived.merge(keys)


def main():
    ap = argparse.ArgumentParser(description="Generate the compliance report.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default=None,
                    help="write the report here as well as to stdout")
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
        policies = mask.generate(results)

        grants, members, events = corpus.generate_access(n_queries=args.queries)
        flagged = [r.address for r in results if r.flagged]
        start = WINDOW_END - dt.timedelta(days=WINDOW_DAYS - 1)
        exposures = access.report(flagged, grants, members, events, graph,
                                  start, WINDOW_END)

        report = compliance.build(
            con, results, policies, exposures, coverage.grade(),
            generated_by=COMMAND, graph=graph)

        text = compliance.render(report)

        missing = compliance.undelivered(report, text)
        if missing:
            print("REFUSALS LOST IN RENDERING: {}".format(", ".join(missing)),
                  file=sys.stderr)
            return 1

        # The control. A renderer that really did drop a section has to be named by the
        # same call, or the clean result above is a check that has never seen a failure.
        dropped = compliance.undelivered(report, compliance.render(
            report, drop_refusals=True))
        if not dropped:
            print("CONTROL FAILED: dropping every refusal was not detected",
                  file=sys.stderr)
            return 1

        print(text)
        answered, asked = report.answer_rate()
        print("checked {} refusals survived rendering, control named {} subjects "
              "when they were dropped".format(len(report.refusals), len(dropped)))
        print("refusals by question:")
        for question, n in sorted(compliance.refusals_by_question(report).items()):
            print("  {:>3}  {}".format(n, question))
        print("answer rate {} of {}".format(answered, asked))

        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
            print("written to {}".format(args.out))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
