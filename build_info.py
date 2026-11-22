# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Application version, identity, and runtime metadata for ATK-Pulse."""
from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "ATK-Pulse"
DISPLAY_NAME = "ATK Battery Monitor"
APP_VERSION = "0.8.2"
VERSION_LABEL = "V0.8.2"
SOURCE_VERSION_TAG = f"V{'.'.join(APP_VERSION.split('.')[:2])}"
AUTHOR_NAME = "Liebe Kiana"
AUTHOR_HANDLE = "@liaman-li"
REPO_URL = "https://github.com/liaman-li/ATK-Pulse"
DEBUG_ENV_VAR = "ATK_PULSE_DEBUG"
LICENSE_SPDX = "GPL-3.0-or-later"


def resource_path(relative: str) -> Path:
    """Resolve bundled data file paths for both source runs and PyInstaller --onedir builds."""
    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parent
    return base / relative
