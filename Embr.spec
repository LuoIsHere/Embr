# PyInstaller spec: one onedir build, one GUI-subsystem exe; `Embr.exe --worker` runs the worker.
# Build with: uv run pyinstaller Embr.spec --noconfirm
import datetime
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# the build time shown in About (embr.build); a source checkout has no build.txt
build_file = Path("build/embr_build/build.txt")
build_file.parent.mkdir(parents=True, exist_ok=True)
build_file.write_text(
    datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC") + "\n", encoding="utf-8"
)

datas = [
    (str(build_file), "embr/resources"),
    ("src/embr/resources/esptool.cfg", "embr/resources"),
    ("src/embr/resources/logo/*.svg", "embr/resources/logo"),
    ("src/embr/resources/fonts/*", "embr/resources/fonts"),  # HarmonyOS Sans + license
]
datas += collect_data_files("esptool")  # stub flasher images
datas += collect_data_files("esp_pylib")

a = Analysis(
    ["src/embr/__main__.py"],
    pathex=["src"],
    datas=datas,
    # pyserial imports URL handlers (rfc2217://, socket://) by name at runtime
    hiddenimports=collect_submodules("esptool.targets")
    + collect_submodules("serial.urlhandler")
    + ["embr.core.flasher.worker.jobs"],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Embr",
    console=False,
    icon="src/embr/resources/logo/embr.ico",  # made by scripts/make_icon.py
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Embr", upx=False)
