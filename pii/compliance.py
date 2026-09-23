"""The compliance report, assembled from what the other modules already answered.

Nothing here classifies a column or measures a k. Every figure arrives from `pii.classify`
or `pii.mask` or `pii.access` or `pii.coverage`, which are the modules that have checks and
a mutation score behind them. What this module owns is the shape of the document and one
idea that is not in any of them.

**A refusal is a record, not a missing row.**

The other modules already decline to answer things. `mask.residual_sql` raises rather than
measure k around an undecided column. `access.unanswerable` returns the columns where the
query log gives a floor of zero and a ceiling above it. Those refusals are correct and on
the way to a document they turn into whitespace, because a renderer writes the rows it has
and says nothing about the rows it does not.

So `Refusal` is a record with four fields. The question it was asked and the subject it is
about. Why the tool will not answer it and what would close it.

The refusals are counted in the header and rendered in their own section,
and `undelivered` grades the rendered text against the refusal set so a refusal cannot
leave the document by somebody editing prose. That last function is the only check in this
repo whose two sides are a data structure and a piece of English, and it is the reason the
"what this cannot tell you" section is generated rather than written.

The header figure is `answered of asked`. On this warehouse it is low and that is the
report working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from pii import access, classify, coverage, mask, safeharbor, schema
from pii.access import Exposure
from pii.classify import Band, Classification
from pii.mask import Policy, Residual
from pii.taxonomy import TAXONOMY, Identifiability

# No weight table here any more. `mask.weight_for` reads it off the lineage graph, which is
# the only place that fact was ever really recorded. This module carried a hand written
# {table: column} dict for one afternoon and that dict is what made the report correct on
# the one warehouse whose answer I already knew.


# A refusal that no work closes. Written as a value rather than as prose so the report can
# count them, and it has to be counted, because a reader skimming thirty three refusals
# reasonably assumes every one of them is a task somebody has not done yet. Two of these
# are not. They are facts about what a metadata driven scan can represent at all.
DOES_NOT_CLOSE = "this does not close from here"


@dataclass(frozen=True)
class Refusal:
    """One question the report was asked and will not answer.

    `closes_when` is required and the constructor refuses an empty one. A refusal with no
    route out of it reads as a limitation of the tool forever, and most of these are a
    limitation of the input, which is a different thing and the reader cannot tell them
    apart unless somebody says.

    The field started out as free text and that was a defect. Two Safe Harbor clauses have
    no route out, and a required field with nowhere to put that fact produced two sentences
    promising a taxonomy change that would not have helped either of them. `DOES_NOT_CLOSE`
    is what a refusal says when the honest answer is nothing.
    """

    question: str
    subject: str
    because: str
    closes_when: str

    def __post_init__(self) -> None:
        for field in ("question", "subject", "because", "closes_when"):
            if not getattr(self, field).strip():
                raise ValueError(
                    "a refusal about {!r} has an empty {}".format(self.subject, field))

    @property
    def closeable(self) -> bool:
        return self.closes_when != DOES_NOT_CLOSE

    def line(self) -> str:
        """Subject first, because a reader scanning this section is looking for a name."""
        tail = ("Closes when {}.".format(self.closes_when.rstrip("."))
                if self.closeable else "Nothing closes this.")
        return "`{}` {}? {}. {}".format(
            self.subject, self.question, self.because.rstrip("."), tail)


@dataclass(frozen=True)
class Section:
    """A block of the report. Rows it could fill and questions it could not."""

    heading: str
    intro: str
    rows: Tuple[str, ...]
    refusals: Tuple[Refusal, ...] = ()

    @property
    def asked(self) -> int:
        return len(self.rows) + len(self.refusals)


@dataclass(frozen=True)
class Report:
    generated_by: str
    schema_fingerprint: str
    taxonomy_fingerprint: str
    sections: Tuple[Section, ...]

    @property
    def refusals(self) -> Tuple[Refusal, ...]:
        return tuple(r for s in self.sections for r in s.refusals)

    @property
    def unclosable(self) -> Tuple[Refusal, ...]:
        """Refusals no work closes. The ones that are not a backlog."""
        return tuple(r for r in self.refusals if not r.closeable)

    @property
    def answered(self) -> int:
        return sum(len(s.rows) for s in self.sections)

    @property
    def asked(self) -> int:
        return sum(s.asked for s in self.sections)

    def answer_rate(self) -> Tuple[int, int]:
        """Answered and asked, as two integers.

        Two integers rather than a share, for the same reason `access.star_share` returns
        two. A percentage over thirty questions reads like a percentage over thirty
        thousand, and the denominator is the part a reader needs.
        """
        return self.answered, self.asked


def scope_section(results: Sequence[Classification]) -> Section:
    tables = sorted({r.table for r in results})
    rows = []
    for table in tables:
        cols = [r for r in results if r.table == table]
        personal = sum(1 for r in cols if r.identifiability is not Identifiability.NONE)
        rows.append("{:<28} {:>3} columns, {:>2} in a personal category".format(
            table, len(cols), personal))
    return Section(
        heading="What was scanned",
        intro=("Recovered from the catalog rather than read from the schema file. "
               "No value from a user column was selected at any point."),
        rows=tuple(rows),
    )


def classification_section(results: Sequence[Classification]) -> Section:
    """Answers are the accept band. Refusals are the review band.

    The ignore band is neither. A column the tool decided is not personal is an answer and
    it is counted as one, and the argument for that is in the README under the band split.
    """
    counts = classify.band_counts(results)
    rows = ["{:<12} {}".format(band, counts[band])
            for band in sorted(counts)]
    refusals = tuple(
        Refusal(
            question="is this column personal data",
            subject=r.address,
            because=("it scored {:.4f}, which is inside the band where the tool "
                     "declined to decide".format(r.confidence)),
            closes_when="a reviewer answers it in the queue",
        )
        for r in sorted(results, key=lambda c: c.address)
        if r.band is Band.REVIEW
    )
    return Section(
        heading="What the classifier found",
        intro=("Three bands. Accept is masked with nobody looking, review is a person's "
               "decision, ignore is the tool saying no rule fired."),
        rows=tuple(rows),
        refusals=refusals,
    )


def policy_section(policies: Sequence[Policy]) -> Section:
    counts = mask.action_counts(policies)
    rows = ["{:<12} {}".format(action, counts[action])
            for action in ("redact", "generalise", "retain", "review")]
    absent = mask.retained_on_absence(policies)
    rows.append("{} of the {} retained columns are kept because no rule fired".format(
        len(absent), counts["retain"]))
    return Section(
        heading="The policy that follows",
        intro=("Generated from the classification. A review action is not a policy, it is "
               "the absence of one, and it is counted here so the two are not confused."),
        rows=tuple(rows),
    )


def residual_section(con, policies: Sequence[Policy], graph=None) -> Section:
    """k per table after the policy is applied, or the reason there is no number.

    `mask.residual_sql` already raises on an unresolved review. Catching that and turning
    the message into a `Refusal` is the whole job here. The alternative, which the first
    version of the masking probe did, is to print the exception text into a table cell,
    where it reads as an error rather than as the report declining.

    `graph` is how the group size column gets found. Passing None counts rows on every
    table, which is right for a warehouse of raw tables and silently wrong for a mart, so
    it is a deliberate choice by the caller rather than a default that quietly works.
    """
    by_table: Dict[str, list] = {}
    for p in policies:
        by_table.setdefault(p.table, []).append(p)

    rows = []
    refusals = []
    for table in sorted(by_table):
        try:
            weight = None if graph is None else mask.weight_for(graph, table)
            r = measure(con, table, by_table[table], weight)
        except ValueError as exc:
            refusals.append(_residual_refusal(table, str(exc)))
            continue
        rows.append("{:<28} k {:>5}  {:>6} groups  {:.4f} alone  over {}".format(
            r.table, r.k, r.groups, r.alone_share, ", ".join(r.columns)))
    return Section(
        heading="What risk is left",
        intro=("k is the smallest group on the masked quasi set. k of 1 means somebody is "
               "alone in their group and the masking did not protect them."),
        rows=tuple(rows),
        refusals=tuple(refusals),
    )


def measure(con, table: str, policies: Sequence[Policy],
            weight: Optional[str] = None) -> Residual:
    """Thin pass through, here so the section body has one call to mock in a check."""
    return mask.measure_residual(con, table, policies, weight=weight)


def _residual_refusal(table: str, message: str) -> Refusal:
    """Build the refusal from the exception the measurement raised.

    The reason is taken from the message rather than rewritten, because a second sentence
    saying the same thing in different words is a second place for the two to drift apart.
    """
    return Refusal(
        question="what is the smallest group left after masking",
        subject=table,
        because=message.split(". ")[0],
        closes_when="the columns it names are decided in the review queue",
    )


def access_section(exposures: Sequence[Exposure]) -> Section:
    """Could have read and did read, per column, with the undecidable ones refused.

    A column where nobody named it and somebody ran a star over a table holding it has a
    floor of zero and a ceiling above it. Printing either end is picking one and hoping,
    so it is refused.
    """
    cannot = set(access.unanswerable(exposures))
    rows = []
    refusals = []
    for e in sorted(exposures, key=lambda x: x.address):
        if e.address in cannot:
            n = len(e.only_by_star)
            refusals.append(Refusal(
                question="did anybody read this column",
                subject=e.address,
                because=("nobody named it and {} {} ran a star over a table holding it, "
                         "so the answer is between 0 and {}".format(
                             n, "user" if n == 1 else "users",
                             len(e.did_read_at_most))),
                closes_when=("the query log records the columns a star expanded to, or "
                             "the catalog keeps enough history to expand it afterwards"),
            ))
            continue
        rows.append("{:<44} {:>3} could read, {:>3} did, {:>3} never used it".format(
            e.address, len(e.could_have_read), len(e.did_read),
            len(e.granted_and_never_used)))
    return Section(
        heading="Who could have read it, and who did",
        intro=("Permission and use, kept apart. The could column follows the value "
               "through lineage, so it counts users a table level grant review misses."),
        rows=tuple(rows),
        refusals=tuple(refusals),
    )


def coverage_section(report: coverage.Report) -> Section:
    """Recall against the clause list, with the discount in the row rather than below it.

    The recall figure is graded against a scope list somebody else published, which is what
    makes the denominator defensible. The numerator is a scan whose rules were written with
    the planted labels open, so the figure is a report on the author's memory. That belongs
    beside the number and not in a limitations section two screens down.
    """
    scope = report.in_scope
    found = sum(1 for v in scope if v.naive_calls_it_personal)
    gaps = safeharbor.gaps()
    rows = [
        "{} published Safe Harbor clauses, {} of them reach a category here".format(
            len(safeharbor.CLASSES), len(safeharbor.CLASSES) - len(gaps)),
        "{} of {} in scope columns found by the floor scan".format(found, len(scope)),
        "the rules behind that numerator were written with the answer key open",
    ]
    # Both of these say, in their own gap reason, that nothing can assign them from a
    # column's metadata. A fax number and a telephone number are the same string, and
    # clause R is a catch all whose membership is a property of the values. So the route
    # out is not a taxonomy change and the report must not offer one.
    refusals = tuple(
        Refusal(
            question="is this clause satisfied",
            subject="Safe Harbor clause {}".format(c.letter),
            because="{}. {}".format(c.clause, c.gap_reason),
            closes_when=DOES_NOT_CLOSE,
        )
        for c in gaps
    )
    return Section(
        heading="Against the published clause list",
        intro=("HIPAA Safe Harbor is the answer key. It has another author, which is the "
               "only reason a recall denominator here means anything."),
        rows=tuple(rows),
        refusals=refusals,
    )


def build(con,
          results: Sequence[Classification],
          policies: Sequence[Policy],
          exposures: Sequence[Exposure],
          graded: coverage.Report,
          generated_by: str,
          graph=None) -> Report:
    return Report(
        generated_by=generated_by,
        schema_fingerprint=schema.fingerprint(),
        taxonomy_fingerprint=TAXONOMY.fingerprint(),
        sections=(
            scope_section(results),
            classification_section(results),
            policy_section(policies),
            residual_section(con, policies, graph),
            access_section(exposures),
            coverage_section(graded),
        ),
    )


def render(report: Report, drop_refusals: bool = False) -> str:
    """The document.

    `drop_refusals` exists for one caller and it is `tests/test_compliance.py`. A check
    that a refusal survives rendering cannot pass on a renderer that has never dropped
    one, so the control has to be reachable. It is a keyword with a default of False and
    no production caller passes it.
    """
    answered, asked = report.answer_rate()
    out = [
        "# Compliance report",
        "",
        "Generated by `{}`.".format(report.generated_by),
        "",
        "Schema fingerprint `{}`, taxonomy `{}`.".format(
            report.schema_fingerprint, report.taxonomy_fingerprint),
        "",
        "**{} of {} questions answered. {} refused, and {} of those close when somebody "
        "does something.**".format(
            answered, asked, len(report.refusals),
            len(report.refusals) - len(report.unclosable)),
        "",
        ("A refused question is not a gap in the run. It is the tool declining to publish "
         "a number about a set nobody has agreed to. Each one below carries what would "
         "close it, or says that nothing does."),
        "",
    ]
    for section in report.sections:
        out.append("## {}".format(section.heading))
        out.append("")
        out.append(section.intro)
        out.append("")
        out.append("```")
        for row in section.rows:
            out.append(row)
        if not section.rows:
            out.append("nothing this section can state")
        out.append("```")
        out.append("")
        if section.refusals and not drop_refusals:
            out.append("Refused, {} of them:".format(len(section.refusals)))
            out.append("")
            for r in section.refusals:
                out.append("- {}".format(r.line()))
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def undelivered(report: Report, rendered: str) -> Tuple[str, ...]:
    """Refusal subjects that the report holds and the rendered document does not name.

    The one check here whose two sides are different kinds of thing. Everything else in
    this repo compares a structure against another structure, which cannot catch a
    renderer quietly dropping a section, because both sides come out of the same builder.
    This compares a tuple of refusals against a piece of English.

    It returns subjects rather than a boolean so a failure says which one went missing.
    """
    if not report.refusals:
        raise ValueError(
            "this report refused nothing, so a check that its refusals survived "
            "rendering would pass having compared nothing")
    return tuple(sorted({r.subject for r in report.refusals
                         if r.subject not in rendered}))


def refusals_by_question(report: Report) -> Dict[str, int]:
    """How many refusals each distinct question accounts for.

    Worth having separately from the count in the header. Thirty one refusals reads as a
    broken tool and four questions refused thirty one times reads as what it is, which is
    a small number of missing facts about the input repeated across every subject.
    """
    out: Dict[str, int] = {}
    for r in report.refusals:
        out[r.question] = out.get(r.question, 0) + 1
    return out
