"""Checks that need a real database.

Not in `tests/run_all.py`. They run under `tests/run_with_duckdb.py`, which fails rather
than skips when the driver is missing, because a suite that quietly drops half its checks
reports a number that means something different depending on what happened to be
installed.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii.corpus import generate  # noqa: E402
from pii.schema import TABLES, tables_by_fqn  # noqa: E402
from scripts.plant import ddl_for, emit_ddl, load  # noqa: E402


def _tmp_db():
    # Under TMPDIR rather than in the repo. DuckDB has to unlink its write ahead log to
    # check point, and some mounts refuse that, which surfaces as a fatal error reading
    # like corruption.
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    return path


def check_the_generated_ddl_names_every_column_of_every_table():
    for t in TABLES:
        sql = ddl_for(t)
        assert sql.startswith("CREATE TABLE {}".format(t.fqn)), t.fqn
        for c in t.columns:
            assert "  {} {}".format(c.name, c.sql_type) in sql, (t.fqn, c.name)


def check_a_not_null_column_carries_not_null_and_a_nullable_one_does_not():
    sql = ddl_for(tables_by_fqn()["raw.patient"])
    assert "patient_id BIGINT NOT NULL" in sql
    assert "email VARCHAR NOT NULL" not in sql
    assert "  email VARCHAR," in sql


def check_the_emitted_ddl_file_holds_every_table_and_every_schema():
    path = _tmp_db() + ".sql"
    try:
        n = emit_ddl(path)
        assert n == len(TABLES)
        with open(path) as fh:
            text = fh.read()
        for t in TABLES:
            assert "CREATE TABLE {}".format(t.fqn) in text, t.fqn
        for s in {t.schema for t in TABLES}:
            assert "CREATE SCHEMA IF NOT EXISTS {};".format(s) in text, s
    finally:
        if os.path.exists(path):
            os.unlink(path)


def check_the_load_reads_its_counts_back_out_of_the_database():
    # The loader returns what a query says is there rather than the length of the list it
    # sent. A loader reporting what it intended to write is not a check, and this is the
    # fixture that separates the two: if it ever reverted to len(rows), the derived table
    # would have no entry at all because nothing sends it rows.
    path = _tmp_db()
    try:
        corpus = generate(n_patients=60, seed=808)
        counts = load(path, corpus)
        assert counts["raw.patient"] == 60
        assert counts["raw.encounter"] == len(corpus.encounters)
        assert counts["raw.claim"] == len(corpus.claims)
        assert counts["raw.device_reading"] == len(corpus.readings)
        assert counts["analytics.encounter_daily"] > 0
        assert set(counts) == {t.fqn for t in TABLES}
    finally:
        if os.path.exists(path):
            os.unlink(path)


def check_the_mart_never_holds_more_rows_than_the_encounters_it_groups():
    path = _tmp_db()
    try:
        corpus = generate(n_patients=60, seed=808)
        counts = load(path, corpus)
        assert counts["analytics.encounter_daily"] <= counts["raw.encounter"]
    finally:
        if os.path.exists(path):
            os.unlink(path)


def check_the_mart_carries_a_postal_code_that_came_out_of_the_patient_table():
    # The lineage work later has to show that masking the source masks this. If the join
    # ever stops carrying the column through, that demonstration has no subject.
    import duckdb

    path = _tmp_db()
    try:
        corpus = generate(n_patients=60, seed=808)
        load(path, corpus)
        con = duckdb.connect(path)
        try:
            leaked = con.execute("""
                SELECT count(*) FROM analytics.encounter_daily d
                WHERE d.postal_code IS NOT NULL
                  AND d.postal_code NOT IN (SELECT postal_code FROM raw.patient)
            """).fetchone()[0]
            assert leaked == 0, leaked
            carried = con.execute("""
                SELECT count(DISTINCT postal_code) FROM analytics.encounter_daily
            """).fetchone()[0]
            assert carried > 1, carried
        finally:
            con.close()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def check_loading_twice_into_one_file_leaves_the_same_counts():
    # The loader drops and recreates, so a second run has to be a no op on the counts. A
    # defect that only appears on the second run is invisible to a run that happens once.
    path = _tmp_db()
    try:
        corpus = generate(n_patients=40, seed=31)
        first = load(path, corpus)
        second = load(path, corpus)
        assert first == second, (first, second)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def check_every_declared_sql_type_is_one_duckdb_accepts():
    # The types are written by hand in pii/schema.py and nothing else would catch a typo
    # until the day the crawler ran.
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        for t in TABLES:
            for c in t.columns:
                con.execute("SELECT CAST(NULL AS {})".format(c.sql_type))
    finally:
        con.close()
