## v0.1.1

### Flashing

- Added **Quick flash**, a switch next to "Erase the whole flash first". With it on, Flash works without Connect & identify first. The flash job still identifies the chip and runs every check: writing is refused if the chip does not match or a security feature is enabled. It is off by default and kept in `settings.ini`.
  - A firmware for several chips, or with no chip set, still needs Connect & identify, and the hint above Flash says so.
  - With erase on, a warning says that the whole flash will be erased without a prior Connect & identify.
  - For USB-OTG and USB-Serial/JTAG ports, the hint says how the board enters download mode.
- The selected port no longer moves to another board. When it disappears (a cable unplugged, or a board re-enumerating after a reset), it stays selected as "<port> · unavailable" and the board actions are unavailable. When it comes back, it is selected again with its identification and the hint of the last job.
- A chip refused for Secure Download Mode, flash encryption or Secure Boot is now reset as the "reset after" setting says, instead of being left in download mode.
- After Connect & identify, a chip in Secure Download Mode is shown as "in download mode" instead of "reset, running".
- A job now always ends, even when something fails after the board is done (saving the record or the read data, remembering the baud rate, copying the board log). Before, Embr could stay busy until it was restarted. The log says whether the record was saved.

### Following builds

- A followed build is now also checked on a click anywhere in the Embr window and when files are dragged into it, so a build that finishes while Embr is active shows at once. At most one check is queued at a time, and none runs while a dialog or menu is open, while the window is minimized or while a job runs.
- A renamed app file is now followed automatically: renaming an ESP-IDF project, or changing the PlatformIO output name (`PROGNAME`). This applies when exactly one app entry disappears and one appears, at the same address and for the same chip. The entry keeps its note, check box and address, and the change is listed as "App file renamed: a -> b". The firmware name in the sidebar does not change.
- PlatformIO: the app file is taken from `prog_path` in `idedata.json`; otherwise the newest app image in the build folder is used. Before, the file name was always `firmware.bin`, and an old `firmware.bin` left in the build folder could be flashed after the output name changed.
- PlatformIO: when the followed env is no longer declared in `platformio.ini`, the banner says that it may have been renamed. If exactly one other env has been built and is not followed by another firmware, **Follow env** switches to it; otherwise **Change directory…** is offered.
- PlatformIO: envs declared in the files listed in `extra_configs` are found, and those files are watched for changes.
- ESP-IDF: a build in progress can no longer be accepted as a mix of new and old files. Before, a new bootloader with the old app could be shown as updated and flashable for several seconds during a build. An image older than its ELF file, or an ELF file older than the libraries it is linked from, now keeps the firmware in "Build in progress".
- Following stays correct when a file changes while it is being read. Before, a half-written build could stop all further checks until Embr was restarted.
- An ESP-IDF build that fails its first check right after import is no longer kept, so it can be imported again.
- PlatformIO projects using Arduino core 3.x (pioarduino) have been verified with pioarduino 55.03.311 (Arduino core 3.3.11) on an ESP32-S3: import, flashing and start-up. v0.1.0 did not guarantee their support.

### Projects and sidebar

- Projects and groups can be reordered by dragging them in the sidebar. A project moves with its groups and firmware; a group moves with its firmware and stays within its project. Esc cancels the drag. The project order is kept in `settings.ini`; the group order is kept in the project and in `.embr` packages. Firmware within a group is still sorted by name.
- The whole empty area below the list accepts dropped files and build folders, and is highlighted as one target.
- Group rows always show their number of firmware.
- After a rename or move, the header, the project path and the Parameters tab show the new values at once.
- Clicking the selected firmware again keeps its "Build updated" tag, its "content changed" marks and the hint of the last job.
- While a job runs, deleting a project and removing a group are unavailable.
- The project page no longer has "Erase the whole flash first" in its defaults for new firmware. New firmware starts with it off; the check box of each firmware is unchanged.
- Search waits for a short pause in typing before filtering.

### Entries

- Added **Show in Explorer** to the entry menu and the firmware menu (header and sidebar). It shows the entry's file, the build folder or the firmware's folder.
- The status tags of an entry ("content changed", "new file", "file missing", "added automatically", "placed by partition table", "address differs") are shown to the right of its address.
- The header shows when the entry files were last updated ("Files updated 14:32:05").
- ESP32-P4: a firmware no longer shows both "ESP32-P4" and "ESP32-P4 v0–v1" (or "v3"), is no longer shown as "multi" in the sidebar for that reason, and can be merged. This affected archived versions of followed firmware and merged images added to Embr. Data saved by v0.1.0 is shown correctly.
- ESP32-P4: an image whose header gives only the legacy minimum chip revision is assigned the revision line that esptool uses.
- Dropping a file that is already in the firmware no longer adds a second entry. If its content changed, the new content is accepted and the entry keeps its address, check box and note.
- **Copy to firmware…** no longer copies a file that the target firmware already has. **Re-locate…** and **Copy to firmware…** now say why nothing was done.
- Attachment addresses and the merge start offset accept hexadecimal values only. Before, values such as negative numbers were accepted.

### Merge

- Merging to an existing file asks before replacing it, and names the firmware that use that file. A failed merge leaves the existing file unchanged. The default file name no longer reuses the name of an earlier output.
- The note of a merged firmware names the source as project / group / firmware.

### Records

- The Records tab shows the 50 newest records, with a button to show earlier ones.

### Interface and wording

- Text being typed in the workspace is kept when the page refreshes in the background (a job ending, a port change, a build check).
- Chip names are shown as "ESP32-S3" instead of "esp32s3" in checks, banners and dialogs, and chip mismatch messages are complete sentences.
- The identification row "Flash" is now "Flash size".
- English terms are used consistently: "build folder", "board" and "enabled" entries.
- The remaining English texts of the Chinese interface are translated, and many Chinese texts were revised.
- The tooltips of the New menu and the entry menu are shown.
- The border between the sidebar and the workspace is removed, and the firmware name sits next to its tags without a gap.
- The Copy button of the equivalent command keeps its full width. Before, its text could be cut off, depending on the display scaling.
- Checking or clearing an entry, switching between firmware and opening a firmware with many records are faster.

### Data and robustness

- Embr now starts from a folder whose path contains `#` or `%`. Before, it could not open its data there.
- Unexpected errors are written to `logs/crash-<date>.log` and reported in the log panel. The first one of a run also opens a dialog with a button that opens the log folder.
- A damaged `.embr` package is reported as damaged, and an import that fails partway leaves nothing behind.
- Clean archive keeps the index entry of a file it could not delete, and tries again next time. An archived file whose content no longer matches its hash is written again.
- A failed write no longer leaves temporary files next to the data.

## v0.1.0

Initial release. Embr is a firmware manager and flasher for Espressif chips on Windows. It keeps track of every firmware a developer works with (where its files are, the addresses and parameters they are written with, and notes on what each one is) and flashes them through a built-in copy of esptool.

### Platform and distribution

- Runs on Windows 10 and Windows 11 (x64). Other operating systems are not supported.
- Portable: every file Embr creates (settings, data, logs, temporary files) is kept in the folder that contains `Embr.exe`. Nothing is written to `%APPDATA%`, the registry or the system temporary folder.
- Uses its built-in esptool 5.3.1 only; an esptool installed elsewhere on the computer is never used, and `esptool.cfg` files elsewhere on the computer do not affect it.
- Supported chips: the chips defined by the built-in esptool, except the ESP8266.

### Projects, groups and firmware

- Firmware can be organized into projects, and within a project into groups (for example, one group per board). Firmware can also be kept outside any project as standalone firmware.
- Projects: create, rename, edit notes, set the defaults for new firmware (reset before/after, erase the whole flash) and delete (optionally together with all of their firmware). A project overview lists its firmware by group.
- Groups: create, rename and delete. Firmware in a deleted group becomes ungrouped.
- Firmware: create, rename (`F2`), duplicate, move between projects and groups (by drag and drop in the sidebar or with the Move dialog) and delete. Each firmware has a name, notes and tags.
- Deleting a project or firmware removes it from Embr only. It does not modify or delete any file outside Embr's folder, such as `.bin` files and build output. Archived copies are kept until Clean archive removes the ones that no firmware uses, and flash records are kept.
- Global search over projects and firmware.
- A context menu on sidebar firmware offers the same actions as the firmware menu in the header.

### Firmware sources

- **Manual firmware**: built from `.bin` files chosen by the user. All files must be in the firmware's folder (the folder of the first file) or its subfolders.
- **ESP-IDF build**: follows an ESP-IDF `build` directory. Entries, addresses, flash parameters, the chip and the reset settings are read from `flasher_args.json`, with the project name, ESP-IDF version and target from `project_description.json`. Builds with flash encryption enabled are refused.
- **PlatformIO build**: follows one or more environments of a PlatformIO project. Supported: the Arduino framework (Arduino core 2.0.x on the official Espressif 32 platform) and the ESP-IDF framework. The image list is read from the `idedata.json` that PlatformIO writes for the environment; when that file is missing or older than `platformio.ini`, the build folder is scanned and each file is classified by its content. Embr does not run PlatformIO itself.
  - For the Arduino framework, `boot_app0.bin` (which selects the first OTA app partition) is not part of the build output. Embr takes it from a framework package set manually (in Settings or per firmware); otherwise from the package PlatformIO lists for the environment in `idedata.json`; otherwise from the installed package version named in `platform_packages`, or the highest installed version. Its address follows the `otadata` partition of the partition table.
  - The file is an entry like any other and is included when the firmware is merged, archived or exported. It is left out when the partition table has no `otadata` partition, and a blank otadata entry is used when no framework package provides it.
  - Projects using Arduino core 3.x (pioarduino) can be imported and flashed in the same way, but this version has not been fully verified with them, and their support is not guaranteed.
- **Archived firmware**: a fixed copy of a firmware whose files are stored in Embr's archive (see Archive).
- New firmware can also be created by dragging `.bin` files from File Explorer into the sidebar. Dragging an ESP-IDF build directory, a PlatformIO build directory or a PlatformIO project folder opens the matching import dialog with the path filled in.
- A build directory that is already followed by a firmware cannot be imported a second time.

### Entry recognition

- Each file is classified by its content: bootloader, application, partition table, otadata, merged image, file system image (LittleFS, FAT, and SPIFFS when a partition of the same size exists), or unknown.
- Default addresses come from the chip's bootloader offset in esptool, the partition table and the build output. Every address can be changed by hand; clearing an address restores the recognized value.
- Merged images are recognized, including merged images that start at a non-zero offset.
- When the partition table has an `otadata` partition and no otadata entry is selected, a blank otadata entry is added automatically and marked as such.
- Attachments (data that the firmware reads from flash, such as resources or fonts) can be placed by partition name or by address. A file larger than its partition is refused.
- Entries can be copied to another firmware.
- A manual firmware can contain entries for more than one chip. Entries without chip information can be assigned a chip by hand. Only entries for the identified chip are written.
- ESP32-P4 images are told apart by chip revision (v0–v1 and v3), following the revision range in their image headers.

### Following builds

- Followed firmware is checked when it is selected, when the Embr window becomes active again, when Flash is clicked, and every 60 seconds for the selected and recently used firmware. Files are re-read only when their size or modification time has changed.
- When a rebuild changes only file contents, Embr follows it automatically and shows the time of the update.
- Other changes (entries added or removed, addresses, chip, ESP-IDF version, chip revision range, flash parameters, reset settings, partition table) are listed, and the firmware cannot be flashed until the user confirms the entries.
- A firmware cannot be flashed while its build is incomplete, while the files of different builds or ESP-IDF versions are mixed, or after the configured target has changed but the build has not been redone. Embr names the cause.
- When the build output is missing, the last known entries are shown and the build directory can be changed.
- Embr never runs a build itself.

### Connection and identification

- The port list refreshes automatically when devices are connected or removed.
- The connection type is determined from the USB VID/PID before each connection: UART bridge, USB-Serial/JTAG or USB-OTG.
- The baud rate applies to UART bridges only; the last successful rate is remembered per USB VID/PID.
- **Connect & identify** reads the chip, chip revision, MAC address, flash size, security state and connection type, then disconnects and releases the port. The result is kept as a snapshot of the last identification. Firmware that does not match the identified chip or chip revision is dimmed in the sidebar.
- Embr never keeps a port open between operations. Every operation opens its own connection and releases the port when it ends, whatever the result.
- Reset settings that do not suit the connection type are adjusted for that connection (for example, `watchdog-reset` after flashing over USB-Serial/JTAG), and the adjustment is shown in the checklist. The firmware's own settings are not changed.

### Checks

- A checklist shows each check as passed, warning or refused before flashing: address overlap, file consistency (name, path and SHA-256), reset settings, chip, chip revision, flash size in the image header, security features and missing build output.
- Flashing is refused when:
  - the chip is in Secure Download Mode, or flash encryption or Secure Boot is enabled;
  - the identified chip differs from the firmware or from the chip in any selected image header;
  - the chip revision is outside the range declared by any selected image;
  - selected entries overlap, or an entry or a partition extends beyond the detected flash size;
  - an entry's file is missing or its content no longer matches.
- esptool's `force` option is never used.
- Flash is available only after the selected port has been identified and no check is refused. The flash operation identifies the chip again in its own connection and repeats every check.

### Flashing, erasing and reading flash

- Flash with `Ctrl+Enter`; cancel with `Esc`. Cancelling stops the operation at any stage and releases the port.
- Progress and the entry being written are shown; entries whose content is already in flash are skipped and reported.
- **Erase the whole flash** can be selected for a flash operation.
- **Erase flash…** erases the entire flash of the identified device after a confirmation that names the port, chip and MAC address.
- **Read flash…** reads the whole flash or a chosen range to a file. The result can also be imported as a firmware. The dialog warns that the contents may include sensitive data such as Wi-Fi credentials.
- Over native USB, Embr does not guarantee that the chip leaves download mode after flashing. It asks for a manual reset if the device does not restart. Flashing counts as successful once all data has been written and verified.

### Merge to .bin

- Any firmware can be merged into a single raw `.bin` image with esptool `merge-bin`, from its selected entries (including attachments) and flash parameters.
- Options: start offset, and padding to the full flash size. An existing target file is overwritten.
- The result can be added to Embr as a new firmware in the same project and group.

### Archive

- **Archive this version** creates a fixed copy of a followed firmware, including manual addresses and selections. The files are copied, never moved, into the archive, where identical contents are stored once.
- **Clean archive…** lists the archived files that no firmware uses, with their sizes, and deletes them after confirmation.

### Export and import

- A project, or one or more firmware, can be exported to a `.embr` package containing all referenced files. Followed firmware is exported as its current fixed contents. Firmware that cannot be flashed cannot be exported, and firmware that contains flash read from a device is exported only after a warning about sensitive data.
- Importing a `.embr` package validates the entire package (format version and the size and SHA-256 of every file) before anything is written. A package that fails validation is rejected without partial import.
- Imported projects and firmware receive new identities; conflicting names receive a suffix. Imported firmware is placed where the package is dropped or where the import dialog says.

### Records and logs

- Every flash, erase and read operation is recorded with its time, port, connection type, reset settings used, chip, revision, MAC address, detected flash size, the entries processed (with SHA-256), the result (`ok`, `failed`, `cancelled` or `refused`), the reason for a refusal, the duration, the esptool version and the equivalent command.
- The Records tab lists the history of the selected firmware. Selecting a record opens the matching part of the log.
- Logs are written per day. Operations on a device are also appended to a log for that device (by MAC address), which **Board log** opens.
- The log panel can be expanded to the full height of the workspace; **Log folder** opens the log folder.
- The bar at the bottom shows the equivalent esptool command for the selected firmware and port, with a copy button.

### Interface

- English and Simplified Chinese; the language follows the system by default and can be chosen in the menu (applied after a restart).
- Light and dark themes; the theme follows the system by default.
- Frameless window with resizable sidebar, side panel, log panel and entry table columns; sizes are remembered.
- `Ctrl+B` shows or hides the sidebar.

### Data safety

- Projects and firmware are stored as TOML files, one file each; records and the archive index are stored in an SQLite database. All carry a format version.
- Data written by a newer version of Embr is opened read-only and is not modified.
- A project or firmware file that cannot be read is skipped and reported; a copy is kept in the backup folder and the original is left unchanged.
- A damaged database is reported; the user can exit, or back it up to a chosen location and start with a new database.
- Only one instance of Embr runs from the same folder. Embr reports a program folder that cannot be written to, and offers to rename files that block its folders.

### Not supported

- PlatformIO projects using Arduino core 1.x.
- Chips with flash encryption, Secure Boot or Secure Download Mode enabled (detected and refused).
- Reading or writing eFuses.
- The ESP8266.
