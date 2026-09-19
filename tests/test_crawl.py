"""Checks for the metadata crawler. Needs a real database.

Not in `tests/run_all.py`. A crawler tested against a hand built fake catalog is a crawler
tested against my idea of what DuckDB returns, and the two have already disagreed once
here: `duckdb_columns()` has no `table_schema` column and the first query written here
named one. So these run under `tests/run_with_duckdb.py` and they run against a database
`scripts/plant.py` really built.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import crawl as crawler  # noqa: E402
from pii.corpus import generate  # noqa: E402
from pii.schema import Column, Table, fingerprint, tables_by_fqn  # noqa: E402
from scripts.plant import load  # noqa: E402

_CACHE = {}


def _built():
    """One database for the module, built once.

    Each check used to get its own and the load is the expensive part. Cached rather than
    module level so importing this file does not build a warehouse as a side effect.

    The published defaults, not a small sample. It was 120 patients and seed 606 first,
    and at that size one nullable column out of 26 came out with no null in it by chance,
    which failed the "every nullable column is exercised" check for a reason that has
    nothing to do with the crawler. That claim is a property of the shipped corpus at a
    thousand rows rather than of any corpus, so the fixture is now the shipped corpus and
    the claim is about the artefact somebody actually runs. At a 2 percent rate a 120 row
    column misses about 9 percent of the time, so the old fixture would have flaked.
    """
    if "path" not in _CACHE:
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        load(path, generate())
        _CACHE["path"] = path
    return _CACHE["path"]


def _crawl(read_only=True):
    import duckdb

    con = duckdb.connect(_built(), read_only=read_only)
    try:
        return crawler.crawl(con), None
    finally:
        con.close()


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def check_the_crawl_recovers_the_declared_schema_exactly():
    # The day's whole point. This is the first thing in this repo that agrees with
    # pii/schema.py without having been written from it.
    crawled, _ = _crawl()
    assert crawler.recovers_declared_schema(crawled), (
        crawled.fingerprint(), fingerprint())
    assert crawler.compare(crawled) == ()


def check_the_crawl_finds_every_table_and_every_column():
    crawled, _ = _crawl()
    declared = tables_by_fqn()
    assert set(crawled.fqns) == set(declared)
    assert crawled.n_columns == sum(len(t.columns) for t in declared.values())


def check_the_crawl_recovers_the_keys_from_constraints_and_not_from_the_name():
    # raw.encounter.patient_id ends in _id and is not a key. A crawler guessing from the
    # name would call it one, and the fingerprint would then not match, which is the
    # reason the key flag is in the hash at all.
    crawled, _ = _crawl()
    keys = set(crawled.key_columns)
    assert ("raw.patient", "patient_id") in keys
    assert ("raw.encounter", "encounter_id") in keys
    assert ("raw.encounter", "patient_id") not in keys
    assert ("raw.device_reading", "patient_id") not in keys
    declared = {(t.fqn, c.name) for t in tables_by_fqn().values()
                for c in t.columns if c.is_key}
    assert keys == declared


def check_the_crawl_recovers_a_parameterised_type_and_not_just_its_family():
    # DECIMAL(12,2) has to come back with its precision and scale. A crawler returning
    # DECIMAL would match nothing and a crawler returning DOUBLE would be wrong in a way
    # that matters to a masking policy.
    crawled, _ = _crawl()
    claim = crawled.by_fqn()["raw.claim"]
    assert claim.column("billed_amount").sql_type == "DECIMAL(12,2)"
    assert claim.column("claim_id").sql_type == "BIGINT"


def check_the_crawl_skips_the_engine_schemas():
    crawled, _ = _crawl()
    for t in crawled.tables:
        assert t.schema in ("raw", "analytics"), t.fqn
    assert "information_schema" in crawler.ENGINE_SCHEMAS
    assert "pg_catalog" in crawler.ENGINE_SCHEMAS


def check_the_comparison_catches_a_single_flipped_flag():
    # The control the recovery check needs. Without it, a crawl function that ignored the
    # catalog and returned pii/schema.py would pass everything above.
    crawled, _ = _crawl()
    broken = crawler.Crawl(
        tables=tuple(
            Table(t.schema, t.name, tuple(
                Column(c.name, c.sql_type, nullable=not c.nullable, is_key=c.is_key)
                if c.name == "mrn" else c for c in t.columns))
            for t in crawled.tables),
        key_columns=crawled.key_columns,
        engine=crawled.engine,
        engine_version=crawled.engine_version,
    )
    diffs = crawler.compare(broken)
    assert len(diffs) == 1, diffs
    assert diffs[0].field == "nullable"
    assert diffs[0].where == "raw.patient.mrn"
    assert not crawler.recovers_declared_schema(broken)


def check_the_comparison_catches_a_changed_type():
    crawled, _ = _crawl()
    broken = crawler.Crawl(
        tables=tuple(
            Table(t.schema, t.name, tuple(
                Column(c.name, "VARCHAR", nullable=c.nullable, is_key=c.is_key)
                if c.name == "billed_amount" else c for c in t.columns))
            for t in crawled.tables),
        key_columns=crawled.key_columns,
        engine=crawled.engine,
        engine_version=crawled.engine_version,
    )
    diffs = crawler.compare(broken)
    assert [d.field for d in diffs] == ["sql_type"], diffs


def check_the_comparison_looks_both_ways():
    # A table the warehouse holds and nobody declared is the case that matters most on a
    # real warehouse and is the easiest half to leave out. Same shape as the
    # mapping, where every clause had a category and not every category had a clause.
    crawled, _ = _crawl()
    short = crawler.Crawl(crawled.tables[1:], crawled.key_columns,
                          crawled.engine, crawled.engine_version)
    missing = [d for d in crawler.compare(short) if d.field == "table"]
    assert len(missing) == 1, missing
    assert missing[0].crawled is None

    extra = crawler.Crawl(
        crawled.tables + (Table("raw", "invented", (Column("x", "BIGINT"),)),),
        crawled.key_columns, crawled.engine, crawled.engine_version)
    undeclared = [d for d in crawler.compare(extra)
                  if d.field == "table" and d.declared is None]
    assert len(undeclared) == 1, undeclared
    assert undeclared[0].where == "raw.invented"


def check_a_reordered_table_is_reported_as_order_and_not_as_a_change():
    # Written first on the assumption that the fingerprint sorts everything and therefore
    # could not see a column swap, so the comparison had to catch it alone. That was
    # wrong. schema.fingerprint sorts the tables and not the columns, and sort_keys sorts
    # the keys of each row dict rather than the list, so the hash moves on a column swap
    # and does not move on a table swap. Both directions pinned here, because the
    # asymmetry is the sort of thing a later run would assume either way.
    crawled, _ = _crawl()
    patient = crawled.by_fqn()["raw.patient"]
    flipped = Table(patient.schema, patient.name,
                    (patient.columns[1], patient.columns[0]) + patient.columns[2:])
    other = tuple(t for t in crawled.tables if t.fqn != "raw.patient")
    reordered = crawler.Crawl((flipped,) + other, crawled.key_columns,
                              crawled.engine, crawled.engine_version)
    fields = [d.field for d in crawler.compare(reordered)]
    assert fields == ["order"], fields
    assert reordered.fingerprint() != fingerprint(), "the hash should see a column swap"

    # And the table order really is invisible to it, which is what makes the comparison's
    # own ordering report the only thing watching that half.
    shuffled = crawler.Crawl(tuple(reversed(crawled.tables)), crawled.key_columns,
                             crawled.engine, crawled.engine_version)
    assert shuffled.fingerprint() == fingerprint()
    assert crawler.compare(shuffled) == ()


def check_the_fingerprint_helper_puts_the_declared_tables_back():
    # crawl.Crawl.fingerprint borrows pii/schema.fingerprint by swapping the module level
    # TABLES, which is a sharp edge worth a check. If the restore in the finally ever
    # stopped running, every later check in the process would grade against whatever the
    # last crawl happened to hold and would still look green.
    import pii.schema as declared

    before = declared.TABLES
    crawled, _ = _crawl()
    crawled.fingerprint()
    assert declared.TABLES is before
    assert declared.fingerprint() == fingerprint()


def check_the_nullability_grader_reads_rows_and_judges_both_directions():
    import duckdb

    con = duckdb.connect(_built(), read_only=True)
    try:
        crawled = crawler.crawl(con)
        verdicts = crawler.grade_nullability(con, crawled)
    finally:
        con.close()

    assert len(verdicts) == crawled.n_columns
    # No NOT NULL column may hold a null. The database enforces this, so a failure here
    # means the crawl recovered the wrong flag rather than that the data is bad.
    assert crawler.contradictions(verdicts) == ()
    # And every nullable column has to be exercised, which is the direction that could not
    # fire at all before the corpus wrote its first null.
    assert crawler.unexercised_nullable(verdicts) == ()
    assert sum(1 for v in verdicts if v.exercised) > 0
    for v in verdicts:
        assert v.nulls_observed <= v.rows, v.address


def check_an_unexercised_nullable_column_is_named_rather_than_passed():
    # The state the whole repo was in before the corpus wrote a null, reproduced deliberately. A nullable
    # column nothing has ever put a null in is not a defect and it is not evidence
    # either, and the grader has to say which of those it is looking at.
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute("CREATE TABLE raw.t (a BIGINT NOT NULL, b VARCHAR)")
        con.execute("INSERT INTO raw.t VALUES (1, 'x'), (2, 'y')")
        crawled = crawler.crawl(con)
        verdicts = crawler.grade_nullability(con, crawled)
    finally:
        con.close()

    by_name = {v.column: v for v in verdicts}
    assert by_name["b"].unexercised is True
    assert by_name["b"].exercised is False
    assert by_name["b"].contradicted is False
    assert crawler.unexercised_nullable(verdicts) == ("raw.t.b",)


def check_an_empty_table_gives_the_grader_nothing_to_find():
    # The check most likely to be wrong is the one with the least to look at. Zero rows
    # must not read as "nullable flags verified", which is the same failure as a prose
    # checker reporting clean over zero files.
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute("CREATE TABLE raw.t (a BIGINT NOT NULL, b VARCHAR)")
        verdicts = crawler.grade_nullability(con, crawler.crawl(con))
    finally:
        con.close()

    assert all(v.rows == 0 for v in verdicts)
    assert not any(v.exercised for v in verdicts)
    assert not any(v.contradicted for v in verdicts)
    assert crawler.unexercised_nullable(verdicts) == ("raw.t.b",)


def check_the_grader_names_a_contradiction_when_the_flag_is_wrong():
    # A nullable column crawled as NOT NULL. The database cannot produce this, so it is
    # built by hand: load the data with the column nullable, then grade it against a
    # crawl whose flag says otherwise. Without this the contradicted branch is a branch
    # nothing has ever reached.
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute("CREATE TABLE raw.t (a BIGINT NOT NULL, b VARCHAR)")
        con.execute("INSERT INTO raw.t VALUES (1, NULL), (2, 'y')")
        honest = crawler.crawl(con)
        lying = crawler.Crawl(
            tables=tuple(
                Table(t.schema, t.name, tuple(
                    Column(c.name, c.sql_type, nullable=False, is_key=c.is_key)
                    for c in t.columns))
                for t in honest.tables),
            key_columns=honest.key_columns,
            engine=honest.engine,
            engine_version=honest.engine_version,
        )
        verdicts = crawler.grade_nullability(con, lying)
    finally:
        con.close()

    assert crawler.contradictions(verdicts) == ("raw.t.b",)
    assert not any(v.exercised for v in verdicts)


def check_the_crawl_reads_no_user_data():
    # A governance tool gets pointed at data somebody is not allowed to look at, so the
    # crawl itself must never select a value. Asserted by reading the module rather than
    # by trusting the docstring, and it is scoped to crawl() because grade_nullability
    # reads counts on purpose and says so.
    # Comments are stripped first. The first version of this scanned the raw source and
    # failed on the words "raw.encounter.patient_id" inside a comment explaining why the
    # keys do not come from a naming convention. A check that a function does not query a
    # table must not be satisfiable or breakable by prose.
    import inspect

    def code_only(fn):
        out = []
        for line in inspect.getsource(fn).splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            out.append(line.split("  #")[0])
        return "\n".join(out)

    source = code_only(crawler.crawl)
    assert "select *" not in source.lower()
    for table in ("raw.patient", "raw.encounter", "raw.claim", "raw.device_reading"):
        assert table not in source, table
    # The stripper has to leave the SQL behind or the assertions above pass on nothing.
    assert "duckdb_columns()" in source
    assert "duckdb_constraints()" in source

    counts = code_only(crawler.grade_nullability)
    assert "count(" in counts
    assert "select *" not in counts.lower()


def check_the_crawl_refuses_nothing_quietly_when_the_catalog_is_empty():
    # An empty database has to produce an empty crawl and a comparison full of missing
    # tables, not a clean recovery. Nothing to check is a finding.
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        crawled = crawler.crawl(con)
    finally:
        con.close()

    assert crawled.tables == ()
    assert crawled.n_columns == 0
    assert not crawler.recovers_declared_schema(crawled)
    diffs = crawler.compare(crawled)
    assert len(diffs) == len(tables_by_fqn()), diffs
    assert all(d.field == "table" and d.crawled is None for d in diffs)


def check_a_table_with_no_columns_cannot_be_constructed():
    # Table.__post_init__ refuses it, and the crawl groups by table so an empty group
    # cannot occur. Pinned because the grader builds a SELECT from the column list and an
    # empty list would produce "SELECT count(*), FROM t", which is a syntax error rather
    # than a clear refusal.
    assert _raises(lambda: Table("raw", "t", ()), ValueError)


def check_one_null_is_enough_to_call_a_column_exercised():
    # `nulls_observed > 0` mutated to `> 1` and survived, because every nullable column in
    # the shipped corpus holds at least thirteen nulls, so the boundary was never near.
    # One null is the whole claim: it is what turns a nullable declaration into a fact
    # about the data.
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute("CREATE TABLE raw.t (a BIGINT NOT NULL, b VARCHAR, c VARCHAR)")
        con.execute("INSERT INTO raw.t VALUES (1, NULL, 'x'), (2, 'y', 'z')")
        verdicts = crawler.grade_nullability(con, crawler.crawl(con))
    finally:
        con.close()

    by_name = {v.column: v for v in verdicts}
    assert by_name["b"].nulls_observed == 1
    assert by_name["b"].exercised is True, "one null has to be enough"
    assert by_name["b"].unexercised is False
    assert by_name["c"].nulls_observed == 0
    assert by_name["c"].exercised is False
    assert crawler.unexercised_nullable(verdicts) == ("raw.t.c",)


def check_a_crawled_fact_cannot_be_edited_after_it_was_read():
    # Three frozen=True flags mutated to False and all three survived. These carry what
    # the catalog said, and the whole argument for the crawl is that it is a fact about
    # the database rather than something a later caller adjusted.
    crawled, _ = _crawl()
    assert _raises(lambda: setattr(crawled, "tables", ()), Exception)
    assert _raises(lambda: setattr(crawled, "engine_version", "made up"), Exception)
    diff = crawler.Difference("raw.t.a", "nullable", True, False)
    assert _raises(lambda: setattr(diff, "crawled", True), Exception)
    verdict = crawler.NullabilityVerdict("raw.t", "a", True, 3, 10)
    assert _raises(lambda: setattr(verdict, "nulls_observed", 0), Exception)


def check_the_module_exposes_no_convenience_wrapper_nothing_calls():
    # first_difference was deleted after a mutant changing which element it
    # returned survived, because nothing anywhere called it. Pinned so it does not come
    # back as a helper somebody adds for a caller that still does not exist.
    assert not hasattr(crawler, "first_difference")


def check_a_catalog_identifier_is_quoted_before_it_reaches_sql():
    # grade_nullability builds its count expressions from names the catalog handed back,
    # and a name cannot be a bound parameter, so quoting is the only defence. Exercised
    # against names that really do break unquoted SQL: a reserved word, a space, and an
    # embedded quote.
    import duckdb

    assert crawler._quote("plain") == '"plain"'
    assert crawler._quote('we"ird') == '"we""ird"'
    assert _raises(lambda: crawler._quote("bad\x00name"), ValueError)

    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute(
            'CREATE TABLE raw."select" ("order" BIGINT NOT NULL, "a b" VARCHAR, '
            '"we""ird" VARCHAR)')
        con.execute('INSERT INTO raw."select" VALUES (1, NULL, NULL), (2, \'x\', \'y\')')
        crawled = crawler.crawl(con)
        verdicts = crawler.grade_nullability(con, crawled)
    finally:
        con.close()

    by_name = {v.column: v for v in verdicts}
    assert set(by_name) == {"order", "a b", 'we"ird'}
    assert by_name["a b"].nulls_observed == 1
    assert by_name['we"ird'].nulls_observed == 1
    assert by_name["order"].nulls_observed == 0
    assert crawler.contradictions(verdicts) == ()
