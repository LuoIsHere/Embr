"""Which firmware the periodic re-check visits (design doc §3.4 "检测时机"). No Qt: the UI
runs a timer every REFRESH_SECONDS, asks round() for the ids and checks them one per event
loop pass, skipping rounds while a job runs or the window is minimized.
"""

from collections.abc import Iterable

from embr.core.history import History

REFRESH_SECONDS = 60
ROUND_LIMIT = 8
_KEEP_ROUNDS = 4


class RefreshSchedule:
    """Most recently used firmware, newest first."""

    def __init__(self, limit: int = ROUND_LIMIT) -> None:
        self.limit = limit
        self._recent: list[str] = []

    def seed(self, history: History) -> None:
        """At startup: the firmware of the newest flash records, distinct, newest first,
        up to the limit (records without a firmware are skipped)."""
        # several records per firmware are common; read enough to find `limit` distinct ones
        for record in history.query(limit=self.limit * 20):
            if record.firmware_id and record.firmware_id not in self._recent:
                self._recent.append(record.firmware_id)
            if len(self._recent) >= self.limit:
                break

    def touch(self, firmware_id: str) -> None:
        """Selected or flashed: move to the front."""
        self.forget(firmware_id)
        self._recent.insert(0, firmware_id)
        # keep more than a round needs, so deleted firmware do not shorten rounds
        del self._recent[self.limit * _KEEP_ROUNDS :]

    def forget(self, firmware_id: str) -> None:
        """Deleted firmware."""
        if firmware_id in self._recent:
            self._recent.remove(firmware_id)

    def round(self, selected: str | None, existing: Iterable[str]) -> list[str]:
        """Ids to check this round: the selected firmware first, then the most recently used
        ones, without duplicates, only ids in `existing`, at most `limit` in total."""
        known = set(existing)
        ids: list[str] = []
        for firmware_id in [selected, *self._recent]:
            if firmware_id is not None and firmware_id in known and firmware_id not in ids:
                ids.append(firmware_id)
        return ids[: self.limit]
