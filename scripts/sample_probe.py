"""What declaration-only classification misses, measured twice.

    python3 scripts/plant.py --db /tmp/pii.duckdb
    python3 scripts/sample_probe.py --db /tmp/pii.duckdb

Two readings, and they answer two different questions.

The first is on the shipped warehouse. Forty two columns I wrote, with the sampling arm off
and then on. The question is what turning it on costs, and the answer is not only review
load. An unnamed identifier is a disagreement with every named category by construction, so
the arm can lower the confidence of a column the classifier already had right.

The second is on `probe.outside_rule`, which is four columns deliberately formatted outside
the predicate set. That is `ot-077`'s actual ask. The sample warehouse cannot answer it
because I wrote the corpus and the predicates in the same week.

Both go through `pii.ablation.ablate` rather than through a second diff written here. An
arm set is an arm set, and the harness committed on day 1 for removing arms takes adding
one with no change at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import ablation, classify, crawl, lineage, profile, safeharbor, schema  # noqa: E402
from pii import sample, sample_bench  # noqa: E402
from pii.classify import ALL_ARMS, ARMS_WITH_SAMPLING  # noqa: E402
from pii.schema import planted_index  # noqa: E402

RULE = "-" * 96

ARM_SETS = (
    ("declaration only", ALL_ARMS),
    ("with sampling", ARMS_WITH_SAMPLING),
)


def connect(path: str, read_only: bool = False):
    import duckdb
    return duckdb.connect(path, read_only=read_only)


def upstream_for(con, profiles):
    """The same graph every other probe reads, from `pii.lineage` rather than from a copy."""
    graph = lineage.warehouse_graph(con)
    return graph.upstream_table_map(sorted({p.table for p in profiles}))


def sampled(con, crawled, profiles):
    """Sample every crawled column and hand the signals back on the profiles.

    The samples come back too, because `samples_hold_no_values` has to run over the objects
    that hold the shapes and the profiles do not hold them.
    """
    samples = []
    by_table = {t.fqn: t for t in crawled.tables}
    for p in profiles:
        table = by_table[p.table]
        col = next(c for c in table.columns if c.name == p.column)
        samples.append(sample.sample_column(con, table.schema, table.name, col))
    return sample.attach(profiles, samples), tuple(samples)


def section_warehouse(con):
    """Reading one. The forty two columns the repo publishes figures about."""
    crawled = crawl.crawl(con)
    profiles = profile.profile_crawl(con, crawled)
    upstream = upstream_for(con, profiles)
    with_samples, samples = sampled(con, crawled, profiles)

    leaks = sample.samples_hold_no_values(samples)
    index = planted_index()

    def key_for(r):
        return index[(r.table, r.column)].category_key

    report = ablation.ablate(
        with_samples,
        upstream_tables=upstream,
        scope=frozenset(safeharbor.mapped_keys()),
        planted_key_for=key_for,
        arm_sets=ARM_SETS,
    )

    print("\nTHE SHIPPED WAREHOUSE WITH THE SAMPLING ARM OFF AND THEN ON")
    print(RULE)
    print("{} columns sampled at a limit of {} rows each".format(
        len(samples), sample.SAMPLE_LIMIT))
    print("shape allowlist violations {}".format(len(leaks)))
    print()
    print(report.table())

    moved = report.by_label("with sampling")
    print("\n{} bands move when the arm is turned on".format(moved.bands_moved))
    for m in moved.moves:
        print("  {}".format(m.describe()))

    fired = [p for p in with_samples if p.sampled_signals]
    print("\n{} of {} columns produced a sampled signal at all".format(
        len(fired), len(with_samples)))
    for p in sorted(fired, key=lambda x: x.address):
        for s in p.sampled_signals:
            print("  {:<40} {:<20} {:.4f}  {}".format(
                p.address, s.category_key, s.weight, s.detail))
    pieces = (profiles, samples, upstream, frozenset(safeharbor.mapped_keys()), key_for)
    return report, samples, leaks, pieces


def section_bench(con):
    """Reading two. Four columns the predicate set was never going to reach."""
    loaded = sample_bench.load(con)
    table = sample_bench.BENCH
    profiles = tuple(profile.profile_column(con, table.schema, table.name, c)
                     for c in table.columns)
    samples = tuple(sample.sample_column(con, table.schema, table.name, c)
                    for c in table.columns)
    with_samples = sample.attach(profiles, samples)

    before = classify.classify_warehouse(with_samples, arms=ALL_ARMS)
    after = classify.classify_warehouse(with_samples, arms=ARMS_WITH_SAMPLING)
    outcomes = sample_bench.grade(before, after, sample_bench.planted_key_for_bench)
    tally = sample_bench.counts(outcomes)

    print("\n\nFOUR COLUMNS FORMATTED OUTSIDE THE PREDICATE SET")
    print(RULE)
    print("{} rows loaded into {}".format(loaded, sample_bench.BENCH_FQN))
    print()
    print("{:<22} {:<22} {:<26} {:<26} {}".format(
        "column", "planted", "declaration only", "with sampling", "verdict"))
    for o in outcomes:
        print("{:<22} {:<22} {:<26} {:<26} {}".format(
            o.address.split(".")[-1],
            o.planted,
            "{} {:.4f}".format(o.before_band, o.before_confidence),
            "{} {} {:.4f}".format(o.after_band, o.after_category, o.after_confidence),
            o.verdict))
    print()
    for key in ("recovered", "still missed", "false alarm", "unchanged"):
        print("  {:<14} {}".format(key, tally[key]))

    linked_before = classify.table_is_person_linked(before)
    linked_after = classify.table_is_person_linked(after)
    print("\ntable reads as person linked: {} before, {} after".format(
        linked_before, linked_after))
    return outcomes, tally, loaded, (linked_before, linked_after), (profiles, samples)


def section_sweep(warehouse_pieces, bench_pieces):
    """`ot-129`. The shape concentration threshold swept over both readings together.

    One table rather than two runs, because the whole question is whether a threshold that
    finds more on the fixture costs anything on the warehouse. Swept separately they are
    two numbers somebody has to hold in their head at the same time.
    """
    w_profiles, w_samples, upstream, scope, w_key_for = warehouse_pieces
    b_profiles, b_samples = bench_pieces

    fixture = sample_bench.sweep_fixture(b_profiles, b_samples)
    warehouse = sample_bench.sweep_warehouse(
        w_profiles, w_samples, upstream, scope, w_key_for)

    print("\n\nSHAPE CONCENTRATION SWEEP")
    print(RULE)
    print("the fixture, four columns formatted outside the predicate set")
    print("{:>13} {:>8} {:>10} {:>13} {:>13} {:>10} {:>14}".format(
        "concentration", "signals", "recovered", "still missed", "false alarms",
        "net", "person linked"))
    for r in fixture:
        print("{:>13.2f} {:>8} {:>10} {:>13} {:>13} {:>10} {:>14}".format(
            r.concentration, r.signals, r.recovered, r.still_missed,
            r.false_alarms, r.net, str(r.person_linked)))

    print("\nthe shipped warehouse, forty two columns, through the ablation harness")
    print("{:>13} {:>8} {:>7} {:>8} {:>13} {:>12}".format(
        "concentration", "signals", "found", "masked", "false alarms", "bands moved"))
    for r in warehouse:
        print("{:>13.2f} {:>8} {:>7} {:>8} {:>13} {:>12}".format(
            r.concentration, r.signals, r.found, r.masked, r.false_alarms,
            r.bands_moved))

    shipped = sample.SHAPE_CONCENTRATION
    floor = sample.concentration_floor()
    moved = sample_bench.cliff(fixture)
    print("\nthe shipped concentration is {:.2f}".format(shipped))
    if moved:
        print("the highest swept value below it that emits a different number of "
              "signals is {:.2f}".format(moved))
    else:
        print("nothing in this sweep emits a different number of signals")

    # The point of the whole table. Verdicts do not move anywhere on it, and the reason is
    # arithmetic rather than this fixture.
    print("the decision floor is {:.4f}, which is REVIEW_AT over "
          "UNKNOWN_IDENTIFIER_STRENGTH".format(floor))
    print("a signal from a shape holding less than that lands in the ignore band, so "
          "every swept value under it")
    print("emits evidence and changes no decision")
    verdicts = {(r.recovered, r.still_missed, r.false_alarms) for r in fixture}
    print("distinct verdict tallies across the whole sweep: {}".format(len(verdicts)))
    return fixture, warehouse


def write_manifest(path, warehouse_report, outcomes, tally, loaded, linked,
                   fixture_sweep, warehouse_sweep):
    payload = {
        "concentration_sweep": {
            "concentrations": list(sample_bench.CONCENTRATIONS),
            "shipped": sample.SHAPE_CONCENTRATION,
            "decision_floor": round(sample.concentration_floor(), 6),
            "fixture": [vars(r) for r in fixture_sweep],
            "warehouse": [vars(r) for r in warehouse_sweep],
        },
        "warehouse": warehouse_report.manifest(),
        "bench": {
            "table": sample_bench.BENCH_FQN,
            "rows": loaded,
            "person_linked_before": linked[0],
            "person_linked_after": linked[1],
            "counts": tally,
            "columns": [
                {
                    "address": o.address,
                    "planted": o.planted,
                    "before_band": o.before_band,
                    "after_band": o.after_band,
                    "after_category": o.after_category,
                    "verdict": o.verdict,
                }
                for o in outcomes
            ],
        },
        "sample_limit": sample.SAMPLE_LIMIT,
    }
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print("\nmanifest written to {}".format(path))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="/tmp/pii.duckdb")
    ap.add_argument("--bench-db", default="/tmp/pii-bench.duckdb",
                    help="where the fixture table is written. Never the warehouse")
    ap.add_argument("--manifest", default=None,
                    help="write the measurement to this path")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("no database at {}. Run scripts/plant.py first.".format(args.db))
        return 2

    # Read only on the warehouse and a second file for the fixture. The first version of
    # this wrote `probe.outside_rule` into the warehouse database and dropped it again at
    # the start of its own next run, which left it there for every other probe. The run
    # after it had `scripts/classify_probe.py` raising a KeyError on a column the planted
    # index has never heard of. A measurement script that mutates the thing being measured
    # is a defect even when it cleans up after itself, because the cleanup happens on the
    # next run rather than this one.
    con = connect(args.db, read_only=True)
    try:
        warehouse_report, _samples, leaks, warehouse_pieces = section_warehouse(con)
    finally:
        con.close()

    bench_con = connect(args.bench_db, read_only=False)
    try:
        outcomes, tally, loaded, linked, bench_pieces = section_bench(bench_con)
    finally:
        bench_con.close()

    fixture_sweep, warehouse_sweep = section_sweep(warehouse_pieces, bench_pieces)

    if args.manifest:
        write_manifest(args.manifest, warehouse_report, outcomes, tally, loaded, linked,
                       fixture_sweep, warehouse_sweep)

    if leaks:
        print("\nFAIL: a shape carried something outside the allowlist")
        for line in leaks:
            print("  {}".format(line))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
