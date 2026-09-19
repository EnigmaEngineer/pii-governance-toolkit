from __future__ import annotations

import datetime as dt

from pii import reidentify


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def check_a_population_where_everybody_is_alone_reports_every_row_unique():
    rows = [{"a": i} for i in range(10)]
    u = reidentify.measure(rows, ["a"])
    assert u.unique_rows == 10
    assert u.measured_unique_share == 1.0
    assert u.k_anonymity == 1
    assert u.distinct_combinations == 10


def check_a_population_where_everybody_is_identical_reports_none_unique():
    rows = [{"a": 1} for _ in range(10)]
    u = reidentify.measure(rows, ["a"])
    assert u.unique_rows == 0
    assert u.measured_unique_share == 0.0
    assert u.k_anonymity == 10
    assert u.distinct_combinations == 1


def check_a_mixed_population_counts_only_the_rows_sitting_alone():
    # Four rows. Two share a value, two do not. So two unique rows and k of 1.
    rows = [{"a": 1}, {"a": 1}, {"a": 2}, {"a": 3}]
    u = reidentify.measure(rows, ["a"])
    assert u.unique_rows == 2
    assert u.measured_unique_share == 0.5
    assert u.k_anonymity == 1
    assert u.distinct_combinations == 3


def check_zero_rows_and_zero_columns_are_both_refused():
    assert _raises(lambda: reidentify.measure([], ["a"]), ValueError)
    assert _raises(lambda: reidentify.measure([{"a": 1}], []), ValueError)


def check_a_column_the_rows_do_not_have_raises_rather_than_being_skipped():
    assert _raises(lambda: reidentify.measure([{"a": 1}], ["b"]), KeyError)


def check_the_uniform_prediction_reads_only_the_cardinalities():
    # Two different populations with the same row count and the same cardinality product
    # must get the same prediction, because the prediction knows nothing about the data.
    a = [{"x": i % 4} for i in range(40)]
    b = [{"x": 0 if i < 37 else i % 4} for i in range(40)]
    ua = reidentify.measure(a, ["x"])
    ub = reidentify.measure(b, ["x"])
    assert ua.cardinality_product == ub.cardinality_product == 4
    assert ua.uniform_unique_share == ub.uniform_unique_share
    assert ua.measured_unique_share != ub.measured_unique_share


def check_the_uniform_prediction_matches_the_closed_form_on_both_sides_of_one_row():
    assert reidentify.uniform_unique_share(0, 10) == 0.0
    assert reidentify.uniform_unique_share(1, 10) == 1.0
    assert reidentify.uniform_unique_share(2, 10) == 0.9
    assert abs(reidentify.uniform_unique_share(3, 10) - 0.81) < 1e-12
    assert reidentify.uniform_unique_share(5, 1) == 0.0


def check_the_uniform_prediction_refuses_nonsense_inputs():
    assert _raises(lambda: reidentify.uniform_unique_share(-1, 10), ValueError)
    assert _raises(lambda: reidentify.uniform_unique_share(10, 0), ValueError)


def check_the_prediction_falls_as_the_population_grows_against_a_fixed_product():
    a = reidentify.uniform_unique_share(100, 1000)
    b = reidentify.uniform_unique_share(500, 1000)
    assert a > b > 0.0


def check_the_shape_effect_is_the_difference_and_reaches_both_signs():
    # It has to reach both signs or it is not carrying information, and the fixtures have
    # to be built for each one. The first version of this asserted a negative on four rows
    # over three values, which is a long tail rather than a cluster and comes out
    # positive at +0.2037. Both are asserted now and the arithmetic is checked in each.
    long_tail = [{"a": 1}, {"a": 1}, {"a": 2}, {"a": 3}]
    u = reidentify.measure(long_tail, ["a"])
    assert u.shape_effect == u.measured_unique_share - u.uniform_unique_share
    assert u.measured_unique_share == 0.5
    assert u.shape_effect > 0, "a long tail must read as easier to pick out"

    # Twelve rows spread evenly over three values. Nobody is alone, so the measurement
    # is zero while the prediction is not.
    even = [{"a": i % 3} for i in range(12)]
    e = reidentify.measure(even, ["a"])
    assert e.shape_effect == e.measured_unique_share - e.uniform_unique_share
    assert e.measured_unique_share == 0.0
    assert e.uniform_unique_share > 0.0
    assert e.shape_effect < 0, "an even spread must read as harder to pick out"


def check_the_sweep_adds_one_column_at_a_time_in_the_order_given():
    rows = [{"a": i % 2, "b": i % 3, "c": i} for i in range(12)]
    out = reidentify.sweep(rows, ["a", "b", "c"])
    assert [u.n_columns for u in out] == [1, 2, 3]
    assert [u.cardinality_product for u in out] == [2, 6, 72]
    # Adding a column can never make anybody less identifiable.
    shares = [u.measured_unique_share for u in out]
    assert shares == sorted(shares), shares
    assert shares[-1] == 1.0


def check_the_cardinality_product_is_the_product_and_not_the_sum():
    rows = [{"a": i % 5, "b": i % 7} for i in range(35)]
    u = reidentify.measure(rows, ["a", "b"])
    assert u.cardinality_product == 35


def check_generalising_a_postal_code_keeps_the_width_and_loses_the_detail():
    assert reidentify.generalise_postal("10199", 3) == "101**"
    assert reidentify.generalise_postal("10199", 5) == "10199"
    assert reidentify.generalise_postal("10199", 1) == "1****"


def check_generalising_refuses_to_widen_rather_than_quietly_doing_nothing():
    # A generaliser that returns the value unchanged when asked for more digits than it
    # has is how a masking policy reports success on a column it never touched.
    assert _raises(lambda: reidentify.generalise_postal("101", 5), ValueError)
    assert _raises(lambda: reidentify.generalise_postal("10199", 0), ValueError)


def check_generalising_really_reduces_uniqueness_on_a_case_built_to_show_it():
    rows = [{"z": "1010{}".format(i)} for i in range(9)]
    before = reidentify.measure(rows, ["z"])
    coarse = [{"z": reidentify.generalise_postal(r["z"], 3)} for r in rows]
    after = reidentify.measure(coarse, ["z"])
    assert before.measured_unique_share == 1.0
    assert after.measured_unique_share == 0.0
    assert after.k_anonymity == 9


def check_coarsening_a_date_returns_its_year():
    assert reidentify.coarsen_date_to_year(dt.date(1984, 7, 2)) == 1984
    assert reidentify.coarsen_date_to_year(dt.datetime(1984, 7, 2, 13, 5)) == 1984


def check_the_uniqueness_result_is_frozen():
    u = reidentify.measure([{"a": 1}], ["a"])
    try:
        u.n_rows = 99
    except Exception:
        return
    raise AssertionError("Uniqueness is not frozen")


def check_cardinalities_counts_distinct_values_per_column():
    rows = [{"a": 1, "b": "x"}, {"a": 1, "b": "y"}, {"a": 2, "b": "y"}]
    assert reidentify.cardinalities(rows, ["a", "b"]) == {"a": 2, "b": 2}
    assert _raises(lambda: reidentify.cardinalities(rows, []), ValueError)


# The corpus writes nulls now, so every function here meets one. A null is
# missing data and it is not anonymity, and these are the checks that stop the module
# from quietly treating it as either a zero or a category.


def check_a_generaliser_refuses_a_null_by_message_and_not_by_accident():
    # Assert the message, not the type. len(None) raises TypeError on its own and
    # value.year raises AttributeError on its own, so deleting either guard would leave a
    # test asserting "it raises" perfectly green. That is the 08-02 lesson arriving on a
    # different module.
    def says(fn):
        try:
            fn()
        except Exception as exc:
            return "{}: {}".format(type(exc).__name__, exc)
        return "did not raise"

    postal = says(lambda: reidentify.generalise_postal(None, 3))
    assert postal.startswith("ValueError"), postal
    assert "null" in postal, postal

    date = says(lambda: reidentify.coarsen_date_to_year(None))
    assert date.startswith("ValueError"), date
    assert "null" in date, date


def check_complete_rows_keeps_only_the_rows_carrying_every_column():
    rows = [
        {"a": 1, "b": "x"},
        {"a": None, "b": "x"},
        {"a": 1, "b": None},
        {"a": None, "b": None},
        {"a": 2, "b": "y"},
    ]
    assert len(reidentify.complete_rows(rows, ["a", "b"])) == 2
    assert len(reidentify.complete_rows(rows, ["a"])) == 3
    assert len(reidentify.complete_rows(rows, ["b"])) == 3


def check_a_null_is_counted_as_a_cell_by_the_naive_measure():
    # Not a bug report, a statement of what Counter does, pinned so that the difference
    # between the two figures has a cause a reader can check. Three rows sharing a null
    # postal code are one cell of three and none of them counts as unique.
    rows = ([{"pc": None}] * 3) + [{"pc": "a"}, {"pc": "b"}]
    u = reidentify.measure(rows, ["pc"])
    assert u.distinct_combinations == 3
    assert u.unique_rows == 2
    assert u.k_anonymity == 1


def check_the_null_effect_reports_both_populations_and_their_sizes():
    rows = ([{"pc": None, "sex": "F"}] * 4) + [
        {"pc": "a", "sex": "F"}, {"pc": "b", "sex": "M"}, {"pc": "c", "sex": "M"}]
    e = reidentify.measure_with_null_effect(rows, ["pc", "sex"])
    assert e.n_rows == 7
    assert e.n_complete == 3
    assert e.n_incomplete == 4
    assert e.all_rows.n_rows == 7
    assert e.complete_only.n_rows == 3


def check_the_null_effect_refuses_a_population_with_no_complete_row():
    # Nothing to check is a finding. If every row is missing something there is no honest
    # figure to compare the naive one against, and returning the naive one alone would be
    # the worst available answer.
    rows = [{"pc": None, "sex": "F"}, {"pc": None, "sex": "M"}]
    assert _raises(lambda: reidentify.measure_with_null_effect(rows, ["pc", "sex"]),
                   ValueError)


def check_a_rare_null_makes_a_row_read_more_unique_not_less():
    # The direction that contradicts the obvious reading, built as a fixture rather than
    # asserted off the corpus. Four people share a postal code, one of them is missing it.
    # That one becomes the only member of the null cell, so the naive figure counts it as
    # unique when nothing about them changed.
    rows = [{"pc": "a"}, {"pc": "a"}, {"pc": "a"}, {"pc": None}]
    e = reidentify.measure_with_null_effect(rows, ["pc"])
    assert e.all_rows.measured_unique_share == 0.25
    assert e.complete_only.measured_unique_share == 0.0
    assert e.flattery < 0, e.flattery


def check_a_common_null_makes_a_row_read_less_unique():
    # And the other direction, which is the one people expect. Three people missing the
    # postal code pool into one cell, and the two who have it stay unique.
    rows = [{"pc": None}, {"pc": None}, {"pc": None}, {"pc": "a"}, {"pc": "b"}]
    e = reidentify.measure_with_null_effect(rows, ["pc"])
    assert e.all_rows.measured_unique_share == 0.4
    assert e.complete_only.measured_unique_share == 1.0
    assert e.flattery > 0, e.flattery


def check_the_sign_of_the_null_effect_is_reported_as_changing_only_when_it_does():
    rare = reidentify.measure_with_null_effect(
        [{"pc": "a"}, {"pc": "a"}, {"pc": "a"}, {"pc": None}], ["pc"])
    common = reidentify.measure_with_null_effect(
        [{"pc": None}, {"pc": None}, {"pc": None}, {"pc": "a"}, {"pc": "b"}], ["pc"])
    assert reidentify.flattery_changes_sign([rare, common]) is True
    assert reidentify.flattery_changes_sign([rare, rare]) is False
    assert reidentify.flattery_changes_sign([common]) is False
    # A zero is not a direction, so a run of them is not a sign change.
    flat = reidentify.measure_with_null_effect([{"pc": "a"}, {"pc": "b"}], ["pc"])
    assert flat.flattery == 0.0
    assert reidentify.flattery_changes_sign([flat, flat]) is False
    assert reidentify.flattery_changes_sign([flat, rare]) is False


def check_a_published_measurement_cannot_be_edited_after_the_fact():
    # frozen=True on Uniqueness mutated to frozen=False and survived. These objects are
    # what the README quotes, so a caller being able to rewrite a share after it was
    # measured is the one thing the dataclass is protecting against.
    u = reidentify.measure([{"a": 1}, {"a": 2}], ["a"])
    assert _raises(lambda: setattr(u, "measured_unique_share", 0.0), Exception)
    assert _raises(lambda: setattr(u, "n_rows", 99), Exception)
    e = reidentify.measure_with_null_effect([{"a": 1}, {"a": None}], ["a"])
    assert _raises(lambda: setattr(e, "n_complete", 0), Exception)
