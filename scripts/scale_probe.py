"""Both profiler query shapes, timed against each other as the table gets wider.

    python3 scripts/scale_probe.py --widths 42,84,168
    python3 scripts/scale_probe.py --widths 336 --only batched
    python3 scripts/scale_probe.py --widths 42,84,168 --manifest measurements/profiler-scaling.json

`ot-078` asked where one query per column stops being fine. The expected answer was that it
stops being fine somewhere wide and batching fixes it. On DuckDB the answer came back the
other way round, so the per-width output is printed and flushed as each width finishes
rather than collected and printed at the end. A width that takes the process down with it
still leaves every width before it on the terminal, and that is how the ceiling got
measured at all.

`--only` exists so peak resident memory can be attributed to one shape. `ru_maxrss` is a
high water mark for the whole process, so two shapes in one process give one number and it
belongs to whichever was worse.

A note on an instrument that did not work. The first version of the ceiling measurement set
`RLIMIT_AS` so an over-budget query would raise instead of being killed. It raised at 42
columns under a 1 GB cap, where peak resident memory is a fifth of that, because the cap
counts the allocator's virtual reservation rather than the query's working set. One run of
it also segfaulted. Address space is not a proxy for memory used and the number it produces
is not published anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import profile, scale  # noqa: E402

RULE = "-" * 96


def connect():
    import duckdb
    return duckdb.connect(":memory:")


def peak_rss_mb() -> int:
    """Process high water mark in MB. Linux reports `ru_maxrss` in kilobytes."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def header(passes: int) -> None:
    print("\nPROFILER QUERY SHAPES AS THE TABLE GETS WIDER")
    print(RULE)
    print("median of {} passes. spread is the slowest pass over the fastest".format(passes))
    print("{:>6}  {:>6}  {:<10} {:>6} {:>9} {:>9} {:>8} {:>8} {:>9}".format(
        "cols", "rows", "shape", "stmts", "sql chars", "median s", "spread",
        "ms/col", "peak MB"))
    sys.stdout.flush()


def row(rep, peak) -> None:
    print("{:>6}  {:>6}  {:<10} {:>6} {:>9} {:>9.3f} {:>8.2f} {:>8.3f} {:>9}".format(
        rep.columns, rep.rows, rep.shape, rep.statements,
        rep.sql_chars, rep.middle, rep.spread, rep.per_column_ms, peak))
    sys.stdout.flush()


def one_width(con, width: int, rows: int, only: str, passes: int):
    """Measure one width and return the timings and any disagreement between the shapes.

    The agreement check only runs when both shapes ran. Under `--only` there is nothing to
    compare and saying so is better than comparing a profile set against itself.
    """
    loaded = scale.build(con, width, rows)
    columns = scale.columns_for(width)
    timings = []
    per_profiles = batch_profiles = None

    if only in ("per", "both"):
        rep, per_profiles = scale.repeat(
            scale.time_per_column, con, columns, loaded, passes)
        timings.append(rep)
        row(rep, peak_rss_mb())

    if only in ("batched", "both"):
        rep, batch_profiles = scale.repeat(
            scale.time_batched, con, columns, loaded, passes)
        timings.append(rep)
        row(rep, peak_rss_mb())

    bad = ()
    if per_profiles is not None and batch_profiles is not None:
        bad = profile.disagreements(per_profiles, batch_profiles)
        print("{:>6}  {:>6}  agreement: {}".format(
            width, loaded,
            "identical on every field" if not bad
            else "{} disagreements".format(len(bad))))
        for line in bad:
            print("        {}".format(line))
        sys.stdout.flush()
    return timings, bad


def section_sample_counts(con, width: int, rows: int, passes: int) -> None:
    """The second caller's first statement, batched and not.

    `pii/sample.py` issues two statements per column and only one of them can be collapsed
    without widening how many values are in the process at once. This times the one that
    can.
    """
    loaded = scale.build(con, width, rows)
    columns = scale.columns_for(width)
    per_seconds = []
    batched_seconds = []
    for _ in range(max(1, passes)):
        per, batched = scale.time_sample_counts(con, columns, loaded)
        per_seconds.append(per.seconds)
        batched_seconds.append(batched.seconds)

    print("\n\nTHE SAMPLING ARM'S COUNT STATEMENT AT {} COLUMNS".format(width))
    print(RULE)
    for shape, seconds, statements in (
            ("per column", per_seconds, len(columns)),
            ("batched", batched_seconds, 1)):
        print("{:<12} {:>6} statements  median {:>7.3f}s  low {:>7.3f}s  "
              "high {:>7.3f}s".format(shape, statements, scale.median(seconds),
                                      min(seconds), max(seconds)))
    ratio = scale.median(per_seconds) / scale.median(batched_seconds)
    print("median ratio {:.2f}x over {} passes".format(ratio, len(per_seconds)))
    # The ranges overlapping is the result here, not the ratio. Said out loud because a
    # ratio printed on its own invites being quoted.
    if min(per_seconds) < max(batched_seconds) and min(batched_seconds) < max(per_seconds):
        print("the two ranges overlap, so this machine cannot separate the two shapes")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--widths", default=",".join(str(w) for w in scale.WIDTHS))
    ap.add_argument("--rows", type=int, default=scale.BENCH_ROWS)
    ap.add_argument("--only", choices=("per", "batched", "both"), default="both")
    ap.add_argument("--passes", type=int, default=scale.DEFAULT_PASSES,
                    help="how many times to time each shape at each width")
    ap.add_argument("--sample-counts-at", type=int, default=0,
                    help="also time the sampling arm's count statement at this width")
    ap.add_argument("--manifest", default=None)
    args = ap.parse_args()

    widths = [int(w) for w in args.widths.split(",") if w.strip()]
    con = connect()
    header(args.passes)

    timings = []
    failures = []
    disagreed = []
    for width in widths:
        try:
            got, bad = one_width(con, width, args.rows, args.only, args.passes)
        except Exception as exc:
            print("{:>6}  {:>6}  DID NOT COMPLETE  {}: {}".format(
                width, args.rows, type(exc).__name__, str(exc).splitlines()[0][:80]))
            sys.stdout.flush()
            failures.append({"columns": width, "error": type(exc).__name__})
            continue
        timings.extend(got)
        disagreed.extend(bad)

    if args.only == "both":
        print()
        medians = [r.as_timing() for r in timings]
        for w, time_ratio, sql_ratio in scale.speedups(medians):
            print("width {:>4}  batched is {:.2f}x the per column wall time  "
                  "and {:.2f}x its SQL length".format(w, 1.0 / time_ratio
                                                      if time_ratio else 0.0,
                                                      1.0 / sql_ratio
                                                      if sql_ratio else 0.0))
        print()
        print("SQL length growth exponent against width")
        for shape in ("per column", "batched"):
            exponent = scale.growth_exponent(medians, shape)
            if exponent == 0.0:
                # A single width cannot give an exponent and printing 0.000 reads like one.
                print("  {:<12} needs at least two widths".format(shape))
            else:
                print("  {:<12} {:.3f}".format(shape, exponent))

    if args.sample_counts_at:
        section_sample_counts(con, args.sample_counts_at, args.rows, args.passes)

    con.close()

    if args.manifest:
        # Merged rather than overwritten, because the peak memory figure is only honest
        # when one process measures one shape at one width. A process that has already run
        # the batched shape at 168 columns carries its allocator high water into the 336
        # reading, and a five pass run of all four widths in one process was killed before
        # it reached the last one. So the published table is eight runs and this is how
        # they land in one file. Keyed on shape, columns and rows, so a re-run of one cell
        # replaces that cell.
        existing = {}
        previous = {}
        if os.path.exists(args.manifest):
            with open(args.manifest) as fh:
                previous = json.load(fh)
            for entry in previous.get("timings", []):
                existing[(entry["shape"], entry["columns"], entry["rows"])] = entry

        payload = {
            "table": scale.BENCH_FQN,
            "rows": args.rows,
            "widths": sorted(set(previous.get("widths", [])) | set(widths)),
            "passes": args.passes,
            "ceiling_width": scale.CEILING_WIDTH,
            "timings": [
                {
                    "shape": r.shape,
                    "columns": r.columns,
                    "rows": r.rows,
                    "statements": r.statements,
                    "sql_chars": r.sql_chars,
                    "median_seconds": round(r.middle, 4),
                    "seconds": [round(s, 4) for s in r.seconds],
                    "spread": round(r.spread, 3),
                }
                for r in timings
            ],
            "did_not_complete": failures,
            "disagreements": list(disagreed),
        }
        # The peak is a process figure, so it only belongs to a shape when one shape ran.
        # Under `--only both` the batched reading leaves its high water mark behind and the
        # per column entry would inherit it, which would publish the wrong number for the
        # cheaper of the two shapes.
        for entry in payload["timings"]:
            if args.only != "both":
                entry["peak_rss_mb"] = peak_rss_mb()
            existing[(entry["shape"], entry["columns"], entry["rows"])] = entry
        payload["timings"] = [existing[k] for k in sorted(existing)]
        payload["did_not_complete"] = (
            [f for f in previous.get("did_not_complete", []) if f not in failures]
            + failures)

        with open(args.manifest, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("\nmanifest written to {}".format(args.manifest))

    if disagreed:
        print("\nFAIL: the two query shapes returned different numbers")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
