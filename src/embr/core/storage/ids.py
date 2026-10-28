"""ULID identifiers and unique display names."""

from collections.abc import Iterable


def new_id() -> str:
    """A new ULID: 26 characters of Crockford base32, upper case; later IDs sort after
    earlier ones."""
    raise NotImplementedError


def is_id(text: str) -> bool:
    """True if `text` is a well-formed ULID."""
    raise NotImplementedError


def unique_name(name: str, taken: Iterable[str]) -> str:
    """`name` if it is not taken, else the first free "name (2)", "name (3)", ...
    Comparison is exact (case-sensitive)."""
    raise NotImplementedError
