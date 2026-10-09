"""Re-derive the committed arm ablation and fail when it drifts.

`tests/test_ablation.py` says the harness is sound. This says the number it produced on the
sample warehouse is still the number in `measurements/arm-ablation.json`, which is what
turns a measurement into something a README can quote. The old ablation was a print loop.
It ran when somebody ran the probe, nothing compared its output to anything, and the table
pasted into the README was true on the day it was pasted.

It needs a real warehouse, so it runs under `tests/run_with_duckdb.py`.

What drift means here. A legitimate change to a token rule or a threshold will move these
numbers and the fix is to regenerate the manifest and say so in the commit. The check is not
claiming the numbers are correct. It is claiming that nobody changed them without noticing,
which is the failure this repo has already had once: the README's own table said the
structure arm moved one band while the paragraph under it said the arm moved none.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import ablation, corpus, crawl, profile, safeharbor  # noqa: E402
from pii.schema import planted_index  # noqa: E402
from scripts.classify_probe import upstream_for  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(HERE, "measurements", "arm-ablation.json")

REGENERATE = ("python3 scripts/plant.py --db /tmp/pii.duckdb && "
              "python3 scripts/classify_probe.py --db /tmp/pii.duckdb --skip-nulls "
              "--ablation-manifest measurements/arm-ablation.json")

_STATE = {}


def _report():
    """The ablation over a freshly planted warehouse.

    The graph comes from the probe's own `upstream_for`, so this re-derives what the probe
    publishes rather than something close to it. It does not go through the probe's module
    level `UPSTREAM` binding, which is the thing the arm set parameter was added to avoid.
    """
    if "report" in _STATE:
        return _STATE["report"]

    from scripts.plant import load

    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    # The same defaults scripts/plant.py carries, written out rather than read off its
    # argument parser. A check that followed the parser would stop being a statement about
    # the committed manifest.
    load(path, corpus.generate(n_patients=1000, seed=20260917))

    import duckdb

    con = duckdb.connect(path, read_only=True)
    try:
        profiles = profile.profile_crawl(con, crawl.crawl(con))
        upstream = upstream_for(con, profiles)
        idx = planted_index()
        report = ablation.ablate(
            profiles,
            upstream_tables=upstream,
            scope=frozenset(safeharbor.mapped_keys()),
            planted_key_for=lambda r: idx[(r.table, r.column)].category_key,
        )
    finally:
        con.close()
        os.unlink(path)

    _STATE["report"] = report
    return report


def _committed():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def check_the_committed_manifest_is_what_the_code_produces():
    produced = _report().manifest()
    committed = _committed()
    if produced == committed:
        return
    # Name the first row that differs. "not equal" on a seven row manifest sends the next
    # person to a visual comparison, which is how a real change gets read as a reordering.
    by_label = {row["label"]: row for row in committed["arm_sets"]}
    for row in produced["arm_sets"]:
        was = by_label.get(row["label"])
        if was is None:
            raise AssertionError(
                "arm set {!r} is not in the manifest. Regenerate with: {}".format(
                    row["label"], REGENERATE))
        if was != row:
            raise AssertionError(
                "arm set {!r} reads {} in the manifest and the code produces {}. "
                "Regenerate with: {}".format(row["label"], was, row, REGENERATE))
    raise AssertionError(
        "the manifest and the code disagree outside the arm set rows: {} against {}. "
        "Regenerate with: {}".format(committed.get("decides_nothing"),
                                     produced.get("decides_nothing"), REGENERATE))


def check_no_arm_of_this_classifier_is_free_to_delete_on_this_warehouse():
    """The finding, as a check rather than as a paragraph.

    Every arm moves at least one band, so there is no arm whose removal is free. This is the
    claim the README got wrong, and it is the one a reader is most likely to act on, because
    an arm that decides nothing is an arm somebody deletes.

    If a later change really does make an arm free, this fails and the right response is to
    delete that arm and then delete this check. It is not a check that defends three arms.
    It is a check that refuses a silent change of the answer.
    """
    report = _report()
    assert report.decides_nothing() == (), (
        "these arms now move no band and should be deleted rather than shipped: "
        "{}".format([a.value for a in report.decides_nothing()]))


def check_the_structure_arm_decides_exactly_the_column_round_two_exists_for():
    """The one band the structure arm moves, named.

    `analytics.encounter_daily.day` is a patient's admission date cast to a day. It carries
    no token the name arm knows and no value the predicates can see, so the catalog type is
    its entire evidence. Round two was built to stop deleting that evidence. Take the
    structure arm away and there is nothing left for round two to rescue, so the repo's
    flagship lineage result produces no flagged column at all.

    That is why the count of one was worth naming. One band moved reads like a rounding
    detail and this one is the column the design is organised around.
    """
    reading = _report().by_label("without structure")
    assert reading.moved_addresses == ("analytics.encounter_daily.day",), \
        reading.moved_addresses
    move = reading.moves[0]
    assert move.from_band.value == "review"
    assert move.to_band.value == "ignore"
    assert move.to_category == "not_personal"


def check_the_name_arm_is_the_one_the_warehouse_turns_on():
    """The part of the original finding that survives.

    Sixteen of forty two bands move when the name arm goes, against two for the value arm
    and one for the structure arm. The confidence really is mostly a restatement of the
    token list. The claim that needed correcting was never this one.
    """
    report = _report()
    name = report.by_label("without name").bands_moved
    assert name > report.by_label("without value").bands_moved
    assert name > report.by_label("without structure").bands_moved
    assert report.by_label("without name").found < report.by_label("all three").found


README = os.path.join(HERE, "README.md")


def check_the_readme_table_is_the_one_the_harness_generates():
    """The README's own table, compared against the measurement character for character.

    This is the check the repo needed and did not have. The table was pasted in by hand and
    the paragraph under it contradicted the row above it for three weeks. A generated table
    does not stop somebody writing a wrong paragraph. It does stop the table drifting away
    from the code while still looking authoritative.

    The comparison is on the stripped table body, so reflowing the surrounding prose is
    free and changing a number is not.
    """
    produced = _report().table().splitlines()
    with open(README, encoding="utf-8") as fh:
        lines = [line.rstrip("\n") for line in fh]

    try:
        head = lines.index(produced[0])
    except ValueError:
        raise AssertionError(
            "no line in README.md matches the generated table header {!r}. "
            "Regenerate with: {}".format(produced[0], REGENERATE))

    committed = lines[head:head + len(produced)]
    for n, (left, right) in enumerate(zip(committed, produced)):
        if left != right:
            raise AssertionError(
                "README.md line {} reads {!r} and the harness produces {!r}. "
                "Regenerate with: {}".format(head + n + 1, left, right, REGENERATE))
