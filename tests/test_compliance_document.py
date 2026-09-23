"""Grade the committed sample report against what the code produces today.

The document in `docs/` is the only published artefact in this repo that nothing else
looks at. A README figure is at least read by somebody every day. A generated report
committed once is read by a reviewer six weeks later, and by then the only thing standing
between it and a lie is a check like this one.

It needs a real warehouse, so it lives here rather than in `tests/test_compliance.py`, and
it rebuilds the corpus with the same parameters `scripts/plant.py` defaults to. If those
defaults move, this fails, which is correct: the document was about the old ones.

Regenerating after a legitimate change is one command and it is named in the failure.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import (access, classify, compliance, corpus, coverage, crawl,  # noqa: E402
                 lineage, mask, profile, schema)
from scripts.compliance_report import (COMMAND, WINDOW_DAYS,  # noqa: E402
                                       WINDOW_END, build_graph)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(HERE, "docs", "sample-compliance-report.md")

REGENERATE = ("python3 scripts/plant.py --db /tmp/pii.duckdb && "
              "python3 scripts/compliance_report.py --db /tmp/pii.duckdb "
              "--out docs/sample-compliance-report.md")

_STATE = {}


def _report_and_text():
    if "text" in _STATE:
        return _STATE["report"], _STATE["text"]

    from scripts.plant import load

    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    # The same defaults scripts/plant.py carries. Written out rather than imported from
    # its argument parser, because a check reading the parser would move with the parser
    # and stop being a statement about the document.
    load(path, corpus.generate(n_patients=1000, seed=20260917))

    import duckdb

    con = duckdb.connect(path, read_only=True)
    try:
        crawled = crawl.crawl(con)
        profiles = profile.profile_crawl(con, crawled)
        graph = build_graph(con)
        upstream = graph.upstream_table_map(sorted({p.table for p in profiles}))
        results = classify.classify_warehouse(profiles, upstream_tables=upstream)
        policies = mask.generate(results)
        grants, members, events = corpus.generate_access(n_queries=400)
        flagged = [r.address for r in results if r.flagged]
        start = WINDOW_END - dt.timedelta(days=WINDOW_DAYS - 1)
        exposures = access.report(flagged, grants, members, events, graph,
                                  start, WINDOW_END)
        report = compliance.build(con, results, policies, exposures,
                                  coverage.grade(), generated_by=COMMAND,
                                  graph=graph)
    finally:
        con.close()
        os.unlink(path)

    _STATE["report"] = report
    _STATE["text"] = compliance.render(report)
    return report, _STATE["text"]


def check_the_committed_sample_report_is_what_the_code_produces():
    report, text = _report_and_text()
    with open(DOC, encoding="utf-8") as fh:
        committed = fh.read()
    if committed != text:
        # Name the first differing line. A diff of a fifty line document reported as
        # "not equal" sends the next person to a visual comparison, which is how a real
        # change gets waved through as a whitespace one.
        a = committed.splitlines()
        b = text.splitlines()
        for n, (left, right) in enumerate(zip(a, b), 1):
            if left != right:
                raise AssertionError(
                    "docs/sample-compliance-report.md line {} reads {!r} and the code "
                    "produces {!r}. Regenerate with: {}".format(
                        n, left, right, REGENERATE))
        raise AssertionError(
            "docs/sample-compliance-report.md has {} lines and the code produces {}. "
            "Regenerate with: {}".format(len(a), len(b), REGENERATE))
    assert report.refusals


def check_the_committed_report_names_every_refusal_it_holds():
    """The document check, run against the committed bytes rather than the fresh render.

    The check above would pass on a document and a renderer that both dropped the same
    refusal, because both sides come from one builder. This side is the file on disk.
    """
    report, _ = _report_and_text()
    with open(DOC, encoding="utf-8") as fh:
        committed = fh.read()
    assert compliance.undelivered(report, committed) == ()


def check_the_sample_report_still_refuses_more_than_it_answers():
    """Not a style rule. A regression here means something started answering.

    Every one of these refusals traces to a column sitting in the review band or to a
    query log that records no columns. If a change makes the report suddenly confident,
    the thing to check is whether the evidence improved or whether a guard was removed.
    """
    report, _ = _report_and_text()
    answered, asked = report.answer_rate()
    assert answered < asked - answered, (
        "the sample report now answers {} of {}, which is more than it refuses. "
        "Check what started answering before moving this line.".format(answered, asked))


def check_the_report_is_generated_by_a_command_that_exists():
    report, _ = _report_and_text()
    script = report.generated_by.split()[1]
    assert os.path.exists(os.path.join(HERE, script)), script


def check_the_schema_fingerprint_in_the_document_is_the_live_one():
    # A fingerprint copied into a document is a claim about a schema, and this is the one
    # figure in the report a reader would take entirely on trust.
    with open(DOC, encoding="utf-8") as fh:
        committed = fh.read()
    assert "`{}`".format(schema.fingerprint()) in committed
