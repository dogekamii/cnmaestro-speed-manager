# PyInstaller one-file GUI build for Operations Toolkit.
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("ttkbootstrap")
datas += [("src/operations_toolkit/modules/cnmaestro/config/packages.json", "operations_toolkit/modules/cnmaestro/config")]

a = Analysis(
    ["operations_toolkit_entry.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=["operations_toolkit.modules.cnmaestro.ui"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="OperationsToolkit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
