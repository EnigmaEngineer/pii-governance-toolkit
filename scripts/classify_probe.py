"""Measure the classifier against the floor, and measure the confidence separately.

    python3 scripts/plant.py --db /tmp/pii.duckdb
    python3 scripts/classify_probe.py --db /tmp/pii.duckdb

Recall is the number everybody reaches for and it is the weakest thing here. I wrote the
token rules with the answer key open, so a high recall on these forty two columns is a
report on my own memory rather than on the classifier. It is printed because leaving it out
would look like hiding it, and every arm below exists to say something recall cannot.

What the arms do say:

The floor comparison is the do nothing number. A classifier that beats nothing is not a
result.

The band table asks whether the confidence separates right from wrong, which is the only
question that matters once a low score sends a column to a person instead of to a masking
rule. A confidence that is uniformly high is a constant wearing a score's clothes.

The margin is the distance between the lowest column the classifier masks without asking
and the highest column it gets wrong. That number is the resolution of the whole idea and
it is small.

The ablation removes one arm at a time and counts how many band assignments move. An arm
that moves none of them is not contributing to any decision, whatever it does to the
number printed beside the column.

The obfuscated arm rebuilds the same warehouse with the column names replaced by position
labels and runs the whole thing again. Same rows, same types, same everything except the
one signal the classifier leans on.

The null sweep rebuilds the corpus at rising null rates. The floor drops nulls before
taking its majority and never says so, so a column that is mostly empty scores exactly like
a full one. Here the match rate and the share of the column it was computed over are two
fields and both are printed.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pii.classify as classify  # noqa: E402
from pii import classify, coverage, crawl, lineage, naive, profile, safeharbor  # noqa: E402
from pii.corpus import generate  # noqa: E402
from pii import schema  # noqa: E402
from pii.schema import PLANTED, TABLES, planted_index  # noqa: E402
from pii.taxonomy import TAXONOMY  # noqa: E402

RULE = "-" * 96


def connect(path: str, read_only: bool = True):
    import duckdb
    return duckdb.connect(path, read_only=read_only)


# The lineage graph the classifier's second round reads, set once in `main`. A module
# level binding rather than an argument threaded through nine sections, because the
# alternative was nine signature changes to pass one thing that never varies within a run.
UPSTREAM = None


def classify_all(profiles, **kw):
    """Every section classifies through here, so no section can drift off the graph.

    The ablation and the sweep both used to call `classify_warehouse` directly. Once the
    second round existed, a section that kept calling it directly would publish a recall
    figure from a configuration the tool does not ship.
    """
    return classify.classify_warehouse(profiles, upstream_tables=UPSTREAM, **kw)


def upstream_for(con, profiles):
    cols = [c.name for c in
            schema.tables_by_fqn()["analytics.encounter_daily"].columns]
    derived = lineage.read_insert_select(
        schema.DERIVED_SQL["analytics.encounter_daily"], cols)
    keys = lineage.Graph(edges=lineage.foreign_key_edges(con, crawl.ENGINE_SCHEMAS))
    graph = derived.merge(keys)
    return graph.upstream_table_map(sorted({p.table for p in profiles}))


def classified(con):
    global UPSTREAM
    crawled = crawl.crawl(con)
    profiles = profile.profile_crawl(con, crawled)
    UPSTREAM = upstream_for(con, profiles)
    return profiles, classify_all(profiles)


def scope_keys():
    return frozenset(safeharbor.mapped_keys())


def grade(results, key_for):
    """Split the results against the two answer keys this repo has.

    Two keys rather than one, and mixing them produces a wrong number quietly. Recall is
    counted over the Safe Harbor scope, because that list was written by somebody else and
    it is what makes the denominator defensible. A false alarm cannot be counted the same
    way. A column outside Safe Harbor scope is not thereby harmless, since `sex` is a quasi
    identifier this corpus re-identifies people with and a diagnosis is a GDPR special
    category, and neither is on the Safe Harbor list. So the only thing that can say a
    column is not personal is the planted label, and the planted label is mine.
    """
    scope = scope_keys()
    in_scope = [r for r in results if key_for(r) in scope]
    return {
        "in_scope": in_scope,
        "flagged": [r for r in in_scope if r.flagged],
        "auto": [r for r in in_scope if r.auto_masked],
        "right_category": [r for r in in_scope
                           if r.flagged and r.category_key == key_for(r)],
        "false_alarms": [r for r in results
                         if r.flagged and key_for(r) == "not_personal"],
    }


def naive_arm(con):
    """The floor, run over rows read out of the database the way it was written to be.

    It samples values, which is the thing the new classifier refuses to do, so this is also
    the only place in the repo that pulls personal data into the process. Kept because a
    comparison against a floor nobody ran is not a comparison.
    """
    samples = {}
    for fqn in sorted({t.fqn for t in TABLES}):
        rows = con.execute("SELECT * FROM {} LIMIT 50".format(fqn)).fetchall()
        names = [d[0] for d in con.description]
        samples[fqn] = tuple(dict(zip(names, r)) for r in rows)
    return coverage.grade(samples)


def section_floor(con, results, key_for):
    print("\nTHE FLOOR AND THE CLASSIFIER")
    print(RULE)
    floor = naive_arm(con)
    g = grade(results, key_for)
    n = len(g["in_scope"])
    print("{:<34} {:>12} {:>12}".format("", "substring floor", "classifier"))
    print("{:<34} {:>12} {:>12}".format(
        "in Safe Harbor scope, found",
        "{}/{}".format(sum(1 for v in floor.in_scope if v.naive_calls_it_personal), n),
        "{}/{}".format(len(g["flagged"]), n)))
    print("{:<34} {:>12} {:>12}".format(
        "right category", "{}/{}".format(
            n - len(floor.wrong_category) - len(floor.missed), n),
        "{}/{}".format(len(g["right_category"]), n)))
    print("{:<34} {:>12} {:>12}".format(
        "masked with no human in the loop", "all of them",
        "{}/{}".format(len(g["auto"]), n)))
    print("{:<34} {:>12} {:>12}".format(
        "flagged and planted not personal",
        str(len(floor.false_alarms)), str(len(g["false_alarms"]))))
    print("\nthe floor has no confidence, so every answer it gives carries the same")
    print("authority and the whole column set is masked or none of it is")
    return floor


def section_bands(results, key_for):
    print("\nWHAT THE CONFIDENCE SEPARATES")
    print(RULE)
    scope = scope_keys()
    rows = [r for r in results if r.flagged]
    print("{:<46} {:<22} {:>6} {:<8} {}".format(
        "column", "called", "conf", "band", "planted"))
    for r in sorted(rows, key=lambda x: (x.band.value, -x.confidence)):
        planted = key_for(r)
        note = "" if planted == r.category_key else "  planted {}".format(planted)
        if planted == "not_personal":
            note = "  NOT PERSONAL"
        print("{:<46} {:<22} {:>6.2f} {:<8}{}".format(
            r.address, r.category_key, r.confidence, r.band.value, note))

    accept = [r for r in results if r.auto_masked]
    review = [r for r in results if r.band is classify.Band.REVIEW]
    wrong = [r for r in results if r.flagged and key_for(r) == "not_personal"]
    counts = classify.band_counts(results)
    print("\naccept band  {:>2} columns, {} of them planted not personal".format(
        counts["accept"], sum(1 for r in accept if key_for(r) == "not_personal")))
    print("review band  {:>2} columns, {} of them planted not personal".format(
        counts["review"], sum(1 for r in review if key_for(r) == "not_personal")))
    print("ignored      {:>2} columns, {} of them in Safe Harbor scope".format(
        counts["ignore"],
        sum(1 for r in results if not r.flagged and key_for(r) in scope)))
    if accept and wrong:
        low = min(r.confidence for r in accept)
        high = max(r.confidence for r in wrong)
        print("\nlowest column masked with no human    {:.4f}".format(low))
        print("highest column the classifier got wrong {:.4f}".format(high))
        print("margin                                 {:.4f}".format(low - high))
        print("\nI chose the accept threshold with both of those numbers on the screen.")
        print("The margin is a fact about forty two columns and not about the method.")


def section_threshold_sweep(results, key_for):
    print("\nACCEPT THRESHOLD SWEEP")
    print(RULE)
    print("{:>9} {:>9} {:>9} {:>10}".format("accept at", "masked", "correct", "mistakes"))
    first_clean = None
    for t in (0.40, 0.50, 0.60, 0.70, 0.72, 0.73, 0.75, 0.80, 0.90, 0.95):
        masked = [r for r in results
                  if r.category_key != "not_personal" and r.confidence >= t]
        bad = sum(1 for r in masked if key_for(r) == "not_personal")
        if bad == 0 and first_clean is None:
            first_clean = t
        print("{:>9.2f} {:>9} {:>9} {:>10}".format(
            t, len(masked), len(masked) - bad, bad))
    # Read off the sweep rather than written into the sentence, because the sweep is the
    # thing that moves when a rule changes and a hand typed value beside it does not.
    print("\nthe shipped threshold is {:.2f} and the lowest clean one on this sweep"
          " is {:.2f}".format(classify.ACCEPT_AT, first_clean))


def section_ablation(profiles, key_for):
    print("\nARM ABLATION")
    print(RULE)
    real = (classify.name_signals, classify.value_signals, classify.structure_signals)
    base = {r.address: r.band for r in classify_all(profiles)}
    scope = scope_keys()

    def run(name=True, value=True, structure=True):
        classify.name_signals = real[0] if name else (lambda n: ())
        classify.value_signals = real[1] if value else (lambda p: ())
        classify.structure_signals = real[2] if structure else (lambda p: ())
        try:
            return classify_all(profiles)
        finally:
            (classify.name_signals, classify.value_signals,
             classify.structure_signals) = real

    print("{:<22} {:>9} {:>7} {:>8} {:>13}".format(
        "arms", "found", "masked", "wrong", "bands moved"))
    arms = (
        ("all three", {}),
        ("name only", dict(value=False, structure=False)),
        ("value only", dict(name=False, structure=False)),
        ("structure only", dict(name=False, value=False)),
        ("without name", dict(name=False)),
        ("without value", dict(value=False)),
        ("without structure", dict(structure=False)),
    )
    for label, kwargs in arms:
        res = run(**kwargs)
        ins = [r for r in res if key_for(r) in scope]
        moved = sum(1 for r in res if r.band is not base[r.address])
        print("{:<22} {:>9} {:>7} {:>8} {:>13}".format(
            label,
            "{}/{}".format(sum(1 for r in ins if r.flagged), len(ins)),
            sum(1 for r in ins if r.auto_masked),
            sum(1 for r in res if r.flagged and key_for(r) == "not_personal"),
            moved))
    print("\na band that never moves is a decision the arm did not take part in")


def obfuscated_db(source: str, target: str) -> dict:
    """Rebuild the warehouse with the column names replaced by position labels.

    Same rows and same types and same keys and same nulls. The only thing removed is the
    thing the name arm reads. Done against a real database rather than by stubbing the arm out,
    because a comparison you assemble yourself in Python is a comparison you can rig
    without noticing, and this one also runs the profiler over names it has never seen.
    """
    import duckdb

    if os.path.exists(target):
        os.remove(target)
    mapping = {}
    con = duckdb.connect(target)
    try:
        con.execute("ATTACH '{}' AS src (READ_ONLY)".format(source))
        for schema in sorted({t.schema for t in TABLES}):
            con.execute("CREATE SCHEMA IF NOT EXISTS {}".format(schema))
        for t in TABLES:
            cols = []
            for i, c in enumerate(t.columns, start=1):
                alias = "c{:02d}".format(i)
                mapping[(t.fqn, alias)] = c.name
                cols.append('"{}" AS {}'.format(c.name, alias))
            con.execute("CREATE TABLE {} AS SELECT {} FROM src.{}".format(
                t.fqn, ", ".join(cols), t.fqn))
    finally:
        con.close()
    return mapping


def section_reach(results, key_for):
    """What the taxonomy declares against what anything can actually return."""
    print("\nWHAT THE TAXONOMY DECLARES AND WHAT THE CLASSIFIER CAN RETURN")
    print(RULE)
    planted_keys = {p.category_key for p in PLANTED}
    coverage_map = classify.rule_coverage()
    fired = set()
    for r in results:
        for s in r.signals:
            fired.add(s.category_key)

    print("{:<24} {:>9} {:>10} {:>11} {:>8}".format(
        "category", "a column", "name rule", "value rule", "fired"))
    for key in sorted(coverage_map):
        if key == "not_personal":
            continue
        by_name, by_value = coverage_map[key]
        print("{:<24} {:>9} {:>10} {:>11} {:>8}".format(
            key,
            "yes" if key in planted_keys else "no",
            "yes" if by_name else "no",
            "yes" if by_value else "no",
            "yes" if key in fired else "no"))

    undetectable = classify.undetectable_categories()
    unreached = sorted(set(TAXONOMY.keys()) - planted_keys - {"not_personal"})
    declared_only = sorted(set(undetectable) & set(unreached))
    print("\n{} of {} categories have no column in this warehouse".format(
        len(unreached), len(TAXONOMY)))
    print("{} of those also have no rule in any arm, so nothing can return them".format(
        len(declared_only)))
    rule_but_no_column = sorted(set(unreached) - set(undetectable))
    print("{} has a rule and no column, which is the one worth a fixture: {}".format(
        len(rule_but_no_column), ", ".join(rule_but_no_column) or "none"))
    print("\na rule that has never fired is the easiest kind of coverage to fake. It")
    print("looks reasonable, it is reachable, and nothing has ever run it.")


def section_obfuscated(args, key_for):
    print("\nTHE SAME WAREHOUSE WITH THE COLUMN NAMES TAKEN AWAY")
    print(RULE)
    target = args.db + ".obfuscated"
    mapping = obfuscated_db(args.db, target)
    con = connect(target)
    try:
        _profiles, results = classified(con)
    finally:
        con.close()
    idx = planted_index()

    def planted_for_obfuscated(r):
        return idx[(r.table, mapping[(r.table, r.column)])].category_key

    g = grade(results, planted_for_obfuscated)
    n = len(g["in_scope"])
    print("in Safe Harbor scope, found        {}/{}".format(len(g["flagged"]), n))
    print("right category                     {}/{}".format(len(g["right_category"]), n))
    print("masked with no human in the loop   {}/{}".format(len(g["auto"]), n))
    print("flagged and planted not personal   {}".format(len(g["false_alarms"])))
    print("\nstill found, on values and types alone:")
    for r in sorted(g["flagged"], key=lambda x: -x.confidence):
        real_name = mapping[(r.table, r.column)]
        planted = planted_for_obfuscated(r)
        note = "" if planted == r.category_key else "  planted {}".format(planted)
        print("  {:<28} was {:<22} called {:<22} {:.2f}{}".format(
            r.address, real_name, r.category_key, r.confidence, note))
    print("\nten digits is ten digits. A licence number and a telephone number are the")
    print("same shape and only the name told them apart.")
    os.remove(target)


def section_nulls(args):
    print("\nWHAT A RISING NULL RATE DOES TO THE VALUE ARM")
    print(RULE)
    from scripts.plant import load

    print("raw.patient.email on the left, the whole warehouse on the right\n")
    print("{:>10} {:>9} {:>11} {:>9} {:>9} {:>13} {:>12} {:>9}".format(
        "null rate", "non null", "match rate", "support", "weight",
        "bands moved", "accept band", "emptiest"))
    baseline = None
    total_moved = 0
    for rate in (0.00, 0.02, 0.10, 0.30, 0.60):
        path = "{}.nulls{:02d}".format(args.db, int(rate * 100))
        if os.path.exists(path):
            os.remove(path)
        load(path, generate(n_patients=args.patients, seed=args.seed, null_rate=rate))
        con = connect(path)
        try:
            crawled = crawl.crawl(con)
            column = next(t for t in crawled.tables
                          if t.fqn == "raw.patient").column("email")
            p = profile.profile_column(con, "raw", "patient", column)
            everything_profiled = profile.profile_crawl(con, crawled)
            everything = classify.classify_warehouse(everything_profiled)
        finally:
            con.close()
        os.remove(path)
        bands = {r.address: r.band for r in everything}
        if baseline is None:
            baseline = bands
        moved = sum(1 for k in bands if bands[k] is not baseline[k])
        total_moved = max(total_moved, moved)
        sig = next((s for s in classify.value_signals(p)
                    if s.category_key == "email"), None)
        rate_text = "{:.4f}".format(p.pattern_hits["email"] / p.non_null)
        weight_text = "under floor" if sig is None else "{:.4f}".format(sig.weight)
        # The worst column in the warehouse, beside the one being followed. A reviewer
        # reading a queue entry has no other way to know how much of the column the
        # answer came off, and the headline column is not the hardest case.
        emptiest = max(profile.null_rate_table(everything_profiled).values())
        print("{:>10.2f} {:>9} {:>11} {:>9.4f} {:>9} {:>13} {:>12} {:>9.4f}".format(
            rate, p.non_null, rate_text, p.support, weight_text, moved,
            sum(1 for r in everything if r.auto_masked), emptiest))
    print("\nthe match rate is over the populated values because a missing value has no")
    print("shape to match. The support carries the rest of the column into the weight,")
    print("so a claim about a half empty column arrives worth half as much.")
    print("\nemptying sixty percent of every column moves {} band in {} columns. The".format(
        total_moved, len(baseline)))
    print("name arm carries almost every decision here and a column name does not go")
    print("null, so the arm the missing values damage is the one deciding least.")


def section_context(profiles, key_for):
    print("\nTHE TABLE CONTEXT BAR")
    print(RULE)
    print("a temporal column is an identifier when the date belongs to a person, and no")
    print("property of the column says whether it does. The stand in is whether anything")
    print("else in the table names somebody outright.\n")
    scope = scope_keys()
    print("{:>4} {:>9} {:>8} {:>8}  {}".format(
        "bar", "found", "masked", "wrong", "tables treated as about people"))
    for bar in (0.35, 0.60, 0.70, 0.75, 0.90):
        res = classify_all(profiles, evidence_bar=bar)
        ins = [r for r in res if key_for(r) in scope]
        by_table = {}
        for r in res:
            by_table.setdefault(r.table, []).append(r)
        linked = sorted(t for t, rs in by_table.items()
                        if classify.table_is_person_linked(
                            tuple(classify.classify_column(p) for p in profiles
                                  if p.table == t), bar))
        print("{:>4.2f} {:>9} {:>8} {:>8}  {}".format(
            bar,
            "{}/{}".format(sum(1 for r in ins if r.flagged), len(ins)),
            sum(1 for r in ins if r.auto_masked),
            sum(1 for r in res if r.flagged and key_for(r) == "not_personal"),
            len(linked)))
    print("\nat 0.75 raw.claim stops counting as a table about people, because its only")
    print("direct identifier is a membership number scoring 0.70, and submitted_on then")
    print("loses its only evidence. One column in the review band deleting another")
    print("column's answer is why the bar sits at the review floor.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Grade the classifier and its confidence.")
    ap.add_argument("--db", default="/tmp/pii.duckdb")
    ap.add_argument("--patients", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260917)
    ap.add_argument("--skip-nulls", action="store_true",
                    help="skip the null sweep, which rebuilds the corpus five times")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("no database at {}. Run scripts/plant.py first.".format(args.db))
        return 2

    con = connect(args.db)
    try:
        profiles, results = classified(con)
        leaks = profile.profiles_hold_no_values(profiles)
        print("taxonomy {} over {} categories, {} planted columns".format(
            TAXONOMY.fingerprint(), len(TAXONOMY), len(PLANTED)))
        print("profiled {} columns, values that reached this process: {}".format(
            len(profiles), len(leaks)))
        idx = planted_index()

        def key_for(r):
            return idx[(r.table, r.column)].category_key

        section_floor(con, results, key_for)
    finally:
        con.close()

    section_bands(results, key_for)
    section_threshold_sweep(results, key_for)
    section_ablation(profiles, key_for)
    section_context(profiles, key_for)
    section_reach(results, key_for)
    section_obfuscated(args, key_for)
    if not args.skip_nulls:
        section_nulls(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
