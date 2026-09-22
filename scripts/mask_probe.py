"""Generate the masking policy, apply it, and measure what is left.

    python3 scripts/mask_probe.py --db /tmp/pii.duckdb

The third section is the reason this script exists. Sections one and two report what the
policy says, which is a restatement of the classification in different words. Section three
applies the policy and counts how many people are still alone in their group afterwards, and
that is the only part that can contradict anything.

It does contradict something. The mart comes back safe and it is not, because the set the
policy measures k over is the set the classifier found, and the classifier missed a column.
That is printed rather than explained away.

No value from a user column reaches this process. Every number below arrives as a count out
of a GROUP BY, same rule as `pii/profile.py`.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import classify, crawl, lineage, mask, profile, schema  # noqa: E402
from pii.schema import PLANTED  # noqa: E402
from pii.taxonomy import TAXONOMY, Identifiability  # noqa: E402

RULE = "-" * 96

# The mart stores one row per group and `encounters` says how many admissions that row
# stands for. Counting rows there would report the table as safer than it is by exactly the
# factor its GROUP BY deduplicated by.
WEIGHTS = {"analytics.encounter_daily": "encounters"}


def rule(title):
    print("\n{}\n{}".format(title, RULE))


def planted_key(address):
    for p in PLANTED:
        if "{}.{}".format(p.table, p.column) == address:
            return p.category_key
    raise KeyError(address)


def section_policy(policies):
    rule("WHAT THE POLICY SAYS")
    counts = mask.action_counts(policies)
    for action in ("redact", "generalise", "retain", "review"):
        print("  {:<12} {}".format(action, counts[action]))

    print("\nthe columns whose values change")
    for p in policies:
        if p.changes_the_value:
            print("  {:<42} {:<12} {}".format(
                p.address, p.action.value,
                mask.masking_expression(p)))

    gaps = mask.threshold_disagreements()
    print("\nwhere the clause list is stricter than my own taxonomy")
    for g in gaps:
        print("  {}".format(g))
    if not gaps:
        print("  nowhere, which would mean the taxonomy was written from the clause list")


def section_absence(policies):
    rule("RETAINED ON AN ABSENCE OF EVIDENCE")
    absent = mask.retained_on_absence(policies)
    print("{} of {} columns are kept because no rule fired, not because anything is "
          "known".format(len(absent), len(policies)))
    print("this is what the no values rule costs, as a number rather than as a caveat.")
    print("a format nobody wrote a predicate for lands here looking exactly like a")
    print("column that was genuinely cleared\n")
    wrong = []
    for address in absent:
        planted = planted_key(address)
        flag = "" if planted == "not_personal" else "  <- planted {}".format(planted)
        if flag:
            wrong.append(address)
        print("  {:<44}{}".format(address, flag))
    print("\n{} of them are planted as something personal".format(len(wrong)))


def section_residual(con, policies):
    rule("WHAT IS LEFT AFTER THE POLICY IS APPLIED")
    print("k is the smallest group on the masked quasi set. k of 1 means somebody is")
    print("alone in their group and the masking did not protect them.\n")
    print("  {:<30} {:>4} {:>8} {:>10}  {}".format(
        "table", "k", "groups", "alone", "quasi set the policy measured over"))

    by_table = {}
    for p in policies:
        by_table.setdefault(p.table, []).append(p)

    residuals = []
    for table in sorted(by_table):
        ps = by_table[table]
        try:
            r = mask.measure_residual(con, table, ps, weight=WEIGHTS.get(table))
        except ValueError as exc:
            print("  {:<30} {}".format(table, str(exc).split(". ")[0]))
            continue
        residuals.append(r)
        print("  {:<30} {:>4} {:>8} {:>9.4f}  {}".format(
            r.table, r.k, r.groups, r.alone_share, ", ".join(r.columns)))

    blocked = len(by_table) - len(residuals)
    risky = mask.compliant_but_identifiable(residuals)
    print("\n{} of {} tables cannot be measured at all until somebody works the review "
          "queue".format(blocked, len(by_table)))
    print("that is the honest state of this report and it is not a bug. a k computed")
    print("around an undecided column is a k for a policy nobody has agreed to.")
    print("{} of the {} measurable are under k of 2".format(len(risky), len(residuals)))

    # The same tables again, with a reviewer accepting every queued column as personal.
    # Labelled as a hypothesis rather than a result, because no reviewer ran.
    rule("THE SAME TABLES IF A REVIEWER ACCEPTED EVERY QUEUED COLUMN")
    print("nobody reviewed anything. this is the upper bound on what the queue can buy,")
    print("and it is here because the table above is mostly blank without it.\n")
    print("  {:<30} {:>4} {:>8} {:>10}  {}".format(
        "table", "k", "groups", "alone", "quasi set"))
    for table in sorted(by_table):
        ps = [mask.resolve_review(p, True) if p.action is mask.Action.REVIEW else p
              for p in by_table[table]]
        try:
            r = mask.measure_residual(con, table, ps, weight=WEIGHTS.get(table))
        except ValueError as exc:
            print("  {:<30} {}".format(table, str(exc).split(". ")[0]))
            continue
        print("  {:<30} {:>4} {:>8} {:>9.4f}  {}".format(
            r.table, r.k, r.groups, r.alone_share, ", ".join(r.columns)))
    return residuals


def section_the_set_is_the_problem(con, policies):
    """The measurement that contradicts the one above it.

    The residual figure is a statement about the set the classifier chose. This re-runs it
    over sets the classifier did not choose, and the numbers move a long way.
    """
    rule("THE ANSWER DEPENDS ON WHICH COLUMNS COUNT AS QUASI")
    print("the mart, masked the same way each time, adding one column at a time\n")

    grain = 'substr(cast("postal_code" as varchar), 1, 3)'
    variants = (
        ("what the policy measured", grain),
        ("plus the day, generalised", 'year("day"), ' + grain),
        ("plus department", 'year("day"), ' + grain + ', "department"'),
    )
    print("  {:<30} {:>4} {:>8} {:>10}".format("quasi set", "k", "groups", "alone"))
    for label, group_by in variants:
        row = con.execute(
            "SELECT min(s), count(*), sum(CASE WHEN s = 1 THEN 1 ELSE 0 END), sum(s) "
            "FROM (SELECT sum(encounters) AS s FROM analytics.encounter_daily "
            "GROUP BY {})".format(group_by)).fetchone()
        k, groups, alone, population = row
        print("  {:<30} {:>4} {:>8} {:>9.4f}".format(
            label, k, groups, alone / population))

    print("\ndepartment is planted not personal, the classifier agrees, and it is on no")
    print("clause list anywhere. it still takes the table from k of 20 to k of 1.")
    print("a residual risk number is a claim about a set somebody chose, and here the")
    print("set came from a classifier that had already missed a column.")


def section_unmasked_comparison(con):
    rule("WHAT THE MASKING ACTUALLY BOUGHT")
    print("raw.patient, the classic quasi set, before and after\n")
    print("  {:<40} {:>4} {:>8} {:>10}".format("columns", "k", "groups", "alone"))
    for label, group_by in (
        ("postal_code, birth_date, sex", '"postal_code", "birth_date", "sex"'),
        ("masked: postal3, birth year, sex",
         'substr(cast("postal_code" as varchar), 1, 3), year("birth_date"), "sex"'),
    ):
        row = con.execute(
            "SELECT min(c), count(*), sum(CASE WHEN c = 1 THEN 1 ELSE 0 END), sum(c) "
            "FROM (SELECT count(*) AS c FROM raw.patient GROUP BY {})".format(
                group_by)).fetchone()
        k, groups, alone, population = row
        print("  {:<40} {:>4} {:>8} {:>9.4f}".format(
            label, k, groups, alone / population))
    print("\nevery generalisation the clause list permits, applied in full, and k is")
    print("still 1. satisfying Safe Harbor and protecting the people in the table are")
    print("different properties and this is the gap between them.")


def main():
    ap = argparse.ArgumentParser(description="Generate, apply and grade the masking policy.")
    ap.add_argument("--db", default="/tmp/pii.duckdb")
    args = ap.parse_args()

    import duckdb

    con = duckdb.connect(args.db, read_only=True)
    try:
        crawled = crawl.crawl(con)
        profiles = profile.profile_crawl(con, crawled)
        # The policy is the shipped artefact, so it is generated from the shipped
        # classifier configuration, which since day 6 reads the lineage graph.
        cols = [c.name for c in
                schema.tables_by_fqn()["analytics.encounter_daily"].columns]
        graph = lineage.read_insert_select(
            schema.DERIVED_SQL["analytics.encounter_daily"], cols).merge(
                lineage.Graph(edges=lineage.foreign_key_edges(
                    con, crawl.ENGINE_SCHEMAS)))
        upstream = graph.upstream_table_map(sorted({p.table for p in profiles}))
        results = classify.classify_warehouse(profiles, upstream_tables=upstream)
        policies = mask.generate(results)

        print("policy over {} columns, taxonomy {}".format(
            len(policies), TAXONOMY.fingerprint()))
        held = sum(1 for p in policies
                   if TAXONOMY.get(p.category_key).identifiability
                   is not Identifiability.NONE)
        print("{} of them are in a personal category".format(held))

        section_policy(policies)
        section_absence(policies)
        section_residual(con, policies)
        section_the_set_is_the_problem(con, policies)
        section_unmasked_comparison(con)
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
