"""Firmware <-> TOML document. Placement is stored as "address:0x..." or "partition:<name>"
(design doc §3.2); addresses of partition-placed entries are resolved at run time."""

from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import (
    AddressSource,
    BuildRef,
    Entry,
    EntryOverride,
    Firmware,
    FlashParams,
    Source,
)
from embr.core.storage.documents import (
    Document,
    doc_bool,
    doc_int,
    doc_opt_str,
    doc_str,
    doc_strs,
    doc_table,
    doc_tables,
)


def entry_to_doc(entry: Entry) -> Document:
    # what a reader looks for first: name, kind and where it goes; file checks last
    doc: Document = {"id": entry.id, "name": entry.name, "kind": entry.kind.value}
    if entry.partition is not None:
        doc["placement"] = f"partition:{entry.partition}"
        if entry.address is not None:  # last resolved (or manual) address
            doc["address"] = f"{entry.address:#x}"
    elif entry.address is not None:
        doc["placement"] = f"address:{entry.address:#x}"
    if entry.auto_address is not None:
        doc["auto_address"] = f"{entry.auto_address:#x}"
    doc["address_source"] = entry.address_source.value
    if entry.chip is not None:
        doc["chip"] = entry.chip
    if entry.chip_line:
        doc["chip_line"] = entry.chip_line
    doc["enabled"] = entry.enabled
    if entry.note:
        doc["note"] = entry.note
    doc |= {"file": entry.file, "sha256": entry.sha256, "size": entry.size}
    if entry.from_dump:
        doc["from_dump"] = True
    if entry.auto_added:
        doc["auto_added"] = True
    return doc


def entry_from_doc(doc: Document) -> Entry:
    placement = doc_str(doc, "placement")
    scheme, _, value = placement.partition(":")
    partition = value if scheme == "partition" else None
    address = _hex(value) if scheme == "address" else _hex(doc_str(doc, "address"))
    return Entry(
        id=doc_str(doc, "id"),
        name=doc_str(doc, "name"),
        file=doc_str(doc, "file"),
        sha256=doc_str(doc, "sha256"),
        size=doc_int(doc, "size"),
        kind=EntryKind(doc_str(doc, "kind", EntryKind.UNKNOWN.value)),
        address=address,
        auto_address=_hex(doc_str(doc, "auto_address")),
        address_source=AddressSource(doc_str(doc, "address_source", AddressSource.AUTO.value)),
        partition=partition,
        enabled=doc_bool(doc, "enabled", True),
        note=doc_str(doc, "note"),
        from_dump=doc_bool(doc, "from_dump"),
        auto_added=doc_bool(doc, "auto_added"),
        chip=doc_opt_str(doc, "chip"),
        chip_line=doc_str(doc, "chip_line"),
    )


def firmware_to_doc(fw: Firmware) -> Document:
    doc: Document = {"id": fw.id, "name": fw.name}
    if fw.project_id is not None:
        doc["project_id"] = fw.project_id
    doc |= {
        "device": fw.device,
        "notes": fw.notes,
        "tags": list(fw.tags),
        "source": fw.source.value,
    }
    if fw.chip is not None:
        doc["chip"] = fw.chip
    doc["flash"] = {"mode": fw.flash.mode, "freq": fw.flash.freq, "size": fw.flash.size}
    doc["reset"] = {"before": fw.before, "after": fw.after}
    doc["erase_all"] = fw.erase_all
    if fw.build_ref is not None:
        ref: Document = {"path": fw.build_ref.path}
        if fw.build_ref.env is not None:
            ref["env"] = fw.build_ref.env
        doc["build_ref"] = ref
    if fw.folder:
        doc["folder"] = fw.folder
    if fw.framework_pkg:
        doc["framework_pkg"] = fw.framework_pkg
    if fw.archived_from:
        doc["archived_from"] = fw.archived_from
    doc["created_at"] = fw.created_at
    doc["updated_at"] = fw.updated_at
    if not fw.source.tracking:
        doc["entries"] = [entry_to_doc(e) for e in fw.entries]
    if fw.entry_overrides:
        doc["entry_overrides"] = {k: _override_to_doc(v) for k, v in fw.entry_overrides.items()}
    if fw.last_resolved is not None:
        doc["last_resolved"] = fw.last_resolved
    return doc


def firmware_from_doc(doc: Document) -> Firmware:
    flash = doc_table(doc, "flash") or {}
    reset = doc_table(doc, "reset") or {}
    ref = doc_table(doc, "build_ref")
    overrides = doc_table(doc, "entry_overrides") or {}
    source = Source(doc_str(doc, "source", Source.MANUAL.value))
    return Firmware(
        id=doc_str(doc, "id"),
        name=doc_str(doc, "name"),
        project_id=doc_opt_str(doc, "project_id"),
        device=doc_str(doc, "device"),
        source=source,
        chip=doc_opt_str(doc, "chip"),
        notes=doc_str(doc, "notes"),
        tags=doc_strs(doc, "tags"),
        flash=FlashParams(
            doc_str(flash, "mode", "keep"),
            doc_str(flash, "freq", "keep"),
            doc_str(flash, "size", "keep"),
        ),
        before=doc_str(reset, "before", "default-reset"),
        after=doc_str(reset, "after", "hard-reset"),
        erase_all=doc_bool(doc, "erase_all"),
        entries=[entry_from_doc(d) for d in doc_tables(doc, "entries")],
        build_ref=BuildRef(doc_str(ref, "path"), doc_opt_str(ref, "env"))
        if ref is not None
        else None,
        folder=doc_str(doc, "folder"),
        framework_pkg=doc_str(doc, "framework_pkg"),
        archived_from=doc_str(doc, "archived_from"),
        entry_overrides={
            key: _override_from_doc(value)
            for key in overrides
            if (value := doc_table(overrides, key)) is not None
        },
        last_resolved=doc_table(doc, "last_resolved"),
        created_at=doc_str(doc, "created_at"),
        updated_at=doc_str(doc, "updated_at"),
    )


def _override_to_doc(o: EntryOverride) -> Document:
    doc: Document = {}
    if o.address is not None:
        doc["address"] = f"{o.address:#x}"
    if o.enabled is not None:
        doc["enabled"] = o.enabled
    if o.note is not None:
        doc["note"] = o.note
    return doc


def _override_from_doc(doc: Document) -> EntryOverride:
    enabled = doc.get("enabled")
    return EntryOverride(
        address=_hex(doc_str(doc, "address")),
        enabled=enabled if isinstance(enabled, bool) else None,
        note=doc_opt_str(doc, "note"),
    )


def _hex(text: str) -> int | None:
    try:
        return int(text, 16) if text else None
    except ValueError:
        return None
