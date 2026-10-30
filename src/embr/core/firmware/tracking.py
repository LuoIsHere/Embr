"""Resolve tracking firmware against its build output (design doc §3.4).

Source-agnostic: an importer (import_idf; import_pio later) describes the build as a
DerivedBuild. resolve() classifies its files, applies the user's entry_overrides,
cross-checks chips and ESP-IDF versions, and compares the result with
last_resolved. accept() stores a resolution as the new last_resolved.
"""

import contextlib
import copy
import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from embr.core.firmware.entries import resolve_partitions
from embr.core.firmware.filecache import FileCache
from embr.core.firmware.image_header import ImageHeader, format_revision
from embr.core.firmware.image_sniff import (
    PARTITION_TABLE_MAX_SIZE,
    PartitionTable,
    SniffResult,
    parse_partition_table,
    sniff,
)
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import AddressSource, Entry, EntryOverride, Firmware, FlashParams
from embr.core.storage.documents import (
    Document,
    doc_int,
    doc_opt_str,
    doc_str,
    doc_table,
    doc_tables,
)


class ResolveState(StrEnum):
    OK = "ok"
    """Same as last_resolved."""
    UPDATED = "updated"
    """Only file contents changed: followed without confirmation (accept() is applied)."""
    NEEDS_CONFIRMATION = "needs_confirmation"
    """Anything else changed since last_resolved; not flashable until confirmed."""
    TARGET_SWITCHED = "target_switched"
    """The build output agrees with itself but not with the configured target: rebuild."""
    INCOMPLETE = "incomplete"
    """A file is missing or unparsable, or chips / IDF versions disagree inside the build."""
    MISSING = "missing"
    """The build directory or its description file is gone."""
    BUILDING = "building"
    """Set by the app when a source file changed moments ago; never returned by resolve()."""

    @property
    def flashable(self) -> bool:
        """OK and UPDATED."""
        return self in (ResolveState.OK, ResolveState.UPDATED)


class ChangeKind(StrEnum):
    """Change kinds in the order the change list shows them."""

    CHIP = "chip"
    IDF_VERSION = "idf_version"
    REVISION_RANGE = "revision_range"
    """Chip revision range in a bootloader or app image header."""
    FLASH = "flash"
    RESET = "reset"
    PARTITION_TABLE = "partition_table"
    """Content of the partition table file."""
    ENTRY_ADDED = "entry_added"
    ENTRY_REMOVED = "entry_removed"
    ADDRESS = "address"
    """Derived address changed (also when a manual address overrides it)."""
    OVERRIDE_DROPPED = "override_dropped"
    """An entry_overrides key matches no file any more; dropped on confirmation."""
    MANUAL_ADDRESS = "manual_address"
    """Manual address of a derived entry, listed on a chip or IDF change for review (on a
    chip change it is dropped on confirmation)."""
    CONTENT = "content"
    """Same file, new content. The only kind in an UPDATED resolution."""
    GROUP_HINT = "group_hint"
    """Chip changed: the device group may need adjusting."""


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    subject: str
    """Entry name, or "" for firmware-wide changes."""
    before: str
    after: str
    message: str
    """One line for the change list, e.g. "Target chip: esp32s3 -> esp32p4"."""


class EntryMark(StrEnum):
    CHANGED = "changed"
    """Content changed since last_resolved."""
    NEW = "new"
    """File not in last_resolved."""
    MISSING = "missing"
    """In last_resolved (or flash_files) but the file is gone."""


@dataclass(frozen=True)
class DerivedFile:
    address: int
    path: Path
    """Absolute."""
    expected: EntryKind | None = None
    """Kind the build output says the file is; a file that does not parse as that kind
    (e.g. half written) makes the build INCOMPLETE."""


@dataclass(frozen=True)
class DerivedBuild:
    """What an importer read from the build output, before any file is classified."""

    files: tuple[DerivedFile, ...]
    declared_chips: tuple[str, ...]
    """Chips the build output declares (IDF: flasher_args chip and project_description
    target); empty when it declares none (PIO)."""
    configured_target: str | None
    """Target in the project configuration; None when unknown (the check is skipped)."""
    idf_version: str | None
    """ESP-IDF version from the build metadata; None skips the version checks (PIO)."""
    flash: FlashParams
    before: str | None
    after: str | None
    """Reset modes from the build output; None keeps the firmware's own (PIO)."""
    stub: bool
    inputs: tuple[Path, ...]
    """Files whose stat decides whether the build must be resolved again."""


@dataclass(frozen=True)
class Resolution:
    state: ResolveState
    firmware: Firmware
    """A copy of the firmware as it would be flashed: chip, flash, reset and stub from the
    build; entries = derived entries (id = entry_key(), overrides applied, partition
    placement resolved). For MISSING: entries rebuilt from
    last_resolved for display."""
    changes: tuple[Change, ...]
    """Against last_resolved, ordered by ChangeKind. Empty for OK."""
    problems: tuple[str, ...]
    """Why the state is INCOMPLETE, TARGET_SWITCHED or MISSING, showing the values of
    every side (e.g. "sdkconfig: esp32p4, build output: esp32s3")."""
    marks: Mapping[str, EntryMark]
    """Entry id -> mark, for the entries table."""
    snapshot: Document
    """What accept() stores as last_resolved (chip, IDF version, flash, reset, partition
    table hash, per derived entry: key, name, address, sha256, size, kind and image
    revision range, and the time)."""
    idf_version: str | None
    configured_target: str | None


def entry_key(kind: EntryKind, file_name: str) -> str:
    """Key of a derived entry in entry_overrides and its entry id: "<kind>:<file name>"."""
    return f"{kind.value}:{file_name}"


def resolve(
    firmware: Firmware, build: DerivedBuild, cache: FileCache, *, archive_dir: Path, now: str
) -> Resolution:
    """Resolve `firmware` (a tracking firmware) against `build`. Never raises for a broken
    build; the state says what is wrong. In order of precedence:
    1. a build file missing, or a bootloader / app / partition table that cannot be parsed
       -> INCOMPLETE
    2. chips disagree among declared_chips and the bootloader and app image headers, or
       the IDF versions of the build metadata, bootloader descriptor and app descriptor
       disagree (compared on the first 31 characters; a missing descriptor is skipped)
       -> INCOMPLETE
    3. configured_target set and different from the build's chip -> TARGET_SWITCHED
    4. no last_resolved -> OK (first resolve; the importer accepts it)
    5. only CONTENT changes -> UPDATED; any other change -> NEEDS_CONFIRMATION; else OK
    A derived entry whose override sets a manual address while the derived address changed
    is NEEDS_CONFIRMATION (manual address kept). User entries are checked by name, path and
    hash like fixed entries; a changed or missing user entry is NEEDS_CONFIRMATION or
    INCOMPLETE respectively. `now` is stored in the snapshot."""
    last = firmware.last_resolved
    last_entries = {doc_str(e, "key"): e for e in doc_tables(last or {}, "entries")}
    last_kinds = {doc_str(e, "name"): doc_str(e, "kind") for e in last_entries.values()}
    incomplete: list[str] = []
    marks: dict[str, EntryMark] = {}

    # the partition table first: SPIFFS images are only recognised through it
    table_file = next((f for f in build.files if f.expected == EntryKind.PARTITION_TABLE), None)
    table = None
    if table_file is not None:
        with contextlib.suppress(OSError):
            table = parse_partition_table(cache.head(table_file.path, PARTITION_TABLE_MAX_SIZE))

    derived: list[_Derived] = []
    for file in build.files:
        derived.append(_classify(file, cache, table, last_kinds))
    for d in derived:
        if d.data_missing:
            incomplete.append(f"{d.name}: file missing")
            marks[d.key] = EntryMark.MISSING
        elif d.file.expected is not None and d.kind != d.file.expected:
            incomplete.append(f"{d.name}: cannot be read as {d.file.expected.value}")
    if not incomplete and not any(d.kind == EntryKind.APP for d in derived):
        incomplete.append("the build output has no app image")

    images = [d for d in derived if d.kind in (EntryKind.BOOTLOADER, EntryKind.APP) and d.header]
    chip_sides = [(f"build output ({c})", c) for c in build.declared_chips]
    chip_sides += [
        (f"{d.name} header", d.header.chip) for d in images if d.header and d.header.chip
    ]
    chips = {c for _, c in chip_sides}
    chip = next(iter(chips)) if len(chips) == 1 else None
    if len(chips) > 1:
        incomplete.append("chips differ: " + ", ".join(f"{s}: {c}" for s, c in chip_sides))
    versions = [("build", build.idf_version)] if build.idf_version else []
    versions += [(d.name, d.header.idf_ver) for d in images if d.header and d.header.idf_ver]
    if len({v[:_IDF_VER_CHARS] for _, v in versions}) > 1:
        incomplete.append("ESP-IDF versions differ: " + ", ".join(f"{s} {v}" for s, v in versions))

    resolved = _resolved_firmware(firmware, build, chip, derived)
    if table is not None:
        resolve_partitions(resolved, table)

    snapshot = _snapshot(resolved, build, derived, now)
    problems: list[str] = []
    if incomplete:
        state = ResolveState.INCOMPLETE
        problems = incomplete
    elif build.configured_target and chip and build.configured_target != chip:
        state = ResolveState.TARGET_SWITCHED
        problems = [
            f"configured target: {build.configured_target}, build output: {chip}. "
            "The target was switched; rebuild the project."
        ]
    else:
        state = ResolveState.OK
    changes: list[Change] = []
    if last is not None:
        changes = _changes(firmware, resolved, last, snapshot, derived, marks)
    if state == ResolveState.OK and changes:
        content_only = all(
            c.kind == ChangeKind.CONTENT and c.subject in {d.name for d in derived} for c in changes
        )
        state = ResolveState.UPDATED if content_only else ResolveState.NEEDS_CONFIRMATION
    changes.sort(key=lambda c: _ORDER.index(c.kind))
    return Resolution(
        state=state,
        firmware=resolved,
        changes=tuple(changes),
        problems=tuple(problems),
        marks=marks,
        snapshot=snapshot,
        idf_version=build.idf_version,
        configured_target=build.configured_target,
    )


def missing(firmware: Firmware, problem: str) -> Resolution:
    """MISSING resolution showing the last_resolved snapshot (no entries without one)."""
    resolved = copy.deepcopy(firmware)
    last = firmware.last_resolved or {}
    derived: list[Entry] = []
    for doc in doc_tables(last, "entries"):
        key = doc_str(doc, "key")
        address = _hex(doc_str(doc, "address"))
        entry = Entry(
            id=key,
            name=doc_str(doc, "name"),
            file=doc_str(doc, "file"),
            sha256=doc_str(doc, "sha256"),
            size=doc_int(doc, "size"),
            kind=EntryKind(doc_str(doc, "kind", EntryKind.UNKNOWN.value)),
            address=address,
            auto_address=address,
            address_source=AddressSource.AUTO,
        )
        _apply_override(entry, firmware.entry_overrides.get(key))
        derived.append(entry)
    resolved.entries = derived
    return Resolution(
        state=ResolveState.MISSING,
        firmware=resolved,
        changes=(),
        problems=(problem,),
        marks={e.id: EntryMark.MISSING for e in derived},
        snapshot=last,
        idf_version=doc_opt_str(last, "idf_version"),
        configured_target=None,
    )


def accept(firmware: Firmware, resolution: Resolution) -> None:
    """Store resolution.snapshot as last_resolved and apply what confirmation implies:
    on a chip change, the chip and flash parameters follow the build and the manual
    addresses of derived entries are dropped; overrides whose key matches no derived entry
    are dropped. User entries are kept. Changed user entries take their new hash and size.
    ValueError for states other than OK, UPDATED and NEEDS_CONFIRMATION."""
    if resolution.state not in (
        ResolveState.OK,
        ResolveState.UPDATED,
        ResolveState.NEEDS_CONFIRMATION,
    ):
        raise ValueError(f"cannot accept a {resolution.state} build")
    resolved = resolution.firmware
    firmware.last_resolved = resolution.snapshot
    firmware.chip = resolved.chip
    firmware.flash = dataclasses.replace(resolved.flash)
    firmware.before, firmware.after = resolved.before, resolved.after
    keys = {doc_str(e, "key") for e in doc_tables(resolution.snapshot, "entries")}
    chip_changed = any(c.kind == ChangeKind.CHIP for c in resolution.changes)
    for key in list(firmware.entry_overrides):
        override = firmware.entry_overrides[key]
        if chip_changed:
            override.address = None
        if key not in keys or override == EntryOverride():
            del firmware.entry_overrides[key]


def digest(firmware: Firmware) -> str:
    """Short hash of what a flash would write: chip, flash parameters, reset modes, stub,
    erase_all and (address, sha256) of every enabled entry. Equal digests mean the same
    flash job. Works for fixed firmware too."""
    flash = firmware.flash
    parts: list[object] = [
        firmware.chip,
        flash.mode,
        flash.freq,
        flash.size,
        firmware.before,
        firmware.after,
        firmware.stub,
        firmware.erase_all,
        sorted((e.address or -1, e.sha256) for e in firmware.entries if e.enabled),
    ]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


_IDF_VER_CHARS = 31
"""Characters of idf_ver kept by the image descriptors."""

_ORDER = list(ChangeKind)


@dataclass
class _Derived:
    file: DerivedFile
    name: str
    kind: EntryKind
    key: str
    sha256: str
    size: int
    header: ImageHeader | None
    data_missing: bool


def _classify(
    file: DerivedFile,
    cache: FileCache,
    table: PartitionTable | None,
    last_kinds: Mapping[str, str],
) -> _Derived:
    name = file.path.name
    try:
        sha = cache.sha256(file.path)
        size = cache.size(file.path)
    except OSError:
        known = last_kinds.get(name)
        kind = EntryKind(known) if known else (file.expected or EntryKind.UNKNOWN)
        return _Derived(file, name, kind, entry_key(kind, name), "", 0, None, True)
    found = _sniffed.get((sha, table))
    if found is None:
        found = sniff(cache.read(file.path), name, table)
        _sniffed[(sha, table)] = found
    kind, header = found.kind, found.header
    return _Derived(file, name, kind, entry_key(kind, name), sha, size, header, False)


_sniffed: dict[tuple[str, PartitionTable | None], SniffResult] = {}
"""sniff() results by content hash and partition table; both fully determine the result."""


def _resolved_firmware(
    firmware: Firmware, build: DerivedBuild, chip: str | None, derived: list[_Derived]
) -> Firmware:
    resolved = copy.deepcopy(firmware)
    resolved.chip = chip or firmware.chip
    resolved.flash = dataclasses.replace(build.flash)
    if build.before is not None:
        resolved.before = build.before
    if build.after is not None:
        resolved.after = build.after
    resolved.stub = build.stub
    entries: list[Entry] = []
    seen: set[str] = set()
    for d in derived:
        if d.key in seen:  # the same file listed twice; flash it once
            continue
        seen.add(d.key)
        entry = Entry(
            id=d.key,
            name=d.name,
            file=f"path:{d.file.path}",
            sha256=d.sha256,
            size=d.size,
            kind=d.kind,
            address=d.file.address,
            auto_address=d.file.address,
            address_source=AddressSource.AUTO,
        )
        _apply_override(entry, firmware.entry_overrides.get(d.key))
        entries.append(entry)
    resolved.entries = entries
    return resolved


def _apply_override(entry: Entry, override: EntryOverride | None) -> None:
    if override is None:
        return
    if override.address is not None and override.address != entry.auto_address:
        entry.address = override.address
        entry.address_source = AddressSource.MANUAL
    if override.enabled is not None:
        entry.enabled = override.enabled
    if override.note is not None:
        entry.note = override.note


def _snapshot(
    resolved: Firmware, build: DerivedBuild, derived: list[_Derived], now: str
) -> Document:
    doc: Document = {"resolved_at": now}
    if resolved.chip:
        doc["chip"] = resolved.chip
    if build.idf_version:
        doc["idf_version"] = build.idf_version
    doc["flash"] = {
        "mode": resolved.flash.mode,
        "freq": resolved.flash.freq,
        "size": resolved.flash.size,
    }
    doc["reset"] = {"before": resolved.before, "after": resolved.after}
    table = next((d for d in derived if d.kind == EntryKind.PARTITION_TABLE), None)
    if table is not None and table.sha256:
        doc["partition_table"] = table.sha256
    entries: list[Document] = []
    for d in derived:
        item: Document = {
            "key": d.key,
            "name": d.name,
            "kind": d.kind.value,
            "address": f"{d.file.address:#x}",
            "file": f"path:{d.file.path}",
            "sha256": d.sha256,
            "size": d.size,
        }
        if d.header is not None:
            item["min_rev"] = d.header.min_rev_full
            item["max_rev"] = d.header.max_rev_full
        entries.append(item)
    doc["entries"] = entries
    return doc


def _changes(
    firmware: Firmware,
    resolved: Firmware,
    last: Document,
    snapshot: Document,
    derived: list[_Derived],
    marks: dict[str, EntryMark],
) -> list[Change]:
    changes: list[Change] = []

    def add(kind: ChangeKind, subject: str, before: str, after: str, message: str) -> None:
        changes.append(Change(kind, subject, before, after, message))

    old_chip, new_chip = doc_str(last, "chip"), doc_str(snapshot, "chip")
    chip_changed = bool(old_chip and new_chip and old_chip != new_chip)
    if chip_changed:
        add(ChangeKind.CHIP, "", old_chip, new_chip, f"Target chip: {old_chip} -> {new_chip}")
        add(
            ChangeKind.GROUP_HINT,
            "",
            firmware.device,
            firmware.device,
            "The device group may need adjusting",
        )
    old_idf, new_idf = doc_str(last, "idf_version"), doc_str(snapshot, "idf_version")
    idf_changed = bool(old_idf and new_idf and old_idf != new_idf)
    if idf_changed:
        add(ChangeKind.IDF_VERSION, "", old_idf, new_idf, f"ESP-IDF: {old_idf} -> {new_idf}")
    old_flash, new_flash = _flash_text(last), _flash_text(snapshot)
    if old_flash != new_flash:
        add(ChangeKind.FLASH, "", old_flash, new_flash, f"Flash: {old_flash} -> {new_flash}")
    old_reset, new_reset = _reset_text(last), _reset_text(snapshot)
    if old_reset != new_reset:
        add(ChangeKind.RESET, "", old_reset, new_reset, f"Reset: {old_reset} -> {new_reset}")
    old_table, new_table = doc_str(last, "partition_table"), doc_str(snapshot, "partition_table")
    if old_table and new_table and old_table != new_table:
        add(ChangeKind.PARTITION_TABLE, "", "", "", "Partition table content changed")

    old = {doc_str(e, "key"): e for e in doc_tables(last, "entries")}
    new = {doc_str(e, "key"): e for e in doc_tables(snapshot, "entries")}
    for key, doc in new.items():
        name = doc_str(doc, "name")
        before = old.get(key)
        if before is None:
            add(ChangeKind.ENTRY_ADDED, name, "", doc_str(doc, "address"), f"New file: {name}")
            marks.setdefault(key, EntryMark.NEW)
            continue
        old_rev, new_rev = _rev_text(before), _rev_text(doc)
        if old_rev and new_rev and old_rev != new_rev:
            add(
                ChangeKind.REVISION_RANGE,
                name,
                old_rev,
                new_rev,
                f"{name}: chip revision {old_rev} -> {new_rev}",
            )
        old_addr, new_addr = doc_str(before, "address"), doc_str(doc, "address")
        if old_addr != new_addr:
            override = firmware.entry_overrides.get(key)
            kept = (
                f" (manual address {override.address:#x} kept)"
                if override and override.address is not None
                else ""
            )
            add(
                ChangeKind.ADDRESS,
                name,
                old_addr,
                new_addr,
                f"{name}: address {old_addr} -> {new_addr}{kept}",
            )
        if doc_str(before, "sha256") != doc_str(doc, "sha256") and doc_str(doc, "sha256"):
            add(ChangeKind.CONTENT, name, "", "", f"{name}: content changed")
            marks.setdefault(key, EntryMark.CHANGED)
    for key, doc in old.items():
        if key not in new:
            name = doc_str(doc, "name")
            add(ChangeKind.ENTRY_REMOVED, name, doc_str(doc, "address"), "", f"Removed: {name}")
    for key, override in firmware.entry_overrides.items():
        if key not in new:
            add(
                ChangeKind.OVERRIDE_DROPPED,
                key.split(":", 1)[-1],
                "",
                "",
                f"{key.split(':', 1)[-1]}: your changes no longer match a file and are dropped",
            )
        elif override.address is not None and (chip_changed or idf_changed):
            name = doc_str(new[key], "name")
            fate = "dropped on confirmation" if chip_changed else "kept; check it"
            add(
                ChangeKind.MANUAL_ADDRESS,
                name,
                f"{override.address:#x}",
                "",
                f"{name}: manual address {override.address:#x} {fate}",
            )

    return changes


def _flash_text(doc: Document) -> str:
    flash = doc_table(doc, "flash") or {}
    return " ".join(doc_str(flash, k, "keep") for k in ("mode", "freq", "size"))


def _reset_text(doc: Document) -> str:
    reset = doc_table(doc, "reset") or {}
    return f"{doc_str(reset, 'before')} / {doc_str(reset, 'after')}"


def _rev_text(doc: Document) -> str:
    if "min_rev" not in doc:
        return ""
    low, high = doc_int(doc, "min_rev"), doc_int(doc, "max_rev")
    return f"{format_revision(low)} - {format_revision(high)}"


def _hex(text: str) -> int | None:
    try:
        return int(text, 16) if text else None
    except ValueError:
        return None
