"""Warehouse metadata, and the sample schema with its planted labels.

Two things live here and they are deliberately different objects.

`Column` and `Table` are the metadata model. They hold what a crawler can see: a name, a
SQL type, nullability, whether the column is a key. Nothing in them knows anything about
personal data. The crawler that fills them in arrives later and it will fill in exactly
these fields.

`PlantedColumn` wraps a column with the answer this repo says is correct. That is a label I
wrote, so it can only ever grade something against my own judgement. It is useful for
building a corpus and for regression testing a classifier against a fixed target. It is not
evidence that the classifier is right about anything, and the naming keeps the two apart so
nobody mistakes one for the other six weeks from now.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from pii.taxonomy import TAXONOMY, Category, Granularity, Identifiability


@dataclass(frozen=True)
class Column:
    """What a metadata crawler can see about a column. No classification in here."""

    name: str
    sql_type: str
    nullable: bool = True
    is_key: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("column name must not be empty")
        if not self.sql_type:
            raise ValueError("column {} must carry a sql type".format(self.name))
        if self.is_key and self.nullable:
            raise ValueError(
                "column {} is a key and nullable, which no warehouse allows".format(
                    self.name
                )
            )


@dataclass(frozen=True)
class Table:
    schema: str
    name: str
    columns: Tuple[Column, ...]

    def __post_init__(self) -> None:
        if not self.schema or not self.name:
            raise ValueError("a table needs both a schema and a name")
        if not self.columns:
            raise ValueError("table {} has no columns".format(self.name))
        seen = set()
        for c in self.columns:
            if c.name in seen:
                raise ValueError(
                    "table {}.{} repeats column {}".format(self.schema, self.name, c.name)
                )
            seen.add(c.name)

    @property
    def fqn(self) -> str:
        return "{}.{}".format(self.schema, self.name)

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError("{} has no column {}".format(self.fqn, name))


@dataclass(frozen=True)
class PlantedColumn:
    """A column plus the label I say is correct, and the granularity it is recorded at.

    `observed` matters because several categories are only identifiers above a threshold.
    A column of birth dates stored to the day and a column of birth years are the same
    category and two different answers.
    """

    table: str
    column: str
    category_key: str
    observed: Granularity = Granularity.NOT_APPLICABLE
    why: str = ""

    def __post_init__(self) -> None:
        if self.category_key not in TAXONOMY:
            raise ValueError(
                "{}.{} planted as {} which is not in the taxonomy".format(
                    self.table, self.column, self.category_key
                )
            )
        cat = TAXONOMY.get(self.category_key)
        if cat.identifies_at is not None and self.observed is Granularity.NOT_APPLICABLE:
            raise ValueError(
                "{}.{} is category {} which turns on granularity, so a planted "
                "granularity is required".format(self.table, self.column, self.category_key)
            )

    @property
    def category(self) -> Category:
        return TAXONOMY.get(self.category_key)

    def identifies(self) -> bool:
        return self.category.identifies(self.observed)


_G = Granularity


# The sample warehouse. Four raw tables and one derived one, shaped like a small claims
# and encounters stack. The derived table is here so that column level lineage has
# somewhere to propagate to later, and because a masked source feeding an unmasked mart is
# the failure this project is about.
TABLES: Tuple[Table, ...] = (
    Table("raw", "patient", (
        Column("patient_id", "BIGINT", nullable=False, is_key=True),
        Column("mrn", "VARCHAR", nullable=False),
        Column("first_name", "VARCHAR"),
        Column("last_name", "VARCHAR"),
        Column("email", "VARCHAR"),
        Column("phone", "VARCHAR"),
        Column("street_address", "VARCHAR"),
        Column("city", "VARCHAR"),
        Column("postal_code", "VARCHAR"),
        Column("birth_date", "DATE"),
        Column("sex", "VARCHAR"),
        Column("ssn", "VARCHAR"),
        Column("created_at", "TIMESTAMP", nullable=False),
    )),
    Table("raw", "encounter", (
        Column("encounter_id", "BIGINT", nullable=False, is_key=True),
        Column("patient_id", "BIGINT", nullable=False),
        Column("admitted_at", "TIMESTAMP", nullable=False),
        Column("discharged_at", "TIMESTAMP"),
        Column("department", "VARCHAR"),
        Column("attending_npi", "VARCHAR"),
        Column("primary_diagnosis", "VARCHAR"),
        Column("clinical_note", "VARCHAR"),
        Column("disposition", "VARCHAR"),
    )),
    Table("raw", "claim", (
        Column("claim_id", "BIGINT", nullable=False, is_key=True),
        Column("encounter_id", "BIGINT", nullable=False),
        Column("member_number", "VARCHAR"),
        Column("payer_name", "VARCHAR"),
        Column("billed_amount", "DECIMAL(12,2)"),
        Column("paid_amount", "DECIMAL(12,2)"),
        Column("claim_status", "VARCHAR"),
        Column("submitted_on", "DATE"),
    )),
    Table("raw", "device_reading", (
        Column("reading_id", "BIGINT", nullable=False, is_key=True),
        Column("patient_id", "BIGINT", nullable=False),
        Column("device_serial", "VARCHAR"),
        Column("taken_at", "TIMESTAMP", nullable=False),
        Column("metric", "VARCHAR"),
        Column("reading_value", "DOUBLE"),
        Column("source_ip", "VARCHAR"),
    )),
    Table("analytics", "encounter_daily", (
        Column("day", "DATE", nullable=False),
        # Declared NOT NULL at first and it was wrong. The mart groups by
        # raw.encounter.department, which is nullable, so the moment the corpus wrote its
        # first null the load failed on a constraint. A derived column cannot be stricter
        # than the column it is derived from, and nothing could see that while the corpus
        # had no null in it anywhere. Moving this flag moved the published fingerprint,
        # which is recorded beside the pin in the suite.
        Column("department", "VARCHAR"),
        Column("postal_code", "VARCHAR"),
        Column("encounters", "BIGINT", nullable=False),
        Column("mean_length_of_stay_h", "DOUBLE"),
    )),
)


PLANTED: Tuple[PlantedColumn, ...] = (
    # raw.patient
    PlantedColumn("raw.patient", "patient_id", "not_personal",
                  why="A surrogate key with no meaning outside this warehouse."),
    PlantedColumn("raw.patient", "mrn", "medical_record_number"),
    PlantedColumn("raw.patient", "first_name", "person_name"),
    PlantedColumn("raw.patient", "last_name", "person_name"),
    PlantedColumn("raw.patient", "email", "email"),
    PlantedColumn("raw.patient", "phone", "phone"),
    PlantedColumn("raw.patient", "street_address", "street_address"),
    PlantedColumn("raw.patient", "city", "postal_code", observed=_G.POSTAL_3,
                  why="A geographic subdivision smaller than a state. At city level it "
                      "sits at the coarse end, which is why it is planted as POSTAL_3 "
                      "rather than as a separate category."),
    PlantedColumn("raw.patient", "postal_code", "postal_code", observed=_G.POSTAL_5),
    PlantedColumn("raw.patient", "birth_date", "birth_date", observed=_G.DAY),
    PlantedColumn("raw.patient", "sex", "sex"),
    PlantedColumn("raw.patient", "ssn", "national_id"),
    PlantedColumn("raw.patient", "created_at", "not_personal",
                  why="The row's load time. Not tied to anything the patient did."),

    # raw.encounter
    PlantedColumn("raw.encounter", "encounter_id", "not_personal"),
    PlantedColumn("raw.encounter", "patient_id", "not_personal"),
    PlantedColumn("raw.encounter", "admitted_at", "event_date", observed=_G.SECOND),
    PlantedColumn("raw.encounter", "discharged_at", "event_date", observed=_G.SECOND),
    PlantedColumn("raw.encounter", "department", "not_personal"),
    PlantedColumn("raw.encounter", "attending_npi", "licence_number",
                  why="Identifies the clinician rather than the patient. Still a person."),
    PlantedColumn("raw.encounter", "primary_diagnosis", "health_condition"),
    PlantedColumn("raw.encounter", "clinical_note", "free_text_clinical"),
    PlantedColumn("raw.encounter", "disposition", "not_personal"),

    # raw.claim
    PlantedColumn("raw.claim", "claim_id", "not_personal"),
    PlantedColumn("raw.claim", "encounter_id", "not_personal"),
    PlantedColumn("raw.claim", "member_number", "health_plan_id"),
    PlantedColumn("raw.claim", "payer_name", "not_personal",
                  why="The name of an insurance company, not of a person."),
    PlantedColumn("raw.claim", "billed_amount", "not_personal"),
    PlantedColumn("raw.claim", "paid_amount", "not_personal"),
    PlantedColumn("raw.claim", "claim_status", "not_personal"),
    PlantedColumn("raw.claim", "submitted_on", "event_date", observed=_G.DAY),

    # raw.device_reading
    PlantedColumn("raw.device_reading", "reading_id", "not_personal"),
    PlantedColumn("raw.device_reading", "patient_id", "not_personal"),
    PlantedColumn("raw.device_reading", "device_serial", "device_id"),
    PlantedColumn("raw.device_reading", "taken_at", "event_date", observed=_G.SECOND),
    PlantedColumn("raw.device_reading", "metric", "not_personal"),
    PlantedColumn("raw.device_reading", "reading_value", "not_personal"),
    PlantedColumn("raw.device_reading", "source_ip", "ip_address"),

    # analytics.encounter_daily
    PlantedColumn("analytics.encounter_daily", "day", "not_personal",
                  why="An aggregate grain, not a date tied to one individual."),
    PlantedColumn("analytics.encounter_daily", "department", "not_personal"),
    PlantedColumn("analytics.encounter_daily", "postal_code", "postal_code",
                  observed=_G.POSTAL_5,
                  why="Carried into the mart unaggregated. This is the column the lineage "
                      "work has to prove stays masked."),
    PlantedColumn("analytics.encounter_daily", "encounters", "not_personal"),
    PlantedColumn("analytics.encounter_daily", "mean_length_of_stay_h", "not_personal"),
)


def fingerprint() -> str:
    """Content hash over the sample schema, including the parts nothing reads yet.

    The nullability and key flags are declared here and consumed by almost nothing
    yet, so a mutation pass flipped ten of them and the whole suite stayed green. Writing
    an assertion per flag is transcription and it goes stale the moment a column moves.

    The schema is a published artefact instead, so it gets pinned the way an artefact
    gets pinned. One golden value, checked against a literal in the suite. The crawler
    that arrives next has to recover exactly this, which gives the hash a second job.
    """
    rows = []
    for t in sorted(TABLES, key=lambda x: x.fqn):
        for c in t.columns:
            rows.append({
                "table": t.fqn,
                "column": c.name,
                "sql_type": c.sql_type,
                "nullable": c.nullable,
                "is_key": c.is_key,
            })
    blob = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def tables_by_fqn() -> Dict[str, Table]:
    return {t.fqn: t for t in TABLES}


def planted_index() -> Dict[Tuple[str, str], PlantedColumn]:
    return {(p.table, p.column): p for p in PLANTED}


def all_columns() -> Tuple[Tuple[str, Column], ...]:
    out = []
    for t in TABLES:
        for c in t.columns:
            out.append((t.fqn, c))
    return tuple(out)


def planted_for(table_fqn: str, column: str) -> PlantedColumn:
    key = (table_fqn, column)
    idx = planted_index()
    if key not in idx:
        raise KeyError("no planted label for {}.{}".format(table_fqn, column))
    return idx[key]


def check_planting_is_total() -> Optional[str]:
    """Every column has exactly one planted label and every label names a real column.

    Returns a description of the first problem, or None. The suite asserts on it, and
    `scripts/plant.py` prints it, because a partial answer key is worse than none: the
    columns nobody got round to labelling are the ones the classifier will be worst on.
    """
    idx = planted_index()
    for fqn, col in all_columns():
        if (fqn, col.name) not in idx:
            return "column {}.{} has no planted label".format(fqn, col.name)
    known = {(fqn, c.name) for fqn, c in all_columns()}
    for key in idx:
        if key not in known:
            return "planted label {}.{} names no column".format(*key)
    return None


def quasi_identifier_columns() -> Tuple[PlantedColumn, ...]:
    """Planted quasi identifiers that really do identify at their recorded granularity."""
    return tuple(
        p for p in PLANTED
        if p.category.identifiability is Identifiability.QUASI and p.identifies()
    )
