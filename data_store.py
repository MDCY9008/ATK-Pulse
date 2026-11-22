# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Versioned append-only samples (v2) with strictly read-only legacy CSV support (V0.1/V0.2/v1)."""
from __future__ import annotations

import csv
import json
import logging
import os
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from build_info import APP_VERSION, SOURCE_VERSION_TAG

APP_DIR = Path(
    os.environ.get("ATK_BATTERY_DATA_DIR")
    or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ATK_Battery_Monitor_v02"
)
HISTORY_PATH = APP_DIR / "battery_history.csv"       # V0.2 legacy: read-only
V01_HISTORY = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ATK_Battery_Monitor" / "battery_history.csv"  # V0.1 legacy: read-only
SAMPLES_PATH = APP_DIR / "battery_samples.v1.csv"    # V0.2.1–V0.6.1 legacy: read-only in V0.7+
SAMPLES_V2_PATH = APP_DIR / "battery_samples.v2.csv" # V0.7+ multi-device append-only store
BINDINGS_PATH = APP_DIR / "device_bindings.json"
EVENTS_PATH = APP_DIR / "query_events.v1.jsonl"

HEADER = (
    "schema_version", "query_id", "request_id", "timestamp_local", "query_started_at", "query_completed_at",
    "model", "device_id", "device_identity_basis", "percent_reported", "packet_valid", "percent_valid",
    "voltage_raw", "voltage_mv", "voltage_validation", "charging_state", "status_byte", "link_byte",
    "query_mode", "recovered", "consecutive_failures_before", "elapsed_ms", "response_freshness", "app_version",
)
HEADER_V2 = HEADER + (
    "device_uid",
    "vid_pid",
    "model_id",
    "protocol_family",
    "link_mode",
    "battery_source_mode",
)

READ_WARNINGS: list[str] = []
DEFAULT_READ_LIMIT = 50000
log = logging.getLogger(__name__)
_warned: set[str] = set()


def _iso(value):
    return value.isoformat(timespec="milliseconds") if isinstance(value, datetime) else value


def _is_legacy_f1_v1_row(raw_model: str, raw_device_id: str) -> bool:
    return (
        raw_device_id.lower().startswith("373b:11d9:")
        or "atk f1 v2 ultimate" in raw_model.lower()
    )


def has_legacy_f1_v1_history(samples_path: Path | None = None) -> bool:
    """Read-only probe checking whether battery_samples.v1.csv contains ATK F1 V2 Ultimate records."""
    target = samples_path if samples_path is not None else SAMPLES_PATH
    if not target.is_file():
        return False
    try:
        with target.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for idx, r in enumerate(reader):
                if idx >= 50:
                    break
                if _is_legacy_f1_v1_row(r.get("model") or "", r.get("device_id") or ""):
                    return True
    except (OSError, UnicodeError, csv.Error):
        return False
    return False


def write_sample(event):
    """Append one validated reading strictly to battery_samples.v2.csv; never touch v1/legacy CSVs."""
    pct = event.get("percent")
    if (
        event.get("outcome") != "success"
        or not event.get("packet_valid")
        or not event.get("percent_valid")
        or type(pct) is not int
        or not 0 <= pct <= 100
    ):
        raise ValueError("Only a valid packet and legal device percent may become a sample")

    APP_DIR.mkdir(parents=True, exist_ok=True)
    empty = not SAMPLES_V2_PATH.exists() or SAMPLES_V2_PATH.stat().st_size == 0
    if not empty:
        with SAMPLES_V2_PATH.open("r", encoding="utf-8-sig", newline="") as f:
            if tuple(next(csv.reader(f), ())) != HEADER_V2:
                raise ValueError("Unexpected v2 sample schema; existing file left unchanged")
        with SAMPLES_V2_PATH.open("rb") as f:
            f.seek(-1, 2)
            if f.read(1) not in (b"\n", b"\r"):
                raise ValueError("Truncated v2 sample tail; refusing to append or rewrite history")

    device_uid = event.get("device_uid") or event.get("device_id") or ""
    vid_pid = (event.get("vid_pid") or "373B:11D9").upper()
    model_id = event.get("model_id") or "unknown"
    protocol_family = event.get("protocol_family") or "nordic52"
    link_mode = event.get("link_mode") or "unknown"
    battery_source_mode = event.get("battery_source_mode") or "firmware_reported"

    row = (
        2,
        event["query_id"],
        event.get("request_id"),
        _iso(event["at"]),
        _iso(event["started_at"]),
        _iso(event["completed_at"]),
        event.get("model_display_name") or event.get("name") or "",
        device_uid,
        event.get("device_identity_basis", "unknown"),
        pct,
        1,
        1,
        event.get("voltage_raw"),
        event.get("voltage_mv"),
        event.get("voltage_validation", "unknown"),
        "unknown",  # charging_state strictly unknown
        event.get("status_byte"),
        event.get("link_byte"),
        event["mode"],
        int(bool(event.get("recovered", False))),
        event.get("failures_before", 0),
        event["elapsed_ms"],
        "unverified",
        APP_VERSION,
        device_uid,
        vid_pid,
        model_id,
        protocol_family,
        link_mode,
        battery_source_mode,
    )
    with SAMPLES_V2_PATH.open("a", newline="", encoding="utf-8-sig" if empty else "utf-8") as f:
        writer = csv.writer(f)
        if empty:
            writer.writerow(HEADER_V2)
        writer.writerow(row)
        f.flush()


class _StrictRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        import sys
        raise sys.exc_info()[1] or OSError("Diagnostic write failed")


def write_query_event(event):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in event.items() if k != "last_success"}
    handler = _StrictRotatingHandler(EVENTS_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    try:
        record = logging.LogRecord(
            "query_events",
            logging.INFO,
            "",
            0,
            json.dumps(payload, default=_iso, ensure_ascii=False),
            (),
            None,
        )
        handler.emit(record)
    finally:
        handler.close()


def _warn(message: str):
    READ_WARNINGS.append(message)
    if message not in _warned:
        log.warning(message)
        _warned.add(message)


def _v2_source_label(app_version: str | None) -> str:
    raw = (app_version or "").strip().lstrip("Vv")
    parts = [p for p in raw.split(".") if p.isdigit()]
    if len(parts) >= 2:
        return f"V{parts[0]}.{parts[1]}"
    return SOURCE_VERSION_TAG


def read_samples(
    hours=None,
    limit=DEFAULT_READ_LIMIT,
    *,
    device_uid: str | None = None,
    include_legacy_f1: bool = False,
):
    """Read-only merge across V0.1, V0.2, v1, and v2 CSVs without mutating any original file.

    When `device_uid` is specified, strictly filters to rows matching that `device_uid`
    (plus legacy V0.1/V0.2/v1 F1 V2 records ONLY if `include_legacy_f1=True`),
    guaranteeing zero cross-device contamination.
    """
    READ_WARNINGS.clear()
    cutoff = datetime.now().astimezone() - timedelta(hours=hours) if hours else None
    rows: list[dict] = []

    sources = (
        (V01_HISTORY, "V0.1"),
        (HISTORY_PATH, "V0.2"),
        (SAMPLES_PATH, "V0.2.1"),
        (SAMPLES_V2_PATH, SOURCE_VERSION_TAG),
    )
    for path, source in sources:
        if not path.is_file():
            continue
        is_v2_file = path == SAMPLES_V2_PATH
        is_v1_file = path == SAMPLES_PATH
        selected: deque[dict] = deque(maxlen=limit)
        bad = 0
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                for line_no, r in enumerate(csv.DictReader(f), 2):
                    try:
                        if is_v1_file and r.get("schema_version") != "1":
                            raise ValueError("Unknown v1 schema")
                        if is_v2_file and r.get("schema_version") != "2":
                            raise ValueError("Unknown v2 schema")

                        raw_ts = r["timestamp_local"]
                        timestamp = datetime.fromisoformat(raw_ts)
                        timezone_status = "recorded" if timestamp.tzinfo else "unknown_assumed_local_for_display"
                        if timestamp.tzinfo is None:
                            timestamp = timestamp.astimezone()

                        pct = int(r["percent_reported"])
                        if not 0 <= pct <= 100:
                            raise ValueError("Invalid percent")
                        if cutoff and timestamp < cutoff:
                            continue

                        raw_mv = r.get("voltage_raw") if (is_v1_file or is_v2_file) else r.get("voltage_mv")
                        raw_mv = int(raw_mv) if raw_mv not in (None, "") else None
                        mv = raw_mv if raw_mv is not None and 2500 <= raw_mv <= 4500 else None
                        raw_link = r.get("link_byte") if (is_v1_file or is_v2_file) else None
                        link_byte = int(raw_link) if raw_link not in (None, "") else None

                        if is_v1_file or is_v2_file:
                            if r.get("packet_valid") != "1" or r.get("percent_valid") != "1" or not r.get("query_id"):
                                raise ValueError("Invalid validity flags")

                        raw_model = r.get("model", "") or ""
                        raw_dev_id = r.get("device_id") or ""
                        if is_v2_file:
                            row_source = _v2_source_label(r.get("app_version"))
                            row_uid = r.get("device_uid") or raw_dev_id
                            row_vid_pid = r.get("vid_pid") or ""
                            row_model_id = r.get("model_id") or ""
                            row_proto = r.get("protocol_family") or ""
                            row_link_mode = r.get("link_mode") or "unknown"
                            row_source_mode = r.get("battery_source_mode") or "firmware_reported"
                            is_legacy_f1 = False
                        elif is_v1_file:
                            row_source = source
                            is_legacy_f1 = _is_legacy_f1_v1_row(raw_model, raw_dev_id)
                            row_uid = raw_dev_id or ("legacy:373b:11d9" if is_legacy_f1 else "")
                            row_vid_pid = "373B:11D9" if is_legacy_f1 else ""
                            row_model_id = "atk_f1_v2_ultimate" if is_legacy_f1 else ""
                            row_proto = "nordic52" if is_legacy_f1 else ""
                            row_link_mode = "wireless_dongle" if is_legacy_f1 else "unknown"
                            row_source_mode = "legacy_v1"
                        else:
                            row_source = source
                            # V0.1 / V0.2 legacy CSV: no device_id column, inferred F1 V2 Ultimate only when requested
                            is_legacy_f1 = (not raw_model) or ("atk" in raw_model.lower()) or ("target" in raw_model.lower())
                            row_uid = "legacy:373b:11d9:v01_v02"
                            row_vid_pid = "373B:11D9" if is_legacy_f1 else ""
                            row_model_id = "atk_f1_v2_ultimate" if is_legacy_f1 else ""
                            row_proto = "nordic52" if is_legacy_f1 else ""
                            row_link_mode = "wireless_dongle"
                            row_source_mode = "legacy_inferred"

                        if device_uid is not None:
                            if row_uid == device_uid:
                                pass
                            elif include_legacy_f1 and source in ("V0.1", "V0.2", "V0.2.1") and is_legacy_f1:
                                pass
                            else:
                                continue

                        selected.append(
                            dict(
                                ts=timestamp,
                                timestamp_raw=raw_ts,
                                timezone_status=timezone_status,
                                percent=pct,
                                mv=mv,
                                voltage_raw=raw_mv,
                                link_byte=link_byte,
                                charging=None,
                                charging_state="unknown",
                                legacy_charging_raw=r.get("charging"),
                                voltage_validation=r.get("voltage_validation") or "legacy_candidate_unverified",
                                model=raw_model,
                                source=row_source,
                                source_file=str(path),
                                source_line=line_no,
                                device_id=raw_dev_id or row_uid,
                                device_uid=row_uid,
                                vid_pid=row_vid_pid,
                                model_id=row_model_id,
                                protocol_family=row_proto,
                                link_mode=row_link_mode,
                                battery_source_mode=row_source_mode,
                                query_id=r.get("query_id"),
                                recovered=r.get("recovered") == "1",
                                query_mode=r.get("query_mode", "unknown"),
                            )
                        )
                    except (ValueError, KeyError, TypeError, OverflowError):
                        bad += 1
        except (OSError, UnicodeError, csv.Error) as exc:
            _warn(f"{path.name}: read failed ({type(exc).__name__})")
        if bad:
            _warn(f"{path.name}: {bad} invalid/truncated rows skipped; original unchanged")
        rows.extend(selected)

    rows.sort(key=lambda x: x["ts"])
    return rows[-limit:] if limit is not None else rows
