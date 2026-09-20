"""Column level lineage, and what a classification does when it travels.

The classifier decides one column at a time. It reads the name, it reads counts over the
values, and it reads the shape of the column. Everything it knows is a property of the
column in front of it. That works until a column is derived from another one, at which
point the honest answer is not in the column at all. It is upstream.

Two things live here.

The first is a graph. Nodes are columns and edges are derivations, and an edge carries a
kind saying what the derivation did to the value. A copy preserves it. A cast can coarsen
it. A grouping key survives a `GROUP BY` untouched, which is the one most people get wrong.
An aggregate replaces the value with a statistic over many rows.

The second is a reader that recovers those edges from the SQL that built the table, rather
than from a hand written edge list. A hand written edge list is the circularity `pii/crawl.py`
was built to break. It is a description of the warehouse sitting next to the warehouse, and
checking one against the other learns nothing. So this parses the statement instead, and
the important property is that it refuses. A `SELECT *` or a subquery or a window function
returns a refusal naming what it could not read. A lineage tool that guesses an edge is
worse than one that admits it has no idea, because the guess gets masked and the admission
gets a human.

What it cannot do is invent an edge that is not in the SQL. A column loaded by an ingestion
job nobody wrote SQL for is a root in this graph, and a root has no upstream to inherit
from. That limit turned out to matter more than the feature did, and it is measured in
`scripts/lineage_probe.py` rather than argued about here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Set, Tuple


class EdgeKind(Enum):
    """What the derivation did to the value, which is what decides inheritance."""

    COPY = "copy"
    CAST = "cast"
    GROUPED = "grouped"
    AGGREGATE = "aggregate"
    TRANSFORM = "transform"
    JOIN_KEY = "join_key"


# Kinds where the value that lands downstream is the same value that was upstream. A
# grouping key belongs here and that is the point. Collapsing duplicate rows does not
# change what is in the column, so a postal code that was a postal code before a GROUP BY
# is still a postal code after it.
VALUE_PRESERVING = (EdgeKind.COPY, EdgeKind.CAST, EdgeKind.GROUPED)


@dataclass(frozen=True)
class ColumnRef:
    table: str
    column: str

    @property
    def address(self) -> str:
        return "{}.{}".format(self.table, self.column)

    def __str__(self) -> str:
        return self.address


@dataclass(frozen=True)
class Edge:
    source: ColumnRef
    target: ColumnRef
    kind: EdgeKind
    detail: str = ""

    def __str__(self) -> str:
        return "{:<38} -> {:<38} {:<10} {}".format(
            self.source.address, self.target.address, self.kind.value, self.detail)


@dataclass(frozen=True)
class Refusal:
    """Something in the statement this reader would not guess at.

    Carried rather than raised, because one unreadable select item should not throw away
    the edges the reader did recover from the rest of the statement. The probe prints
    these beside the edges and the count of them is a published number.
    """

    what: str
    detail: str

    def __str__(self) -> str:
        return "{:<28} {}".format(self.what, self.detail)


@dataclass(frozen=True)
class Graph:
    edges: Tuple[Edge, ...]
    refusals: Tuple[Refusal, ...] = ()

    def into(self, address: str) -> Tuple[Edge, ...]:
        return tuple(e for e in self.edges if e.target.address == address)

    def out_of(self, address: str) -> Tuple[Edge, ...]:
        return tuple(e for e in self.edges if e.source.address == address)

    def derivation_edges(self) -> Tuple[Edge, ...]:
        """Everything except the declared join keys.

        A foreign key is a statement about which rows line up. It is not a statement that
        one column's values flowed into another, so it does not propagate a label and it
        is filtered out of anything that walks data flow.
        """
        return tuple(e for e in self.edges if e.kind is not EdgeKind.JOIN_KEY)

    def roots(self, addresses: Sequence[str]) -> Tuple[str, ...]:
        """Columns with nothing flowing into them, out of the addresses given.

        This is the number that says how much of a warehouse lineage can speak about at
        all. Everything here has to be classified on its own properties, because there is
        no upstream to ask.
        """
        derived = {e.target.address for e in self.derivation_edges()}
        return tuple(a for a in addresses if a not in derived)

    def sources_of(self, address: str, _seen: Optional[Set[str]] = None) -> Tuple[str, ...]:
        """Every column upstream of this one, walked to the roots.

        Cycle safe. A warehouse should not contain a cycle in column lineage and several
        do anyway, usually through a table that gets rebuilt from a view over itself.
        """
        seen = _seen if _seen is not None else set()
        out: List[str] = []
        for e in self.into(address):
            if e.kind is EdgeKind.JOIN_KEY:
                continue
            if e.source.address in seen:
                continue
            seen.add(e.source.address)
            out.append(e.source.address)
            out.extend(self.sources_of(e.source.address, seen))
        return tuple(out)

    def merge(self, other: "Graph") -> "Graph":
        return Graph(edges=self.edges + other.edges,
                     refusals=self.refusals + other.refusals)


# --- the SQL reader ---------------------------------------------------------------

_AGGREGATES = ("count", "sum", "avg", "min", "max", "median", "stddev",
               "string_agg", "list", "any_value", "arg_max", "arg_min")

# Read as a cast rather than as an arbitrary transform, because the target type is in the
# statement and a type is something this module can reason about. Everything else that
# takes a column and returns a value is a transform, which inherits the category and makes
# no claim about granularity.
_CAST_CALL = re.compile(r"^cast\s*\(", re.IGNORECASE)

_TOKEN = re.compile(r"""
      (?P<string>'(?:[^']|'')*')
    | (?P<name>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<punct>[(),*./+\-=<>])
    | (?P<ws>\s+)
""", re.VERBOSE)

# Words that appear where a column reference could appear and are not one. Kept short on
# purpose. A longer list starts swallowing real column names, and a column genuinely called
# `day` or `minute` exists in this very warehouse.
_NOT_A_COLUMN = {
    "select", "from", "join", "inner", "left", "right", "full", "outer", "cross",
    "on", "where", "group", "by", "order", "having", "as", "and", "or", "not",
    "null", "is", "distinct", "insert", "into", "values", "case", "when", "then",
    "else", "end", "asc", "desc", "true", "false", "interval",
}

_UNSUPPORTED = (
    ("with", "a common table expression needs the whole statement graph, not one select"),
    ("union", "a set operation has two select lists feeding one target"),
    ("intersect", "a set operation has two select lists feeding one target"),
    ("except", "a set operation has two select lists feeding one target"),
    ("over", "a window function reads rows this reader cannot bound"),
    ("lateral", "a lateral join changes what a table alias means partway through"),
)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)


def _split_top_level(text: str, sep: str = ",") -> List[str]:
    """Split on a separator that is not inside brackets or a string literal."""
    out, depth, current, in_string = [], 0, [], False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            current.append(ch)
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    current.append(text[i + 1])
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    out.append("".join(current))
    return [s.strip() for s in out if s.strip()]


def _strip_cast_types(expression: str) -> str:
    """Rewrite `CAST(x AS DATE)` to `(x)` so the type name stops looking like a column.

    The first draft read `DATE` out of the mart's own statement as an unqualified column
    reference and refused the select item over it. A type is not a column and the only
    place the two are ambiguous is inside a cast, so the fix is here rather than in a list
    of reserved words. This warehouse already has a column called `day` and a list of type
    names would eventually eat one.

    The scan moves forward and never restarts. The first version searched from the start of
    the string again after every rewrite, which terminates only for as long as each rewrite
    really does remove the `cast(` it just read. That is true of the code as written and it
    is a property of the rewrite rather than of the loop, so the loop had no termination
    guarantee of its own. A mutation pass found it by changing one offset and hanging the
    suite. Searching from `at` gives the guarantee back, because the rewritten span starts
    there and anything still to do sits after it.
    """
    lowered = expression.lower()
    at = lowered.find("cast(")
    while at != -1:
        # A name ending in `cast` is not the cast keyword.
        if at > 0 and (expression[at - 1].isalnum() or expression[at - 1] == "_"):
            at = lowered.find("cast(", at + 5)
            continue
        depth, i = 0, at + 4
        while i < len(expression):
            if expression[i] == "(":
                depth += 1
            elif expression[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if i >= len(expression):
            break
        inner = expression[at + 5:i]
        cut = re.search(r"\bas\b(?!.*\bas\b)", inner, re.IGNORECASE)
        kept = inner[:cut.start()] if cut else inner
        expression = expression[:at] + "(" + kept + ")" + expression[i + 1:]
        lowered = expression.lower()
        at = lowered.find("cast(", at)
    return expression


def _names_in(expression: str) -> List[str]:
    """Every identifier in an expression that could be a column reference."""
    expression = _strip_cast_types(expression)
    found = []
    for m in _TOKEN.finditer(expression):
        name = m.group("name")
        if name is None:
            continue
        if name.lower() in _NOT_A_COLUMN:
            continue
        # A function call is a name followed by an open bracket. The name of the function
        # is not a column and reading it as one produced an edge out of `count` on the
        # first draft of this.
        rest = expression[m.end():].lstrip()
        if rest.startswith("("):
            continue
        found.append(name)
    return found


@dataclass(frozen=True)
class _Source:
    """One table in the FROM clause, with whatever it was called locally."""

    fqn: str
    alias: str


def _parse_sources(from_clause: str) -> Tuple[Tuple[_Source, ...], Tuple[Refusal, ...]]:
    """Table references and their aliases, out of a FROM with plain joins in it."""
    refusals: List[Refusal] = []
    # ON predicates are stripped before splitting, so a join condition mentioning a table
    # name cannot be mistaken for another source.
    cleaned = re.sub(r"\bon\b.*?(?=\bjoin\b|$)", " ", from_clause,
                     flags=re.IGNORECASE | re.DOTALL)
    parts = re.split(r"\b(?:inner\s+join|left\s+join|right\s+join|full\s+join|"
                     r"cross\s+join|join)\b|,", cleaned, flags=re.IGNORECASE)
    sources: List[_Source] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith("("):
            refusals.append(Refusal("subquery in from",
                                    "a derived table has its own select list"))
            continue
        words = [w for w in re.split(r"\s+", part) if w and w.lower() != "as"]
        if not words:
            continue
        fqn = words[0]
        alias = words[1] if len(words) > 1 else fqn.split(".")[-1]
        sources.append(_Source(fqn=fqn, alias=alias))
    return tuple(sources), tuple(refusals)


def _resolve(name: str, sources: Sequence[_Source]) -> Tuple[Optional[ColumnRef], Optional[Refusal]]:
    """Turn `e.department` or a bare `department` into a real column address."""
    if "." in name:
        head, _, column = name.rpartition(".")
        for s in sources:
            # A third clause matching the bare table name used to sit here and it was dead.
            # When a source carries no alias the default alias is already the bare name, and
            # when it carries one, SQL requires the alias and refuses the table name. Two
            # mutants sat in that redundancy masking each other, which is how it was found.
            if head == s.alias or head == s.fqn:
                return ColumnRef(s.fqn, column), None
        return None, Refusal("unknown qualifier",
                             "{} does not name a table in the from clause".format(head))
    if len(sources) == 1:
        return ColumnRef(sources[0].fqn, name), None
    # More than one source and no qualifier. Which table owns the column is decided by the
    # catalog and this reader does not have one, so it refuses rather than picking the
    # first table and being right most of the time.
    return None, Refusal("unqualified column",
                         "{} could come from any of {}".format(
                             name, ", ".join(s.fqn for s in sources)))


def _kind_of(expression: str) -> Tuple[EdgeKind, str]:
    body = expression.strip()
    if _CAST_CALL.match(body):
        target_type = body[body.lower().rfind(" as ") + 4:].rstrip(") ").strip()
        return EdgeKind.CAST, "cast to {}".format(target_type.upper())
    head = re.match(r"^([A-Za-z_][A-Za-z_0-9]*)\s*\(", body)
    if head and head.group(1).lower() in _AGGREGATES:
        return EdgeKind.AGGREGATE, "{} over the group".format(head.group(1).lower())
    if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.]*", body):
        return EdgeKind.COPY, ""
    if re.search(r"\b(" + "|".join(_AGGREGATES) + r")\s*\(", body, re.IGNORECASE):
        return EdgeKind.AGGREGATE, "aggregate inside a larger expression"
    return EdgeKind.TRANSFORM, "expression"


def read_insert_select(sql: str, target_columns: Optional[Sequence[str]] = None) -> Graph:
    """Recover column level edges from one `INSERT INTO ... SELECT` statement.

    `target_columns` is the target table's column list in order, needed because an INSERT
    with no explicit column list matches the select list positionally. Pass it from the
    crawl or from the declared schema. Without it the reader falls back to the select
    item's alias, which is usually the same name and is a guess where it is not.
    """
    text = " ".join(_strip_comments(sql).split())
    refusals: List[Refusal] = []

    for word, why in _UNSUPPORTED:
        if re.search(r"\b{}\b".format(word), text, re.IGNORECASE):
            refusals.append(Refusal("unsupported: {}".format(word), why))
    if refusals:
        return Graph(edges=(), refusals=tuple(refusals))

    m = re.search(r"insert\s+into\s+([A-Za-z_][A-Za-z_0-9.]*)\s*(\([^)]*\))?\s*select\s+",
                  text, re.IGNORECASE)
    if not m:
        return Graph(edges=(), refusals=(
            Refusal("not an insert select", "no INSERT INTO ... SELECT found"),))
    target_table = m.group(1)
    if m.group(2):
        target_columns = [c.strip() for c in m.group(2)[1:-1].split(",")]

    body = text[m.end():]
    from_at = re.search(r"\bfrom\b", body, re.IGNORECASE)
    if not from_at:
        return Graph(edges=(), refusals=(
            Refusal("no from clause", "a select with no source has no lineage"),))
    select_list = body[:from_at.start()]
    tail = body[from_at.end():]

    stop = re.search(r"\b(where|group\s+by|having|order\s+by|limit)\b", tail, re.IGNORECASE)
    from_clause = tail[:stop.start()] if stop else tail
    after = tail[stop.start():] if stop else ""

    group_by = ""
    g = re.search(r"\bgroup\s+by\b(.*?)(?=\bhaving\b|\border\s+by\b|\blimit\b|$)",
                  after, re.IGNORECASE | re.DOTALL)
    if g:
        group_by = g.group(1)

    sources, from_refusals = _parse_sources(from_clause)
    refusals.extend(from_refusals)
    if not sources:
        return Graph(edges=(), refusals=tuple(refusals) or (
            Refusal("no source table", "nothing in the from clause parsed"),))

    items = _split_top_level(select_list)
    if target_columns is not None and len(items) != len(target_columns):
        refusals.append(Refusal(
            "arity mismatch",
            "{} select items against {} target columns".format(
                len(items), len(target_columns))))
        target_columns = None

    # Positions named in a GROUP BY, which DuckDB and Postgres both allow and this
    # warehouse uses. `GROUP BY 1, 2, 3` means the first three select items.
    grouped_positions: Set[int] = set()
    grouped_names: Set[str] = set()
    for term in _split_top_level(group_by):
        term = term.strip()
        if term.isdigit():
            grouped_positions.add(int(term) - 1)
        else:
            grouped_names.add(term.lower())

    edges: List[Edge] = []
    for position, item in enumerate(items):
        alias_match = re.search(r"\s+as\s+([A-Za-z_][A-Za-z_0-9]*)\s*$", item, re.IGNORECASE)
        if alias_match:
            expression = item[:alias_match.start()].strip()
            alias = alias_match.group(1)
        else:
            expression, alias = item.strip(), item.strip().split(".")[-1]

        if re.fullmatch(r"(?:[A-Za-z_][A-Za-z_0-9]*\.)*\*", expression.strip()):
            refusals.append(Refusal("star expansion",
                                    "select item {} is a star".format(position + 1)))
            continue
        if "::" in expression:
            refusals.append(Refusal(
                "shorthand cast",
                "select item {} uses :: and this reader only reads CAST".format(
                    position + 1)))
            continue
        if "(" in expression and re.search(r"\bselect\b", expression, re.IGNORECASE):
            refusals.append(Refusal("scalar subquery",
                                    "select item {} contains a select".format(position + 1)))
            continue

        target_name = target_columns[position] if target_columns else alias
        target = ColumnRef(target_table, target_name)

        kind, detail = _kind_of(expression)
        if kind in (EdgeKind.COPY, EdgeKind.CAST, EdgeKind.TRANSFORM):
            if position in grouped_positions or expression.lower() in grouped_names:
                if kind is EdgeKind.COPY:
                    kind, detail = EdgeKind.GROUPED, "grouping key"
                else:
                    detail = (detail + ", grouping key").strip(", ")

        names = _names_in(expression)
        if not names and kind is EdgeKind.AGGREGATE:
            # `count(*)` counts rows of the join and names no column. It is still a
            # derivation and treating it as a root would say this column came from nowhere.
            # The source is written with a `*` for a column name, which reads in the output
            # as the row rather than as a field, and every source table gets an edge because
            # the count depends on all of them.
            for s in sources:
                edges.append(Edge(source=ColumnRef(s.fqn, "*"), target=target,
                                  kind=kind, detail="row count over the join grain"))
            continue

        for name in names:
            ref, refusal = _resolve(name, sources)
            if refusal is not None:
                refusals.append(refusal)
                continue
            edges.append(Edge(source=ref, target=target, kind=kind, detail=detail))

    return Graph(edges=tuple(edges), refusals=tuple(refusals))


def foreign_key_edges(con, engine_schemas: Sequence[str]) -> Tuple[Edge, ...]:
    """Declared referential edges, out of the catalog rather than out of a name.

    `duckdb_constraints()` carries `referenced_table` and `referenced_column_names` and the
    first version of the crawler read neither, which is why a join key was invisible to
    everything downstream. The referenced table arrives as a bare name with no schema on it. That is safe to
    resolve inside the referencing table's own schema here and only here, because DuckDB
    refuses a foreign key across schemas outright with a binder error. On an engine that
    allows one this resolution is wrong and it would need the catalog to say which schema
    it meant.
    """
    placeholders = ", ".join("?" for _ in engine_schemas)
    rows = con.execute("""
        SELECT schema_name, table_name, constraint_column_names,
               referenced_table, referenced_column_names
        FROM duckdb_constraints()
        WHERE constraint_type = 'FOREIGN KEY'
          AND schema_name NOT IN ({})
        ORDER BY schema_name, table_name
    """.format(placeholders), list(engine_schemas)).fetchall()

    out: List[Edge] = []
    for schema_name, table_name, columns, ref_table, ref_columns in rows:
        child = "{}.{}".format(schema_name, table_name)
        parent = "{}.{}".format(schema_name, ref_table)
        for local, remote in zip(columns, ref_columns):
            out.append(Edge(
                source=ColumnRef(parent, remote),
                target=ColumnRef(child, local),
                kind=EdgeKind.JOIN_KEY,
                detail="declared foreign key",
            ))
    return tuple(out)


# --- what a classification does when it travels ------------------------------------

# A cast that lands on one of these types cannot carry a finer unit than the type holds.
# Only the direction matters here, so the map is deliberately small and everything not in
# it is treated as preserving whatever was upstream.
_COARSENS_TO = {
    "DATE": "day",
    "TIMESTAMP": "second",
}


@dataclass(frozen=True)
class Inherited:
    """What upstream says about a column, beside what the column says about itself."""

    address: str
    direct_category: str
    direct_confidence: float
    inherited_category: Optional[str]
    through: Tuple[str, ...]
    rule: str

    @property
    def disagrees(self) -> bool:
        """Upstream names a category and the column's own evidence does not agree."""
        if self.inherited_category is None:
            return False
        return self.inherited_category != self.direct_category

    @property
    def upgrade(self) -> bool:
        """Upstream says personal and the column on its own said nothing."""
        return self.disagrees and self.direct_category == "not_personal"


def propagate(graph: Graph, classifications: Sequence) -> Tuple[Inherited, ...]:
    """Push each column's category down its outgoing value preserving edges.

    The rule is one sentence. A value that arrives unchanged carries its category with it,
    and a value that has been replaced by a statistic over many rows does not.

    Grouping counts as unchanged and that is the whole reason this is worth writing. The
    common belief is that a `GROUP BY` sanitises what it groups, and it does not. It drops
    duplicate rows. A postal code in a grouping key is the same postal code it was in the
    source table, sitting in a table whose name suggests otherwise.

    An aggregate is treated as not personal here and that is a claim about the function
    rather than about the result. A count over a group of one is the row it counted. This
    module does not know the group sizes and `scripts/lineage_probe.py` measures them,
    because the claim is only as good as the smallest group in the table.
    """
    by_address = {c.address: c for c in classifications}
    out: List[Inherited] = []

    for c in classifications:
        incoming = [e for e in graph.into(c.address) if e.kind is not EdgeKind.JOIN_KEY]
        if not incoming:
            out.append(Inherited(c.address, c.category_key, c.confidence, None, (),
                                 "root, nothing upstream"))
            continue

        preserving = [e for e in incoming if e.kind in VALUE_PRESERVING]
        if not preserving:
            kinds = sorted({e.kind.value for e in incoming})
            out.append(Inherited(
                c.address, c.category_key, c.confidence, "not_personal",
                tuple(sorted({e.source.address for e in incoming})),
                "{} only, the value did not survive".format(", ".join(kinds))))
            continue

        # More than one preserving edge into one column means a concatenation or a coalesce
        # and the strongest upstream category is the one that has to win. Anything else
        # lets a column be laundered by pairing it with a harmless one.
        candidates = []
        for e in preserving:
            upstream = by_address.get(e.source.address)
            if upstream is None:
                continue
            candidates.append((upstream.confidence, upstream.category_key, e))
        if not candidates:
            out.append(Inherited(c.address, c.category_key, c.confidence, None,
                                 tuple(e.source.address for e in preserving),
                                 "upstream columns are not classified"))
            continue

        personal = [x for x in candidates if x[1] != "not_personal"]
        pick = max(personal or candidates, key=lambda x: x[0])
        _, category, edge = pick
        rule = "{} from {}".format(edge.kind.value, edge.source.address)
        if edge.kind is EdgeKind.CAST:
            lands_on = _COARSENS_TO.get(edge.detail.replace("cast to ", "").strip())
            if lands_on:
                rule += ", value coarsened to {}".format(lands_on)
        out.append(Inherited(c.address, c.category_key, c.confidence, category,
                             tuple(sorted({e.source.address for e in preserving})), rule))
    return tuple(out)


@dataclass(frozen=True)
class NullabilityBreach:
    """A NOT NULL column fed by a column that is allowed to be empty."""

    source: str
    target: str
    kind: EdgeKind

    def __str__(self) -> str:
        return "{:<38} nullable -> {:<38} NOT NULL  via {}".format(
            self.source, self.target, self.kind.value)


def nullable_into_not_null(graph: Graph,
                           nullable_by_address: Dict[str, bool]) -> Tuple[NullabilityBreach, ...]:
    """Find a derived column declared stricter than the column it derives from.

    This was found by accident. `analytics.encounter_daily.department` was declared NOT NULL
    over a nullable source, and nothing could see it until the corpus wrote its first null
    and the load died on a constraint error. The defect was structural the whole time and the
    only thing standing between it and production was a generator that had not yet produced
    the value that breaks it.

    Only the value preserving kinds are checked. A `count(*)` is never null no matter what
    it counted, so an aggregate over a nullable column is not a breach and reporting one
    would train a reader to ignore the output.
    """
    out: List[NullabilityBreach] = []
    for e in graph.edges:
        if e.kind not in VALUE_PRESERVING:
            continue
        source_nullable = nullable_by_address.get(e.source.address)
        target_nullable = nullable_by_address.get(e.target.address)
        if source_nullable is None or target_nullable is None:
            continue
        if source_nullable and not target_nullable:
            out.append(NullabilityBreach(e.source.address, e.target.address, e.kind))
    return tuple(out)
