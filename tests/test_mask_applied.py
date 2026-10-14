"""Masking checks that need a real database.

Separate from `tests/test_mask.py`, which needs nothing but the standard library. The split
follows the runner split: `tests/run_all.py` must stay importable with no driver installed.

These are the checks that can catch a masking expression that parses and does the wrong
thing. A `substr` with the wrong offset, a `year()` applied to something that is not a date,
a GROUP BY that silently drops nulls. None of that is visible from a string comparison.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import classify, crawl, mask, profile  # noqa: E402
from pii.corpus import generate  # noqa: E402
from pii.taxonomy import Granularity  # noqa: E402
from scripts.plant import load  # noqa: E402

_STATE = {}


def _db():
    """One warehouse for every check in this module, built on first use.

    Rebuilding per check costs about a second each and these are pure reads.
    """
    if "path" not in _STATE:
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        load(path, generate(n_patients=400, seed=20260921))
        _STATE["path"] = path
    import duckdb
    return duckdb.connect(_STATE["path"], read_only=True)


def _policies(con):
    crawled = crawl.crawl(con)
    profiles = profile.profile_crawl(con, crawled)
    return mask.generate(classify.classify_warehouse(profiles))


def check_every_generalisation_the_policy_emits_actually_runs():
    con = _db()
    try:
        ran = 0
        for p in _policies(con):
            if p.action is not mask.Action.GENERALISE:
                continue
            expr = mask.masking_expression(p)
            con.execute("SELECT {} FROM {} LIMIT 1".format(expr, p.table)).fetchone()
            ran += 1
        assert ran >= 3, ran
    finally:
        con.close()


def check_a_generalised_date_keeps_the_year_and_loses_the_day():
    con = _db()
    try:
        row = con.execute(
            'SELECT count(distinct year("birth_date")), count(distinct "birth_date") '
            "FROM raw.patient").fetchone()
        years, days = row
        assert years < days, (years, days)
    finally:
        con.close()


def check_a_generalised_postal_code_is_three_characters_and_not_four():
    con = _db()
    try:
        row = con.execute(
            'SELECT max(length(substr(cast("postal_code" as varchar), 1, 3))) '
            "FROM raw.patient WHERE postal_code IS NOT NULL").fetchone()
        assert row[0] == 3, row
    finally:
        con.close()


def check_generalising_never_raises_k():
    """Coarsening can only pool rows together, so k cannot go down and uniqueness cannot
    go up. A generaliser that widened a value rather than narrowing it would break this,
    and it is the property that makes the whole layer worth anything."""
    con = _db()
    try:
        raw = con.execute(
            "SELECT min(c), sum(CASE WHEN c = 1 THEN 1 ELSE 0 END) FROM "
            '(SELECT count(*) AS c FROM raw.patient GROUP BY "postal_code", '
            '"birth_date", "sex")').fetchone()
        masked = con.execute(
            "SELECT min(c), sum(CASE WHEN c = 1 THEN 1 ELSE 0 END) FROM "
            "(SELECT count(*) AS c FROM raw.patient GROUP BY "
            'substr(cast("postal_code" as varchar), 1, 3), year("birth_date"), "sex")'
        ).fetchone()
        assert masked[0] >= raw[0], (raw, masked)
        assert masked[1] <= raw[1], (raw, masked)
    finally:
        con.close()


def check_the_mart_reports_a_comfortable_k_that_its_own_grain_contradicts():
    """The finding, pinned so it cannot quietly stop being true.

    The policy measures k over the quasi set the classifier found, which on the mart is
    `postal_code` alone. Adding `department`, which is planted not personal and appears on
    no clause list, takes the same table to k of 1. If this check ever fails, either the
    classifier started finding the other columns or the corpus changed shape, and the
    README says something wrong either way.
    """
    con = _db()
    try:
        policies = [p for p in _policies(con)
                    if p.table == "analytics.encounter_daily"]
        r = mask.measure_residual(con, "analytics.encounter_daily", policies,
                                  weight="encounters")
        assert r.columns == ("postal_code",), r.columns
        assert r.k > 1, r.k

        wider = con.execute(
            "SELECT min(s) FROM (SELECT sum(encounters) AS s FROM "
            "analytics.encounter_daily GROUP BY "
            'substr(cast("postal_code" as varchar), 1, 3), "department")').fetchone()
        assert wider[0] == 1, wider
    finally:
        con.close()


def check_the_weight_column_changes_the_population_it_counts():
    con = _db()
    try:
        policies = [p for p in _policies(con)
                    if p.table == "analytics.encounter_daily"]
        rows = mask.measure_residual(con, "analytics.encounter_daily", policies)
        people = mask.measure_residual(con, "analytics.encounter_daily", policies,
                                       weight="encounters")
        assert people.population > rows.population, (rows.population, people.population)
    finally:
        con.close()


def check_the_residual_query_returns_only_integers():
    con = _db()
    try:
        policies = [p for p in _policies(con)
                    if p.table == "analytics.encounter_daily"]
        row = con.execute(mask.residual_sql("analytics.encounter_daily",
                                            policies)).fetchone()
        for value in row:
            assert isinstance(value, int), (value, type(value))
    finally:
        con.close()


def check_a_resolved_review_produces_sql_that_runs():
    con = _db()
    try:
        reviews = [p for p in _policies(con) if p.action is mask.Action.REVIEW]
        assert reviews
        ran = 0
        for p in reviews:
            settled = mask.resolve_review(p, True)
            if not settled.changes_the_value:
                continue
            con.execute("SELECT {} FROM {} LIMIT 1".format(
                mask.masking_expression(settled), settled.table)).fetchone()
            ran += 1
        assert ran > 0
    finally:
        con.close()


def check_the_shipped_warehouse_has_a_table_nobody_can_measure_yet():
    """Four of five tables are blocked on the review queue and that is the honest state.

    Pinned because the probe prints it and a README number with nothing running behind it
    is how the last set of published figures went stale.
    """
    con = _db()
    try:
        by_table = {}
        for p in _policies(con):
            by_table.setdefault(p.table, []).append(p)
        blocked = 0
        for table, ps in by_table.items():
            try:
                mask.residual_sql(table, ps,
                                  weight="encounters"
                                  if table == "analytics.encounter_daily" else None)
            except ValueError:
                blocked += 1
        assert blocked == 4, blocked
    finally:
        con.close()


# The derivation manifest. Re-derived from a database rather than trusted, same rule as
# every other measurement in this repo.

MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "measurements", "weight-refusal.json")

REGENERATE_DERIVATION = ("python3 scripts/plant.py --db /tmp/pii.duckdb && "
                         "python3 scripts/mask_probe.py --db /tmp/pii.duckdb "
                         "--manifest measurements/weight-refusal.json")


def check_the_derivation_manifest_re_derives_from_a_database():
    """Every row of the committed manifest, recomputed here.

    The manifest is what the README is allowed to quote about ot-086. A figure in it that
    cannot be recovered on another machine is the defect day 5 shipped and the second fire
    found, so this is the gate that stops it happening to this one.
    """
    import json

    from pii import lineage, schema
    from scripts.mask_probe import manifest as build_manifest

    with open(MANIFEST, encoding="utf-8") as fh:
        published = json.load(fh)

    con = _db()
    try:
        graph = lineage.warehouse_graph(con)
    finally:
        con.close()
    fresh = build_manifest(graph, sorted({t.fqn for t in schema.TABLES}))
    assert fresh == published, (
        "the derivation manifest did not re-derive. regenerate with: {}".format(
            REGENERATE_DERIVATION))


def check_the_derivation_manifest_carries_no_wall_time():
    # Same door closer as the round two manifest. A derivation state is a count and a
    # verdict and nothing in it is allowed to be a duration.
    with open(MANIFEST, encoding="utf-8") as fh:
        text = fh.read()
    for word in ("_ms", "seconds", "millis"):
        assert word not in text, (
            "{} is a wall time and the manifest is counts only".format(word))


def check_losing_one_statement_costs_the_whole_warehouse_its_weight_verdict():
    """The number the manifest exists to publish, asserted rather than printed.

    Four of five tables stop answering, and the mart keeps its answer because a found
    count edge is evidence that a refusal elsewhere does not unsay. The asymmetry is the
    point. This design trades a quiet wrong answer for a loud missing one.
    """
    import json

    with open(MANIFEST, encoding="utf-8") as fh:
        published = json.load(fh)
    poisoned = published["with_one_unreadable_statement"]
    assert poisoned["tables_refused"] == 4
    assert poisoned["tables_answered"] == 1
    assert published["refusals"] == 0
    answered = [r["table"] for r in published["tables"]
                if not r["refused_with_one_unreadable_statement"]]
    assert answered == ["analytics.encounter_daily"]
