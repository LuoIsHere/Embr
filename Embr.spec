# PyInstaller spec: one onedir build, one GUI-subsystem exe; `Embr.exe --worker` runs the worker.
# Build with: uv run pyinstaller Embr.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [("src/embr/resources/esptool.cfg", "embr/resources")]
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
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Embr", upx=False)
