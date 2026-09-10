"""ULIDs (spec §1): time-sortable request ids, monotonic within a process."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
_RAND_BITS = 80
_RAND_MAX = (1 << _RAND_BITS) - 1
_TS_MAX = (1 << 48) - 1


def encode_ulid(ts_ms: int, rand: int) -> str:
    if not 0 <= ts_ms <= _TS_MAX:
        raise ValueError("ULID timestamp out of range")
    if not 0 <= rand <= _RAND_MAX:
        raise ValueError("ULID randomness out of range")
    value = (ts_ms << _RAND_BITS) | rand
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


class UlidFactory:
    """Monotonic ULIDs: same-millisecond (or backwards-clock) calls increment the random part."""

    def __init__(self, now_ms: Callable[[], int]) -> None:
        self._now = now_ms
        self._lock = threading.Lock()
        self._last_ts = -1
        self._last_rand = 0

    def __call__(self) -> str:
        with self._lock:
            ts = self._now()
            if ts <= self._last_ts:
                ts = self._last_ts
                rand = self._last_rand + 1
                if rand > _RAND_MAX:
                    ts += 1
                    rand = int.from_bytes(os.urandom(10), "big")
            else:
                rand = int.from_bytes(os.urandom(10), "big")
            self._last_ts, self._last_rand = ts, rand
            return encode_ulid(ts, rand)
