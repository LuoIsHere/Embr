## v0.1.0

Initial release. Embr is a firmware manager and flasher for Espressif chips on Windows 10 and 11 (x64), using a built-in esptool 5.3.1.

### Highlights

- **Projects, groups and firmware**: firmware is organized into projects and groups or kept standalone, with notes, tags and search. Deleting a project or firmware removes it from Embr only and does not delete any `.bin` file or build output.
- **Firmware sources**: `.bin` files chosen by hand, ESP-IDF build directories, and PlatformIO environments (Arduino core 2.0.x on the official platform, and the ESP-IDF framework). Followed builds are re-checked automatically; content changes are followed, other changes require confirmation.
- **Recognition**: each file is classified by its content, with default addresses from esptool, the partition table and the build output. Every address can be changed. Attachments can be placed by partition or by address.
- **Safety checks**: flashing is refused for chips with flash encryption, Secure Boot or Secure Download Mode; for a chip or chip revision that does not match the images; and for overlapping entries or entries beyond the flash size.
- **Flashing**: Connect & identify, flash, erase and read flash. Each operation uses its own connection and releases the port when it ends. Cancellation works at any stage.
- **Merge, archive, export**: merge a firmware into a single `.bin`, archive a fixed copy of a build, and move projects or firmware between computers as `.embr` packages.
- **Records and logs**: every flash, erase and read operation is recorded, with daily logs and a log per device. The equivalent esptool command is always shown.
- **Interface**: English and Simplified Chinese, light and dark themes. Portable: all data stays in the program folder.

### Not supported

- PlatformIO projects using Arduino core 1.x. Arduino core 3.x (pioarduino) projects can be imported and flashed, but their support is not guaranteed.
- Chips with flash encryption, Secure Boot or Secure Download Mode enabled; reading or writing eFuses; the ESP8266.

The full list of changes is in [docs/CHANGELOG.md](docs/CHANGELOG.md).
