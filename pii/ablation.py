"""Remove one arm at a time and count the decisions that move.

The classifier has three arms. The question this module answers is which of them any
decision actually turns on, and the answer is not the same as which of them changes the
number printed beside a column. A band is what happens next. Accept means masked with no
person involved, review means queued, ignore means nothing happens at all. An arm that
shifts a confidence from 0.81 to 0.78 and leaves every band alone has changed no outcome
for anybody.

So the unit here is the band and not the score. `Move` records one column whose band
changed when an arm set was swapped in, and it carries the category and the confidence on
both sides, because a column that moved from review to ignore and one that moved from
review to accept are opposite failures and a count cannot tell them apart.

This used to live in `scripts/classify_probe.py` as a print loop that rebound
`classify.name_signals` to a stub for the length of one call. That measured the right thing
and it could not be checked. Nothing asserted the result, so the number lived in the README
where it drifted, and a rebind of another module's globals is visible to everything else
running in the process. The arm set is a parameter now. `classify.column_signals` takes it
and `pii/classify.py` threads it through both rounds, which also fixes a quieter problem:
the table level second pass reads the signals it was handed, so an arm stubbed out after
the fact would still have voted on whether the table names anybody.

What the measurement cannot say. It is one warehouse of forty two columns whose names I
wrote. An arm that decides nothing here is not an arm that decides nothing. The counter
case is in `scripts/classify_probe.py` under the obfuscated run, where the names are
replaced by position labels and recall halves. Read the two together or neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

from pii.classify import ALL_ARMS, Arm, Band, Classification, ColumnProfile, REVIEW_AT
from pii.classify import classify_warehouse


@dataclass(frozen=True)
class Move:
    """One column whose band changed, with both readings kept.

    Both sides are here because the direction is the finding. A column falling out of
    review into ignore has stopped reaching a person. A column rising into accept is being
    masked without one. Counting them together reports a number that hides which happened.
    """

    address: str
    from_band: Band
    from_category: str
    from_confidence: float
    to_band: Band
    to_category: str
    to_confidence: float

    def describe(self) -> str:
        return "{} {} {:.4f} {} -> {} {:.4f} {}".format(
            self.address, self.from_band.value, self.from_confidence, self.from_category,
            self.to_band.value, self.to_confidence, self.to_category)


@dataclass(frozen=True)
class Reading:
    """What one arm set did, against the baseline of every arm."""

    label: str
    arms: Tuple[Arm, ...]
    in_scope: int
    found: int
    masked: int
    false_alarms: int
    moves: Tuple[Move, ...]

    @property
    def bands_moved(self) -> int:
        return len(self.moves)

    @property
    def moved_addresses(self) -> Tuple[str, ...]:
        return tuple(m.address for m in self.moves)


# The arm sets the probe reports, in the order the README prints them. Each entry is a
# label and the arms that stay. `all three` is the baseline every other row is measured
# against, so it has to be first and it has to move nothing by construction.
ARM_SETS: Tuple[Tuple[str, Tuple[Arm, ...]], ...] = (
    ("all three", ALL_ARMS),
    ("name only", (Arm.NAME,)),
    ("value only", (Arm.VALUE,)),
    ("structure only", (Arm.STRUCTURE,)),
    ("without name", (Arm.VALUE, Arm.STRUCTURE)),
    ("without value", (Arm.NAME, Arm.STRUCTURE)),
    ("without structure", (Arm.NAME, Arm.VALUE)),
)

# The label of each single arm's removal, which is what `decides_nothing` reads.
REMOVAL_LABEL: Dict[Arm, str] = {
    Arm.NAME: "without name",
    Arm.VALUE: "without value",
    Arm.STRUCTURE: "without structure",
}


@dataclass(frozen=True)
class Report:
    readings: Tuple[Reading, ...]

    def by_label(self, label: str) -> Reading:
        for r in self.readings:
            if r.label == label:
                return r
        raise KeyError("no reading labelled {!r}".format(label))

    def decides_nothing(self) -> Tuple[Arm, ...]:
        """Arms whose removal moves no band at all.

        This is the only claim in here that licenses deleting an arm, and it is deliberately
        narrow. It reads the single removal row and not the `only` row. An arm can reproduce
        almost nothing on its own and still be the one thing holding a column up, because
        the other two never reach that column. `structure only` finding zero of twenty says
        the structure arm cannot work alone. `without structure` is the row that says whether
        anything needed it.
        """
        return tuple(
            arm for arm in ALL_ARMS
            if self.by_label(REMOVAL_LABEL[arm]).bands_moved == 0
        )

    def manifest(self) -> Dict[str, object]:
        """A shape stable enough to commit and diff.

        Written so a check can re-derive it and fail on drift. The moves are addresses and
        bands rather than Move objects, because the confidence is the part most likely to
        shift under an unrelated edit and a manifest that fails on a rounding change stops
        being read.
        """
        return {
            "arm_sets": [
                {
                    "label": r.label,
                    "arms": [a.value for a in r.arms],
                    "in_scope": r.in_scope,
                    "found": r.found,
                    "masked": r.masked,
                    "false_alarms": r.false_alarms,
                    "bands_moved": r.bands_moved,
                    "moves": [
                        {"address": m.address,
                         "from_band": m.from_band.value,
                         "to_band": m.to_band.value}
                        for m in r.moves
                    ],
                }
                for r in self.readings
            ],
            "decides_nothing": [a.value for a in self.decides_nothing()],
        }

    def table(self) -> str:
        """The README's table, generated rather than retyped.

        The old table was pasted into the README by hand and the paragraph under it said
        the structure arm moved no band while the row above said it moved one. Generating it
        does not stop somebody writing a wrong paragraph. It does stop the table and the
        measurement disagreeing.
        """
        head = "{:<22} {:>9} {:>7} {:>8} {:>13}".format(
            "arms", "found", "masked", "wrong", "bands moved")
        rows = [head]
        for r in self.readings:
            rows.append("{:<22} {:>9} {:>7} {:>8} {:>13}".format(
                r.label,
                "{}/{}".format(r.found, r.in_scope),
                r.masked,
                r.false_alarms,
                r.bands_moved))
        return "\n".join(rows)


def ablate(
    profiles: Sequence[ColumnProfile],
    upstream_tables: Optional[Dict[str, Sequence[str]]] = None,
    evidence_bar: float = REVIEW_AT,
    scope: Optional[FrozenSet[str]] = None,
    planted_key_for: Optional[Callable[[Classification], str]] = None,
    arm_sets: Sequence[Tuple[str, Tuple[Arm, ...]]] = ARM_SETS,
) -> Report:
    """Run every arm set over the same profiles and diff the bands against all three arms.

    `upstream_tables` is passed through unchanged, so the ablation runs the configuration
    the tool actually ships rather than the single round version. A section that classified
    directly would publish a figure from a tool nobody has.

    `scope` and `planted_key_for` are the answer keys and both are optional. Without them
    the found, masked and false alarm columns come back as zero and the moves are still
    correct, because a move is a comparison the classifier makes against itself and needs
    no key. That matters for the checks, which have no planted warehouse to read.
    """
    baseline_arms = arm_sets[0][1]
    baseline = {
        r.address: r for r in classify_warehouse(
            profiles, evidence_bar, upstream_tables, arms=baseline_arms)
    }

    readings: List[Reading] = []
    for label, arms in arm_sets:
        results = classify_warehouse(profiles, evidence_bar, upstream_tables, arms=arms)

        moves: List[Move] = []
        for r in results:
            before = baseline[r.address]
            if r.band is before.band:
                continue
            moves.append(Move(
                address=r.address,
                from_band=before.band,
                from_category=before.category_key,
                from_confidence=before.confidence,
                to_band=r.band,
                to_category=r.category_key,
                to_confidence=r.confidence,
            ))

        if scope is None or planted_key_for is None:
            in_scope, found, masked, wrong = 0, 0, 0, 0
        else:
            ins = [r for r in results if planted_key_for(r) in scope]
            in_scope = len(ins)
            found = sum(1 for r in ins if r.flagged)
            masked = sum(1 for r in ins if r.auto_masked)
            wrong = sum(1 for r in results
                        if r.flagged and planted_key_for(r) == "not_personal")

        readings.append(Reading(
            label=label,
            arms=tuple(arms),
            in_scope=in_scope,
            found=found,
            masked=masked,
            false_alarms=wrong,
            moves=tuple(sorted(moves, key=lambda m: m.address)),
        ))

    return Report(readings=tuple(readings))
