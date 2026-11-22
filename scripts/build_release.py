# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Build Windows portable --onedir release & slimmed ZIP archive for ATK-Pulse."""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from build_info import APP_NAME, VERSION_LABEL  # noqa: E402

EXCLUDED_MODULES = (
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtQml",
    "PySide6.QtQmlModels",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DExtras",
    "PySide6.QtSql",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtCharts",
    "PySide6.QtGraphs",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtDBus",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtPrintSupport",
    "tkinter",
    "unittest",
    "email",
    "http",
    "pydoc",
    "doctest",
    "xmlrpc",
)


def _prune_dist(app_dist_dir: Path) -> int:
    """Remove unused PySide6 DLLs, non-zh_CN translations, and redundant plugins."""
    pyside_dir = app_dist_dir / "_internal" / "PySide6"
    removed_bytes = 0

    def _rm(p: Path):
        nonlocal removed_bytes
        if p.is_file():
            removed_bytes += p.stat().st_size
            p.unlink()
        elif p.is_dir():
            for child in p.rglob("*"):
                if child.is_file():
                    removed_bytes += child.stat().st_size
            shutil.rmtree(p, ignore_errors=True)

    # 1) Unused heavy DLLs/PYDs in PySide6 root
    for pattern in (
        "opengl32sw.dll",
        "Qt6Quick*.dll",
        "Qt6Qml*.dll",
        "Qt6Pdf*.dll",
        "Qt6OpenGL*.dll",
        "Qt6Network*.dll",
        "QtNetwork.pyd",
        "Qt6Svg*.dll",
        "Qt6VirtualKeyboard*.dll",
    ):
        for target in pyside_dir.glob(pattern):
            _rm(target)

    # 2) Translations: keep only Simplified Chinese Qt base catalogs
    tr_dir = pyside_dir / "translations"
    if tr_dir.is_dir():
        keep_tr = {"qtbase_zh_CN.qm", "qt_zh_CN.qm"}
        for item in tr_dir.iterdir():
            if item.name not in keep_tr:
                _rm(item)

    # 3) Plugins: keep minimal runtime set (qwindows, qoffscreen, qico/qjpeg/qwebp, qmodernwindowsstyle)
    plugins_dir = pyside_dir / "plugins"
    if plugins_dir.is_dir():
        for subdir in ("generic", "iconengines", "networkinformation", "platforminputcontexts", "tls"):
            _rm(plugins_dir / subdir)
        plat_dir = plugins_dir / "platforms"
        if plat_dir.is_dir():
            keep_plat = {"qwindows.dll", "qoffscreen.dll"}
            for item in plat_dir.iterdir():
                if item.name not in keep_plat:
                    _rm(item)
        img_dir = plugins_dir / "imageformats"
        if img_dir.is_dir():
            keep_img = {"qico.dll", "qjpeg.dll", "qwebp.dll"}
            for item in img_dir.iterdir():
                if item.name not in keep_img:
                    _rm(item)

    return removed_bytes


def build() -> tuple[Path, Path]:
    ico = ROOT / "assets" / "atk_pulse.ico"
    png = ROOT / "assets" / "atk_pulse.png"
    registry = ROOT / "protocols" / "device_registry.json"
    license_file = ROOT / "LICENSE"
    guide_file = ROOT / "使用说明.txt"
    entry = ROOT / "main.py"

    for req in (ico, png, registry, license_file, guide_file, entry):
        if not req.is_file():
            raise FileNotFoundError(f"Required release asset missing: {req}")

    dist_dir = ROOT / "dist"
    build_dir = ROOT / "build"
    app_dist_dir = dist_dir / APP_NAME
    if app_dist_dir.exists():
        shutil.rmtree(app_dist_dir)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--noconsole",
        "--name",
        APP_NAME,
        "--icon",
        str(ico),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(build_dir),
        "--add-data",
        f"{registry};protocols",
        "--add-data",
        f"{ROOT / 'assets'};assets",
        "--hidden-import",
        "hid",
        "--collect-binaries",
        "hid",
    ]
    for mod in EXCLUDED_MODULES:
        cmd.extend(["--exclude-module", mod])
    cmd.append(str(entry))

    print("Running:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)

    exe_path = app_dist_dir / f"{APP_NAME}.exe"
    if not exe_path.is_file():
        raise FileNotFoundError(f"Expected EXE was not created: {exe_path}")

    pruned_bytes = _prune_dist(app_dist_dir)
    folder_bytes = sum(p.stat().st_size for p in app_dist_dir.rglob("*") if p.is_file())

    zip_path = dist_dir / f"{APP_NAME}-{VERSION_LABEL}-win64.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for file_path in sorted(app_dist_dir.rglob("*")):
            if file_path.is_file():
                arcname = Path(APP_NAME) / file_path.relative_to(app_dist_dir)
                zf.write(file_path, arcname=arcname.as_posix())
        zf.write(license_file, arcname="LICENSE")
        zf.write(guide_file, arcname="使用说明.txt")

    print(f"Pruned unused PySide6 files: {pruned_bytes:,} bytes ({pruned_bytes / 1024 / 1024:.2f} MB)")
    print(f"ONEDIR folder size: {folder_bytes:,} bytes ({folder_bytes / 1024 / 1024:.2f} MB)")
    print(f"EXE ready: {exe_path} ({exe_path.stat().st_size:,} bytes)")
    print(f"ZIP ready: {zip_path} ({zip_path.stat().st_size:,} bytes / {zip_path.stat().st_size / 1024 / 1024:.2f} MB)")
    return exe_path, zip_path


if __name__ == "__main__":
    build()
