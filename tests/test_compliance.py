from __future__ import annotations

from pii import access, compliance, coverage, mask, safeharbor
from pii.access import Exposure
from pii.classify import Arm, Band, Classification, Signal
from pii.compliance import DOES_NOT_CLOSE, Refusal, Report, Section
from pii.lineage import ColumnRef, Edge, EdgeKind, Graph


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def _result(table, column, key, confidence, signals=None):
    if signals is None:
        signals = (Signal(Arm.NAME, key, 0.9, "stub"),)
    return Classification(table, column, key, confidence, tuple(signals))


def _refusal(subject="raw.patient.city", closes="a reviewer answers it"):
    return Refusal(
        question="is this column personal data",
        subject=subject,
        because="it scored inside the band",
        closes_when=closes,
    )


class _Cursor:
    """Twelve lines standing in for a duckdb connection.

    The residual section is the only part of this module that touches a database, and the
    entry point of anything that needs a real dependency is the function least likely to be
    tested. That was a mutation survivor on an earlier project of mine and it is cheaper to
    stub than to argue about.
    """

    def __init__(self, row):
        self.row = row
        self.statements = []

    def execute(self, sql):
        self.statements.append(sql)
        return self

    def fetchone(self):
        return self.row


# Refusal


def check_a_refusal_with_an_empty_field_is_refused():
    for field in ("question", "subject", "because", "closes_when"):
        kwargs = {"question": "q", "subject": "s", "because": "b", "closes_when": "c"}
        kwargs[field] = "   "
        assert _raises(lambda k=kwargs: Refusal(**k), ValueError)


def check_a_refusal_naming_a_route_out_is_closeable():
    assert _refusal().closeable


def check_a_refusal_carrying_the_sentinel_is_not_closeable():
    assert not _refusal(closes=DOES_NOT_CLOSE).closeable


def check_the_line_offers_a_route_out_only_when_there_is_one():
    # Both branches, because the sentinel was added after two clauses got a promise of a
    # taxonomy change that would not have helped either of them.
    assert "Closes when a reviewer answers it." in _refusal().line()
    assert "Nothing closes this." in _refusal(closes=DOES_NOT_CLOSE).line()
    assert "Closes when" not in _refusal(closes=DOES_NOT_CLOSE).line()


def check_the_line_names_its_subject():
    assert "`raw.patient.city`" in _refusal().line()


# Counting


def _report(sections):
    return Report("cmd", "fp", "tfp", tuple(sections))


def check_asked_counts_rows_and_refusals_together():
    s = Section("h", "i", ("a", "b"), (_refusal(),))
    assert s.asked == 3
    r = _report([s])
    assert r.answer_rate() == (2, 3)


def check_a_section_with_no_refusals_still_counts_its_rows():
    # A fixture where every section refuses something cannot catch a count that reads the
    # refusals twice, so one section here has none.
    r = _report([Section("h", "i", ("a",)), Section("h2", "i", (), (_refusal(),))])
    assert r.answer_rate() == (1, 2)


def check_unclosable_is_the_subset_with_no_route_out():
    r = _report([Section("h", "i", (), (
        _refusal("a"), _refusal("b", DOES_NOT_CLOSE), _refusal("c", DOES_NOT_CLOSE)))])
    assert len(r.refusals) == 3
    assert tuple(x.subject for x in r.unclosable) == ("b", "c")


def check_refusals_are_grouped_by_question_not_by_subject():
    s = Section("h", "i", (), (_refusal("a"), _refusal("b"), Refusal(
        "what is the smallest group left", "raw.claim", "unresolved", "a decision")))
    counts = compliance.refusals_by_question(_report([s]))
    assert counts == {"is this column personal data": 2,
                      "what is the smallest group left": 1}


# The document graded against the data


def check_every_refusal_survives_rendering():
    r = _report([Section("h", "i", ("row",), (_refusal("raw.patient.city"),
                                              _refusal("raw.claim.payer_name")))])
    assert compliance.undelivered(r, compliance.render(r)) == ()


def check_a_renderer_that_drops_refusals_is_caught():
    # The control. Without it the check above passes on a renderer that has never lost
    # anything, which is a check that has never seen a failure.
    r = _report([Section("h", "i", ("row",), (_refusal("raw.patient.city"),
                                              _refusal("raw.claim.payer_name")))])
    missing = compliance.undelivered(r, compliance.render(r, drop_refusals=True))
    assert missing == ("raw.claim.payer_name", "raw.patient.city")


def check_grading_a_report_that_refused_nothing_raises():
    # Otherwise the strongest check in this module passes by comparing an empty tuple
    # against a document, which is the shape of every check here that has ever been wrong.
    r = _report([Section("h", "i", ("row",))])
    assert _raises(lambda: compliance.undelivered(r, compliance.render(r)), ValueError)


def check_the_header_separates_refusals_that_close_from_those_that_do_not():
    r = _report([Section("h", "i", (), (_refusal("a"), _refusal("b", DOES_NOT_CLOSE)))])
    assert "2 refused, and 1 of those close when somebody does something" in (
        compliance.render(r))


def check_a_section_with_no_rows_says_so_rather_than_rendering_an_empty_block():
    r = _report([Section("h", "i", (), (_refusal(),))])
    assert "nothing this section can state" in compliance.render(r)


def check_a_section_with_rows_does_not_claim_it_had_none():
    r = _report([Section("h", "i", ("a row",), (_refusal(),))])
    assert "nothing this section can state" not in compliance.render(r)


# Sections built from real objects


def _classified():
    """Three columns, one per band. The review one is the quasi identifier.

    The residual section needs a quasi column to have a set to measure over, and it needs
    that column to be the undecided one for the refusal branch to fire at all. A fixture
    where the review column is a direct identifier tests neither.
    """
    return (
        _result("raw.patient", "email", "email", 0.99),
        _result("raw.patient", "postal_code", "postal_code", 0.60),
        _result("raw.patient", "patient_id", "not_personal", 0.0, signals=()),
    )


def check_the_classification_section_refuses_exactly_the_review_band():
    s = compliance.classification_section(_classified())
    assert tuple(r.subject for r in s.refusals) == ("raw.patient.postal_code",)
    assert [c.band for c in _classified()] == [Band.ACCEPT, Band.REVIEW, Band.IGNORE]


def check_the_scope_section_counts_personal_columns_per_table():
    s = compliance.scope_section(_classified())
    assert len(s.rows) == 1
    assert "3 columns,  2 in a personal category" in s.rows[0]


def check_the_residual_section_turns_the_refusal_into_a_record():
    policies = mask.generate(_classified())
    s = compliance.residual_section(_Cursor(None), policies)
    assert s.rows == ()
    assert len(s.refusals) == 1
    assert s.refusals[0].subject == "raw.patient"
    assert "raw.patient.postal_code is unresolved" in s.refusals[0].because
    assert s.refusals[0].closeable


def check_the_residual_section_reports_a_k_when_the_table_resolves():
    # The other branch. Only one column in review, so resolving it leaves a measurable
    # table, and without this arm the section is only ever tested on its failure path.
    resolved = [mask.resolve_review(p, True) if p.action is mask.Action.REVIEW else p
                for p in mask.generate(_classified())]
    s = compliance.residual_section(_Cursor((4, 10, 0, 40)), resolved)
    assert s.refusals == ()
    assert len(s.rows) == 1
    assert "k     4" in s.rows[0]
    assert "10 groups" in s.rows[0]


def check_the_residual_section_takes_the_weight_off_the_lineage_graph():
    """No weight table any more. The graph is the only place that fact is recorded.

    The mart shape, built here so the section is exercised on a table that really is pre
    aggregated. Without the graph argument this counts rows, which on a mart reports the
    table as safer by exactly the factor its GROUP BY deduplicated by.
    """
    resolved = [mask.resolve_review(p, True) if p.action is mask.Action.REVIEW else p
                for p in mask.generate(_classified())]
    graph = Graph(edges=(Edge(ColumnRef("raw.encounter", "*"),
                              ColumnRef("raw.patient", "encounters"),
                              EdgeKind.AGGREGATE, "row count"),))
    cur = _Cursor((4, 10, 0, 40))
    compliance.residual_section(cur, resolved, graph)
    assert 'sum("encounters")' in cur.statements[0]


def check_the_residual_section_counts_rows_when_no_graph_is_given():
    # The other branch, and the reason `graph` has no useful default. A caller who omits
    # it gets row counting, which is correct for raw tables and wrong for a mart.
    resolved = [mask.resolve_review(p, True) if p.action is mask.Action.REVIEW else p
                for p in mask.generate(_classified())]
    cur = _Cursor((4, 10, 0, 40))
    compliance.residual_section(cur, resolved)
    # The group size expression specifically. The statement carries a `sum(CASE WHEN ...)`
    # for the singleton count either way, so a bare search for `sum(` proves nothing.
    assert "count(*) AS s" in cur.statements[0]
    assert "sum(\"encounters\") AS s" not in cur.statements[0]


def _exposure(address, named=(), starred=()):
    return Exposure(
        address=address, start=None, end=None,
        direct=("u1",), through_a_carrier=(), carriers=(),
        named_the_column=tuple(named), named_a_carrier=(),
        star_over_a_holder=tuple(starred))


def check_the_access_section_refuses_a_column_only_a_star_reached():
    answered = _exposure("raw.patient.email", named=("u1",))
    starred = _exposure("raw.patient.ssn", starred=("u2",))
    s = compliance.access_section((answered, starred))
    assert tuple(r.subject for r in s.refusals) == ("raw.patient.ssn",)
    assert len(s.rows) == 1
    assert "raw.patient.email" in s.rows[0]
    assert set(access.unanswerable((answered, starred))) == {"raw.patient.ssn"}


def check_the_access_refusal_says_one_user_rather_than_one_users():
    s = compliance.access_section((_exposure("raw.patient.ssn", starred=("u2",)),))
    assert "1 user ran a star" in s.refusals[0].because
    s2 = compliance.access_section(
        (_exposure("raw.patient.ssn", starred=("u2", "u3")),))
    assert "2 users ran a star" in s2.refusals[0].because


def check_a_column_nobody_touched_at_all_is_answered_and_not_refused():
    # Zero and zero is an answer. Only a floor of zero under a ceiling above it is not,
    # and folding the two together would refuse most of a quiet warehouse.
    s = compliance.access_section((_exposure("raw.patient.ssn"),))
    assert s.refusals == ()
    assert len(s.rows) == 1


def check_the_clause_gaps_are_refused_with_no_route_out():
    s = compliance.coverage_section(coverage.grade())
    assert s.refusals
    assert all(not r.closeable for r in s.refusals)
    assert all(r.subject.startswith("Safe Harbor clause ") for r in s.refusals)


def check_the_coverage_section_states_the_discount_beside_the_recall():
    s = compliance.coverage_section(coverage.grade())
    assert any("answer key open" in row for row in s.rows)


def check_the_recall_numerator_is_the_columns_the_scan_actually_found():
    """Both halves of the fraction, asserted against the graded report.

    A mutation pass put this here. The numerator was a `sum(1 for ...)` that nothing read,
    so a mutant summing twos published a recall of twenty out of twenty and every check
    stayed green. A figure in a report row is the worst place for an ungraded mutant,
    because the row is the part somebody quotes.
    """
    graded = coverage.grade()
    scope = graded.in_scope
    found = sum(1 for v in scope if v.naive_calls_it_personal)
    s = compliance.coverage_section(graded)
    assert "{} of {} in scope columns".format(found, len(scope)) in s.rows[1]
    assert found < len(scope), "the floor scan is not supposed to find all of them"


def check_the_clause_row_counts_the_clauses_that_reach_a_category():
    """The other survivor. The subtraction was a plus away from reading 20 of 18."""
    s = compliance.coverage_section(coverage.grade())
    total = len(safeharbor.CLASSES)
    reached = total - len(safeharbor.gaps())
    assert reached < total
    assert "{} published Safe Harbor clauses, {} of them reach a category".format(
        total, reached) in s.rows[0]


# Immutability. Three mutants flipped `frozen=True` and nothing noticed, because no code
# path here assigns to one of these after construction. The report is passed between six
# builders and a renderer, and a frozen dataclass is what stops any of them editing a
# refusal on the way past.


def check_a_refusal_cannot_be_edited_after_it_is_built():
    r = _refusal()
    assert _raises(lambda: setattr(r, "because", "something else"), Exception)


def check_a_section_cannot_be_edited_after_it_is_built():
    s = Section("h", "i", ("a",))
    assert _raises(lambda: setattr(s, "rows", ()), Exception)


def check_a_report_cannot_be_edited_after_it_is_built():
    r = _report([Section("h", "i", ("a",))])
    assert _raises(lambda: setattr(r, "sections", ()), Exception)
