"""Grading the obvious classifier against the one answer key I did not write.

The comparison is between two things with different authors. `pii.naive` is mine. The
Safe Harbor clause list is not. A column is "in scope" here when the category planted on it
is reachable from a Safe Harbor clause, so scope is decided by the published list rather
than by how personal something feels to me.

The planted labels are still mine, so this is not a clean experiment and it is not sold as
one. What it does buy is that the set of things that count as identifiers was fixed by a
regulation before I opened the editor, and I cannot quietly shrink it when the naive
classifier does badly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from pii import naive, safeharbor
from pii.schema import PLANTED, PlantedColumn
from pii.taxonomy import TAXONOMY, Identifiability


@dataclass(frozen=True)
class ColumnVerdict:
    table: str
    column: str
    planted_key: str
    naive_key: str
    in_safe_harbor_scope: bool
    identifies_at_recorded_granularity: bool

    @property
    def address(self) -> str:
        return "{}.{}".format(self.table, self.column)

    @property
    def naive_calls_it_personal(self) -> bool:
        return self.naive_key != "not_personal"

    @property
    def missed(self) -> bool:
        """In scope under the published list and the naive scan says nothing."""
        return self.in_safe_harbor_scope and not self.naive_calls_it_personal

    @property
    def false_alarm(self) -> bool:
        """The naive scan flags a column the planted label says is not personal."""
        return self.naive_calls_it_personal and self.planted_key == "not_personal"

    @property
    def wrong_category(self) -> bool:
        """Flagged, and flagged as the wrong thing. A separate failure from missing it,
        because a masking policy keys off the category and not off the boolean."""
        return (
            self.naive_calls_it_personal
            and self.planted_key != "not_personal"
            and self.naive_key != self.planted_key
        )


@dataclass(frozen=True)
class Report:
    verdicts: Tuple[ColumnVerdict, ...]
    taxonomy_fingerprint: str

    @property
    def n_columns(self) -> int:
        return len(self.verdicts)

    @property
    def in_scope(self) -> Tuple[ColumnVerdict, ...]:
        return tuple(v for v in self.verdicts if v.in_safe_harbor_scope)

    @property
    def missed(self) -> Tuple[ColumnVerdict, ...]:
        return tuple(v for v in self.verdicts if v.missed)

    @property
    def false_alarms(self) -> Tuple[ColumnVerdict, ...]:
        return tuple(v for v in self.verdicts if v.false_alarm)

    @property
    def wrong_category(self) -> Tuple[ColumnVerdict, ...]:
        return tuple(v for v in self.verdicts if v.wrong_category)

    @property
    def recall(self) -> float:
        scope = self.in_scope
        if not scope:
            raise ValueError(
                "no column is in Safe Harbor scope, so recall has no denominator"
            )
        return sum(1 for v in scope if v.naive_calls_it_personal) / len(scope)

    def by_identifiability(self) -> Dict[str, Dict[str, int]]:
        """Recall split by what the column does on its own.

        This split is the whole argument for the second axis. If the naive scan does well
        on direct identifiers and badly on quasi identifiers then a single PII flag is
        hiding the failure, because both are Safe Harbor identifiers and only one of them
        looks like one.
        """
        out: Dict[str, Dict[str, int]] = {}
        for v in self.in_scope:
            level = TAXONOMY.get(v.planted_key).identifiability.value
            bucket = out.setdefault(level, {"in_scope": 0, "found": 0})
            bucket["in_scope"] += 1
            if v.naive_calls_it_personal:
                bucket["found"] += 1
        return out


def _scope_keys() -> frozenset:
    return frozenset(safeharbor.mapped_keys())


def grade(samples: Dict[str, Tuple[dict, ...]] = None) -> Report:
    """Run the naive scan over every planted column and grade it.

    `samples` maps a table fqn to rows, so the value regexes get something to read. Passing
    nothing is legal and it grades the name heuristic alone, which is a different and
    weaker arm rather than the same arm with less data. The probe runs both.
    """
    samples = samples or {}
    scope = _scope_keys()
    verdicts = []
    for p in PLANTED:
        rows = samples.get(p.table, ())
        sample = naive.sample_column(rows, p.column) if rows else ()
        verdicts.append(ColumnVerdict(
            table=p.table,
            column=p.column,
            planted_key=p.category_key,
            naive_key=naive.classify(p.column, sample),
            in_safe_harbor_scope=p.category_key in scope,
            identifies_at_recorded_granularity=p.identifies(),
        ))
    return Report(verdicts=tuple(verdicts),
                  taxonomy_fingerprint=TAXONOMY.fingerprint())


def quasi_only_report(report: Report) -> Tuple[ColumnVerdict, ...]:
    """In scope columns whose planted category is a quasi identifier."""
    return tuple(
        v for v in report.in_scope
        if TAXONOMY.get(v.planted_key).identifiability is Identifiability.QUASI
    )


def planted_summary() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for p in PLANTED:
        level = TAXONOMY.get(p.category_key).identifiability.value
        counts[level] = counts.get(level, 0) + 1
    return counts


def columns_planted_as(key: str) -> Tuple[PlantedColumn, ...]:
    return tuple(p for p in PLANTED if p.category_key == key)
