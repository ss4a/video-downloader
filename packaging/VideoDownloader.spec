# PyInstaller spec — builds two variants from the same analysis:
#   dist/VideoDownloader/                 one-folder build (used by the installer; fast startup)
#   dist/VideoDownloader_Portable.exe     single-file portable build
#
# Build with:  pyinstaller packaging/VideoDownloader.spec --noconfirm
from pathlib import Path

ROOT = Path(SPECPATH).parent
VENDOR_BIN = ROOT / "vendor" / "bin"

# Bundled ffmpeg (+ its shared DLLs) and Deno go in "bin". They are added as
# plain data so PyInstaller doesn't dependency-scan them and copy the DLLs twice.
datas = [(str(ROOT / "static"), "static")]
datas += [(str(p), "bin") for p in VENDOR_BIN.iterdir() if p.suffix.lower() in (".exe", ".dll")]
binaries = []

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["webview.platforms.edgechromium", "webview.platforms.winforms", "clr_loader", "pythonnet"],
    excludes=["tkinter", "PIL", "numpy", "pytest", "webview.platforms.qt", "webview.platforms.gtk",
              "webview.platforms.cocoa", "webview.platforms.android", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
# PyInstaller still dependency-scans the ffmpeg exes and copies their DLLs to the
# bundle root as well; drop those duplicates (the copies in bin/ are the ones used).
_vendor_names = {p.name.lower() for p in VENDOR_BIN.iterdir()}
a.binaries = [b for b in a.binaries if b[0].replace("\\", "/").lower() not in _vendor_names]
a.datas = [d for d in a.datas if d[0].replace("\\", "/").lower() not in _vendor_names]

pyz = PYZ(a.pure)

common = dict(
    debug=False,
    strip=False,
    upx=False,          # UPX trips antivirus heuristics; not worth it
    console=False,      # windowed app, no console
    icon=str(ROOT / "packaging" / "app.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)

# ---- one-folder build (installer) ----
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VideoDownloader", **common)
COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VideoDownloader")

# ---- single-file portable build ----
EXE(pyz, a.scripts, a.binaries, a.datas, [], name="VideoDownloader_Portable",
    runtime_tmpdir=None, **common)
