"""Where a wide statement spends its time, split by the phase that spent it.

The scaling bench in `pii/scale.py` says batching every column into one statement is
slower at every width and unusable past a few hundred columns, and that the cost tracks the
number of aggregate expressions rather than the rows under them. It does not say which part
of the engine spends it. An early README draft said binding and planning, and that sentence
came out again because it named a mechanism the evidence did not reach.

This reaches it. DuckDB's detailed profile reports the planner, the optimizers and the
physical planner separately from the query's own latency, so planning and execution can be
compared instead of guessed at.

The reason this is a module rather than a function in the probe is the third number.
`latency` is what DuckDB says the query cost and it is not what the caller waited. The
difference is the part no profiler metric covers, parsing a quarter of a megabyte of SQL
among it, and it gets its own name because nothing had looked. I expected it to be most of
the cost at the wide end. It is 0.21 percent of the wall time at 336 columns and under 2
percent everywhere measured, which is the answer only because it was reported rather than
assumed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Dict, Tuple

# The three phases DuckDB times before the query runs. `all_optimizers` is the rollup of
# the thirty odd individual optimizer metrics and it is used instead of summing them,
# because the set of optimizers changes between releases and a sum written out here would
# go stale quietly.
PLANNING_METRICS: Tuple[str, ...] = ("planner", "all_optimizers", "physical_planner")

PLANNING = "planning"
EXECUTING = "executing"
UNPROFILED = "unprofiled"


@dataclass(frozen=True)
class PlanSplit:
    """One statement's cost, cut three ways.

    `wall` is measured by the caller around the whole round trip. `latency` is DuckDB's own
    figure for the query. Everything in `wall` and not in `latency` is parsing the
    statement text and whatever else happens before the profiler starts counting, and it
    gets its own name rather than being folded into either phase, because folding an
    unmeasured remainder into a phase is how a phase gets credited with it.
    """

    shape: str
    columns: int
    expressions: int
    sql_chars: int
    wall: float
    latency: float
    planner: float
    optimizers: float
    physical: float

    @property
    def planning(self) -> float:
        return self.planner + self.optimizers + self.physical

    @property
    def executing(self) -> float:
        return self.latency - self.planning

    @property
    def unprofiled(self) -> float:
        return self.wall - self.latency

    def share(self, part: str) -> float:
        """A part of the wall time, as a fraction of it.

        Shares rather than durations, because a share is the figure that survives being
        read on another machine. The durations are this machine's and are published as a
        median with a spread beside them.
        """
        if self.wall <= 0:
            raise ValueError("a statement cannot have taken no time at all")
        return getattr(self, part) / self.wall

    @property
    def verdict(self) -> str:
        """Which of the three parts is the largest share of the wall time."""
        parts = {name: getattr(self, name) for name in (PLANNING, EXECUTING, UNPROFILED)}
        return max(parts, key=lambda name: parts[name])


def split(shape: str, columns: int, expressions: int, sql_chars: int,
          wall: float, profile: Dict) -> PlanSplit:
    """Turn a parsed DuckDB profile and a measured wall time into one split.

    Raises on a profile missing any phase rather than defaulting it to zero. A missing
    metric would be read as a phase that cost nothing, which is the same shape of lie as a
    gate passing on no files.
    """
    for metric in PLANNING_METRICS + ("latency",):
        if metric not in profile:
            raise KeyError(
                "profile carries no {}, so this is not a detailed profile".format(metric))
    taken = PlanSplit(
        shape=shape, columns=columns, expressions=expressions, sql_chars=sql_chars,
        wall=float(wall), latency=float(profile["latency"]),
        planner=float(profile["planner"]),
        optimizers=float(profile["all_optimizers"]),
        physical=float(profile["physical_planner"]),
    )
    if taken.executing < 0:
        raise ValueError(
            "the phases add up to more than the latency they sit inside, "
            "{} against {}".format(taken.planning, taken.latency))
    if taken.unprofiled < 0:
        raise ValueError(
            "DuckDB reports a latency longer than the wall time around it, "
            "{} against {}".format(taken.latency, taken.wall))
    return taken


def profile_statement(con, sql: str) -> Tuple[Dict, float]:
    """Run one statement under detailed profiling and return its profile and wall time.

    `EXPLAIN ANALYZE` rather than a profiling output file. The file route needs
    `profiling_output` set to a path whose extension matches the format, and it wrote
    nothing on four of six attempts here with no error raised, which is not an instrument
    to publish a number from. `EXPLAIN ANALYZE` hands the profile back in the result set,
    so there is nothing to miss.

    The statement really runs. `EXPLAIN ANALYZE` executes and then throws the rows away,
    which is what makes it usable on a statement returning one row of several thousand
    values.
    """
    con.execute("PRAGMA enable_profiling='json'")
    con.execute("PRAGMA profiling_mode='detailed'")
    try:
        start = time.perf_counter()
        rows = con.execute("EXPLAIN ANALYZE " + sql).fetchall()
        wall = time.perf_counter() - start
    finally:
        con.execute("PRAGMA disable_profiling")
    if not rows:
        raise RuntimeError("EXPLAIN ANALYZE returned nothing to read")
    return json.loads(rows[0][-1]), wall
