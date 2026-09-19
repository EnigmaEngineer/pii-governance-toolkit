"""Checks for the profiler, which is the only thing here that runs SQL over user rows.

These need a real database. The value predicates are SQL strings, so nothing in the
standard library suite can tell whether one of them parses, let alone whether it matches
what its name says. A predicate that is never executed is a regex in a tuple.

`payment_card` gets a fixture of its own here. No column in the sample warehouse is a
payment card, so its token rule and its predicate had both never fired anywhere, which is
the most flattering kind of coverage there is: a rule that looks reasonable, is reachable,
and has never run.
"""

from __future__ import annotations

import duckdb

from pii import classify, profile
from pii.classify import ColumnProfile
from pii.schema import Column


def _con():
    return duckdb.connect(":memory:")


def _load(con, table, sql_type, values):
    con.execute("CREATE SCHEMA IF NOT EXISTS t")
    con.execute('CREATE OR REPLACE TABLE t."{}" (v {})'.format(table, sql_type))
    if values:
        con.executemany('INSERT INTO t."{}" VALUES (?)'.format(table),
                        [(v,) for v in values])
    return profile.profile_column(con, "t", table, Column("v", sql_type))


def check_every_value_predicate_executes():
    # A predicate that does not parse is invisible until the day a column reaches it.
    #
    # The first fixture here was ["x", "y"] and the sex predicate matched, because `x` is
    # a real gender coding and it is in the vocabulary. That is not a defect in the
    # predicate and it is worth leaving written down: any single character categorical
    # column will read as a sex column on values alone, and the only things standing
    # between that and a false alarm are the name arm and the review band.
    con = _con()
    p = _load(con, "anything", "VARCHAR", ["alpha", "bravo", None])
    for key, _expr, _s, _w in classify.VALUE_PREDICATES:
        assert key in p.pattern_hits, key
        assert p.pattern_hits[key] == 0, key
    assert classify.value_signals(p) == ()


def check_each_predicate_matches_its_own_shape_and_not_the_others():
    con = _con()
    cases = {
        "email": ["ada.lovelace@example.org", "b.c@x.co.uk"],
        "national_id": ["123-45-6789", "000-11-2222"],
        "ip_address": ["10.0.0.1", "192.168.1.254"],
        "phone": ["+15551234567", "555 123 4567"],
        "payment_card": ["4111111111111111", "4111 1111 1111 1111"],
        "sex": ["M", "female"],
    }
    for key, values in cases.items():
        p = _load(con, "shape_" + key, "VARCHAR", values)
        assert p.pattern_hits[key] == len(values), (key, p.pattern_hits)
        keys = [s.category_key for s in classify.value_signals(p)]
        assert key in keys, (key, keys)


def check_the_payment_card_rule_fires_on_a_card_even_though_no_column_is_one():
    # Both arms, since a token rule and a predicate can each be wrong on their own.
    con = _con()
    p = _load(con, "card", "VARCHAR",
              ["4111111111111111", "5500005555555559", "340000000000009"])
    assert [s.category_key for s in classify.value_signals(p)] == ["payment_card"]
    named = classify.name_signals("card_number")
    assert [s.category_key for s in named] == ["payment_card"]
    assert "payment_card" not in classify.undetectable_categories()


def check_a_single_letter_category_reads_as_a_sex_column_on_values_alone():
    # The cost of a vocabulary the classifier brings with it. Asserted rather than
    # avoided, because a reader deciding whether to trust the value arm on a warehouse of
    # coded columns needs to know this, and a comment is not a measurement.
    con = _con()
    p = _load(con, "grade", "VARCHAR", ["M", "F", "M", "F", "X"])
    assert p.pattern_hits["sex"] == 5
    assert [s.category_key for s in classify.value_signals(p)] == ["sex"]
    # The name arm is what stops it becoming a false alarm, and it only demotes the column
    # to the review band rather than clearing it.
    assert classify.classify_column(p).band is classify.Band.REVIEW


def check_a_date_column_is_not_a_telephone_number():
    # The defect in the floor, asserted against the database rather than against a Python
    # copy of the pattern. Eight digits once the dashes come off, and a phone needs ten.
    con = _con()
    p = _load(con, "submitted", "DATE", ["2026-01-05", "2025-12-31", "2024-06-30"])
    assert p.pattern_hits["phone"] == 0
    assert classify.value_signals(p) == ()


def check_a_ten_digit_licence_number_does_look_like_a_telephone_number():
    # The honest other half. The predicate is not wrong, the shapes really are the same,
    # and the name arm is what separates them.
    con = _con()
    p = _load(con, "npi", "VARCHAR", ["1234567890", "1987654321", "1122334455"])
    assert p.pattern_hits["phone"] == 3
    result = classify.classify_column(
        ColumnProfile(table="raw.encounter", column="attending_npi", sql_type="VARCHAR",
                      nullable=True, is_key=False, rows=p.rows, non_null=p.non_null,
                      distinct=p.distinct, mean_length=p.mean_length,
                      pattern_hits=p.pattern_hits))
    assert result.category_key == "licence_number"
    assert result.runner_up == "phone"
    assert result.band is classify.Band.REVIEW


def check_a_null_never_satisfies_a_predicate():
    con = _con()
    p = _load(con, "half_empty", "VARCHAR",
              ["a@b.co", None, "c@d.co", None, "e@f.co", None])
    assert p.rows == 6 and p.non_null == 3
    assert p.pattern_hits["email"] == 3
    assert abs(p.support - 0.5) < 1e-9
    sig = [s for s in classify.value_signals(p) if s.category_key == "email"][0]
    # The rate is over the three values that exist and the support carries the other
    # three into the weight. Counting a null as a failure would put the rate at 0.5 and
    # drop the column under the floor, so a half empty column of email addresses would
    # never be flagged at all.
    assert abs(sig.strength - 0.90) < 1e-9
    assert abs(sig.weight - 0.45) < 1e-9


def check_a_column_that_is_entirely_null_produces_nothing():
    con = _con()
    p = _load(con, "all_null", "VARCHAR", [None, None, None])
    assert p.non_null == 0 and p.distinct == 0 and p.support == 0.0
    assert classify.value_signals(p) == ()
    assert classify.classify_column(p).category_key == "not_personal"


def check_an_empty_table_profiles_rather_than_dividing_by_zero():
    con = _con()
    p = _load(con, "empty", "VARCHAR", [])
    assert p.rows == 0 and p.non_null == 0
    assert p.support == 0.0 and p.distinct_ratio == 0.0
    assert p.mean_length == 0.0
    assert classify.value_signals(p) == ()


def check_the_profiler_survives_a_hostile_column_name():
    # The identifier quoting path, exercised through the profiler rather than trusted.
    # A governance tool is pointed at a warehouse somebody else named.
    con = _con()
    con.execute("CREATE SCHEMA IF NOT EXISTS t")
    con.execute('CREATE OR REPLACE TABLE t."select" ("order" VARCHAR, "we""ird" VARCHAR)')
    con.execute("""INSERT INTO t."select" VALUES ('a@b.co', 'x'), ('c@d.co', 'y')""")
    p = profile.profile_column(con, "t", "select", Column("order", "VARCHAR"))
    assert p.address == "t.select.order"
    assert p.rows == 2 and p.pattern_hits["email"] == 2
    q = profile.profile_column(con, "t", "select", Column('we"ird', "VARCHAR"))
    assert q.rows == 2 and q.pattern_hits["email"] == 0


def check_a_profile_built_from_a_database_still_holds_no_values():
    # The invariant asserted against a real table rather than against a constructed
    # fixture, because the fixture is the thing an author controls.
    con = _con()
    p = _load(con, "people", "VARCHAR", ["ada@example.org", "grace@example.org"])
    assert profile.profiles_hold_no_values((p,)) == ()
    text = repr(p)
    assert "ada" not in text and "grace" not in text and "example.org" not in text


def check_mean_length_is_the_average_character_count():
    # Nothing pinned this, so reading it off the wrong element of the result row was
    # invisible. It is not a spare field: the free text rule is a threshold on it, and a
    # mean length of zero silently turns that rule off for every column in the warehouse.
    con = _con()
    p = _load(con, "lengths", "VARCHAR", ["a", "bbb", "cccccccc"])
    assert abs(p.mean_length - 4.0) < 1e-9
    q = _load(con, "prose", "VARCHAR", ["x" * 50, "y" * 51, "z" * 40])
    assert abs(q.mean_length - 47.0) < 1e-9
    assert [s.category_key for s in classify.value_signals(q)] == ["free_text_clinical"]
    # And a null does not count as a zero length value, which would drag the mean down.
    r = _load(con, "with_null", "VARCHAR", ["aaaa", None, "cccccc"])
    assert abs(r.mean_length - 5.0) < 1e-9


def check_the_profile_statement_lists_its_predicate_keys_in_order():
    # The statement's shape and the order the counts come back in are one fact, and this
    # is the check that they have not drifted apart.
    sql, keys = profile._profile_sql("raw", "patient", "email")
    assert list(keys) == [k for k, _e, _s, _w in classify.VALUE_PREDICATES]
    assert sql.count("count(*) filter") == len(keys)
    assert sql.startswith("SELECT count(*)")
    assert 'FROM "raw"."patient"' in sql


def check_profiling_a_crawl_covers_every_column_it_found():
    con = _con()
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("CREATE TABLE raw.p (id BIGINT PRIMARY KEY, email VARCHAR)")
    con.execute("INSERT INTO raw.p VALUES (1, 'a@b.co'), (2, NULL)")
    from pii import crawl
    crawled = crawl.crawl(con)
    profiles = profile.profile_crawl(con, crawled)
    assert len(profiles) == crawled.n_columns == 2
    assert [p.column for p in profiles] == ["id", "email"]
    assert profiles[0].is_key and not profiles[1].is_key
    rates = profile.null_rate_table(profiles)
    assert rates["raw.p.id"] == 0.0
    assert abs(rates["raw.p.email"] - 0.5) < 1e-9
