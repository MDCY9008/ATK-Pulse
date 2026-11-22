# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""100% offline sanitized CSV & diagnostic ZIP exporters for ATK-Pulse."""
from __future__ import annotations

import csv
import getpass
import hashlib
import io
import json
import os
import re
import socket
import zipfile
from datetime import datetime
from pathlib import Path

from build_info import APP_NAME, APP_VERSION

REDACTED_CSV_HEADER = [
    "timestamp_local",
    "device_uid",
    "vid_pid",
    "model_id",
    "protocol_family",
    "link_mode",
    "battery_source_mode",
    "display_percent",
    "percent_reported",
    "voltage_mv",
    "voltage_raw",
    "link_byte",
    "charging_state",
    "query_mode",
]

_WIN_USER_RE = re.compile(r"(?i)([A-Za-z]:\\Users\\)([^\\/:*?\"<>|\s]+)")
_POSIX_USER_RE = re.compile(r"(/(?:Users|home)/)([^/\s]+)")
_HID_PATH_RE = re.compile(r"(?i)\\\\\?\\hid#[^\s\"'<>]+")
_WIN_ABS_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[^\s\"'<>]+)")

ECOSYSTEM_VIDS = frozenset({0x373B, 0x3554, 0x391D})
ECOSYSTEM_KEYWORDS = ("atk", "vxe", "vgn", "dragonfly", "blazing")


def salted_hash(raw_value: str, salt: str = "atk-pulse-offline-v1") -> str:
    """Return deterministic 16-char salted SHA256 digest for private identifiers."""
    text = str(raw_value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(f"{salt}:{text}".encode("utf-8", errors="ignore")).hexdigest()[:16]


def _default_protected_dirs() -> list[Path]:
    local_app = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    base = Path(local_app)
    return [
        (base / "ATK_Battery_Monitor_v02").resolve(),
        (base / "ATK_Battery_Monitor").resolve(),
    ]


def _assert_safe_output_path(output_path: Path) -> Path:
    resolved = Path(output_path).resolve()
    protected_names = {
        "battery_samples.v1.csv",
        "battery_samples.v2.csv",
        "battery_history.csv",
        "device_bindings.json",
        "device_events.v2.jsonl",
    }
    if resolved.name.lower() in protected_names:
        raise ValueError(f"Refusing to overwrite protected runtime file: {resolved.name}")
    for pdir in _default_protected_dirs():
        try:
            resolved.relative_to(pdir)
        except ValueError:
            continue
        raise ValueError("Refusing to write exports inside protected LOCALAPPDATA storage directories")
    return resolved


def sanitize_text(text: str, *, extra_secrets=()) -> str:
    """Redact OS username, hostname, drive-letter paths, HID paths, and raw secrets."""
    out = str(text or "")
    for secret in extra_secrets or ():
        s = str(secret or "").strip()
        if len(s) >= 3:
            out = out.replace(s, f"<SECRET:{salted_hash(s)[:8]}>")

    out = _HID_PATH_RE.sub("<HID_PATH_REDACTED>", out)
    out = _WIN_USER_RE.sub(r"<USER_HOME>", out)
    out = _POSIX_USER_RE.sub(r"\1<USER_REDACTED>", out)
    out = _WIN_ABS_PATH_RE.sub("<PATH_REDACTED>", out)

    candidates = set()
    for val in (
        os.environ.get("USERNAME"),
        os.environ.get("USER"),
        os.environ.get("COMPUTERNAME"),
        Path.home().name,
    ):
        if val and len(val.strip()) >= 3:
            candidates.add(val.strip())
    try:
        candidates.add(getpass.getuser())
    except Exception:
        pass
    try:
        candidates.add(socket.gethostname())
    except Exception:
        pass

    for item in sorted(candidates, key=len, reverse=True):
        if len(item) < 3:
            continue
        out = re.sub(re.escape(item), "<REDACTED>", out, flags=re.IGNORECASE)
    return out


def _sanitize_mapping(obj, *, extra_secrets=()):
    secret_keys = {
        "serial",
        "serial_number",
        "path",
        "device_path",
        "raw_path",
        "username",
        "hostname",
        "user",
        "home",
    }
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in secret_keys or "serial" in kl or kl.endswith("_path"):
                raw = str(v or "").strip()
                cleaned[k] = f"hash:{salted_hash(raw)}" if raw else ""
            else:
                cleaned[k] = _sanitize_mapping(v, extra_secrets=extra_secrets)
        return cleaned
    if isinstance(obj, (list, tuple)):
        return [_sanitize_mapping(x, extra_secrets=extra_secrets) for x in obj]
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    if isinstance(obj, str):
        return sanitize_text(obj, extra_secrets=extra_secrets)
    return obj


def _rows_to_redacted_csv_text(rows, *, estimates=None) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(REDACTED_CSV_HEADER)
    for idx, r in enumerate(rows or []):
        ts = r.get("ts")
        ts_text = ts.isoformat(timespec="seconds") if isinstance(ts, datetime) else sanitize_text(r.get("timestamp_raw", ""))
        if estimates is not None and idx < len(estimates):
            disp_pct = int(estimates[idx].display_percent)
        else:
            disp_pct = r.get("display_percent", r.get("percent", ""))
        uid = sanitize_text(str(r.get("device_uid") or r.get("device_id") or ""))
        writer.writerow([
            ts_text,
            uid,
            sanitize_text(str(r.get("vid_pid") or "")),
            sanitize_text(str(r.get("model_id") or "")),
            sanitize_text(str(r.get("protocol_family") or "")),
            sanitize_text(str(r.get("link_mode") or "")),
            sanitize_text(str(r.get("battery_source_mode") or "")),
            disp_pct,
            r.get("percent", ""),
            r.get("mv", "") if r.get("mv") is not None else "",
            r.get("voltage_raw", "") if r.get("voltage_raw") is not None else "",
            r.get("link_byte", "") if r.get("link_byte") is not None else "",
            "unknown",
            sanitize_text(str(r.get("query_mode") or "")),
        ])
    return buf.getvalue()


def export_cycle_csv(rows, output_path, *, estimates=None) -> Path:
    """Write sanitized battery cycle/history rows to a user-chosen CSV file."""
    target = _assert_safe_output_path(Path(output_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    csv_text = _rows_to_redacted_csv_text(rows, estimates=estimates)
    target.write_text(csv_text, encoding="utf-8-sig")
    return target


def _fmt_hex16(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, int):
        return f"0x{val:04X}"
    s = str(val).strip()
    if not s:
        return None
    try:
        return f"0x{int(s, 0):04X}"
    except ValueError:
        return sanitize_text(s)


def _parse_vid_pid_int(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    try:
        return int(str(val).strip(), 0)
    except ValueError:
        return None


def sanitize_hid_descriptors(raw_descriptors, *, caps_probe=None) -> list[dict]:
    """Filter to ATK/VXE/VGN ecosystem HID interfaces and strip all paths/serials."""
    out: list[dict] = []
    for d in raw_descriptors or ():
        if not isinstance(d, dict):
            continue
        vid_i = _parse_vid_pid_int(d.get("vendor_id"))
        pid_i = _parse_vid_pid_int(d.get("product_id"))
        if (vid_i is None or pid_i is None) and isinstance(d.get("vid_pid"), str) and ":" in d["vid_pid"]:
            parts = d["vid_pid"].split(":", 1)
            try:
                vid_i = int(parts[0], 16)
                pid_i = int(parts[1], 16)
            except ValueError:
                continue
        if vid_i is None or pid_i is None:
            continue

        prod_raw = str(d.get("product_string") or "")
        mfg_raw = str(d.get("manufacturer_string") or "")
        text = f"{prod_raw} {mfg_raw}".lower()
        if vid_i not in ECOSYSTEM_VIDS and not any(kw in text for kw in ECOSYSTEM_KEYWORDS):
            continue

        out_len = d.get("output_report_length")
        in_len = d.get("input_report_length")
        raw_path = d.get("path") or d.get("device_path")
        if (out_len is None or in_len is None) and raw_path and caps_probe is not None:
            try:
                c_out, c_in = caps_probe(raw_path)
                if out_len is None:
                    out_len = c_out
                if in_len is None:
                    in_len = c_in
            except Exception:
                pass

        if raw_path:
            path_str = bytes(raw_path).decode("utf-8", errors="ignore") if isinstance(raw_path, (bytes, bytearray)) else str(raw_path)
            path_hash = salted_hash(path_str)
        else:
            path_hash = str(d.get("path_hash") or "")

        serial_raw = str(d.get("serial_number") or "").strip()
        has_serial = bool(serial_raw) if "serial_number" in d else bool(d.get("has_serial", False))

        out.append({
            "vid_pid": f"{vid_i:04X}:{pid_i:04X}",
            "vendor_id": f"0x{vid_i:04X}",
            "product_id": f"0x{pid_i:04X}",
            "usage_page": _fmt_hex16(d.get("usage_page")),
            "usage": _fmt_hex16(d.get("usage")),
            "interface_number": d.get("interface_number"),
            "output_report_length": int(out_len) if out_len is not None else None,
            "input_report_length": int(in_len) if in_len is not None else None,
            "product_string": sanitize_text(prod_raw),
            "manufacturer_string": sanitize_text(mfg_raw),
            "has_serial": has_serial,
            "path_hash": path_hash,
        })
    out.sort(key=lambda item: (item["vid_pid"], str(item["interface_number"]), str(item["usage_page"]), str(item["usage"])))
    return out


def collect_sanitized_ecosystem_hid_descriptors(enumerate_fn=None, caps_probe=None) -> list[dict]:
    """Read-only HID enumeration of ATK/VXE/VGN ecosystem devices (never opens for I/O)."""
    try:
        if enumerate_fn is not None:
            raw = list(enumerate_fn() or [])
        else:
            import hid
            raw = list(hid.enumerate(0, 0) or [])
    except Exception:
        return []
    if caps_probe is None:
        try:
            from protocols.hid_transport import query_hid_caps
            caps_probe = query_hid_caps
        except Exception:
            caps_probe = None
    return sanitize_hid_descriptors(raw, caps_probe=caps_probe)


def export_redacted_diagnostic_zip(
    output_path,
    *,
    rows=None,
    estimates=None,
    device_info=None,
    hid_descriptors=None,
    extra_notes: str = "",
    extra_secrets=(),
) -> Path:
    """Create a 100% offline redacted diagnostic ZIP archive for GitHub issue submission."""
    target = _assert_safe_output_path(Path(output_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_rows = list(rows or [])

    collected_secrets = list(extra_secrets or ())
    if isinstance(device_info, dict):
        for sk in ("serial", "serial_number", "raw_serial", "device_path", "path"):
            if device_info.get(sk):
                collected_secrets.append(str(device_info[sk]))
    for d in hid_descriptors or ():
        if isinstance(d, dict):
            for sk in ("serial_number", "path", "device_path"):
                val = d.get(sk)
                if val:
                    s_val = bytes(val).decode("utf-8", errors="ignore") if isinstance(val, (bytes, bytearray)) else str(val)
                    if s_val:
                        collected_secrets.append(s_val)

    safe_device = _sanitize_mapping(device_info or {}, extra_secrets=collected_secrets)
    safe_hid = sanitize_hid_descriptors(hid_descriptors or [])
    manifest = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "offline_only": True,
        "redacted": True,
        "exported_at_local": datetime.now().isoformat(timespec="seconds"),
        "sample_count": len(safe_rows),
        "hid_descriptor_count": len(safe_hid),
        "device_summary": safe_device,
    }
    csv_text = _rows_to_redacted_csv_text(safe_rows, estimates=estimates)
    notes_text = sanitize_text(extra_notes or "No additional notes.", extra_secrets=collected_secrets)

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("device_profile_snapshot.json", json.dumps(safe_device, ensure_ascii=False, indent=2))
        zf.writestr("hid_descriptors_sanitized.json", json.dumps(safe_hid, ensure_ascii=False, indent=2))
        zf.writestr("samples_redacted.csv", csv_text)
        zf.writestr("diagnostic_notes.txt", notes_text + "\n")
    return target
