"""Who could have read this column, and did they.

Two questions with two different sources, and the gap between them is the report.

"Could have read" comes from grants. It is a statement about permission and it is true
whether or not anybody used it. "Did read" comes from query history. It is a statement
about what happened. An auditor asks the first and a breach notification turns on the
second, so a tool that answers one and calls it the other is worse than useless.

The part most access reviews get wrong is that a grant on a table is not the boundary of
what that grant exposes. `analytics.encounter_daily.postal_code` is a grouping key copied
out of `raw.patient.postal_code`, so a role holding SELECT on the mart and nothing else can
read a patient's postal code. A grant review that lists roles per table says that role has
no access to `raw.patient`, which is true, and reading it as "cannot see patient postal
codes" is wrong. So the reachable set here is closed over value preserving lineage rather
than stopping at the table named in the grant.

The closure is value preserving only, and that restriction is the whole correctness of the
module. `analytics.encounter_daily.encounters` is a row count derived from every column in
`raw.encounter`. Closing over aggregate edges would report that reading a count exposes
every patient identifier that was counted, which is false and would drown the real finding
in noise. A count is derived from values it does not carry.

What this cannot do is see a read that left no row in the history. A query log is a record
somebody else's system kept, and the report is bounded by it. Worse, `SELECT *` records no
column at all, so column level "did read" is a floor rather than a count until the star is
expanded against a catalog, and expanding it against today's catalog attributes columns to
a query that may have run before those columns existed. Both readings are printed. Neither
is presented as the number.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Sequence, Set, Tuple

from pii.lineage import VALUE_PRESERVING, EdgeKind, Graph


class Privilege(Enum):
    """Only the ones that read. A grant that cannot select cannot expose a value."""

    SELECT = "select"
    ALL = "all"


READS = (Privilege.SELECT, Privilege.ALL)


@dataclass(frozen=True)
class Grant:
    """One role, one table, one privilege.

    Table level rather than column level, because that is what the warehouses this is
    modelled on actually hold for SELECT. Column level control is the masking policy in
    `pii/mask.py` and it is a different mechanism with a different failure mode.
    """

    role: str
    table: str
    privilege: Privilege = Privilege.SELECT

    @property
    def reads(self) -> bool:
        return self.privilege in READS


@dataclass(frozen=True)
class RoleMember:
    user: str
    role: str


@dataclass(frozen=True)
class QueryEvent:
    """One query that ran, as a query log records it.

    `columns` is what the statement named. `select_star` is true when it named none of
    them because it asked for all of them, which is the case that makes column level
    attribution a floor. `tables` is recorded separately rather than derived from
    `columns`, because a star query has tables and no columns.
    """

    query_id: str
    user: str
    at: dt.date
    tables: Tuple[str, ...]
    columns: Tuple[str, ...] = ()
    select_star: bool = False

    def __post_init__(self) -> None:
        if not self.tables:
            raise ValueError(
                "{} names no table, so there is nothing it could have read".format(
                    self.query_id))
        if self.select_star and self.columns:
            raise ValueError(
                "{} is marked a star query and also names columns. One of the two is "
                "wrong and guessing which would put a made up column in an audit "
                "report".format(self.query_id))


def users_by_role(members: Sequence[RoleMember]) -> Dict[str, Tuple[str, ...]]:
    out: Dict[str, Set[str]] = {}
    for m in members:
        out.setdefault(m.role, set()).add(m.user)
    return {r: tuple(sorted(us)) for r, us in out.items()}


def readable_tables(role: str, grants: Sequence[Grant]) -> Tuple[str, ...]:
    return tuple(sorted({g.table for g in grants if g.role == role and g.reads}))


# What counts as carrying a value out of one column into another, for the purpose of
# asking who could read it. Wider than `VALUE_PRESERVING` by exactly one kind, and the
# difference is the point.
#
# `TRANSFORM` is any call this reader could not classify further. `upper(name)` is a
# transform and it plainly still names the person. `sha256(name)` is also a transform and
# it does not. The reader cannot tell them apart, so one of the two errors has to be
# chosen, and for an exposure report the safe error is to over report. Saying a column
# might have been reachable and being wrong costs a reviewer some time. Saying it was not
# reachable and being wrong is the sentence that goes in a breach notification.
#
# This is the same argument `table_is_person_linked` makes about its evidence bar, and it
# runs the opposite way to `VALUE_PRESERVING`, which exists to decide whether a *label*
# propagates. Inheriting a granularity through a transform would be a claim about the
# value's shape. Reaching a reader through one is a claim about who saw bytes.
CARRIES_A_VALUE = VALUE_PRESERVING + (EdgeKind.TRANSFORM,)


def carriers_of(graph: Graph, address: str,
                kinds: Sequence[EdgeKind] = CARRIES_A_VALUE) -> Tuple[str, ...]:
    """Downstream columns holding this column's value, walked as far as it travels.

    A carrier is a column somebody could read instead of this one and learn the same
    thing. The walk stops at the first edge not in `kinds`, so an aggregate ends it: a
    count is derived from values it does not carry.

    The default was `VALUE_PRESERVING` and that was wrong in the unsafe direction. It
    made every reachable set a floor while the report called it the answer. There is no
    transform edge on this warehouse, so no published figure moved when this changed,
    which means the checks are the only evidence that it is fixed.
    """
    found: list = []
    frontier = [address]
    seen = {address}
    while frontier:
        current = frontier.pop()
        for e in graph.out_of(current):
            if e.kind is EdgeKind.JOIN_KEY or e.kind not in kinds:
                continue
            nxt = e.target.address
            if nxt in seen:
                continue
            seen.add(nxt)
            found.append(nxt)
            frontier.append(nxt)
    return tuple(sorted(found))


def _tables_of(addresses: Sequence[str]) -> Tuple[str, ...]:
    """The table part of each address. Column names carry no dots, so rsplit is safe."""
    return tuple(sorted({a.rsplit(".", 1)[0] for a in addresses}))


@dataclass(frozen=True)
class Exposure:
    """One column over one date range. Permission and use, kept apart.

    `direct` and `through_a_carrier` are both sets of users who could have read the value.
    They are reported separately because the second set is the one a grant review does not
    show, and collapsing them into a single count hides exactly the thing worth finding.
    """

    address: str
    start: dt.date
    end: dt.date
    direct: Tuple[str, ...]
    through_a_carrier: Tuple[str, ...]
    carriers: Tuple[str, ...]
    named_the_column: Tuple[str, ...]
    named_a_carrier: Tuple[str, ...]
    star_over_a_holder: Tuple[str, ...]

    @property
    def could_have_read(self) -> Tuple[str, ...]:
        return tuple(sorted(set(self.direct) | set(self.through_a_carrier)))

    @property
    def did_read(self) -> Tuple[str, ...]:
        """Users a query log row names against this column or one carrying its value.

        Star queries are deliberately not in here. They belong to `did_read_at_most`,
        because a star query is evidence that a column may have been returned and not
        evidence that anybody read it.
        """
        return tuple(sorted(set(self.named_the_column) | set(self.named_a_carrier)))

    @property
    def did_read_at_most(self) -> Tuple[str, ...]:
        return tuple(sorted(set(self.did_read) | set(self.star_over_a_holder)))

    @property
    def only_by_star(self) -> Tuple[str, ...]:
        """Users whose only evidence is a star query. The width of the uncertainty."""
        return tuple(sorted(set(self.star_over_a_holder) - set(self.did_read)))

    @property
    def read_without_a_direct_grant(self) -> Tuple[str, ...]:
        """Read the value and were never granted the table it lives in.

        The finding. Every user here passes an access review on this table and has read
        the column anyway, through something downstream that was granted instead.
        """
        return tuple(sorted(set(self.did_read) - set(self.direct)))

    @property
    def granted_and_never_used(self) -> Tuple[str, ...]:
        """Could have read it over this range and did not. What a revocation would cost."""
        return tuple(sorted(set(self.could_have_read) - set(self.did_read_at_most)))


def exposure(address: str,
             grants: Sequence[Grant],
             members: Sequence[RoleMember],
             events: Sequence[QueryEvent],
             graph: Optional[Graph] = None,
             start: Optional[dt.date] = None,
             end: Optional[dt.date] = None) -> Exposure:
    """The access report for one column over a date range, inclusive at both ends.

    `graph` is optional and leaving it out is a real choice rather than a degraded one. It
    answers the narrow question, which is the one a table level grant review answers, and
    the point of passing the graph is that the two answers differ.
    """
    if start is not None and end is not None and start > end:
        raise ValueError(
            "range starts at {} and ends at {}, which is no range at all".format(
                start, end))

    table = address.rsplit(".", 1)[0]
    by_role = users_by_role(members)
    carriers = carriers_of(graph, address) if graph is not None else ()
    carrier_tables = _tables_of(carriers)

    direct: Set[str] = set()
    derived: Set[str] = set()
    for g in grants:
        if not g.reads:
            continue
        who = by_role.get(g.role, ())
        if g.table == table:
            direct.update(who)
        elif g.table in carrier_tables:
            derived.update(who)

    named: Set[str] = set()
    via: Set[str] = set()
    starred: Set[str] = set()
    holders = (table,) + carrier_tables
    for e in events:
        if start is not None and e.at < start:
            continue
        if end is not None and e.at > end:
            continue
        if address in e.columns:
            named.add(e.user)
        elif any(c in e.columns for c in carriers):
            via.add(e.user)
        elif e.select_star and any(t in e.tables for t in holders):
            starred.add(e.user)

    return Exposure(
        address=address,
        start=start or min((e.at for e in events), default=dt.date.min),
        end=end or max((e.at for e in events), default=dt.date.max),
        direct=tuple(sorted(direct)),
        # A user holding both grants is a direct reader. Reporting them in both sets would
        # double count the finding, and the finding is about users who hold only the second.
        through_a_carrier=tuple(sorted(derived - direct)),
        carriers=carriers,
        named_the_column=tuple(sorted(named)),
        named_a_carrier=tuple(sorted(via)),
        star_over_a_holder=tuple(sorted(starred)),
    )


def report(addresses: Sequence[str],
           grants: Sequence[Grant],
           members: Sequence[RoleMember],
           events: Sequence[QueryEvent],
           graph: Optional[Graph] = None,
           start: Optional[dt.date] = None,
           end: Optional[dt.date] = None) -> Tuple[Exposure, ...]:
    return tuple(
        exposure(a, grants, members, events, graph, start, end)
        for a in addresses
    )


def star_share(events: Sequence[QueryEvent],
               start: Optional[dt.date] = None,
               end: Optional[dt.date] = None) -> Tuple[int, int]:
    """Star queries and total queries in range. The size of what column attribution misses.

    Returned as two integers rather than a ratio, because a ratio over three queries reads
    like a ratio over three thousand and the denominator is the part worth seeing.
    """
    in_range = [
        e for e in events
        if (start is None or e.at >= start) and (end is None or e.at <= end)
    ]
    return sum(1 for e in in_range if e.select_star), len(in_range)


def unanswerable(exposures: Sequence[Exposure]) -> Tuple[str, ...]:
    """Columns where the log gives a floor of zero and a ceiling above it.

    The report cannot say whether anybody read these. It can say nobody named them and
    that somebody ran a star over a table holding them, and those two facts do not
    combine into an answer. This is a count rather than a sentence in a README because I
    wrote the sentence first with the wrong number in it, having read the table instead of
    counting it.
    """
    return tuple(sorted(
        e.address for e in exposures
        if not e.did_read and e.did_read_at_most
    ))


def unused_grants(exposures: Sequence[Exposure]) -> Tuple[str, ...]:
    """Role members who could read every column here and read none of them.

    A least privilege review wants this list. It is computed as an intersection rather
    than a union, so a user appears only if no column in the report was touched by them.
    """
    if not exposures:
        return ()
    could: Set[str] = set()
    for e in exposures:
        could.update(e.could_have_read)
    used: Set[str] = set()
    for e in exposures:
        used.update(e.did_read_at_most)
    return tuple(sorted(could - used))


def widening(exposures: Sequence[Exposure]) -> Tuple[str, ...]:
    """Columns whose reachable set grows once lineage is followed, and by how much.

    This is the headline the module exists for. A table level grant review misses every
    user counted here.
    """
    out = []
    for e in exposures:
        # No fallback for an empty carrier list, because the guard above makes it
        # unreachable: `through_a_carrier` is only ever populated from a table that came
        # out of `carriers`. It carried an `or "nothing"` until a mutation pass flipped the
        # operator and nothing failed, which is how an unreachable branch announces itself.
        if e.through_a_carrier:
            out.append("{}: {} direct, {} more through {}".format(
                e.address, len(e.direct), len(e.through_a_carrier),
                ", ".join(e.carriers)))
    return tuple(out)
