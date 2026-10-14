"""Masking policy generation, application, and the measurement that grades it.

Three parts, and the third is the one most tools leave out.

Generation turns a classification into an action. Redact a direct identifier, generalise a
quasi identifier to the coarsest form the regulation permits, retain everything else, and
send the review band to a human. That part is a lookup and it is not interesting.

Application turns an action into SQL. Also not interesting, except that the expressions are
built here rather than in a report script so a mutant can reach them.

The third part is the reason this module is not fifty lines. A policy generator that emits
actions and stops has reported compliance with a rulebook. It has not reported whether
anybody is still identifiable, and those are different questions with different answers.
`residual_risk` applies the generated policy and measures k over the quasi set afterwards.
On this warehouse the answer is that a fully compliant masking policy leaves people alone in
their group, and the number is in the README.

The per column answer cannot settle it and that is structural. k is a property of a set of
columns. `postal_code` masked to three digits is safe beside nothing and unsafe beside a
birth date, and no amount of looking at `postal_code` on its own will say which case you are
in. So generation is per column, sufficiency is per quasi set, and this module refuses to
let the first stand in for the second.

The no values rule from `pii/profile.py` holds here too. Every measurement below is a
`count(*)` over a `GROUP BY` and what comes back is integers. A residual risk report names
how many people are alone in their group and never which people.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Sequence, Tuple

from pii.classify import Band, Classification
from pii.crawl import _quote
from pii.lineage import ROW_COUNT_SOURCE, EdgeKind
from pii.taxonomy import (
    TAXONOMY,
    Granularity,
    Identifiability,
    granularity_family,
)


class Action(Enum):
    """What the policy does to one column."""

    REDACT = "redact"
    GENERALISE = "generalise"
    RETAIN = "retain"
    REVIEW = "review"


# What Safe Harbor permits a value to be coarsened to, per granularity family.
#
# Written down rather than derived from the taxonomy's `identifies_at`, because the two
# disagree and deriving one from the other would hide that. The taxonomy says a temporal
# category identifies at DAY, which makes MONTH non identifying by its own ordering. Clause C
# removes all elements of a date except the year, so a month is not permitted to survive
# either. `threshold_disagreements` below reports the gap instead of smoothing it.
SAFE_HARBOR_TARGET: Dict[str, Granularity] = {
    "temporal": Granularity.YEAR,
    "postal": Granularity.POSTAL_3,
}


def derived_target(family: str) -> Granularity:
    """The coarsest non identifying granularity, taken from the taxonomy's own ordering.

    This is what the permitted target would be if the taxonomy were the only authority. It
    exists to be compared against `SAFE_HARBOR_TARGET` and for no other purpose.
    """
    from pii.taxonomy import _GRANULARITY_ORDER

    order = _GRANULARITY_ORDER[family]
    thresholds = [
        c.identifies_at for c in TAXONOMY
        if c.identifies_at is not None and granularity_family(c.identifies_at) == family
    ]
    if not thresholds:
        raise ValueError("no category carries a threshold in family {}".format(family))
    finest = max(order.index(t) for t in thresholds)
    coarsest = min(order.index(t) for t in thresholds)
    if finest != coarsest:
        raise ValueError(
            "family {} has categories with different thresholds, so there is no single "
            "derived target".format(family)
        )
    if coarsest == 0:
        raise ValueError(
            "family {} identifies at its coarsest form, so nothing in it is "
            "permitted".format(family)
        )
    return order[coarsest - 1]


def threshold_disagreements() -> Tuple[str, ...]:
    """Families where the regulation is stricter than this taxonomy's own ordering.

    A check rather than a comment, because the taxonomy is mine and the clause list is not.
    Where they disagree the clause wins and the disagreement gets said out loud.
    """
    out = []
    for family, permitted in sorted(SAFE_HARBOR_TARGET.items()):
        mine = derived_target(family)
        if mine is not permitted:
            out.append(
                "{}: the taxonomy would allow {} and Safe Harbor permits only {}".format(
                    family, mine.value, permitted.value)
            )
    return tuple(out)


@dataclass(frozen=True)
class Policy:
    """One column, one action, and the reason it was chosen.

    `target` is the granularity the value is coarsened to and it is set only for
    GENERALISE. `evidence_only` marks a RETAIN that was reached because no rule fired
    rather than because anything is known about the column. Those two retains look the same
    in a report and they are not the same claim, which is what that flag exists to keep
    apart.
    """

    table: str
    column: str
    category_key: str
    action: Action
    confidence: float
    reason: str
    target: Optional[Granularity] = None
    evidence_only: bool = False

    def __post_init__(self) -> None:
        if self.action is Action.GENERALISE and self.target is None:
            raise ValueError(
                "{}.{} generalises to nothing, which is a retain wearing a "
                "different name".format(self.table, self.column)
            )
        if self.action is not Action.GENERALISE and self.target is not None:
            raise ValueError(
                "{}.{} carries a target granularity and does not generalise".format(
                    self.table, self.column)
            )
        if self.evidence_only and self.action is not Action.RETAIN:
            raise ValueError(
                "{}.{} is marked evidence only and is not a retain".format(
                    self.table, self.column)
            )
        if not self.reason:
            raise ValueError(
                "{}.{} has no stated reason".format(self.table, self.column))

    @property
    def address(self) -> str:
        return "{}.{}".format(self.table, self.column)

    @property
    def changes_the_value(self) -> bool:
        return self.action in (Action.REDACT, Action.GENERALISE)


@dataclass(frozen=True)
class Decision:
    """What the rulebook says about a settled category, before any column is named.

    Split out of `policy_for` so the reviewer path can reach the same mapping without
    holding a `Classification`. It used to build one, complete with a `Signal` nobody
    produced, purely to get back in through the front door.
    """

    action: Action
    reason: str
    target: Optional[Granularity] = None
    evidence_only: bool = False


def settled_action(category_key: str, fired: bool) -> Decision:
    """The action for a category that is no longer in the review band.

    `fired` says whether any arm produced evidence on the column. It only changes the
    reading of a RETAIN in a non personal category, where nothing firing is an absence of
    evidence rather than a finding.

    It has no default and that is deliberate. It carried True until a mutation pass on
    2026-10-14 flipped the default and nothing failed, because the only category the flag
    can reach cannot reach a review. A default no caller depends on is a site no check can
    kill, so both callers say what they mean instead.
    """
    category = TAXONOMY.get(category_key)

    if category.identifiability is Identifiability.NONE:
        # Two ways to land here and they carry different weight. A column the classifier
        # scored on real evidence and placed outside the personal categories is a finding. A
        # column nothing fired on at all is an absence of evidence, and the value arm cannot
        # test a pattern nobody wrote down, so the absence is worth less than it looks.
        return Decision(
            Action.RETAIN,
            ("no rule fired on this column, so it is retained on an absence of "
             "evidence rather than on a finding")
            if not fired else
            "classified outside the personal categories",
            evidence_only=not fired,
        )

    if category.identifiability is Identifiability.SENSITIVE:
        # A diagnosis is not an identifier. Redacting it protects nobody who has not already
        # been identified by the quasi columns, and it destroys the column the warehouse
        # exists to analyse. The protection for a sensitive attribute is the k of the quasi
        # set beside it, which is measured below rather than asserted here.
        return Decision(
            Action.RETAIN,
            "a sensitive attribute is not an identifier, and what protects it is the "
            "k of the quasi columns beside it",
        )

    if category.identifiability is Identifiability.DIRECT:
        return Decision(
            Action.REDACT,
            "a direct identifier names a person by itself and has no coarser form that "
            "stops doing so",
        )

    # Quasi. Generalise where the category carries a granularity threshold, because that is
    # the case where a coarser form exists. Where it does not, there is nothing between
    # keeping the value and destroying it.
    if category.identifies_at is None:
        return Decision(
            Action.RETAIN,
            "a quasi identifier with no coarser form. It is retained and it counts "
            "toward the k of its set",
        )

    family = granularity_family(category.identifies_at)
    target = SAFE_HARBOR_TARGET[family]
    return Decision(
        Action.GENERALISE,
        "identifies at {} or finer, and {} is the coarsest form the clause list "
        "permits".format(category.identifies_at.value, target.value),
        target=target,
    )


def policy_for(result: Classification) -> Policy:
    """The action for one column, from its classification alone.

    Deliberately blind to the other columns in the table. Whether the set this column sits
    in is safe afterwards is `residual_risk`'s question and answering it here would mean
    answering it wrongly, because this function cannot see the set.
    """
    if result.band is Band.REVIEW:
        return Policy(
            result.table, result.column, result.category_key, Action.REVIEW,
            result.confidence,
            "scored {:.4f}, under the accept threshold, so a person decides".format(
                result.confidence),
        )

    decided = settled_action(result.category_key, fired=bool(result.signals))
    reason = decided.reason
    if (result.signals
            and TAXONOMY.get(result.category_key).identifiability is Identifiability.NONE):
        # The non personal retain is the only reason that quotes how much evidence there
        # was, and the count is a property of this result rather than of the category.
        reason = "{} on {} signals".format(reason, len(result.signals))
    return Policy(
        result.table, result.column, result.category_key, decided.action,
        result.confidence, reason, target=decided.target,
        evidence_only=decided.evidence_only,
    )


def generate(results: Sequence[Classification]) -> Tuple[Policy, ...]:
    if not results:
        raise ValueError("cannot generate a policy over zero columns")
    return tuple(policy_for(r) for r in results)


def resolve_review(policy: Policy, personal: bool) -> Policy:
    """Apply a reviewer's decision to one column in the review band.

    This is the only way a REVIEW becomes something maskable, and it takes a decision as an
    argument rather than inferring one. A function that resolved reviews by re-reading the
    confidence would be the classifier voting twice.

    `personal` false does not mean the column is harmless. It means a person looked and
    decided it does not need masking, and the reason is recorded as theirs.
    """
    if policy.action is not Action.REVIEW:
        raise ValueError(
            "{} is not in the review band, so there is nothing to resolve".format(
                policy.address))
    if not personal:
        return Policy(policy.table, policy.column, "not_personal", Action.RETAIN,
                      policy.confidence, "a reviewer decided this does not need masking")

    # Asks the rulebook directly rather than rebuilding a Classification to ask through
    # policy_for. The old version stamped a synthetic Signal carrying the accept threshold
    # and credited it to the name arm, which no arm had run. The confidence carried on the
    # returned policy is the classifier's original, because that is the number that was
    # true about the evidence and a reviewer's decision is not a score.
    # fired is True because a person looked at the column, which is evidence even though
    # no arm ran.
    decided = settled_action(policy.category_key, fired=True)
    # No evidence_only here and that is not an oversight. The flag only comes back for a
    # category of no identifiability, `not_personal` is the only one, and that key is
    # pinned to the ignore band so it never reaches a review. Passing the flag through
    # would be a line that cannot fire. A check asserts the premise instead, so a second
    # non personal category added later fails rather than quietly lands here.
    return Policy(policy.table, policy.column, policy.category_key, decided.action,
                  policy.confidence,
                  "a reviewer confirmed the category, then " + decided.reason,
                  target=decided.target)


def action_counts(policies: Sequence[Policy]) -> Dict[str, int]:
    out = {a.value: 0 for a in Action}
    for p in policies:
        out[p.action.value] += 1
    return out


def retained_on_absence(policies: Sequence[Policy]) -> Tuple[str, ...]:
    """Addresses retained because nothing fired, rather than because anything is known.

    This is the running cost of the rule that no value ever reaches the process. A
    classifier that can only count the matches of patterns it already holds returns zero on
    a format nobody wrote down, and the column then lands in exactly this bucket looking
    identical to a column that was genuinely cleared. The size of this set is what that
    design decision costs, stated as a number instead of as a caveat.
    """
    return tuple(sorted(p.address for p in policies if p.evidence_only))


def _quote_fqn(table: str) -> str:
    """Quote a qualified table name part by part.

    `_quote` handles one identifier. Passing `raw.patient` through it produces a single
    quoted name containing a dot, which is a table nobody has. Every column in this module
    was already quoted and the table was not, which is the inconsistency rather than the
    dot being harmless. A schema or table named with a reserved word or a space would have
    produced broken SQL from a governance tool whose premise is being pointed at a warehouse
    somebody else owns and named.
    """
    if not table:
        raise ValueError("cannot quote an empty table name")
    return ".".join(_quote(part) for part in table.split("."))


# The masking expressions. One per family plus redaction, kept small on purpose.
def _generalise_sql(column: str, target: Granularity) -> str:
    quoted = _quote(column)
    if target is Granularity.YEAR:
        return "year({})".format(quoted)
    if target is Granularity.POSTAL_3:
        return "substr(cast({} as varchar), 1, 3)".format(quoted)
    raise ValueError(
        "no generalisation is implemented for {}. A target with no expression behind it "
        "is a policy that reports success and changes nothing".format(target.value)
    )


def masking_expression(policy: Policy) -> str:
    """The SQL that replaces the column under this policy.

    REVIEW raises rather than returning the column untouched. A review is an unanswered
    question and running a masking pass over one silently publishes the unmasked value,
    which is the failure mode worth crashing on.
    """
    if policy.action is Action.REDACT:
        return "NULL"
    if policy.action is Action.GENERALISE:
        return _generalise_sql(policy.column, policy.target)
    if policy.action is Action.RETAIN:
        return _quote(policy.column)
    raise ValueError(
        "{} is in the review band and has no masking expression. Decide it before "
        "masking, rather than shipping the raw value".format(policy.address)
    )


@dataclass(frozen=True)
class Residual:
    """What is left after the policy has been applied, measured on the data.

    `population` is the number of people the grouping covers, which is not always the row
    count. On a mart that has already grouped, one row stands for several encounters, and
    counting rows would report a table as safer than it is by exactly the factor it
    deduplicated by.
    """

    table: str
    columns: Tuple[str, ...]
    k: int
    groups: int
    singleton_groups: int
    population: int

    @property
    def alone_share(self) -> float:
        """Share of the population sitting alone in its group."""
        return self.singleton_groups / self.population

    def meets(self, k_target: int) -> bool:
        if k_target < 1:
            raise ValueError("a k target under 1 is not a threshold")
        return self.k >= k_target


def residual_sql(table: str,
                 policies: Sequence[Policy],
                 weight: Optional[str] = None) -> str:
    """The statement that measures k over the masked quasi set of one table.

    `weight` names a column holding how many people each row stands for. Left out, each row
    counts once. This is the mart case and getting it wrong is how a deduplicating GROUP BY
    gets reported as anonymisation.

    Everything this returns is an integer. No value from a user column is selected.
    """
    quasi = [p for p in policies
             if TAXONOMY.get(p.category_key).identifiability is Identifiability.QUASI]
    if not quasi:
        raise ValueError(
            "{} has no quasi identifier under this policy, so there is no set to "
            "measure k over".format(table))
    unresolved = [p.address for p in quasi if p.action is Action.REVIEW]
    if unresolved:
        raise ValueError(
            "cannot measure residual risk on {} while {} is unresolved. Measuring around "
            "a review would report a k for a policy nobody has agreed to".format(
                table, unresolved[0]))

    grain = ", ".join(masking_expression(p) for p in quasi)
    size = "count(*)" if weight is None else "sum({})".format(_quote(weight))
    return (
        "SELECT min(s), count(*), "
        "sum(CASE WHEN s = 1 THEN 1 ELSE 0 END), sum(s) "
        "FROM (SELECT {} AS s FROM {} GROUP BY {})".format(
            size, _quote_fqn(table), grain)
    )


def weight_candidates(graph, table: str) -> Tuple[str, ...]:
    """Columns of `table` that hold a count of the rows each output row stands for.

    The lineage reader writes a `count(*)` as an AGGREGATE edge whose source column is a
    literal `*`, because the count names no field and depends on every source table. That
    star is the whole discriminator. `mean_length_of_stay_h` is also an AGGREGATE target on
    this warehouse and its sources are named columns, so it is an average over rows and not
    a count of them.

    This used to be a dict typed into two scripts, which meant a mart in somebody else's
    warehouse was measured by row count and reported safer than it is by exactly the factor
    its GROUP BY deduplicated by. That is the precise failure this module exists to catch
    and supplying the answer by hand is how a tool avoids catching it.

    It is worth being plain about what this reads. `lineage.ROW_COUNT_SOURCE` is a
    convention of this repo's own reader and not a property of SQL. A graph arriving from a
    dbt manifest or an information_schema view records nothing of the sort, so an empty
    result here means either no group size column or a graph this function cannot speak
    about. `derivation_of` is how the second case stops looking like the first.
    """
    found = set()
    for edge in graph.edges:
        if edge.kind is not EdgeKind.AGGREGATE:
            continue
        if edge.source.column != ROW_COUNT_SOURCE:
            continue
        if edge.target.table != table:
            continue
        found.add(edge.target.column)
    return tuple(sorted(found))


class Derivation(Enum):
    """What the lineage graph is entitled to say about where a table's rows came from."""

    READ = "read"
    NOT_DERIVED = "not_derived"
    UNREADABLE = "unreadable"


def derivation_of(graph, table: str) -> Derivation:
    """Whether a missing weight edge on this table is a fact or an unread statement.

    READ means the reader parsed a statement that writes this table and recorded no
    refusal against it, so an absent group size column is a finding. NOT_DERIVED means
    nothing in the graph builds this table and nothing was given up on, which is the four
    raw tables of this warehouse and the case where rows are people. UNREADABLE means the
    reader refused something, so the absence of an edge says nothing at all.

    An unattributed refusal makes every table UNREADABLE and that is deliberate. A
    statement rejected on an unsupported keyword never reached its INSERT INTO, so the
    reader knows one table's derivation is unread and cannot say which. Calling the rest
    NOT_DERIVED on that evidence is the guess this function exists to refuse.
    """
    if graph.refusals_for(table) or graph.unattributed_refusals():
        return Derivation.UNREADABLE
    if any(e.target.table == table for e in graph.derivation_edges()):
        return Derivation.READ
    return Derivation.NOT_DERIVED


def weight_for(graph, table: str) -> Optional[str]:
    """The one weight column, or a refusal when the graph cannot pick.

    None means the table is not pre-aggregated and rows are people, which is the ordinary
    case and the right default. Two candidates is not a default, it is a question, and
    guessing between them silently is worse than the typed in constant this replaced.

    The third case is the one that was missing. A table the reader could not fully read has
    no group size edge for the same reason it has no edges at all, and returning None there
    reported a mart as safer than it is by the factor its GROUP BY deduplicated by. That
    now raises. The cost is that a warehouse holding one statement this reader cannot parse
    gets no k for any of its tables, which is a loud wrong answer instead of a quiet one.
    """
    candidates = weight_candidates(graph, table)
    if len(candidates) > 1:
        raise ValueError(
            "{} has {} columns that could be its group size, {}. Pass the one you mean "
            "rather than letting this pick".format(
                table, len(candidates), ", ".join(candidates)))
    if candidates:
        return candidates[0]

    state = derivation_of(graph, table)
    if state is Derivation.UNREADABLE:
        blocking = graph.refusals_for(table) or graph.unattributed_refusals()
        raise ValueError(
            "cannot say whether {} is pre-aggregated, because the lineage reader did not "
            "finish reading what builds it. The first thing it refused was {}. Counting "
            "rows here would report the table safer than it is if it has already "
            "grouped".format(table, blocking[0].what))
    return None


def measure_residual(con,
                     table: str,
                     policies: Sequence[Policy],
                     weight: Optional[str] = None) -> Residual:
    quasi = tuple(p.column for p in policies
                  if TAXONOMY.get(p.category_key).identifiability is Identifiability.QUASI)
    row = con.execute(residual_sql(table, policies, weight)).fetchone()
    k, groups, singletons, population = row
    return Residual(
        table=table,
        columns=quasi,
        k=int(k),
        groups=int(groups),
        singleton_groups=int(singletons),
        population=int(population),
    )


def compliant_but_identifiable(residuals: Sequence[Residual],
                               k_target: int = 2) -> Tuple[Residual, ...]:
    """Tables where every permitted generalisation was applied and k is still under target.

    The name is the finding. These are tables a Safe Harbor checklist passes and a person
    can still be picked out of. Satisfying the clause list and protecting the people in the
    table are different properties, and a tool that reports only the first is reporting the
    easier one.
    """
    return tuple(r for r in residuals if not r.meets(k_target))
