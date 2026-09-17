from __future__ import annotations

from pii.rng import derive, stream


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def check_the_same_seed_and_stream_give_the_same_value():
    assert derive(7, "patient") == derive(7, "patient")


def check_different_streams_of_one_seed_differ():
    assert derive(7, "patient") != derive(7, "encounter")


def check_different_seeds_of_one_stream_differ():
    assert derive(7, "patient") != derive(8, "patient")


def check_adding_a_stream_name_does_not_move_any_other_stream():
    # The whole point of hashing the name. Under seed plus offset, inserting a stream
    # shifts every stream after it and nothing raises.
    before = {n: derive(11, n) for n in ("a", "b", "c")}
    after = {n: derive(11, n) for n in ("a", "new_one", "b", "c")}
    for n in before:
        assert before[n] == after[n], n


def check_a_negative_seed_and_an_empty_stream_name_are_refused():
    assert _raises(lambda: derive(-1, "a"), ValueError)
    assert _raises(lambda: derive(0, ""), ValueError)


def check_a_bool_is_not_accepted_as_a_seed():
    # isinstance(True, int) is True, so a plain int check lets a bool through and
    # derive(True, ...) and derive(1, ...) become the same stream.
    assert _raises(lambda: derive(True, "a"), TypeError)
    assert _raises(lambda: derive("7", "a"), TypeError)
    derive(0, "a")


def check_the_derived_value_fits_in_sixty_four_bits():
    v = derive(123456, "patient")
    assert 0 <= v < 2 ** 64, v


def check_two_streams_built_from_one_seed_produce_different_sequences():
    a = stream(3, "left")
    b = stream(3, "right")
    assert [a.random() for _ in range(5)] != [b.random() for _ in range(5)]


def check_a_rebuilt_stream_replays_exactly():
    first = [stream(3, "left").random() for _ in range(1)]
    second = [stream(3, "left").random() for _ in range(1)]
    assert first == second
