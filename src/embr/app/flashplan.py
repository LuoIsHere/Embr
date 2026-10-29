"""FlashPlan from a firmware plus the port and baud chosen in the connection panel
(design doc §7.2). The worker call and the equivalent command line come from the same plan.
"""

from embr.app.context import AppContext
from embr.core.firmware.model import Firmware
from embr.core.flasher.plan import FlashPlan, PlanEntry
from embr.core.serial import PortInfo

PREVIEW_PORT = "PORT"
"""Port shown in the equivalent command while no port is selected."""


class PlanError(ValueError):
    """The firmware cannot be turned into a plan (no chip, an enabled entry without an
    address or whose file is missing)."""


def plan_entries(context: AppContext, firmware: Firmware, *, write_files: bool) -> list[PlanEntry]:
    """Enabled entries by address with file paths the worker can read: "path:" as is,
    "archive:" -> data/archive/<sha256>.bin, "blank:" -> temp/blank-<size>.bin (0xFF bytes;
    written only when `write_files`, the path is the same either way). Image header
    fields (chip, revision range) are read from the files. Raises PlanError."""
    raise NotImplementedError


def assemble_plan(
    context: AppContext,
    firmware: Firmware,
    port: PortInfo,
    baud: int | None,
    *,
    write_files: bool = True,
) -> FlashPlan:
    """Plan for `firmware` on `port`: reset modes adjusted to the connection type, baud
    ignored on native USB, flash settings from the firmware, declared flash size from the
    firmware ("keep"/"detect": the bootloader image header), partitions_end from the
    enabled partition table. The firmware must have a chip (Operations.flash fills it from
    the identified chip first). Raises PlanError."""
    raise NotImplementedError


def preview_command(
    context: AppContext, firmware: Firmware, port: PortInfo | None, baud: int | None
) -> str:
    """Equivalent esptool command for the command bar; writes nothing. Without a port it
    uses PREVIEW_PORT on a UART bridge. A firmware that cannot be planned gives ""."""
    raise NotImplementedError
