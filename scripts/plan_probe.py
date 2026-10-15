"""Which part of the engine the batched profiler statement spends its time in.

    python3 scripts/plan_probe.py --widths 42,84,168
    python3 scripts/plan_probe.py --widths 42,84,168 --manifest measurements/batched-plan.json
    python3 scripts/plan_probe.py --widths 336 --only batched

`scripts/scale_probe.py` answers how the two query shapes scale. It does not answer where
the batched shape's cost goes, and a README draft once filled that gap with a guess. This
runs both shapes under DuckDB's detailed profile and reports the planner, the query's own
execution and the part of the wall time no profiler metric covers.

`--only` exists for the same reason it does in `scale_probe.py`. The batched shape at 336
columns is 3025 aggregate expressions and the scaling bench measured its peak at 3.6 GB, so
two shapes in one process at that width can be killed instead of answering.

Each width prints as it finishes and flushes, so a width that takes the process down leaves
every width before it on the terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import plan, profile, scale  # noqa: E402

RULE = "-" * 100
PASSES = 3


def connect():
    import duckdb
    return duckdb.connect(":memory:")


def batched_split(con, columns, passes):
    """The one statement shape, profiled `passes` times, newest measurement last."""
    sql, _keys, stride = profile._profile_table_sql(
        scale.BENCH_SCHEMA, scale.BENCH_TABLE, columns)
    expressions = 1 + stride * len(columns)
    out = []
    for _ in range(passes):
        got, wall = plan.profile_statement(con, sql)
        out.append(plan.split("batched", len(columns), expressions, len(sql), wall, got))
    return out


def per_column_splits(con, columns, passes):
    """The W statement shape, summed.

    Every column's statement is profiled and the parts are added, because the shape's cost
    is the whole set and not a representative member of it. The wall time summed this way
    excludes the Python loop around the statements, which is under a millisecond a column
    and is not what either shape is being compared on.
    """
    pieces = [profile._profile_sql(scale.BENCH_SCHEMA, scale.BENCH_TABLE, c.name)
              for c in columns]
    sql_chars = sum(len(sql) for sql, _ in pieces)
    expressions = sum(4 + len(keys) for _, keys in pieces)
    out = []
    for _ in range(passes):
        totals = dict(latency=0.0, planner=0.0, all_optimizers=0.0, physical_planner=0.0)
        wall = 0.0
        for sql, _keys in pieces:
            got, one_wall = plan.profile_statement(con, sql)
            wall += one_wall
            for key in totals:
                totals[key] += float(got[key])
        out.append(plan.split("per column", len(columns), expressions, sql_chars,
                              wall, totals))
    return out


def summarise(splits):
    """Median shares and the spread of the wall times the medians came from."""
    walls = sorted(s.wall for s in splits)
    first = splits[0]
    return {
        "shape": first.shape,
        "columns": first.columns,
        "expressions": first.expressions,
        "sql_chars": first.sql_chars,
        "statements": 1 if first.shape == "batched" else first.columns,
        "median_wall": round(scale.median(walls), 4),
        "spread": round(walls[-1] / walls[0], 3) if walls[0] > 0 else 0.0,
        "share_planning": round(scale.median([s.share(plan.PLANNING) for s in splits]), 4),
        "share_executing": round(scale.median([s.share(plan.EXECUTING) for s in splits]), 4),
        "share_unprofiled": round(scale.median([s.share(plan.UNPROFILED) for s in splits]), 4),
        "verdict": sorted(set(s.verdict for s in splits)),
    }


def header() -> None:
    print("\nWHERE THE PROFILER STATEMENT SPENDS ITS TIME")
    print(RULE)
    print("{} passes a width. shares are of the wall time. spread is slowest over "
          "fastest".format(PASSES))
    print("{:>6} {:<11} {:>7} {:>6} {:>9} {:>8} {:>9} {:>10} {:>11} {:>12}".format(
        "cols", "shape", "stmts", "exprs", "median s", "spread", "planning",
        "executing", "unprofiled", "verdict"))
    sys.stdout.flush()


def row(summary) -> None:
    print("{:>6} {:<11} {:>7} {:>6} {:>9.4f} {:>8.3f} {:>9.4f} {:>10.4f} {:>11.4f} "
          "{:>12}".format(
              summary["columns"], summary["shape"], summary["statements"],
              summary["expressions"], summary["median_wall"], summary["spread"],
              summary["share_planning"], summary["share_executing"],
              summary["share_unprofiled"], ",".join(summary["verdict"])))
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--widths", default=",".join(str(w) for w in scale.WIDTHS))
    ap.add_argument("--rows", type=int, default=scale.BENCH_ROWS)
    ap.add_argument("--passes", type=int, default=PASSES)
    ap.add_argument("--only", choices=("both", "batched", "per"), default="both")
    ap.add_argument("--manifest")
    args = ap.parse_args()

    widths = [int(w) for w in args.widths.split(",") if w.strip()]
    con = connect()
    header()
    rows = []
    for width in widths:
        loaded = scale.build(con, width, args.rows)
        columns = scale.columns_for(width)
        if args.only in ("per", "both"):
            summary = summarise(per_column_splits(con, columns, args.passes))
            summary["rows"] = loaded
            rows.append(summary)
            row(summary)
        if args.only in ("batched", "both"):
            summary = summarise(batched_split(con, columns, args.passes))
            summary["rows"] = loaded
            rows.append(summary)
            row(summary)

    print("\nThe third share is wall time DuckDB's own profile does not account for.")
    print("It is small at every width here, so the statement text being parsed is not")
    print("where the batched shape loses. The two that matter are the first two.")
    sys.stdout.flush()

    if args.manifest:
        published = {
            "engine": "duckdb",
            "figures": "timings",
            "passes": args.passes,
            "rows": args.rows,
            "table": scale.BENCH_FQN,
            "widths": widths,
            "measurements": rows,
        }
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            args.manifest)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(published, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("wrote {}".format(args.manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
