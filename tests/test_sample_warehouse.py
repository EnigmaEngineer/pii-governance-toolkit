"""Re-derive the committed value sampling measurement and fail when it drifts.

`tests/test_sample.py` says the arm's rules do what they claim on a sample object built by
hand. This says the two numbers the README quotes are still the numbers the code produces
against a real database, which is the part a hand built sample cannot speak to. The shapes
come out of DuckDB's own cast to varchar here, and a cast is exactly the kind of thing that
changes under a driver upgrade without anybody choosing it.

Two readings, matching `scripts/sample_probe.py`. The shipped warehouse with the arm off
and on, and the four column fixture.

Drift means the same thing it means in `tests/test_ablation_warehouse.py`. A real change to
a threshold or a rule will move these and the fix is to regenerate and say so in the commit.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii import ablation, corpus, crawl, profile, safeharbor, sample, sample_bench  # noqa: E402
from pii import classify  # noqa: E402
from pii.classify import ALL_ARMS, ARMS_WITH_SAMPLING  # noqa: E402
from pii.schema import planted_index  # noqa: E402
from scripts.classify_probe import upstream_for  # noqa: E402
from scripts.sample_probe import ARM_SETS, sampled  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(HERE, "measurements", "value-sampling.json")

REGENERATE = ("python3 scripts/plant.py --db /tmp/pii.duckdb && "
              "python3 scripts/sample_probe.py --db /tmp/pii.duckdb "
              "--manifest measurements/value-sampling.json")

_STATE = {}


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    return path


def _warehouse():
    if "warehouse" in _STATE:
        return _STATE["warehouse"]
    from scripts.plant import load

    path = _fresh_db()
    load(path, corpus.generate(n_patients=1000, seed=20260917))

    import duckdb

    con = duckdb.connect(path, read_only=True)
    try:
        crawled = crawl.crawl(con)
        profiles = profile.profile_crawl(con, crawled)
        upstream = upstream_for(con, profiles)
        with_samples, samples = sampled(con, crawled, profiles)
        idx = planted_index()
        report = ablation.ablate(
            with_samples,
            upstream_tables=upstream,
            scope=frozenset(safeharbor.mapped_keys()),
            planted_key_for=lambda r: idx[(r.table, r.column)].category_key,
            arm_sets=ARM_SETS,
        )
    finally:
        con.close()
    os.unlink(path)
    _STATE["warehouse"] = (report, with_samples, samples)
    return _STATE["warehouse"]


def _bench():
    if "bench" in _STATE:
        return _STATE["bench"]
    import duckdb

    path = _fresh_db()
    con = duckdb.connect(path)
    try:
        loaded = sample_bench.load(con)
        table = sample_bench.BENCH
        profiles = tuple(profile.profile_column(con, table.schema, table.name, c)
                         for c in table.columns)
        samples = tuple(sample.sample_column(con, table.schema, table.name, c)
                        for c in table.columns)
    finally:
        con.close()
    os.unlink(path)
    with_samples = sample.attach(profiles, samples)
    before = classify.classify_warehouse(with_samples, arms=ALL_ARMS)
    after = classify.classify_warehouse(with_samples, arms=ARMS_WITH_SAMPLING)
    outcomes = sample_bench.grade(before, after, sample_bench.planted_key_for_bench)
    _STATE["bench"] = (loaded, before, after, outcomes, samples)
    return _STATE["bench"]


def _committed():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def check_no_shape_from_the_real_warehouse_leaks_a_value():
    _report, _profiles, samples = _warehouse()
    leaks = sample.samples_hold_no_values(samples)
    assert leaks == (), leaks


def check_every_warehouse_column_was_sampled():
    _report, profiles, samples = _warehouse()
    assert len(samples) == len(profiles)
    assert {s.address for s in samples} == {p.address for p in profiles}


def check_the_arm_moves_no_band_on_the_warehouse_it_was_built_against():
    # The headline of the 2026-10-10 measurement and the least comfortable number in it.
    # Four of forty two columns produce a sampled signal and every one of them agrees with
    # a column the name arm or the value arm already had, so turning the arm on changes no
    # decision here at all. The fixture is where it does something.
    report, _profiles, _samples = _warehouse()
    moved = report.by_label("with sampling")
    assert moved.bands_moved == 0, [m.describe() for m in moved.moves]
    baseline = report.by_label("declaration only")
    assert moved.masked == baseline.masked
    assert moved.false_alarms == baseline.false_alarms


def check_four_warehouse_columns_produce_a_sampled_signal():
    _report, profiles, _samples = _warehouse()
    fired = sorted(p.address for p in profiles if p.sampled_signals)
    assert fired == ["raw.encounter.attending_npi", "raw.patient.mrn",
                     "raw.patient.phone", "raw.patient.ssn"], fired


def check_removals_measured_is_empty_for_a_two_label_report():
    # The guard added the same day. `decides_nothing` used to raise a KeyError on a report
    # whose arm sets are not the removal set, which read as a crash rather than as an
    # unanswerable question.
    report, _profiles, _samples = _warehouse()
    assert report.removals_measured() == ()
    assert report.decides_nothing() == ()


def check_the_fixture_is_invisible_to_the_shipped_arm_set():
    # `ot-077`'s actual ask. Every fixture column scores zero with the arm off, which is
    # the ignore band, which queues nothing and masks nothing.
    _loaded, before, _after, _outcomes, _samples = _bench()
    for r in before:
        assert r.category_key == "not_personal", r.explain()
        assert r.confidence == 0.0, r.explain()
        assert r.band.value == "ignore", r.explain()


def check_the_fixture_recovers_two_columns_and_misses_one_and_is_wrong_once():
    _loaded, _before, _after, outcomes, _samples = _bench()
    tally = sample_bench.counts(outcomes)
    assert tally == {"recovered": 2, "still missed": 1, "false alarm": 1,
                     "unchanged": 1}, tally
    by_column = {o.address.split(".")[-1]: o for o in outcomes}
    assert by_column["travel_doc_ref"].after_category == "unknown_identifier"
    # The checksum names the category and the shape rule cannot, which is the only place
    # the arm returns a verdict strong enough to mask on.
    assert by_column["payer_ref"].after_category == "payment_card"
    assert by_column["payer_ref"].after_band == "accept"
    assert by_column["site_patient_ref"].verdict == "still missed"
    assert by_column["request_ref"].verdict == "false alarm"


def check_one_false_alarm_turns_the_whole_fixture_table_person_linked():
    # The second order cost. A trace id flagged as an unnamed identifier is a direct
    # identifier at review confidence, and that is the test `table_is_person_linked` runs.
    # So a single wrong column changes the verdict for every other column in the table.
    _loaded, before, after, _outcomes, _samples = _bench()
    assert classify.table_is_person_linked(before) is False
    assert classify.table_is_person_linked(after) is True


def check_the_fixture_loads_the_row_count_it_declares():
    loaded, _before, _after, _outcomes, _samples = _bench()
    assert loaded == sample_bench.N_ROWS, loaded


def check_no_fixture_shape_leaks_a_value():
    _loaded, _before, _after, _outcomes, samples = _bench()
    assert sample.samples_hold_no_values(samples) == ()


def check_the_fixture_is_not_in_the_declared_schema():
    # The reason the declared fingerprint did not move. A fixture column added to
    # `pii/schema.py` would move it and every published figure with it.
    from pii import schema
    assert sample_bench.BENCH_FQN not in schema.tables_by_fqn()
    assert all(t.schema != sample_bench.BENCH_SCHEMA for t in schema.TABLES)


def check_the_committed_measurement_is_what_the_code_produces():
    committed = _committed()
    report, _profiles, _samples = _warehouse()
    loaded, _before, _after, outcomes, _samples2 = _bench()

    produced_warehouse = report.manifest()
    if produced_warehouse != committed["warehouse"]:
        by_label = {row["label"]: row for row in committed["warehouse"]["arm_sets"]}
        for row in produced_warehouse["arm_sets"]:
            was = by_label.get(row["label"])
            if was != row:
                raise AssertionError(
                    "arm set {!r} reads {} in the manifest and the code produces {}. "
                    "Regenerate with: {}".format(row["label"], was, row, REGENERATE))
        raise AssertionError(
            "the warehouse half of the manifest drifted outside its arm set rows. "
            "Regenerate with: {}".format(REGENERATE))

    bench = committed["bench"]
    assert bench["rows"] == loaded, (bench["rows"], loaded)
    assert bench["counts"] == sample_bench.counts(outcomes), REGENERATE
    produced_columns = {o.address: o.verdict for o in outcomes}
    committed_columns = {row["address"]: row["verdict"] for row in bench["columns"]}
    assert produced_columns == committed_columns, REGENERATE
    assert committed["sample_limit"] == sample.SAMPLE_LIMIT


def check_the_three_confidences_the_waiver_rescued_are_re_derivable():
    """The numbers in the README and in the comment on `two_readings_of_one_fact`.

    They describe a state the code is no longer in, so running the classifier cannot
    reproduce them. What can be reproduced is the arithmetic that produced them, out of the
    signals the arms return today. So this replays `_decide` without the waiver over the
    real signals and checks the three published figures against the result.

    It was first written against the rounded confidences in the README and it failed by two
    ten thousandths, because the penalty is applied before the rounding and a check fed the
    published numbers is checking the wrong thing.
    """
    from pii.classify import CONFLICT_PENALTY, _combine

    _report, profiles, _samples = _warehouse()
    by_address = {p.address: p for p in profiles}
    published = {
        "raw.patient.ssn": 0.7253,
        "raw.patient.phone": 0.6984,
        "raw.patient.mrn": 0.6525,
    }
    for address, penalised in sorted(published.items()):
        signals = classify.column_signals(by_address[address], ARMS_WITH_SAMPLING)
        grouped = {}
        for sig in signals:
            grouped.setdefault(sig.category_key, []).append(sig.weight)
        scored = sorted(((k, _combine(w)) for k, w in grouped.items()),
                        key=lambda kv: (-kv[1], kv[0]))
        best_key, best = scored[0]
        runner_up, runner_up_score = scored[1]
        assert runner_up == "unknown_identifier", (address, runner_up)
        assert round(best * (1.0 - CONFLICT_PENALTY * runner_up_score), 4) == penalised, (
            address, best, runner_up_score)
        # And the waiver is what keeps the real answer above the accept threshold.
        assert classify.two_readings_of_one_fact(best_key, runner_up)
        assert round(best, 4) == classify.classify_column(
            by_address[address], ARMS_WITH_SAMPLING).confidence


def check_the_lineage_graph_has_one_builder_and_every_caller_agrees():
    """`ot-126`, closed on 2026-10-10 when a third caller appeared.

    Two probes built this graph with the same six lines and nothing compared them. This
    compares them, which is the part the deduplication does not give for free. A caller that
    drifts back to its own copy fails here rather than quietly describing a different graph.
    """
    from scripts.compliance_report import build_graph
    from scripts.plant import load
    from pii import lineage

    path = _fresh_db()
    load(path, corpus.generate(n_patients=50, seed=20260917))
    import duckdb

    con = duckdb.connect(path, read_only=True)
    try:
        one = lineage.warehouse_graph(con)
        assert build_graph(con).edges == one.edges
        tables = sorted(t.fqn for t in crawl.crawl(con).tables)
        from scripts.classify_probe import upstream_for as probe_map
        from scripts.sample_probe import upstream_for as sample_map
        profiles = profile.profile_crawl(con, crawl.crawl(con))
        assert probe_map(con, profiles) == sample_map(con, profiles)
        assert probe_map(con, profiles) == one.upstream_table_map(tables)
    finally:
        con.close()
    os.unlink(path)
