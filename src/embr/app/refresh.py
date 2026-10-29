"""Which firmware the periodic re-check visits (design doc §3.4 "检测时机"). No Qt: the UI
runs a timer every REFRESH_SECONDS, asks round() for the ids and checks them one per event
loop pass, skipping rounds while a job runs or the window is minimized.
"""

from collections.abc import Iterable

from embr.core.history import History

REFRESH_SECONDS = 60
ROUND_LIMIT = 8


class RefreshSchedule:
    """Most recently used firmware, newest first."""

    def __init__(self, limit: int = ROUND_LIMIT) -> None:
        raise NotImplementedError

    def seed(self, history: History) -> None:
        """At startup: the firmware of the newest flash records, distinct, newest first,
        up to the limit (records without a firmware are skipped)."""
        raise NotImplementedError

    def touch(self, firmware_id: str) -> None:
        """Selected or flashed: move to the front."""
        raise NotImplementedError

    def forget(self, firmware_id: str) -> None:
        """Deleted firmware."""
        raise NotImplementedError

    def round(self, selected: str | None, existing: Iterable[str]) -> list[str]:
        """Ids to check this round: the selected firmware first, then the most recently used
        ones, without duplicates, only ids in `existing`, at most `limit` in total."""
        raise NotImplementedError
