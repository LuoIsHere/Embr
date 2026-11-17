"""Whole-file writes that never leave a half-written file behind."""

import contextlib
import os
from pathlib import Path

from embr.core.storage.ids import new_id


def write_whole(path: Path, data: bytes) -> None:
    """Write `data` to a temporary file in path's folder (created if needed), then rename it
    over `path` in one step: `path` is either untouched or complete. The temporary file is
    removed when anything fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{new_id()}.part")
    try:
        partial.write_bytes(data)
        os.replace(partial, path)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        raise
