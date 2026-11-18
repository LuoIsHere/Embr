"""The right panel: ports, identification, checks, the hint above Flash and Quick flash."""

from typing import TYPE_CHECKING

from embr.app.checklist import flash_ready, preflight
from embr.app.flashplan import plan_mcu, preview_command
from embr.app.identify import IdentifySnapshot
from embr.app.operations import (
    JobKind,
)
from embr.core.firmware.model import Firmware, editable_params
from embr.core.history import RecordResult
from embr.core.serial import DEFAULT_BAUD, ConnectionType, PortChange, PortInfo, default_baud
from embr.i18n import N_, tr
from embr.ui.adapters import settings as ui_settings

if TYPE_CHECKING:
    from embr.ui.window.main import EmbrWindow

COMMAND_UNKNOWN = N_("(identify the board, or add entries with addresses, to see the command)")
HINT_MANUAL_RESET = N_(
    "Flashed and verified. If the board did not restart on its own (usually when download "
    "mode was entered by hand), reset it manually."
)
HINT_DOWNLOAD_MODE = N_(
    "Identified. The chip stays in download mode so the next flash can start right away; "
    "reset the board to return to its application."
)
HINT_IDENTIFY = N_(
    "Connect & identify this board first; Flash becomes available when no check refuses."
)
HINT_OTG = N_(
    "Connect & identify this board first. USB-OTG: put the board into download mode as "
    "described by its vendor before connecting. Cancel is available while waiting."
)
HINT_JTAG = N_(
    "Connect & identify this board first. USB-Serial/JTAG: automatic reset is tried. If the "
    "running firmware does not keep this peripheral, the connect step times out; then put "
    "the board into download mode as described by its vendor."
)
QUICK_KEY = "quick_flash"
"""settings.ini [ui] quick_flash: the Quick flash switch, off by default."""
HINT_QUICK_NEEDS_ID = N_(
    "This firmware is for several chips or has no chip selected, so the board must be "
    "identified first."
)
HINT_QUICK_OTG = N_(
    "Quick flash over USB-OTG: put the board into download mode as described by its vendor "
    "before flashing. Cancel is available while waiting."
)
HINT_QUICK_JTAG = N_(
    "Quick flash over USB-Serial/JTAG: automatic reset is tried. If the running firmware "
    "does not keep this peripheral, the connect step times out; then put the board into "
    "download mode as described by its vendor."
)


class ConnectionPresenter:
    """Keeps the right panel and the command bar in step with the selection, the port
    and the last job."""

    def __init__(self, window: "EmbrWindow") -> None:
        self._window = window
        self.quick = ui_settings.load_flag(window.context.settings, QUICK_KEY, False)
        self._dismissed: tuple[int, str | None, str] | None = None
        """(id of the job outcome, port, text) of the hint the user clicked away."""
        self.ports_pending = False
        """Ports changed during a job; the list follows once it ends."""

    def refresh(self) -> None:
        if self._window.refresh_held("panel"):
            return
        self._window.header.setVisible(self._window.selection is not None)
        port = self._window.connection.selected_port()
        snapshot = self._window.memory.snapshot
        self._window.connection.show_identity(snapshot)
        firmware_id = self._window.selected_firmware_id()
        busy = self._window.ops.busy
        if firmware_id is None:
            self._window.connection.show_checks(None)
            self._window.connection.set_flash_enabled(False)
            self._window.cmdbar.set_command(tr("(select a firmware to see its command)"))
        else:
            # the status as the files are now, not the cached one: the page may be waiting
            # for a field it shows (an address typed with Enter), the checks may not
            status = self._window.library.tracker.check(firmware_id)
            firmware = status.firmware
            items = preflight(self._window.context, firmware, port, snapshot, status)
            self._window.connection.show_checks(items)
            quick = self.quick and plan_mcu(self._window.context, firmware) is not None
            ready = flash_ready(items, snapshot, port, quick_chip_known=quick)
            self._window.connection.set_flash_enabled(ready and not busy)
            command = preview_command(
                self._window.context,
                firmware,
                port,
                self._window.connection.selected_baud(),
                self._window.identified_chip(),
                self._window.identified_revision(),
            )
            self._window.cmdbar.set_command(command or tr(COMMAND_UNKNOWN))
        self._window.connection.identify.setEnabled(port is not None and not busy)
        self._window.connection.set_board_actions(
            port is not None
            and snapshot is not None
            and snapshot.port.device == port.device
            and not busy
        )
        self._show_erase_all(firmware_id, busy)
        text, kind = self.hint(port, snapshot)
        key = (id(self._window.jobs.last_outcome), port.device if port else None, text)
        self._window.connection.set_hint("" if key == self._dismissed else text, kind)

    def hint(self, port: PortInfo | None, snapshot: IdentifySnapshot | None) -> tuple[str, str]:
        """The hint above Flash: what the last job left behind, else what to do first. With
        Quick flash on and a firmware selected: why it still needs Connect &
        identify, or how the connection enters download mode, and a warning when the whole
        flash would be erased without a prior Connect & identify."""
        outcome = self._window.jobs.last_outcome
        if outcome is not None and outcome.result == RecordResult.OK:
            if outcome.kind == JobKind.FLASH and outcome.native_usb:
                return HINT_MANUAL_RESET, "warn"
            if (
                outcome.kind == JobKind.IDENTIFY
                and snapshot is not None
                and snapshot.info.in_download_mode
                and not snapshot.info.security.secure_download_mode  # a flash is refused
            ):
                return HINT_DOWNLOAD_MODE, "ok"
        if port is None or (snapshot is not None and snapshot.port.device == port.device):
            return "", "ok"
        firmware_id = self._window.selected_firmware_id()
        if self.quick and firmware_id is not None:
            return self._quick_hint(port, self._window.library.firmware(firmware_id))
        if port.connection == ConnectionType.USB_OTG:
            return HINT_OTG, "warn"
        if port.connection == ConnectionType.USB_SERIAL_JTAG:
            return HINT_JTAG, "ok"
        return HINT_IDENTIFY, "ok"

    def _quick_hint(self, port: PortInfo, firmware: Firmware) -> tuple[str, str]:
        """Quick flash on, this port not identified."""
        if plan_mcu(self._window.context, firmware) is None:
            return HINT_QUICK_NEEDS_ID, "ok"
        if firmware.erase_all:
            erase = N_(
                f"Quick flash with erase on: the whole flash of the board on {port.device} is "
                "erased without a prior Connect & identify."
            )
            return erase, "warn"
        if port.connection == ConnectionType.USB_OTG:
            return HINT_QUICK_OTG, "warn"
        if port.connection == ConnectionType.USB_SERIAL_JTAG:
            return HINT_QUICK_JTAG, "ok"
        return "", "ok"

    def set_quick(self, on: bool) -> None:
        """The Quick flash switch: kept in settings.ini at once."""
        self.quick = on
        self._window.connection.set_quick(on)
        settings = self._window.context.settings
        ui_settings.save_flag(settings, QUICK_KEY, on)
        settings.save()
        self._window.note("dim", f"[flash] quick flash {'on' if on else 'off'}")
        self.refresh()

    def dismiss_hint(self) -> None:
        """Keep a clicked-away hint hidden until the job outcome, port or text changes."""
        port = self._window.connection.selected_port()
        text, _ = self.hint(port, self._window.memory.snapshot)
        self._dismissed = (id(self._window.jobs.last_outcome), port.device if port else None, text)

    def _show_erase_all(self, firmware_id: str | None, busy: bool) -> None:
        if firmware_id is None:
            self._window.connection.set_erase_all(False, False, visible=False)
            return
        firmware = self._window.library.firmware(firmware_id)
        editable = "erase_all" in editable_params(firmware) and not firmware.read_only
        enabled = editable and not busy and not self._window.is_locked(firmware_id)
        self._window.connection.set_erase_all(firmware.erase_all, enabled, visible=True)

    def set_erase_all(self, checked: bool) -> None:
        firmware_id = self._window.selected_firmware_id()
        if firmware_id is None or self._window.ops.busy:
            return
        try:
            self._window.library.set_param(firmware_id, "erase_all", checked)
        except ValueError as exc:
            self._window.note("error", f"[params] {exc}")
        self._window.changed()

    def ports_changed(self, change: object) -> None:
        if isinstance(change, PortChange):
            self._window.memory.ports_changed(change)
        if self._window.ops.busy:
            # the job keeps its port; the list (and selection) follow once it ends
            self.ports_pending = True
            return
        self.apply_ports()

    def apply_ports(self) -> None:
        self.ports_pending = False
        before = self._window.connection.wanted
        self._window.connection.set_ports(self._window.ports.ports)
        after = self._window.connection.wanted
        if before is None or after is None or before.device != after.device:
            self.port_changed()
            return
        # the chosen port stayed, is away for now (a board re-enumerating after a reset) or
        # came back: its identification and the last job's hint stay
        now = self._window.connection.selected_port()
        if now is not None:
            self._window.memory.select_port(now)  # another device under the same name: forgotten
        self._window.refresh_tree()
        self.refresh()

    def port_changed(self) -> None:
        self._window.connection.keep_selection()
        port = self._window.connection.selected_port()
        self._window.memory.select_port(port)
        self._window.jobs.last_outcome = None
        self._window.connection.set_baud(
            port, default_baud(self._window.context.settings, port) if port else DEFAULT_BAUD
        )
        self._window.refresh_tree()
        self._window.refresh_workspace()  # entries for another chip turn grey, or back
        self.refresh()
