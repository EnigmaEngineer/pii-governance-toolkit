"""What the obvious PII scan finds, and what it walks past.

    python3 scripts/coverage_probe.py

Three sections.

The first grades `pii.naive` against the Safe Harbor clause list. Two arms, names only and
names plus a value sample, so the value regexes get credit for what they add rather than
being folded into one score.

The second measures how many people the quasi identifiers pick out on their own, and prints
the uniform prediction beside every measurement so the reader can see which part of the
number is the data and which part is arithmetic over cardinalities.

The third is controls. Every arm above can be satisfied by something broken, so each one
has a case where the right answer is known and is not the answer the arm would give by
accident.

Every number here comes off the generated corpus in `pii/corpus.py`. Nothing in this repo
has ever seen a real patient.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import coverage, naive, reidentify, safeharbor  # noqa: E402
from pii.corpus import generate, summarise  # noqa: E402
from pii.schema import (  # noqa: E402
    PLANTED,
    check_planting_is_total,
    quasi_identifier_columns,
)
from pii.taxonomy import TAXONOMY, Granularity, Identifiability  # noqa: E402


def rule(title):
    print("\n" + title)
    print("-" * len(title))


def section_answer_key():
    rule("the answer key")

    problem = check_planting_is_total()
    print("planting is total          {}".format(problem or "yes"))
    print("taxonomy categories        {}".format(len(TAXONOMY)))
    print("taxonomy fingerprint       {}".format(TAXONOMY.fingerprint()))
    print("safe harbor clauses        {}".format(len(safeharbor.CLASSES)))
    print("clauses with no category   {}".format(
        ", ".join(c.letter for c in safeharbor.gaps()) or "none"))
    unreached = safeharbor.hipaa_keys_not_reached()
    print("hipaa tagged, no clause    {}".format(", ".join(unreached) or "none"))
    print("columns in sample schema   {}".format(len(PLANTED)))

    counts = coverage.planted_summary()
    for level in ("direct", "quasi", "sensitive", "none"):
        print("  planted {:<10} {}".format(level, counts.get(level, 0)))


def section_scan(corpus):
    rule("what the obvious scan finds")

    samples = {
        "raw.patient": tuple(corpus.patients),
        "raw.encounter": tuple(corpus.encounters),
        "raw.claim": tuple(corpus.claims),
        "raw.device_reading": tuple(corpus.readings),
    }

    names_only = coverage.grade()
    with_values = coverage.grade(samples)

    # The corpus holds the four generated tables and not the derived one, because the mart
    # is built by SQL inside the database. So every column in `analytics.encounter_daily`
    # reaches the value arm with nothing to read, and until the day column came into scope
    # the mart had one in scope column that the name arm catches anyway, which is why this
    # was invisible. A line reading "names plus values" over a table that got no values is
    # the number lying about which arm produced it. Re-deriving the mart in Python here
    # would fix the line and duplicate `DERIVED_SQL`, and a copy of the loader's statement
    # is exactly the circularity the rest of this repo refuses. So the gap is printed.
    unsampled = sorted({p.table for p in PLANTED} - set(samples))
    blind = [v for v in with_values.in_scope if v.table in unsampled]

    print("in safe harbor scope       {} of {} columns".format(
        len(with_values.in_scope), with_values.n_columns))
    print("recall, names only         {}/{}  {:.4f}".format(
        sum(1 for v in names_only.in_scope if v.naive_calls_it_personal),
        len(names_only.in_scope), names_only.recall))
    print("recall, names plus values  {}/{}  {:.4f}".format(
        sum(1 for v in with_values.in_scope if v.naive_calls_it_personal),
        len(with_values.in_scope), with_values.recall))
    print("  no value sample reached   {}, so {} in scope column{} there were graded"
          .format(", ".join(unsampled) or "nothing",
                  len(blind), "" if len(blind) == 1 else "s"))
    print("  on names alone. scripts/classify_probe.py reads its sample out of the")
    print("  database and sees all five tables, which is why its floor row is higher.")
    print("false alarms               {}".format(len(with_values.false_alarms)))
    print("flagged as wrong category  {}".format(len(with_values.wrong_category)))
    print("  recall counts those as found, because the column did get flagged. A masking")
    print("  policy keyed off the category would still apply the wrong mask to them, so")
    print("  the two counts have to be read together.")

    print("\nrecall split by what the column does on its own")
    split = with_values.by_identifiability()
    for level in ("direct", "quasi", "sensitive"):
        b = split.get(level)
        if b is None:
            print("  {:<10} nothing in scope".format(level))
            continue
        print("  {:<10} {}/{}  {:.4f}".format(
            level, b["found"], b["in_scope"], b["found"] / b["in_scope"]))

    quasi = coverage.quasi_only_report(with_values)
    print("\nthe quasi identifiers on their own, which is the half that decides the design")
    for v in quasi:
        print("  {:<40} {:<16} {}".format(
            v.address, v.planted_key,
            "flagged" if v.naive_calls_it_personal else "walked past"))

    print("\nmissed, in scope and unflagged")
    for v in with_values.missed:
        cat = TAXONOMY.get(v.planted_key)
        print("  {:<40} {:<16} {}".format(v.address, v.planted_key,
                                          cat.identifiability.value))

    if with_values.false_alarms:
        print("\nflagged but planted as not personal")
        for v in with_values.false_alarms:
            print("  {:<40} naive said {}".format(v.address, v.naive_key))

    if with_values.wrong_category:
        print("\nflagged as the wrong category")
        for v in with_values.wrong_category:
            print("  {:<40} planted {:<20} naive {}".format(
                v.address, v.planted_key, v.naive_key))

    return with_values


def section_uniqueness(corpus):
    rule("how many people the quasi identifiers pick out")

    s = summarise(corpus)
    print("patients                   {}".format(s["counts"]["raw.patient"]))
    print("distinct postal codes      {}".format(s["distinct_postal"]))
    print("distinct birth dates       {}".format(s["distinct_birth_date"]))
    print("distinct sex values        {}".format(s["distinct_sex"]))
    print("busiest postal code share  {:.6f}".format(s["postal_head_share"]))
    print("age min median max         {} {} {}".format(
        s["age_min"], s["age_median"], s["age_max"]))

    # Derived from the taxonomy rather than typed here. The first version of this held a
    # hard coded list of three column names, which is a second place for the answer to
    # live and the one nobody updates. Anything planted on the patient table as a quasi
    # identifier that really identifies at its recorded granularity is in, which is why
    # `city` is out: it is planted at the coarse end and does not identify.
    #
    # Sorted by how many distinct values each holds, cheapest first, so the sweep reads as
    # how few columns it takes rather than as an arbitrary order.
    cols = [p.column for p in quasi_identifier_columns() if p.table == "raw.patient"]
    card = reidentify.cardinalities(corpus.patients, cols)
    cols = sorted(cols, key=lambda c: card[c])
    print("\nquasi identifiers on raw.patient, derived: {}".format(", ".join(cols)))

    # The corpus writes nulls, so this sweep has to say which population it
    # is about. It is the rows carrying all three columns. A null is missing data and not
    # anonymity, and counting it as a cell moves every figure below in a direction that
    # depends on the null rate rather than on anybody being harder to find. That
    # comparison is measured in scripts/crawl_probe.py and is not folded in here.
    people = reidentify.complete_rows(corpus.patients, cols)
    print("rows carrying all three    {} of {}, {} dropped".format(
        len(people), len(corpus.patients), len(corpus.patients) - len(people)))

    print("\n{:<34} {:>7} {:>9} {:>10} {:>10} {:>10}".format(
        "quasi identifiers", "k", "cells", "measured", "uniform", "gap"))
    for u in reidentify.sweep(people, cols):
        used = " + ".join(cols[:u.n_columns])
        print("{:<34} {:>7} {:>9} {:>10.4f} {:>10.4f} {:>+10.4f}".format(
            used, u.k_anonymity, u.cardinality_product,
            u.measured_unique_share, u.uniform_unique_share, u.shape_effect))

    print("\nthe same three after truncating the postal code and the date")
    coarse = []
    for p in people:
        coarse.append({
            "sex": p["sex"],
            "postal_code": reidentify.generalise_postal(p["postal_code"], 3),
            "birth_date": reidentify.coarsen_date_to_year(p["birth_date"]),
        })
    u = reidentify.measure(coarse, cols)
    print("{:<34} {:>7} {:>9} {:>10.4f} {:>10.4f} {:>+10.4f}".format(
        "sex + postal 3 + birth year", u.k_anonymity, u.cardinality_product,
        u.measured_unique_share, u.uniform_unique_share, u.shape_effect))

    # The same population as the table above, which means the rows that carry a postal
    # code. Handed the whole corpus it counted the patients missing one as a sixth three
    # digit area of 54 people, so the clause B question was being asked about a group that
    # is not a geographic area at all. A null is not a place.
    floor = safeharbor.postal_3_floor(
        tuple(p["postal_code"] for p in people))
    print("\nis that row evidence about Safe Harbor  {}".format(
        "yes" if floor.applies else "no"))
    print("  three digit areas                    {}".format(floor.groups))
    print("  smallest and largest                 {} and {}".format(
        floor.smallest_group, floor.largest_group))
    print("  areas over the {} person floor    {}".format(
        floor.floor, floor.groups_meeting_floor))
    if not floor.applies:
        print("  {}".format(floor.why_not()))
        print("  So the row above is arithmetic about this corpus. Read it as the cost")
        print("  of truncation on a small population and not as a result about the rule.")


def section_controls(corpus):
    rule("controls")

    ok = True

    # A scan that flags nothing must score zero, or the recall arm is measuring the
    # denominator rather than the scan.
    blank = coverage.Report(
        verdicts=tuple(
            coverage.ColumnVerdict(v.table, v.column, v.planted_key, "not_personal",
                                   v.in_safe_harbor_scope,
                                   v.identifies_at_recorded_granularity)
            for v in coverage.grade().verdicts
        ),
        taxonomy_fingerprint=TAXONOMY.fingerprint(),
    )
    print("a scan flagging nothing scores       {:.4f}   expected 0.0000".format(
        blank.recall))
    ok = ok and blank.recall == 0.0

    # And a scan that flags everything must score one, which is the other end and is the
    # reason recall is printed beside a false alarm count rather than on its own.
    everything = coverage.Report(
        verdicts=tuple(
            coverage.ColumnVerdict(v.table, v.column, v.planted_key, "person_name",
                                   v.in_safe_harbor_scope,
                                   v.identifies_at_recorded_granularity)
            for v in coverage.grade().verdicts
        ),
        taxonomy_fingerprint=TAXONOMY.fingerprint(),
    )
    print("a scan flagging everything scores    {:.4f}   expected 1.0000".format(
        everything.recall))
    ok = ok and everything.recall == 1.0
    print("  and its false alarms                {:<8} expected {}".format(
        len(everything.false_alarms),
        len(coverage.columns_planted_as("not_personal"))))
    ok = ok and len(everything.false_alarms) == len(
        coverage.columns_planted_as("not_personal"))

    # The uniform predictor must not be reading the data. Hand it a population of
    # identical rows and the measured share falls to zero while the prediction does not
    # move, because the prediction only knows cardinalities.
    same = [{"sex": "F", "postal_code": "10101", "birth_date": corpus.patients[0]["birth_date"]}
            for _ in corpus.patients]
    u = reidentify.measure(same, ["sex", "postal_code", "birth_date"])
    print("identical rows, measured unique      {:.4f}   expected 0.0000".format(
        u.measured_unique_share))
    print("identical rows, k                    {:<8} expected {}".format(
        u.k_anonymity, len(corpus.patients)))
    ok = ok and u.measured_unique_share == 0.0 and u.k_anonymity == len(corpus.patients)

    # A granularity threshold that does nothing would leave this True at year precision.
    birth = TAXONOMY.get("birth_date")
    at_year = birth.identifies(Granularity.YEAR)
    at_day = birth.identifies(Granularity.DAY)
    print("birth_date identifies at year        {:<8} expected False".format(str(at_year)))
    print("birth_date identifies at day         {:<8} expected True".format(str(at_day)))
    ok = ok and at_year is False and at_day is True

    # The name heuristic must be reading the name. A column called nothing in its list
    # has to come back not personal even though its values are obviously email addresses,
    # which is what separates the two arms.
    only_values = naive.classify("contact_handle",
                                 tuple(p["email"] for p in corpus.patients[:20]))
    only_name = naive.classify_by_name("contact_handle")
    print("unlisted name, values seen           {:<8} expected email".format(only_values))
    print("unlisted name, name only             {:<8} expected None".format(str(only_name)))
    ok = ok and only_values == "email" and only_name is None

    print("\ncontrols {}".format("clean" if ok else "FAILED"))
    return ok


def main():
    corpus = generate()
    section_answer_key()
    report = section_scan(corpus)
    section_uniqueness(corpus)
    ok = section_controls(corpus)

    rule("read this before quoting anything above")
    print("Every figure in the uniqueness section is a figure about pii/corpus.py.")
    print("The recall figures are about a sample schema I wrote, graded against a")
    print("clause list I did not. Those are two different strengths of evidence.")
    print("Taxonomy fingerprint {} over {} columns.".format(
        report.taxonomy_fingerprint, report.n_columns))

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
