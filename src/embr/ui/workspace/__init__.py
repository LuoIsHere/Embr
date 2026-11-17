"""The middle pane for one firmware: header, banner and the Entries, Parameters
and Records tabs. One module per part; everything is used through this package."""

from embr.ui.workspace.entries import (
    ADDRESS_FIELD_PX,
    ADDRESS_PX,
    ADDRESS_SOURCES,
    CELL_TAG_GAP,
    COLUMN_WIDTHS,
    EN_DASH,
    MARK_TAGS,
    MCU_WIDEST,
    NOTE_MIN_WIDTH,
    ROW_PX,
    EntriesTab,
    entry_error,
    kept_notes,
)
from embr.ui.workspace.header import (
    CRUMB_MIN,
    HEADING_MIN,
    NOTES_GAP,
    NOTES_WIDTH,
    SOURCE_LABELS,
    STATE_TAGS,
    Banner,
    Header,
    crumb,
    lift_to_baseline,
    shrink_to,
    time_text,
)
from embr.ui.workspace.params import NotesEdit, ParamsTab
from embr.ui.workspace.records import RESULT_TAGS, RecordRow, RecordsTab
from embr.ui.workspace.view import FirmwareView

__all__ = [
    "ADDRESS_FIELD_PX",
    "ADDRESS_PX",
    "ADDRESS_SOURCES",
    "CELL_TAG_GAP",
    "COLUMN_WIDTHS",
    "CRUMB_MIN",
    "EN_DASH",
    "HEADING_MIN",
    "MARK_TAGS",
    "MCU_WIDEST",
    "NOTES_GAP",
    "NOTES_WIDTH",
    "NOTE_MIN_WIDTH",
    "RESULT_TAGS",
    "ROW_PX",
    "SOURCE_LABELS",
    "STATE_TAGS",
    "Banner",
    "EntriesTab",
    "FirmwareView",
    "Header",
    "NotesEdit",
    "ParamsTab",
    "RecordRow",
    "RecordsTab",
    "crumb",
    "entry_error",
    "kept_notes",
    "lift_to_baseline",
    "shrink_to",
    "time_text",
]
