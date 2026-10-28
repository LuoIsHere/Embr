"""ULID identifiers and unique display names."""

import os
import threading
import time
from collections.abc import Iterable


def new_id() -> str:
    """A new ULID: 26 characters of Crockford base32, upper case; later IDs sort after
    earlier ones."""
    value = _clock.next()
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


def is_id(text: str) -> bool:
    """True if `text` is a well-formed ULID."""
    return len(text) == 26 and text[0] <= "7" and all(c in _ALPHABET for c in text)


def unique_name(name: str, taken: Iterable[str]) -> str:
    """`name` if it is not taken, else the first free "name (2)", "name (3)", ...
    Comparison is exact (case-sensitive)."""
    used = set(taken)
    if name not in used:
        return name
    n = 2
    while f"{name} ({n})" in used:
        n += 1
    return f"{name} ({n})"


_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


_RANDOM_BITS = 80


class _UlidClock:
    """Keeps IDs created in the same millisecond increasing."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_ms = -1
        self.last_random = 0

    def next(self) -> int:
        with self.lock:
            ms = time.time_ns() // 1_000_000
            if ms <= self.last_ms:
                ms = self.last_ms
                self.last_random = (self.last_random + 1) % (1 << _RANDOM_BITS)
            else:
                self.last_random = int.from_bytes(os.urandom(_RANDOM_BITS // 8))
            self.last_ms = ms
            return (ms << _RANDOM_BITS) | self.last_random


_clock = _UlidClock()
