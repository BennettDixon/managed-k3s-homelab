from gateway.ulid import UlidFactory, encode_ulid


def test_ulid_shape_and_ordering() -> None:
    now = [1_700_000_000_000]
    factory = UlidFactory(lambda: now[0])
    first = factory()
    second = factory()  # same millisecond: random part increments
    now[0] += 1
    third = factory()
    assert len(first) == len(second) == len(third) == 26
    assert first < second < third
    assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in first)


def test_ulid_monotonic_across_backwards_clock() -> None:
    now = [1_700_000_000_000]
    factory = UlidFactory(lambda: now[0])
    a = factory()
    now[0] -= 5_000
    b = factory()
    assert b > a


def test_encode_known_timestamp_prefix() -> None:
    # The first 10 chars encode the timestamp; equal timestamps share them.
    a = encode_ulid(1_700_000_000_000, 0)
    b = encode_ulid(1_700_000_000_000, 1)
    assert a[:10] == b[:10]
    assert a != b
