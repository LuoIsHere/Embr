## v0.1.1

- Added **Quick flash**: with it on, Flash works without Connect & identify first. The chip is still identified and checked during the flash, and writing is refused if it does not match. Off by default.
- Projects and groups can be reordered by dragging them in the sidebar.
- Followed builds are checked again on a click anywhere in the window. A renamed app file (ESP-IDF project name, PlatformIO `PROGNAME`) is followed without confirmation, and a renamed PlatformIO env can be followed with one click.
- An ESP-IDF build in progress is no longer accepted as a mix of new and old files.
- The selected port no longer moves to another board when it disappears; it is selected again when it comes back.
- Added **Show in Explorer** for entries and firmware. Entry status tags are shown next to the address, and the header shows when the files were last updated.
- The Records tab shows the 50 newest records first.
- Unexpected errors are written to a crash log in the `logs` folder and reported in the window.
- Fixed a number of issues in following builds, ESP32-P4 chip revisions, merging, archives and the interface. See the [full changelog](https://github.com/LuoIsHere/Embr/blob/main/docs/CHANGELOG.md#v011) for details.
