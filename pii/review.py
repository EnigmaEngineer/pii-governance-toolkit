"""The review queue, and the record of what was decided in it.

The review band has existed since day 3 and until now it was a count. Nine columns scored
between the two thresholds and the tool said so and stopped. That is the shape most
classifiers ship: a confidence, a band, and an implicit assumption that somebody else
builds the part where a person acts on it.

What a reviewer needs is not the list. It is, per column, the evidence that produced the
score, where the column came from, and what stays broken until they answer. The third one
is the part that turns a list into a queue, because it is the only thing that says which
column to look at first.

Ordering is by what the decision unblocks. `residual_sql` refuses to measure a table
holding an undecided column, so one review can be the only thing standing between a table
and any residual risk number at all. That is a property of the warehouse rather than of
the column, and it is why confidence is the last tiebreak here rather than the first.
Sorting a review queue by confidence sorts it by how close the machine got, which is a fact
about the machine.

Decisions are recorded rather than applied and forgotten. A masked warehouse whose audit
trail cannot say who decided a column was personal, and when, is a warehouse that cannot
answer the question an audit exists to ask.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from pii.classify import ACCEPT_AT, Band, Classification
from pii.lineage import Graph
from pii.mask import Action, Policy, resolve_review
from pii.taxonomy import TAXONOMY, Identifiability

# Getting a direct identifier wrong is the worst of the three, because a missed direct
# identifier names somebody on its own and no other column has to cooperate. Sensitive
# ranks below quasi on purpose: `policy_for` retains a sensitive attribute either way, so
# the review changes the label and not the action.
#
# NONE is unreachable and kept anyway so the table is total over the enum. `not_personal`
# is the only category carrying NONE, and `Classification.band` forces it to IGNORE, so a
# NONE column cannot be in the review band and cannot reach this queue. A mutation pass
# moves that 3 and nothing fails, correctly, because nothing reads it.
_IDENTIFIABILITY_RANK = {
    Identifiability.DIRECT: 0,
    Identifiability.QUASI: 1,
    Identifiability.SENSITIVE: 2,
    Identifiability.NONE: 3,
}


@dataclass(frozen=True)
class QueueItem:
    """One column waiting on a person, with everything they need to answer.

    `came_from` is the lineage trace and it is not decoration. On this warehouse the only
    reason `analytics.encounter_daily.day` is in the queue is that it was cast out of
    `raw.encounter.admitted_at`, and a reviewer looking at a mart column called `day` with
    no trace would reasonably call it a reporting grain and be wrong.
    """

    classification: Classification
    policy: Policy
    came_from: Tuple[str, ...] = ()
    carried_into: Tuple[str, ...] = ()
    tables_blocked: Tuple[str, ...] = ()
    sole_blocker_of: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.policy.action is not Action.REVIEW:
            raise ValueError(
                "{} is a {} and not a review, so nobody is waiting on it".format(
                    self.policy.address, self.policy.action.value))
        if self.classification.address != self.policy.address:
            raise ValueError(
                "queue item pairs {} with a policy for {}".format(
                    self.classification.address, self.policy.address))

    @property
    def address(self) -> str:
        return self.policy.address

    @property
    def candidate_key(self) -> str:
        return self.classification.category_key

    @property
    def distance_to_accept(self) -> float:
        return round(ACCEPT_AT - self.classification.confidence, 4)

    @property
    def sort_key(self) -> Tuple[int, int, int, float, str]:
        """Tables this one decision would free first, then breadth, then severity.

        The first term went in after the first version of this queue was read back. Sorting
        on `tables_blocked` alone put every item level with every other, because a review
        blocks its own table by definition and eight of the ten only ever blocked that one.
        A key that cannot separate the rows it is sorting is not an ordering.

        `sole_blocker_of` separates them properly. A table waiting on three reviews is
        freed by none of them individually, and a table waiting on one is freed the moment
        somebody answers it. On this warehouse `raw.device_reading.taken_at` is the only
        item in the queue that frees a table by itself, and nothing about its confidence or
        its category says so.

        The last term is arguable and the argument against it is worth writing down. Taking
        the near misses first clears the most queue per hour of reviewer time. Taking the
        middle of the band first gives the person the columns the machine was least sure
        about, which is where a reviewer adds the most. I chose throughput because the
        queue blocks a measurement, and a column sitting at 0.4 with nothing waiting on it
        costs nothing to leave until Friday.
        """
        return (
            -len(self.sole_blocker_of),
            -len(self.tables_blocked),
            _IDENTIFIABILITY_RANK[TAXONOMY.get(self.candidate_key).identifiability],
            self.distance_to_accept,
            self.address,
        )

    def brief(self) -> str:
        """What the reviewer reads. The question first, then the evidence for it."""
        lines = [
            "{}  candidate {} at {:.4f}".format(
                self.address, self.candidate_key, self.classification.confidence),
            "  question   is this column personal data",
            "  short of   the accept threshold by {:.4f}".format(
                self.distance_to_accept),
        ]
        if self.classification.runner_up:
            lines.append("  runner up  {} at {:.4f}".format(
                self.classification.runner_up,
                self.classification.runner_up_score))
        for s in self.classification.signals:
            lines.append("  evidence   {:<9} {:<22} {:.4f}  {}".format(
                s.arm.value, s.category_key, s.weight, s.detail))
        if self.came_from:
            lines.append("  came from  " + ", ".join(self.came_from))
        if self.carried_into:
            lines.append("  carried into " + ", ".join(self.carried_into))
        if self.sole_blocker_of:
            lines.append("  frees      " + ", ".join(self.sole_blocker_of)
                         + " on its own")
        shared = tuple(t for t in self.tables_blocked if t not in self.sole_blocker_of)
        if shared:
            lines.append("  waits with others on " + ", ".join(shared))
        if not self.tables_blocked:
            lines.append("  blocks     nothing measurable")
        return "\n".join(lines)


@dataclass(frozen=True)
class Decision:
    """A person's answer, with their name and the date on it.

    `note` is required and the constructor refuses an empty one. A decision with no reason
    recorded is the thing an auditor asks about and nobody can answer six months later, and
    making it optional means it is empty on the rows that matter.
    """

    address: str
    personal: bool
    reviewer: str
    decided_at: dt.date
    note: str

    def __post_init__(self) -> None:
        if not self.reviewer:
            raise ValueError(
                "{} was decided by nobody. An unattributed decision is not a review "
                "trail".format(self.address))
        if not self.note.strip():
            raise ValueError(
                "{} was decided by {} with no reason recorded".format(
                    self.address, self.reviewer))


def _blocked_tables(address: str, all_tables: Sequence[str],
                    carriers: Sequence[str]) -> Tuple[str, ...]:
    """Tables whose residual risk cannot be measured while this column is undecided.

    Its own table, because `residual_sql` refuses on an undecided column in the set. Plus
    any table holding a column that carries this value, because the policy that lands
    there is inherited from the decision made here.
    """
    own = address.rsplit(".", 1)[0]
    downstream = {c.rsplit(".", 1)[0] for c in carriers}
    blocked = {own} | downstream
    return tuple(sorted(t for t in blocked if t in set(all_tables)))


def build(classifications: Sequence[Classification],
          policies: Sequence[Policy],
          graph: Optional[Graph] = None) -> Tuple[QueueItem, ...]:
    """The queue, ordered. Everything in the review band and nothing else.

    Built from the policies rather than from the bands, because `policy_for` is what
    decides that a review band column needs a person, and reading the band again here
    would be a second implementation of that rule waiting to disagree with the first.
    """
    from pii.access import carriers_of

    by_address = {c.address: c for c in classifications}
    all_tables = sorted({c.table for c in classifications})

    # First pass builds the items. `sole_blocker_of` needs the whole queue in view, so it
    # is filled in on a second pass rather than guessed at here.
    partial = []
    for p in policies:
        if p.action is not Action.REVIEW:
            continue
        result = by_address.get(p.address)
        if result is None:
            raise ValueError(
                "{} has a policy and no classification behind it".format(p.address))
        carriers = carriers_of(graph, p.address) if graph is not None else ()
        partial.append(QueueItem(
            classification=result,
            policy=p,
            came_from=graph.sources_of(p.address) if graph is not None else (),
            carried_into=carriers,
            tables_blocked=_blocked_tables(p.address, all_tables, carriers),
        ))

    waiting: Dict[str, int] = {}
    for item in partial:
        for table in item.tables_blocked:
            waiting[table] = waiting.get(table, 0) + 1

    items = [
        QueueItem(
            classification=i.classification,
            policy=i.policy,
            came_from=i.came_from,
            carried_into=i.carried_into,
            tables_blocked=i.tables_blocked,
            sole_blocker_of=tuple(
                t for t in i.tables_blocked if waiting[t] == 1),
        )
        for i in partial
    ]
    return tuple(sorted(items, key=lambda i: i.sort_key))


def apply(queue: Sequence[QueueItem],
          decisions: Sequence[Decision]) -> Tuple[Tuple[Policy, ...], Tuple[str, ...]]:
    """Fold decisions into policies. Returns the resolved policies and what is still open.

    Refuses a decision for an address not in the queue. A reviewer answering a question
    nobody asked is either reading a stale queue or looking at the wrong warehouse, and
    silently accepting it would put a masking policy on a column the classifier never
    flagged.

    Refuses two decisions for one address as well, rather than taking the later one. Which
    of two conflicting reviews wins is a policy question about an organisation and not
    something this function gets to assume.
    """
    open_addresses = {i.address: i for i in queue}
    seen = set()
    for d in decisions:
        if d.address not in open_addresses:
            raise ValueError(
                "{} was decided by {} and is not in the queue".format(
                    d.address, d.reviewer))
        if d.address in seen:
            raise ValueError(
                "{} has two decisions against it. Pick one before applying".format(
                    d.address))
        seen.add(d.address)

    resolved = tuple(
        resolve_review(open_addresses[d.address].policy, d.personal)
        for d in decisions
    )
    still_open = tuple(sorted(a for a in open_addresses if a not in seen))
    return resolved, still_open


def unblocks(queue: Sequence[QueueItem],
             decisions: Sequence[Decision]) -> Tuple[str, ...]:
    """Tables that become measurable once these decisions land and no others.

    A table is unblocked when every queue item blocking it has been decided. Reporting a
    table as unblocked while one review against it is still open would promise a residual
    number that `residual_sql` will refuse to produce.
    """
    decided = {d.address for d in decisions}
    blocked_by: Dict[str, set] = {}
    for item in queue:
        for table in item.tables_blocked:
            blocked_by.setdefault(table, set()).add(item.address)
    return tuple(sorted(
        table for table, waiting in blocked_by.items()
        if waiting and waiting <= decided
    ))


def worked_share(queue: Sequence[QueueItem],
                 decisions: Sequence[Decision]) -> Tuple[int, int]:
    """Decided and total. Two integers, for the same reason `star_share` returns two."""
    decided = {d.address for d in decisions}
    return sum(1 for i in queue if i.address in decided), len(queue)


def trail(decisions: Sequence[Decision]) -> Tuple[str, ...]:
    """The audit trail, one line per decision, oldest first.

    Sorted by date and then address so the output is stable, because a trail that reorders
    itself between runs cannot be diffed against the previous one.
    """
    return tuple(
        "{}  {:<44} {:<12} {:<10} {}".format(
            d.decided_at.isoformat(), d.address,
            "personal" if d.personal else "not personal",
            d.reviewer, d.note)
        for d in sorted(decisions, key=lambda d: (d.decided_at, d.address))
    )


def frees_a_table(queue: Sequence[QueueItem]) -> Tuple[QueueItem, ...]:
    """Items that free a table by themselves. The short list, for a reviewer with an hour.

    This replaced a version filtering on `tables_blocked`, which returned the whole queue,
    because an undecided column always blocks the table it lives in. A short list that is
    the long list is worse than no short list, since it reads as though somebody checked.
    """
    return tuple(i for i in queue if i.sole_blocker_of)
