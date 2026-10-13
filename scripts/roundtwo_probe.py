"""What round two costs, on the shipped warehouse and on a wider synthetic one.

    python3 scripts/plant.py --db /tmp/pii.duckdb
    python3 scripts/roundtwo_probe.py --db /tmp/pii.duckdb

`ot-085` said round two classifies every column of a rescued table twice and that the cost
is database round trips, because the value arm sends a query per column. Half of that is
right. The double pass was real. The round trips are not: the value arm reads counts off a
`ColumnProfile` that the profiler already built, so round two reaches no database at all.
This prints the statement count to show that rather than arguing it.

`ot-138` said `grain.confidence_map` sends one statement per table before any column is
classified. That one is right and it was understated. A table the graph says was grouped
costs two, because the weight column is checked against the catalog first.

The arithmetic is in `pii/roundtwo.py` and `pii/classify.py`, which the suite imports and
mutation reaches. This file drives, prints and controls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import classify, crawl, grain, lineage, profile, roundtwo  # noqa: E402

RULE = "-" * 88


def rule(title: str) -> None:
    print("\n{}\n{}".format(title.upper(), RULE))


def connect(path: str, read_only: bool = False):
    import duckdb
    return duckdb.connect(path, read_only=read_only)


def shipped_profiles(con):
    found = crawl.crawl(con)
    out = []
    for table in found.tables:
        for column in table.columns:
            out.append(profile.profile_column(con, table.schema, table.name, column))
    return tuple(out)


def section_shipped(con) -> dict:
    rule("the shipped warehouse, 42 columns over 5 tables")
    profiles = shipped_profiles(con)
    graph = lineage.warehouse_graph(con)
    tables = sorted({p.table for p in profiles})
    upstream = graph.upstream_table_map(tables)

    cost = roundtwo.arm_cost(profiles, upstream)
    rescued = classify.person_linked_by_derivation(
        classify.classify_warehouse(profiles, upstream_tables=upstream), upstream)

    counted = roundtwo.CountingConnection(con)
    grain.classify_shipped(counted, profiles, graph)
    shipped_statements = counted.count

    counted.reset()
    grain.confidence_map(counted, graph, tables)
    eager_statements = counted.count

    print("tables round two rescues           {} of {}".format(
        cost.rescued_tables, len(tables)))
    print("  {}".format(", ".join(rescued)))
    print("arm runs, one pass                 {}".format(cost.memoised))
    print("arm runs, re-profiling round two   {}".format(cost.reprofiling))
    print("share of them that was the repeat  {:.4f}".format(cost.wasted_share))
    print("statements classify_shipped sends  {}".format(shipped_statements))
    print("  before 2026-10-13, every table   {}".format(eager_statements))
    return {"rescued": list(rescued), "tables": len(tables),
            "columns": cost.columns,
            "arm_runs_memoised": cost.memoised,
            "arm_runs_reprofiling": cost.reprofiling,
            "statements_lazy": shipped_statements,
            "statements_eager": eager_statements}


def section_grain_curve(con) -> list:
    rule("the grain map, with the rescued set held at {}".format(roundtwo.RESCUED))
    print("A warehouse grows its ordinary tables and not its rescued ones. This is the")
    print("cost the eager map was paying for tables round two never looks at.")
    print()
    print("{:>7}  {:>8}  {:>8}  {:>7}  {:>6}  {:>10}".format(
        "tables", "rescued", "eager", "lazy", "saved", "per table"))
    rows = []
    for total in roundtwo.TABLE_COUNTS:
        roundtwo.build(con, total)
        graph = roundtwo.graph_for(total)
        names = list(roundtwo.table_names(total))
        rescued = [n for n in names if ".mart_" in n]
        cost = roundtwo.grain_cost(con, graph, names, rescued)
        print("{:>7}  {:>8}  {:>8}  {:>7}  {:>6}  {:>10.2f}".format(
            total, cost.lazy_tables, cost.eager_statements, cost.lazy_statements,
            cost.saved, cost.per_eager_table))
        rows.append({"tables": total, "eager": cost.eager_statements,
                     "lazy": cost.lazy_statements, "saved": cost.saved})
    return rows


def section_double_pass(passes: int) -> list:
    rule("the double pass, with every derived table rescued")
    print("The other extreme, and the one where the repeat is proportional rather than")
    print("constant. Nothing here touches a database. Wall time on this machine, median")
    print("of {} passes, and the call counts are the number that transfers.".format(passes))
    print()
    print("{:>7}  {:>8}  {:>9}  {:>9}  {:>9}  {:>9}  {:>6}".format(
        "tables", "columns", "one pass", "repeated", "memo ms", "reprof ms", "ratio"))
    rows = []
    for total in roundtwo.TABLE_COUNTS:
        rescued = total - 1
        profiles = roundtwo.profiles_for(total, rescued)
        upstream = roundtwo.upstream_for(total, rescued)
        cost = roundtwo.arm_cost(profiles, upstream)
        memo = roundtwo.time_call(
            lambda: classify.classify_warehouse(profiles, upstream_tables=upstream),
            passes)
        reprof = roundtwo.time_call(
            lambda: roundtwo.reprofiling_warehouse(profiles, upstream_tables=upstream),
            passes)
        print("{:>7}  {:>8}  {:>9}  {:>9}  {:>9.2f}  {:>9.2f}  {:>6.2f}".format(
            total, cost.columns, cost.memoised, cost.reprofiling,
            memo.ms, reprof.ms, reprof.ms / memo.ms if memo.ms else 0.0))
        rows.append({"tables": total, "columns": cost.columns,
                     "memoised_ms": round(memo.ms, 3),
                     "reprofiling_ms": round(reprof.ms, 3),
                     "arm_runs_memoised": cost.memoised,
                     "arm_runs_reprofiling": cost.reprofiling})
    return rows


def section_controls(con) -> int:
    rule("controls")
    bad = 0

    def report(what: str, got, expected) -> None:
        nonlocal bad
        ok = got == expected
        bad += 0 if ok else 1
        print("{:<52} {:<12} expected {}".format(what, str(got), str(expected)))

    profiles = roundtwo.profiles_for(25)
    upstream = roundtwo.upstream_for(25)
    report("the two shapes disagree nowhere, fixture",
           roundtwo.disagreements(profiles, upstream), ())
    report("and nowhere with a discount applied",
           roundtwo.disagreements(profiles, upstream,
                                  {"grainbench.mart_000": 0.5}), ())

    shipped = shipped_profiles(con)
    graph = lineage.warehouse_graph(con)
    tables = sorted({p.table for p in shipped})
    ship_upstream = graph.upstream_table_map(tables)
    report("nor on the shipped warehouse",
           roundtwo.disagreements(shipped, ship_upstream,
                                  dict(grain.confidence_map(con, graph, tables))), ())

    eager = grain.classify_shipped(con, shipped, graph)
    lazy_map = dict(grain.confidence_map(con, graph, tables))
    plain = classify.classify_warehouse(shipped, upstream_tables=ship_upstream,
                                        grain_confidence=lazy_map)
    report("lazy grain gives the eager answer, columns moved",
           sum(1 for a, b in zip(eager, plain)
               if (a.category_key, a.confidence) != (b.category_key, b.confidence)), 0)

    # A grain source that answers for fewer tables than it was asked about is a bug in the
    # caller and the classifier should say so rather than silently keep the evidence whole.
    try:
        classify.classify_warehouse(profiles, upstream_tables=upstream,
                                    grain_confidence=lambda rescued: {})
        report("a short answer from a grain source", "accepted", "a refusal")
    except ValueError:
        report("a short answer from a grain source", "a refusal", "a refusal")

    try:
        classify.settle_table(profiles[:2], [classify.classify_column(profiles[3])])
        report("round one results of the wrong length", "accepted", "a refusal")
    except ValueError:
        report("round one results of the wrong length", "a refusal", "a refusal")

    print("controls clean" if bad == 0 else "{} control(s) FAILED".format(bad))
    return bad


def manifest(shipped: dict, grain_rows: list, double_rows: list) -> dict:
    """The figures a README is allowed to quote, and no wall times.

    Every other measurement in this repo commits a manifest and a check re-derives it.
    This one did not until the audit found a timing range in the README that nothing could
    reproduce.

    Wall times are left out on purpose. A committed number has to re-derive on another
    machine and a millisecond does not, which is the whole reason the call counts are the
    published figures here. The arm runs and the statement counts are properties of the
    algorithm and they come back identical anywhere.
    """
    return {
        "shipped": {k: v for k, v in shipped.items() if k != "rescued"},
        "shipped_rescued": sorted(shipped["rescued"]),
        "grain_curve": grain_rows,
        "double_pass": [{k: v for k, v in r.items() if not k.endswith("_ms")}
                        for r in double_rows],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/tmp/pii.duckdb")
    ap.add_argument("--bench-db", default="/tmp/pii-roundtwo.duckdb")
    ap.add_argument("--passes", type=int, default=5)
    ap.add_argument("--manifest", default=None,
                    help="write the machine independent figures here as JSON")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("no database at {}. run scripts/plant.py first".format(args.db))
        return 2

    con = connect(args.db, read_only=True)
    shipped = section_shipped(con)

    if os.path.exists(args.bench_db):
        os.remove(args.bench_db)
    bench = connect(args.bench_db)
    grain_rows = section_grain_curve(bench)
    double_rows = section_double_pass(args.passes)
    bad = section_controls(con)

    if args.manifest:
        with open(args.manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest(shipped, grain_rows, double_rows), fh,
                      indent=2, sort_keys=True)
            fh.write("\n")
        print("\nwrote {}".format(args.manifest))

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
