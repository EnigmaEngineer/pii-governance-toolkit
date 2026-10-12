"""Measure the lineage rescue against grain, and show where it should refuse.

    python3 scripts/plant.py --db /tmp/pii.duckdb
    python3 scripts/grain_probe.py --db /tmp/pii.duckdb

The rescue in `classify.classify_warehouse` round two keeps a mart's temporal evidence when
the tables it came from name people. Until today it did that on every such mart, at full
strength, which is a necessary condition spending a sufficient one. The number that decides
it is how many people one row of the mart stands for, and `pii/grain.py` now measures it.

One warehouse cannot show that. The shipped mart is grouped finely enough that the rescue is
right, so running the new code on it alone produces a figure that moved by two hundredths
and proves nothing. So this rebuilds the same mart from the same encounters at two coarser
grains and runs the whole path again. The lineage read, then the weight column lookup,
then the grain and the classifier. Nothing is told the answer.

The arithmetic is all in `pii/grain.py` and `pii/classify.py`, which the suite imports and
mutation reaches. This file drives, prints and controls.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import classify, crawl, grain, lineage, mask, profile  # noqa: E402

RULE = "-" * 92
SHIPPED = "analytics.encounter_daily"


def rule(title: str) -> None:
    print("\n{}\n{}".format(title.upper(), RULE))


def connect(path: str, read_only: bool = False):
    import duckdb
    return duckdb.connect(path, read_only=read_only)


def temporal_column(results, table: str):
    """The rescued column of one table.

    `day` by name would work on all three tables here and it would be the wrong lookup.
    The thing being measured is whatever column the structure arm's temporal rule fired on,
    so this asks the classifier instead of asking the schema.
    """
    hits = [r for r in results
            if r.table == table and r.category_key == "event_date"]
    if len(hits) != 1:
        raise ValueError(
            "{} has {} temporal columns and this probe reads one".format(
                table, len(hits)))
    return hits[0]


def grade_all(con, profiles, graph, upstream, tables):
    """Classify the whole warehouse twice, scored and binary, and pair with the grains.

    One classify pass rather than one per table. `crawl.crawl` reads the whole catalog by
    design, so a per table call would profile the same columns three times and the three
    marts would each be classified without the others in view, which is not what the tool
    does.
    """
    shares = grain.confidence_map(con, graph, tables)
    scored = classify.classify_warehouse(
        profiles, upstream_tables=upstream, grain_confidence=shares)
    binary = classify.classify_warehouse(profiles, upstream_tables=upstream)

    out = []
    for table in tables:
        witness = grain.witness_for(con, graph, table)
        out.append(grain.RescueRow(
            table=table,
            rows=witness.rows,
            population=witness.population,
            k_anonymity=witness.k_anonymity,
            singleton_rows=witness.singleton_rows,
            singleton_share=shares[table],
            confidence=temporal_column(scored, table).confidence,
            binary_confidence=temporal_column(binary, table).confidence,
        ))
    return out


def section_witnesses(con, graph, tables) -> None:
    rule("how many people one row stands for")
    print("{:<34} {:>7} {:>7} {:>4} {:>10} {:>8} {:>8}  {}".format(
        "table", "rows", "people", "k", "singletons", "share", "per row",
        "weight column"))
    for w in grain.witnesses_for(con, graph, tables):
        print("{:<34} {:>7} {:>7} {:>4} {:>10} {:>8.4f} {:>8.2f}  {}".format(
            w.table, w.rows, w.population, w.k_anonymity, w.singleton_rows,
            w.singleton_share, w.people_per_row,
            w.weight_column or "none, rows are people"))
    print("\nper row is the factor the GROUP BY deduplicated by. counting rows instead of")
    print("people reports a table safer than it is by exactly that number.")
    print("\nthe weight column comes off the lineage graph and not off a list in this file.")
    print("mask.weight_for refuses rather than picks when the graph offers two.")


def section_floor() -> None:
    rule("the floor the discount puts under the rescue")
    floor = grain.rescue_floor()
    print("review band opens at                  {:.4f}".format(classify.REVIEW_AT))
    print("the structure arm's temporal rule is  {:.4f}".format(
        classify.TEMPORAL_TYPE_STRENGTH))
    print("so a rescued column queues only above {:.4f} of its rows being one person"
          .format(floor))
    print("\nderived rather than swept. day 3 of this project swept a threshold that could")
    print("not move a verdict at any setting, because the same multiply put a floor under")
    print("it that nobody had worked out. this one was worked out first.")


def section_grains(rows) -> None:
    rule("the same mart at three grains")
    print("{:<34} {:>7} {:>4} {:>8} {:>7} {:<8} {:>7} {:<8}".format(
        "table", "rows", "k", "share", "scored", "band", "binary", "band"))
    for r in sorted(rows, key=lambda r: -r.singleton_share):
        print("{:<34} {:>7} {:>4} {:>8.4f} {:>7.4f} {:<8} {:>7.4f} {:<8}{}".format(
            r.table, r.rows, r.k_anonymity, r.singleton_share, r.confidence,
            r.band, r.binary_confidence, r.binary_band,
            "  <- moved" if r.moved else ""))
    report = grain.RescueReport(rows=tuple(rows))
    print("\nk tells the bands apart              {}".format(
        "yes" if report.k_separates() else "NO"))
    print("the singleton share tells them apart {}".format(
        "yes" if report.share_separates() else "NO"))
    print("\nthe thread asked for a k on the grain. k is min over every group, so one group")
    print("of one holds it at 1 and the rest stop counting. two of the tables above share")
    print("a k of 1 and end in different bands, which is the whole reason the share is the")
    print("statistic and the minimum is not.")


def section_controls(con, rows, profiles, graph, upstream) -> bool:
    rule("controls")
    ok = True

    # A share of zero has to delete the evidence outright, or the discount is cosmetic.
    mart = [r for r in rows if r.table == SHIPPED][0]
    zeroed = classify.classify_warehouse(
        profiles, upstream_tables=upstream, grain_confidence={SHIPPED: 0.0})
    at_zero = temporal_column(zeroed, SHIPPED).confidence
    print("a share of 0 scores                  {:.4f}   expected 0.0000".format(at_zero))
    ok = ok and at_zero == 0.0

    # And a share of one has to leave the number exactly where the binary left it, or the
    # discount is changing answers it was never meant to touch.
    whole = classify.classify_warehouse(
        profiles, upstream_tables=upstream, grain_confidence={SHIPPED: 1.0})
    at_one = temporal_column(whole, SHIPPED).confidence
    print("a share of 1 scores                  {:.4f}   expected {:.4f}".format(
        at_one, mart.binary_confidence))
    ok = ok and at_one == mart.binary_confidence

    # The discount must reach the temporal column and nothing else. postal_code on the
    # shipped mart scores on its name, and a grouping does not make a postal code less of
    # a postal code.
    scored = {r.address: r.confidence for r in classify.classify_warehouse(
        profiles, upstream_tables=upstream,
        grain_confidence={SHIPPED: mart.singleton_share})}
    binary = {r.address: r.confidence for r in classify.classify_warehouse(
        profiles, upstream_tables=upstream)}
    untouched = [a for a in binary if a != SHIPPED + ".day" and scored[a] != binary[a]]
    print("columns moved other than the date    {:<8} expected 0".format(len(untouched)))
    ok = ok and not untouched

    # A table nobody derived must not be reachable by the discount at all, because the
    # discount only ever applies to a verdict that was borrowed.
    source = classify.classify_warehouse(
        profiles, upstream_tables=upstream, grain_confidence={"raw.encounter": 0.0})
    admitted = [r for r in source if r.address == "raw.encounter.admitted_at"][0]
    plain = [r for r in classify.classify_warehouse(profiles, upstream_tables=upstream)
             if r.address == "raw.encounter.admitted_at"][0]
    print("raw.encounter.admitted_at at share 0 {:.4f}   expected {:.4f}".format(
        admitted.confidence, plain.confidence))
    ok = ok and admitted.confidence == plain.confidence

    # The weight column has to be the one the graph names. Measuring the mart by row count
    # is the failure mask.weight_for exists to stop, and it reports a safer number.
    by_rows = grain.measure_grain(con, SHIPPED, None)
    print("the mart measured by row count       {:.4f}   shipped answer {:.4f}".format(
        by_rows.singleton_share, mart.singleton_share))
    ok = ok and by_rows.singleton_share > mart.singleton_share

    # A refusal rather than a guess when the floor is handed an impossible strength.
    try:
        grain.rescue_floor(strength=0.0)
        print("a floor on a zero strength           accepted   expected a refusal")
        ok = False
    except ValueError:
        print("a floor on a zero strength           refused    expected a refusal")

    print("\ncontrols {}".format("clean" if ok else "FAILED"))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/tmp/pii.duckdb")
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--scratch", default="/tmp/pii-grain.duckdb")
    args = ap.parse_args()

    # The coarse marts are created, so they go into a copy and the warehouse this was
    # pointed at is left exactly as it was found. Two tables left behind in the shipped
    # database would also change the 42 column figure every other probe publishes.
    scratch = args.scratch
    shutil.copyfile(args.db, scratch)
    con = connect(scratch, read_only=False)
    for variant in grain.COARSER:
        grain.build_variant(con, variant)

    graph = lineage.warehouse_graph(con)
    for variant in grain.COARSER:
        graph = graph.merge(
            lineage.read_insert_select(variant.insert_sql(), list(variant.columns)))

    tables = [SHIPPED] + [v.table for v in grain.COARSER]
    everything = sorted(set(tables) | {"raw.encounter", "raw.patient"})
    upstream = graph.upstream_table_map(everything)
    profiles = profile.profile_crawl(con, crawl.crawl(con))

    section_witnesses(con, graph, everything)
    section_floor()
    rows = grade_all(con, profiles, graph, upstream, tables)
    section_grains(rows)
    ok = section_controls(con, rows, profiles, graph, upstream)

    if args.manifest:
        report = grain.RescueReport(rows=tuple(rows))
        with open(args.manifest, "w") as fh:
            json.dump(report.manifest(), fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("\nmanifest written to {}".format(args.manifest))

    rule("read this before quoting anything above")
    print("The two coarse marts are mine. They are regroupings of the same encounters and")
    print("the shares they produce are facts about this corpus. What is not about this")
    print("corpus is the floor, which comes out of two constants, and the finding that k")
    print("cannot separate the bands, which is a property of min.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
