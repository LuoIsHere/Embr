"""app/merging: merge a firmware into one raw image and optionally add it to the list."""

import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from app_testkit import (
    IDF,
    Clock,
    build_files,
    copy_files,
    idf_project,
    root_first,
    write_newer,
)

from embr.app import versions as app_versions
from embr.app.chips import Mcu, firmware_chips, firmware_mcus
from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.library import Library
from embr.app.merging import MergeOptions, Merger
from embr.app.tracking import Tracker
from embr.app.versions import BuildVersions
from embr.core.firmware.entries import blank_otadata_entry
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import FlashParams, Source
from embr.core.versions import Origin


def _rig(context: AppContext) -> tuple[Library, Merger]:
    tracker = Tracker(context, clock=Clock())
    return Library(context, tracker), Merger(context, tracker)


def _hello(library: Library) -> str:
    source = library.create_firmware("hello")
    library.add_files(source.id, root_first(build_files("esp32s3_hello_v61")))
    return source.id


def test_the_default_output_keeps_earlier_merges(context: AppContext, tmp_path: Path) -> None:
    library, merger = _rig(context)
    firmware = library.firmware(_hello(library))
    assert merger.default_output(firmware, tmp_path) == tmp_path / "hello_merged.bin"
    (tmp_path / "hello_merged.bin").write_bytes(b"first")
    (tmp_path / "hello_merged_2.bin").write_bytes(b"second")
    assert merger.default_output(firmware, tmp_path) == tmp_path / "hello_merged_3.bin"


def test_a_folder_is_no_output_and_users_are_named(context: AppContext, tmp_path: Path) -> None:
    library, merger = _rig(context)
    source_id = _hello(library)
    folder = tmp_path / "out"
    folder.mkdir()
    with pytest.raises(PlanError, match="is a folder"):
        merger.merge(source_id, folder, MergeOptions())
    assert list(folder.iterdir()) == []

    output = tmp_path / "hello_merged.bin"
    merger.merge(source_id, output, MergeOptions())
    assert merger.users(output) == ["hello (merged)"]
    assert merger.users(tmp_path / "HELLO_MERGED.BIN") == ["hello (merged)"]  # as Windows
    assert merger.users(tmp_path / "other.bin") == []


def test_a_failed_write_keeps_the_previous_output(
    context: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library, merger = _rig(context)
    source_id = _hello(library)
    output = tmp_path / "out" / "hello_merged.bin"
    output.parent.mkdir()
    output.write_bytes(b"previous")

    def full(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(OSError, match="disk full"):
        merger.merge(source_id, output, MergeOptions(add_to_list=False))
    assert output.read_bytes() == b"previous"
    assert list(output.parent.iterdir()) == [output]  # no partial file left


def test_merge_writes_the_image_and_adds_a_firmware(context: AppContext, tmp_path: Path) -> None:
    library, merger = _rig(context)
    project = library.create_project("p")
    source = library.create_firmware("hello", project.id, "board")
    files = build_files("esp32s3_hello_v61")
    library.add_files(source.id, root_first(files))
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
    assert (firmware_chips(context, merged), merged.flash, merged.before, merged.after) == (
        ("esp32s3",),
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
    assert Path(merged.folder) == output.parent.resolve()
    assert library.firmware(source.id).entries == before.entries

    again = tmp_path / "again.bin"
    assert merger.merge(source.id, again, MergeOptions(add_to_list=False)) is None
    assert again.read_bytes() == output.read_bytes()
    assert len(context.firmware.all()) == 2


def test_a_merged_p4_image_keeps_the_revision_line(context: AppContext, tmp_path: Path) -> None:
    """The merged entry is for the chip and line of the source: one MCU, also when an entry
    stored without a line (older data) comes first."""
    library, merger = _rig(context)
    source = library.create_firmware("p4")
    folder = tmp_path / "p4"
    library.add_files(source.id, root_first(copy_files(build_files("esp32p4_rev1_hello"), folder)))
    data = folder / "data.bin"
    write_newer(data, b"Z" * 0x1000)
    attached = library.add_attachment(source.id, data, address=0x0)
    stored = context.firmware.get(source.id)
    next(e for e in stored.entries if e.id == attached.id).chip = "esp32p4"  # no line
    context.firmware.save(stored)

    merged = merger.merge(source.id, tmp_path / "p4-merged.bin", MergeOptions())

    assert merged is not None
    (entry,) = library.firmware(merged.id).entries
    assert (entry.chip, entry.chip_line) == ("esp32p4", "v0-v1")
    assert firmware_mcus(context, library.firmware(merged.id)) == (Mcu("esp32p4", "v0-v1"),)


def _no_chip(library: Library, firmware_id: str, tmp_path: Path) -> MergeOptions:
    data = tmp_path / "data.bin"
    write_newer(data, b"\x5a" * 0x1000)
    library.add_attachment(firmware_id, data, address=0x100000)
    return MergeOptions()


def _pad_without_flash_size(library: Library, firmware_id: str, tmp_path: Path) -> MergeOptions:
    library.add_files(firmware_id, [build_files("esp32s3_hello")[2]])  # app only, size "keep"
    return MergeOptions(pad_to_flash_size=True)


def _mixed_mcu(library: Library, firmware_id: str, tmp_path: Path) -> MergeOptions:
    s3_app = build_files("esp32s3_hello")[2]
    p4_boot = build_files("esp32p4_rev1_hello")[0]  # at 0x2000, clear of the app
    library.add_files(firmware_id, copy_files([s3_app, p4_boot], tmp_path / "mixed"))
    return MergeOptions()


def _mixed_p4_lines(library: Library, firmware_id: str, tmp_path: Path) -> MergeOptions:
    rev1_app = build_files("esp32p4_rev1_hello")[2]
    rev3_boot = build_files("esp32p4_rev3_hello")[0]  # at 0x2000, clear of the app
    library.add_files(firmware_id, copy_files([rev1_app, rev3_boot], tmp_path / "mixed"))
    return MergeOptions()


# the reason is the one the dialog shows (app.chips.merge_problem())
REFUSED: dict[str, tuple[Callable[[Library, str, Path], MergeOptions], str]] = {
    "no_chip": (_no_chip, "No MCU set for data.bin"),
    "mixed_mcu": (
        _mixed_mcu,
        "Enabled entries are for different MCUs: ESP32-P4 v0\u2013v1, ESP32-S3",
    ),
    "mixed_p4_lines": (
        _mixed_p4_lines,
        "Enabled entries are for different MCUs: ESP32-P4 v0\u2013v1, ESP32-P4 v3",
    ),
    "pad_without_flash_size": (_pad_without_flash_size, "fw: flash size unknown"),
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_merge_is_refused(context: AppContext, tmp_path: Path, case: str) -> None:
    library, merger = _rig(context)
    firmware = library.create_firmware("fw")
    setup, reason = REFUSED[case]
    options = setup(library, firmware.id, tmp_path)
    output = tmp_path / "out.bin"
    with pytest.raises(PlanError, match=re.escape(reason)):
        merger.merge(firmware.id, output, options)
    assert not output.exists()
    assert len(context.firmware.all()) == 1


@pytest.mark.parametrize(
    ("project", "group", "expected"),
    [
        ("Station", "Main", "Merged from Station/Main/hello: start offset 0x0"),
        ("Station", "", "Merged from Station/hello: start offset 0x0"),
        (None, "", "Merged from hello: start offset 0x0"),
    ],
)
def test_the_merged_note_says_where_it_came_from(
    context: AppContext, tmp_path: Path, project: str | None, group: str, expected: str
) -> None:
    library, merger = _rig(context)
    project_id = library.create_project(project).id if project else None
    source = library.create_firmware("hello", project_id, group)
    library.add_files(source.id, root_first(build_files("esp32s3_hello_v61")))

    merged = merger.merge(source.id, tmp_path / "out.bin", MergeOptions())

    assert merged is not None
    assert merged.notes == expected


def test_the_merged_note_follows_a_rename(context: AppContext, tmp_path: Path) -> None:
    library, merger = _rig(context)
    project = library.create_project("Station")
    source = library.create_firmware("hello", project.id, "Main")
    library.add_files(source.id, root_first(build_files("esp32s3_hello_v61")))
    library.rename_group(project.id, "Main", "Core")
    library.rename_firmware(source.id, "renamed")

    merged = merger.merge(source.id, tmp_path / "out.bin", MergeOptions())

    assert merged is not None
    assert merged.notes.startswith("Merged from Station/Core/renamed:")


@pytest.mark.parametrize(
    "options",
    [MergeOptions(add_to_list=False), MergeOptions(pad_to_flash_size=True, add_to_list=False)],
    ids=["to-the-last-entry", "padded"],
)
def test_the_size_is_that_of_the_merged_file(
    context: AppContext, tmp_path: Path, options: MergeOptions
) -> None:
    """The last entry is a blank otadata (no file until merging writes it)."""
    library, merger = _rig(context)
    source_id = _hello(library)
    library.set_param(source_id, "flash_size", "4MB")
    firmware = context.firmware.get(source_id)
    firmware.entries.append(blank_otadata_entry(0x200000, 0x2000))
    context.firmware.save(firmware)
    library.tracker.invalidate(source_id)
    output = tmp_path / "out.bin"

    size = merger.size(source_id, options)
    merger.merge(source_id, output, options)

    assert size == output.stat().st_size
    assert size == (0x400000 if options.pad_to_flash_size else 0x202000)


def test_no_size_for_a_start_offset_above_the_first_entry(context: AppContext) -> None:
    library, merger = _rig(context)
    source_id = _hello(library)
    assert merger.size(source_id, MergeOptions(target_offset=0x1000)) is None


def _no_git(*_args: object, **_kwargs: object) -> None:
    return None


def test_a_version_merges_as_its_build(
    context: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_versions.vcs, "read_state", _no_git)
    tracker = Tracker(context, clock=Clock())
    versions = BuildVersions(context, tracker)
    library, merger = Library(context, tracker, versions), Merger(context, tracker)
    firmware = library.import_idf(idf_project("esp32s3_hello", tmp_path / "hello"))
    version = versions.backup(firmware.id, origin=Origin.MANUAL)
    shown = versions.as_firmware(version)
    by_id, by_object = tmp_path / "by_id.bin", tmp_path / "by_object.bin"
    options = MergeOptions(add_to_list=False)

    merger.merge(firmware.id, by_id, options)
    assert merger.merge_firmware(shown, by_object, options) is None
    assert by_object.read_bytes() == by_id.read_bytes()
    assert merger.size_for(shown, options) == merger.size(firmware.id, options)
    command = merger.command_for(shown, by_object, options)
    assert str(context.paths.archive) in command
    merged = merger.merge_firmware(shown, tmp_path / "listed.bin", MergeOptions())
    assert merged is not None and merged.name == f"{firmware.name} (merged)"
    assert len(context.firmware.all()) == 2  # the version's object itself is never saved
