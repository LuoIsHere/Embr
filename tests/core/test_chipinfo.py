"""core/chipinfo: chip data must match the bundled esptool; nobody else imports it."""

import ast

import pytest
from embr_testkit import REPO, chip_defs
from esp_pylib.constants import ESPRESSIF_VID, USB_JTAG_SERIAL_PID

from embr.core import chipinfo


def test_chips_match_esptool() -> None:
    defs = chip_defs()
    assert [chip.name for chip in chipinfo.supported_chips()] == list(defs)
    for name, defn in defs.items():
        chip = chipinfo.get_chip(name)
        assert chip.display_name == defn.CHIP_NAME
        assert chip.image_chip_id == defn.IMAGE_CHIP_ID
        assert chip.bootloader_offset == defn.BOOTLOADER_FLASH_OFFSET
        assert sorted(chip.flash_sizes) == sorted(defn.FLASH_SIZES)
        assert sorted(chip.flash_freqs) == sorted(defn.FLASH_FREQUENCY)
        assert chip.usb_otg is bool(defn.USB_OTG_SUPPORTED)
        assert chip.usb_serial_jtag is bool(defn.USB_SERIAL_JTAG_SUPPORTED)
        assert chipinfo.chip_for_image_id(defn.IMAGE_CHIP_ID) == chip
        assert chipinfo.chip_for_display_name(defn.CHIP_NAME) == chip
        otg = chipinfo.usb_otg_chip(ESPRESSIF_VID, defn.IMAGE_CHIP_ID)
        assert otg == (chip if defn.USB_OTG_SUPPORTED else None)
    offsets = sorted({int(d.BOOTLOADER_FLASH_OFFSET) for d in defs.values()})
    assert chipinfo.bootloader_offsets() == tuple(offsets)
    assert chipinfo.espressif_vid() == ESPRESSIF_VID
    assert chipinfo.usb_serial_jtag_pid() == USB_JTAG_SERIAL_PID


def test_esp8266_and_unknown_chips() -> None:
    for name in ("esp8266", "esp32x"):
        with pytest.raises(chipinfo.UnknownChipError):
            chipinfo.get_chip(name)
    assert chipinfo.chip_for_image_id(0xFFFF) is None
    assert chipinfo.chip_for_display_name("ESP8266") is None


def test_header_code_names() -> None:
    assert [chipinfo.flash_mode_name(code) for code in range(5)] == [
        "qio",
        "qout",
        "dio",
        "dout",
        None,
    ]
    assert chipinfo.flash_size_name("esp32s3", 0x40) == "16MB"
    assert chipinfo.flash_freq_name("esp32s3", 0xF) == "80m"
    assert chipinfo.flash_freq_name("esp32c2", 0xF) == "60m"


def _imports_esptool_targets(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            names = [f"{node.value.id}.{node.attr}"]  # esptool.targets.X after `import esptool`
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]  # importlib.import_module("esptool.targets")
        if any(n == "esptool.targets" or n.startswith("esptool.targets.") for n in names):
            return True
    return False


def test_only_chipinfo_imports_esptool_targets() -> None:
    src = REPO / "src" / "embr"
    files = sorted(src.rglob("*.py"))
    assert files
    offenders = [
        str(path.relative_to(src))
        for path in files
        if not path.is_relative_to(src / "core" / "chipinfo")
        and _imports_esptool_targets(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"esptool.targets imported outside core/chipinfo: {offenders}"
