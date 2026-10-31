"""When this copy of Embr was packaged (design doc §12 item 22): Embr.spec writes the UTC
time into resources/build.txt of the bundle; a source checkout has no such file."""

from importlib.resources import files

BUILD_FILE = "build.txt"


def build_time() -> str | None:
    """E.g. "2026-11-01 08:12 UTC"; None when running from source."""
    try:
        text = files("embr.resources").joinpath(BUILD_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None
