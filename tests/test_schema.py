from __future__ import annotations

from pii import schema
from pii.schema import Column, PlantedColumn, Table
from pii.taxonomy import TAXONOMY, Granularity, Identifiability


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def check_every_column_has_exactly_one_planted_label():
    assert schema.check_planting_is_total() is None, schema.check_planting_is_total()


def check_the_totality_check_can_fail_in_both_directions():
    # The check exists to catch a partial answer key, so both halves need a control.
    idx = schema.planted_index()
    known = {(fqn, c.name) for fqn, c in schema.all_columns()}
    assert set(idx) == known
    assert len(idx) == len(schema.PLANTED), "a planted label was silently overwritten"


def check_a_column_that_is_a_key_and_nullable_is_refused():
    assert _raises(lambda: Column("a", "BIGINT", nullable=True, is_key=True), ValueError)
    Column("a", "BIGINT", nullable=False, is_key=True)


def check_a_column_with_no_name_or_no_type_is_refused():
    assert _raises(lambda: Column("", "BIGINT"), ValueError)
    assert _raises(lambda: Column("a", ""), ValueError)


def check_a_table_repeating_a_column_is_refused():
    cols = (Column("a", "BIGINT"), Column("a", "VARCHAR"))
    assert _raises(lambda: Table("s", "t", cols), ValueError)


def check_a_table_with_no_columns_or_no_name_is_refused():
    assert _raises(lambda: Table("s", "t", ()), ValueError)
    assert _raises(lambda: Table("", "t", (Column("a", "BIGINT"),)), ValueError)
    assert _raises(lambda: Table("s", "", (Column("a", "BIGINT"),)), ValueError)


def check_table_column_lookup_raises_on_a_name_it_does_not_have():
    t = schema.tables_by_fqn()["raw.patient"]
    assert t.column("email").sql_type == "VARCHAR"
    assert _raises(lambda: t.column("nope"), KeyError)


def check_a_planted_label_naming_an_unknown_category_is_refused():
    assert _raises(lambda: PlantedColumn("raw.patient", "x", "not_a_category"), ValueError)


def check_a_granularity_sensitive_category_planted_without_a_granularity_is_refused():
    # birth_date turns on granularity, so leaving the observed value at NOT_APPLICABLE
    # would make identifies() answer a question nobody asked.
    assert _raises(lambda: PlantedColumn("t", "c", "birth_date"), ValueError)
    PlantedColumn("t", "c", "birth_date", observed=Granularity.DAY)
    assert _raises(lambda: PlantedColumn("t", "c", "postal_code"), ValueError)
    PlantedColumn("t", "c", "postal_code", observed=Granularity.POSTAL_5)


def check_a_category_with_no_threshold_does_not_demand_a_granularity():
    p = PlantedColumn("t", "c", "email")
    assert p.observed is Granularity.NOT_APPLICABLE
    assert p.identifies() is True


def check_a_coarse_planting_really_stops_identifying():
    coarse = PlantedColumn("t", "c", "postal_code", observed=Granularity.POSTAL_3)
    fine = PlantedColumn("t", "c", "postal_code", observed=Granularity.POSTAL_5)
    assert coarse.identifies() is False
    assert fine.identifies() is True


def check_the_city_column_is_planted_at_the_coarse_end_and_does_not_identify():
    p = schema.planted_for("raw.patient", "city")
    assert p.category_key == "postal_code"
    assert p.observed is Granularity.POSTAL_3
    assert p.identifies() is False
    assert p.why, "a planting that surprises a reader has to say why"


def check_the_mart_carries_an_unaggregated_postal_code():
    # The lineage work has to prove a masked source stays masked downstream, and it needs
    # a column in a mart that really did come from a masked source. If this stops being
    # true it has nothing to demonstrate on.
    p = schema.planted_for("analytics.encounter_daily", "postal_code")
    assert p.identifies() is True
    assert p.category.identifiability is Identifiability.QUASI


def check_planted_for_raises_on_a_column_nobody_planted():
    assert _raises(lambda: schema.planted_for("raw.patient", "nope"), KeyError)
    assert _raises(lambda: schema.planted_for("no.table", "email"), KeyError)


def check_the_quasi_identifier_list_only_holds_quasi_columns_that_identify():
    qs = schema.quasi_identifier_columns()
    assert qs, "no quasi identifier identifies at its recorded granularity"
    for p in qs:
        assert p.category.identifiability is Identifiability.QUASI, p.column
        assert p.identifies() is True, p.column
    # And the coarse city column is excluded, which is the case that separates this from
    # a plain filter on identifiability.
    assert ("raw.patient", "city") not in {(p.table, p.column) for p in qs}


def check_every_planted_table_is_a_real_table():
    fqns = set(schema.tables_by_fqn())
    for p in schema.PLANTED:
        assert p.table in fqns, p.table


def check_the_schema_has_both_a_raw_and_a_derived_layer():
    schemas = {t.schema for t in schema.TABLES}
    assert "raw" in schemas
    assert "analytics" in schemas


def check_every_planted_category_exists_and_the_direct_ones_really_are_direct():
    for p in schema.PLANTED:
        assert p.category_key in TAXONOMY, p.category_key
    names = schema.planted_for("raw.patient", "first_name")
    assert names.category.identifiability is Identifiability.DIRECT


def check_a_planted_column_is_frozen():
    p = schema.planted_for("raw.patient", "email")
    try:
        p.category_key = "phone"
    except Exception:
        return
    raise AssertionError("PlantedColumn is not frozen")


def check_a_column_and_a_table_are_frozen_too():
    cases = [
        (Column("a", "BIGINT"), "name", "b"),
        (Table("s", "t", (Column("a", "BIGINT"),)), "name", "u"),
    ]
    for obj, attr, value in cases:
        try:
            setattr(obj, attr, value)
        except Exception:
            continue
        raise AssertionError("{} is not frozen".format(type(obj).__name__))


def check_a_column_declared_with_no_keywords_is_nullable_and_not_a_key():
    # The production default, which every other check here names explicitly and therefore
    # never exercises. A mutant moving `nullable: bool = True` to False survived the whole
    # suite for exactly that reason.
    c = Column("a", "BIGINT")
    assert c.nullable is True
    assert c.is_key is False


def check_every_table_declares_exactly_one_key_column():
    for t in schema.TABLES:
        keys = [c.name for c in t.columns if c.is_key]
        if t.schema == "analytics":
            # The mart is keyed by its grain rather than by a surrogate, so it declares
            # no key column. Stated rather than left as an exception nobody notices.
            assert keys == [], (t.fqn, keys)
            continue
        assert len(keys) == 1, (t.fqn, keys)
        assert t.column(keys[0]).nullable is False, t.fqn


def check_the_committed_ddl_matches_what_the_emitter_produces_today():
    # The DDL file is generated and committed, so it can drift the moment somebody edits
    # the schema module and does not re-run the emitter. Regenerating into a scratch path
    # and comparing is the whole check. It needs no database.
    import os
    import tempfile

    from scripts.plant import emit_ddl

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    committed = os.path.join(root, "schemas", "warehouse.sql")
    assert os.path.exists(committed), "schemas/warehouse.sql is missing"

    fd, tmp = tempfile.mkstemp(suffix=".sql")
    os.close(fd)
    try:
        emit_ddl(tmp)
        with open(tmp) as fh:
            fresh = fh.read()
        with open(committed) as fh:
            on_disk = fh.read()
        assert fresh == on_disk, "schemas/warehouse.sql is stale, re-run scripts/plant.py"
    finally:
        os.unlink(tmp)


def check_the_schema_fingerprint_is_the_published_one():
    # A golden value on purpose. Changing the sample schema is supposed to be a deliberate
    # act, and this is the speed bump. Every nullability and key flag is inside the hash,
    # which is what makes the ten of them that nothing else reads impossible to move by
    # accident.
    #
    # It moved once, on 2026-09-18, from dcff0aa3e7a5 to this value. The speed bump did
    # its job: analytics.encounter_daily.department was declared NOT NULL and is grouped
    # out of a nullable source column, so the first null the corpus ever wrote failed the
    # load on a constraint. A derived column cannot be stricter than the column it comes
    # from. The old value is recorded here rather than overwritten, because a golden value
    # quietly replaced is a golden value that has stopped being one.
    assert schema.fingerprint() == "1501a19ca3d8", schema.fingerprint()


def check_the_schema_fingerprint_moves_on_every_field_it_covers():
    # A golden value is worth nothing without the control that says the hash is reading
    # what the docstring claims. Each variant changes one field of one column.
    import json as _json
    import hashlib as _hashlib

    def hash_rows(rows):
        blob = _json.dumps(rows, sort_keys=True, separators=(",", ":"))
        return _hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    base = [{"table": "s.t", "column": "a", "sql_type": "BIGINT",
             "nullable": False, "is_key": True}]
    original = hash_rows(base)
    for field, value in (("table", "s.u"), ("column", "b"), ("sql_type", "VARCHAR"),
                         ("nullable", True), ("is_key", False)):
        variant = [dict(base[0], **{field: value})]
        assert hash_rows(variant) != original, field


def check_the_fingerprint_covers_every_column_in_every_table():
    # If it silently walked one table the golden value above would still be stable and
    # would be guarding a fraction of the schema.
    rows = []
    for t in schema.TABLES:
        rows.extend(t.columns)
    assert len(rows) == len(schema.all_columns()) == len(schema.PLANTED)


def check_every_declared_not_null_column_really_holds_no_null_in_the_sample_data():
    # Nullability is a claim about the data, so it gets checked against the data rather
    # than transcribed into an assertion. The reverse reading is in the README limitations
    # and it is worse: the generator never writes a null anywhere, so every column
    # declared nullable carries a declaration nothing has ever exercised.
    from pii.corpus import generate

    c = generate(n_patients=40, seed=7)
    rows_for = {
        "raw.patient": c.patients,
        "raw.encounter": c.encounters,
        "raw.claim": c.claims,
        "raw.device_reading": c.readings,
    }
    checked = 0
    for t in schema.TABLES:
        rows = rows_for.get(t.fqn)
        if rows is None:
            continue
        for col in t.columns:
            if col.nullable:
                continue
            for r in rows:
                assert r[col.name] is not None, (t.fqn, col.name)
            checked += 1
    assert checked > 0, "no NOT NULL column was reached, so this checked nothing"
