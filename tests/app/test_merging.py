"""app/merging: merge a firmware into one raw image and optionally add it to the list
(design doc §5 stage two "合成工具")."""

from collections.abc import Callable
from pathlib import Path

import pytest
from app_testkit import IDF, Clock, build_files, write_newer

from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.library import Library
from embr.app.merging import MergeOptions, Merger
from embr.app.tracking import Tracker
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import FlashParams, Source


def _rig(context: AppContext) -> tuple[Library, Merger]:
    tracker = Tracker(context, clock=Clock())
    return Library(context, tracker), Merger(context, tracker)


def test_merge_writes_the_image_and_adds_a_firmware(context: AppContext, tmp_path: Path) -> None:
    library, merger = _rig(context)
    project = library.create_project("p")
    source = library.create_firmware("hello", project.id, "board")
    files = build_files("esp32s3_hello_v61")
    library.add_files(source.id, files)
    for name, value in (("flash_mode", "dio"), ("flash_freq", "80m"), ("flash_size", "2MB")):
        library.set_param(source.id, name, value)
    before = library.firmware(source.id)
    output = tmp_path / "hello-merged.bin"

    assert "merge-bin" in merger.command(source.id, output, MergeOptions())
    merged = merger.merge(source.id, output, MergeOptions())

    # same bytes as `idf.py merge-bin` on that build
    assert output.read_bytes() == (IDF / "esp32s3_hello_v61/build/merged-binary.bin").read_bytes()
    assert merged is not None
    assert (merged.name, merged.source, merged.project_id, merged.device) == (
        "hello (merged)",
        Source.MANUAL,
        project.id,
        "board",
    )
    assert (merged.chip, merged.flash, merged.before, merged.after) == (
        "esp32s3",
        FlashParams("dio", "80m", "2MB"),
        before.before,
        before.after,
    )
    (entry,) = merged.entries
    assert (entry.kind, entry.address, entry.enabled, entry.file) == (
        EntryKind.MERGED,
        0x0,
        True,
        f"path:{output.resolve()}",
    )
    assert all(f.name in entry.note for f in files)
    assert library.firmware(source.id).entries == before.entries

    again = tmp_path / "again.bin"
    assert merger.merge(source.id, again, MergeOptions(add_to_list=False)) is None
    assert again.read_bytes() == output.read_bytes()
    assert len(context.firmware.all()) == 2


def _no_chip(library: Library, firmware_id: str, tmp_path: Path) -> MergeOptions:
    data = tmp_path / "data.bin"
    write_newer(data, b"\x5a" * 0x1000)
    library.add_attachment(firmware_id, data, address=0x100000)
    return MergeOptions()


def _pad_without_flash_size(library: Library, firmware_id: str, tmp_path: Path) -> MergeOptions:
    library.add_files(firmware_id, [build_files("esp32s3_hello")[2]])  # app only, size "keep"
    return MergeOptions(pad_to_flash_size=True)


REFUSED: dict[str, Callable[[Library, str, Path], MergeOptions]] = {
    "no_chip": _no_chip,
    "pad_without_flash_size": _pad_without_flash_size,
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_merge_is_refused(context: AppContext, tmp_path: Path, case: str) -> None:
    library, merger = _rig(context)
    firmware = library.create_firmware("fw")
    options = REFUSED[case](library, firmware.id, tmp_path)
    output = tmp_path / "out.bin"
    with pytest.raises(PlanError):
        merger.merge(firmware.id, output, options)
    assert not output.exists()
    assert len(context.firmware.all()) == 1
