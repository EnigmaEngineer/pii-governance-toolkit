"""Build the review queue, work it, and measure what working it bought.

    python3 scripts/review_probe.py --db /tmp/pii.duckdb

The first section is the queue as a reviewer gets it. The second is the ordering argument,
because a queue is only a queue if its order carries information, and the first version of
this one did not. The third works the queue with decisions recorded here and reports which
tables become measurable, which is the thing day 5 left blocked.

The decisions in section three are mine and they are stamped as mine. They are not the
tool's answers and nothing here grades the classifier against them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import classify, crawl, lineage, mask, profile, review, schema  # noqa: E402
from pii.review import Decision  # noqa: E402

RULE = "-" * 96

TODAY = dt.date(2026, 9, 22)

# One decision per table that a single review is holding up, plus the mart column this
# whole redesign was about. Every reason is a fact about the warehouse rather than a
# restatement of the score, because a reviewer who writes "scored 0.725" in the reason
# field has recorded the classifier's opinion and not their own.
DECISIONS = (
    Decision("raw.device_reading.taken_at", True, "s.hussain", TODAY,
             "a reading is tied to an encounter and through it to one patient"),
    Decision("analytics.encounter_daily.day", True, "s.hussain", TODAY,
             "1386 of 1444 mart rows are a group of one, so the day is one admission"),
    Decision("raw.patient.created_at", False, "s.hussain", TODAY,
             "the row's load time, which is not tied to anything the patient did"),
    Decision("raw.patient.city", True, "s.hussain", TODAY,
             "a geographic subdivision smaller than a state, which clause B covers"),
)


def rule(title):
    print("\n{}\n{}".format(title, RULE))


def build_graph(con):
    cols = [c.name for c in
            schema.tables_by_fqn()["analytics.encounter_daily"].columns]
    derived = lineage.read_insert_select(
        schema.DERIVED_SQL["analytics.encounter_daily"], cols)
    keys = lineage.Graph(edges=lineage.foreign_key_edges(con, crawl.ENGINE_SCHEMAS))
    return derived.merge(keys)


def section_queue(queue):
    rule("THE QUEUE, IN THE ORDER A REVIEWER GETS IT")
    for n, item in enumerate(queue, 1):
        print("\n{:>2}. {}".format(n, item.brief()))


def section_ordering(queue):
    rule("WHETHER THE ORDER CARRIES ANYTHING")
    print("  {:>3}  {:>6}  {:>7}  {:>8}  {}".format(
        "pos", "frees", "blocks", "distance", "column"))
    for n, item in enumerate(queue, 1):
        print("  {:>3}  {:>6}  {:>7}  {:>8.4f}  {}".format(
            n, len(item.sole_blocker_of), len(item.tables_blocked),
            item.distance_to_accept, item.address))

    frees = review.frees_a_table(queue)
    print("\n  {} of {} items free a table on their own".format(len(frees), len(queue)))
    print("  the first version of this queue sorted on blocked tables alone, and every")
    print("  item blocks the table it lives in, so eight of the ten tied at one and the")
    print("  order was the alphabet with extra steps. A key that cannot separate the rows")
    print("  it sorts is not an ordering.")


def section_working_it(queue):
    rule("WHAT WORKING THE QUEUE BUYS")
    resolved, still_open = review.apply(queue, list(DECISIONS))
    done, total = review.worked_share(queue, list(DECISIONS))
    print("  {} of {} decided".format(done, total))
    print()
    for p in resolved:
        print("  {:<42} {:<12} {}".format(
            p.address, p.action.value, p.target.value if p.target else ""))
    print()
    unblocked = review.unblocks(queue, list(DECISIONS))
    print("  tables now measurable: {}".format(", ".join(unblocked) or "none"))
    blocked_still = sorted({t for i in queue if i.address in still_open
                            for t in i.tables_blocked} - set(unblocked))
    print("  tables still blocked:  {}".format(", ".join(blocked_still) or "none"))
    print()
    print("  the mart needed two decisions rather than one. `day` is its own review and")
    print("  `raw.encounter.admitted_at` is carried into it, so deciding the mart column")
    print("  alone leaves the table unmeasurable and a report saying otherwise would")
    print("  promise a residual number residual_sql refuses to produce.")

    rule("THE TRAIL")
    for line in review.trail(list(DECISIONS)):
        print("  " + line)
    print()
    print("  four decisions, four reviewers named, four reasons. This is the part an")
    print("  audit asks for and the part a confidence score cannot supply.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
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
        queue = review.build(results, policies, graph)

        rescued = classify.person_linked_by_derivation(
            classify.classify_warehouse(profiles), upstream)
        print("queue of {} over {} columns".format(len(queue), len(results)))
        print("band counts {}".format(classify.band_counts(results)))
        print("rescued by lineage: {}".format(", ".join(rescued) or "none"))

        section_queue(queue)
        section_ordering(queue)
        section_working_it(queue)
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
