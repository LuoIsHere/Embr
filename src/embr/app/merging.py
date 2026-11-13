"""Merge a firmware into one raw image: the enabled
entries (attachments included) and flash parameters of any firmware; tracking firmware use
their current resolution.
"""

import contextlib
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from embr.app.chips import entry_chip, merge_problem
from embr.app.context import AppContext
from embr.app.flashplan import PlanError, declared_flash_size, entry_path
from embr.app.tracking import Tracker
from embr.core.firmware.entries import sorted_entries
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import AddressSource, Entry, Firmware, FlashParams, Source
from embr.core.flasher.merge import MergeInput, merge_args, merge_image
from embr.core.flasher.plan import FlashSettings
from embr.core.storage.ids import new_id
from embr.i18n import N_


@dataclass(frozen=True)
class MergeOptions:
    target_offset: int = 0
    pad_to_flash_size: bool = False
    """Pad to the firmware's flash size (or the size declared in its bootloader header)."""
    add_to_list: bool = True
    """Import the result into Embr as a firmware."""


class Merger:
    def __init__(self, context: AppContext, tracker: Tracker) -> None:
        self._ctx = context
        self._tracker = tracker

    def command(self, firmware_id: str, output: Path, options: MergeOptions) -> str:
        """Equivalent `esptool ... merge-bin` command line for the dialog. PlanError when
        the firmware cannot be merged (see merge())."""
        firmware, chip, inputs, pad = self._inputs(firmware_id, options, write_files=False)
        args = merge_args(
            chip,
            inputs,
            _settings(firmware),
            output,
            target_offset=options.target_offset,
            pad_to_size=pad,
        )
        return subprocess.list2cmdline(["esptool", *args])

    def merge(self, firmware_id: str, output: Path, options: MergeOptions) -> Firmware | None:
        """Write the merged image to `output` (a temporary file next to it, then renamed
        over it, so a failure never leaves half a file; PlanError when `output` is a
        folder).
        With add_to_list: a new MANUAL firmware named "<source> (merged)" (made unique) in
        the source's project and group, with the source's flash parameters and reset modes,
        folder = the output's folder, one enabled MERGED entry at target_offset referencing
        `output` (MCU = the merged entries' chip), whose note lists the merged files, and
        notes giving the source and options; returned. Without it: None. The source
        firmware is unchanged. PlanError when no entry is enabled, the firmware is not
        flashable, the enabled entries are not all for one MCU (app.chips.merge_problem(),
        whose message the dialog shows while its Merge button is disabled), or padding is
        asked without a known flash size; core.flasher.merge.MergeError propagates."""
        if output.is_dir():
            raise PlanError(N_(f"{output} is a folder; enter a file name"))
        firmware, chip, inputs, pad = self._inputs(firmware_id, options, write_files=True)
        data = merge_image(
            chip,
            inputs,
            _settings(firmware),
            target_offset=options.target_offset,
            pad_to_size=pad,
        )
        _write_whole(output, data)
        if not options.add_to_list:
            return None
        created = self._ctx.firmware.create(
            f"{firmware.name} (merged)",
            source=Source.MANUAL,
            project_id=firmware.project_id,
            device=firmware.device,
            before=firmware.before,
            after=firmware.after,
        )
        created.folder = str(output.parent.resolve())
        created.flash = FlashParams(firmware.flash.mode, firmware.flash.freq, firmware.flash.size)
        listed = ", ".join(f"{i.path.name} @ {i.address:#x}" for i in inputs)
        padding = f", padded to {pad}" if pad else ""
        source = self._source_path(firmware)
        created.notes = f"Merged from {source}: start offset {options.target_offset:#x}{padding}"
        created.entries = [
            Entry(
                id=new_id(),
                name=output.name,
                file=f"path:{output.resolve()}",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                kind=EntryKind.MERGED,
                address=options.target_offset,
                auto_address=options.target_offset,
                address_source=AddressSource.AUTO,
                note=f"Merged: {listed}",
                chip=chip,
            )
        ]
        self._ctx.firmware.save(created)
        return created

    def default_output(self, firmware: Firmware, folder: Path) -> Path:
        """folder/<name>_merged.bin, or _merged_2.bin, _3 ... when that file exists, so a
        second merge does not overwrite the first one's file by default."""
        candidate = folder / f"{firmware.name}_merged.bin"
        number = 2
        while candidate.exists():
            candidate = folder / f"{firmware.name}_merged_{number}.bin"
            number += 1
        return candidate

    def users(self, output: Path) -> list[str]:
        """Names of the firmware with an entry referring to the file `output` (paths
        compared as Windows does), in name order: replacing it changes their content."""
        key = _path_key(output)
        return sorted(
            f.name
            for f in self._ctx.firmware.all()
            if any(
                e.file.startswith("path:") and _path_key(Path(e.file[5:])) == key for e in f.entries
            )
        )

    def _source_path(self, firmware: Firmware) -> str:
        """project/group/firmware, as far as it has them."""
        if firmware.project_id is None:
            return firmware.name
        try:
            project = self._ctx.projects.get(firmware.project_id).name
        except KeyError:
            return firmware.name
        return "/".join(part for part in (project, firmware.device, firmware.name) if part)

    def _inputs(
        self, firmware_id: str, options: MergeOptions, *, write_files: bool
    ) -> tuple[Firmware, str, list[MergeInput], str | None]:
        status = self._tracker.check(firmware_id)
        firmware = status.firmware
        if not status.state.flashable:
            reasons = status.problems or tuple(c.message for c in status.changes[:3])
            why = "; ".join(reasons)
            raise PlanError(N_(f"{firmware.name} cannot be merged now: {why}"))
        enabled = sorted_entries([e for e in firmware.entries if e.enabled])
        if not enabled:
            raise PlanError(N_(f"{firmware.name} has no enabled entries"))
        problem = merge_problem(self._ctx, firmware)
        if problem is not None:
            raise PlanError(problem)
        chip = entry_chip(self._ctx, firmware, enabled[0])
        assert chip is not None  # merge_problem() found every enabled entry's MCU
        inputs: list[MergeInput] = []
        for entry in enabled:
            if entry.address is None:
                raise PlanError(N_(f"{entry.name} has no address"))
            path = entry_path(self._ctx, entry)
            if write_files and entry.file.startswith("blank:"):
                path.write_bytes(b"\xff" * entry.size)
            inputs.append(MergeInput(entry.address, path))
        pad = None
        if options.pad_to_flash_size:
            pad = declared_flash_size(self._ctx, firmware)
            if pad is None:
                raise PlanError(N_(f"{firmware.name}: flash size unknown; set it to pad the image"))
        return firmware, chip, inputs, pad


def _settings(firmware: Firmware) -> FlashSettings:
    flash = firmware.flash
    return FlashSettings(flash.mode, flash.freq, flash.size)


def _write_whole(output: Path, data: bytes) -> None:
    """`data` to a temporary file in output's folder, then renamed over `output` (one
    step on the same volume): `output` is either untouched or complete."""
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f"{output.name}.{new_id()}.part")
    try:
        partial.write_bytes(data)
        os.replace(partial, output)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        raise


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))
