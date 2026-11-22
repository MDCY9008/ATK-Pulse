# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Clean-room HID transport using hidapi directly with strict whitelist filtering."""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from contextlib import suppress
from typing import Callable, Iterable, Sequence

import hid

from .types import HidMatchRule, ReceiverEntry

log = logging.getLogger(__name__)

_CAPS_CACHE: dict[str, tuple[int | None, int | None]] = {}
_CAPS_CACHE_MAX = 512
_HIDP_STATUS_SUCCESS = 0x00110000


if sys.platform == "win32":
    class _HIDP_CAPS(ctypes.Structure):
        _fields_ = [
            ("Usage", ctypes.c_ushort),
            ("UsagePage", ctypes.c_ushort),
            ("InputReportByteLength", ctypes.c_ushort),
            ("OutputReportByteLength", ctypes.c_ushort),
            ("FeatureReportByteLength", ctypes.c_ushort),
            ("Reserved", ctypes.c_ushort * 17),
            ("NumberLinkCollectionNodes", ctypes.c_ushort),
            ("NumberInputButtonCaps", ctypes.c_ushort),
            ("NumberInputValueCaps", ctypes.c_ushort),
            ("NumberInputDataIndices", ctypes.c_ushort),
            ("NumberOutputButtonCaps", ctypes.c_ushort),
            ("NumberOutputValueCaps", ctypes.c_ushort),
            ("NumberOutputDataIndices", ctypes.c_ushort),
            ("NumberFeatureButtonCaps", ctypes.c_ushort),
            ("NumberFeatureValueCaps", ctypes.c_ushort),
            ("NumberFeatureDataIndices", ctypes.c_ushort),
        ]


def _decode_path(path: bytes | str) -> str:
    if isinstance(path, (bytes, bytearray)):
        return bytes(path).decode("utf-8", errors="ignore")
    return str(path or "")


def _is_real_windows_hid_path(path: bytes | str | None) -> bool:
    if not path:
        return False
    return _decode_path(path).lower().startswith("\\\\?\\hid#")


def query_hid_caps(path: bytes | str | None) -> tuple[int | None, int | None]:
    """Read (OutputReportByteLength, InputReportByteLength) without device I/O.

    Opens the Windows HID device path with dwDesiredAccess=0 (metadata-only) and
    queries HidP_GetCaps. Returns (None, None) on non-Windows or any failure.
    """
    if sys.platform != "win32" or not path:
        return None, None
    p = _decode_path(path)
    if not p:
        return None, None
    cached = _CAPS_CACHE.get(p)
    if cached is not None:
        return cached
    result: tuple[int | None, int | None] = (None, None)
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hidd = ctypes.WinDLL("hid", use_last_error=True)
        k32.CreateFileW.argtypes = (
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        )
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        k32.CloseHandle.restype = ctypes.c_int
        hidd.HidD_GetPreparsedData.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        hidd.HidD_GetPreparsedData.restype = ctypes.c_ubyte
        hidd.HidD_FreePreparsedData.argtypes = (ctypes.c_void_p,)
        hidd.HidD_FreePreparsedData.restype = ctypes.c_ubyte
        hidd.HidP_GetCaps.argtypes = (ctypes.c_void_p, ctypes.POINTER(_HIDP_CAPS))
        hidd.HidP_GetCaps.restype = ctypes.c_long

        invalid = ctypes.c_void_p(-1).value
        handle = k32.CreateFileW(p, 0, 3, None, 3, 0, None)
        if handle and handle != invalid:
            try:
                pp = ctypes.c_void_p()
                if hidd.HidD_GetPreparsedData(handle, ctypes.byref(pp)):
                    try:
                        caps = _HIDP_CAPS()
                        if hidd.HidP_GetCaps(pp, ctypes.byref(caps)) == _HIDP_STATUS_SUCCESS:
                            result = (
                                int(caps.OutputReportByteLength),
                                int(caps.InputReportByteLength),
                            )
                    finally:
                        hidd.HidD_FreePreparsedData(pp)
            finally:
                k32.CloseHandle(handle)
    except Exception:
        result = (None, None)

    if len(_CAPS_CACHE) >= _CAPS_CACHE_MAX:
        _CAPS_CACHE.clear()
    _CAPS_CACHE[p] = result
    return result


def matches_hid_rule(
    desc: dict,
    rule: HidMatchRule,
    *,
    caps_probe: Callable[[bytes | str | None], tuple[int | None, int | None]] | None = None,
) -> bool:
    """Return True if a hid.enumerate() descriptor satisfies the collection match rule."""
    if rule.usage_page is not None and desc.get("usage_page") != rule.usage_page:
        return False
    if rule.usage is not None and desc.get("usage") != rule.usage:
        return False
    if rule.interface_number is not None and desc.get("interface_number") != rule.interface_number:
        return False

    if rule.output_report_length is not None or rule.input_report_length is not None:
        out_len = desc.get("output_report_length")
        in_len = desc.get("input_report_length")
        if (
            ((rule.output_report_length is not None and out_len is None)
             or (rule.input_report_length is not None and in_len is None))
            and desc.get("path")
        ):
            probe = caps_probe or query_hid_caps
            c_out, c_in = probe(desc.get("path"))
            if out_len is None:
                out_len = c_out
            if in_len is None:
                in_len = c_in

        is_real_win = _is_real_windows_hid_path(desc.get("path"))
        if rule.output_report_length is not None:
            if out_len is not None:
                if int(out_len) != rule.output_report_length:
                    return False
            elif is_real_win:
                return False

        if rule.input_report_length is not None:
            if in_len is not None:
                if int(in_len) != rule.input_report_length:
                    return False
            elif is_real_win:
                return False

    return True


class HidTransport:
    """Single-transaction interrupt HID transport with bounded read slices and cancellation."""

    def __init__(
        self,
        *,
        enumerate_fn: Callable[[], list[dict]] | None = None,
        device_factory: Callable[[], object] | None = None,
        caps_probe: Callable[[bytes | str | None], tuple[int | None, int | None]] | None = None,
        read_timeout_ms: int = 1000,
        slice_timeout_ms: int = 200,
        write_delay_s: float = 0.1,
    ) -> None:
        self._enumerate = enumerate_fn or (lambda: hid.enumerate(0, 0))
        self._device_factory = device_factory or hid.device
        self._caps_probe = caps_probe or query_hid_caps
        self.read_timeout_ms = int(read_timeout_ms)
        self.slice_timeout_ms = int(slice_timeout_ms)
        self.write_delay_s = float(write_delay_s)

    def enumerate_all(self) -> list[dict]:
        """Enumerate HID interfaces without swallowing transport errors."""
        return list(self._enumerate())

    def filter_for_receiver(self, devices: Iterable[dict], receiver: ReceiverEntry) -> list[dict]:
        """Return raw descriptors for one whitelisted VID:PID."""
        return [
            d for d in devices
            if d.get("vendor_id") == receiver.vid and d.get("product_id") == receiver.pid
        ]

    def match_collections(self, devices: Iterable[dict], receiver: ReceiverEntry) -> list[dict]:
        """Return matching battery HID collections for one whitelisted receiver."""
        return [
            d for d in self.filter_for_receiver(devices, receiver)
            if matches_hid_rule(d, receiver.hid_match, caps_probe=self._caps_probe)
        ]

    def enumerate_whitelisted(
        self,
        receivers: Iterable[ReceiverEntry],
        *,
        devices: list[dict] | None = None,
    ) -> list[tuple[ReceiverEntry, list[dict], list[dict]]]:
        """Match bus snapshot strictly against whitelisted receivers.

        Returns list of (receiver, vid_pid_devices, matched_collections) for every
        whitelisted receiver currently present on the bus. Unknown devices (including
        unregistered 0xFF05 interfaces) are completely excluded. When multiple entries
        share the same (vid, pid) across protocol families (e.g. 373B:12AF), entries
        with matched collections supersede non-matching entries for that (vid, pid).
        """
        snapshot = list(devices) if devices is not None else self.enumerate_all()
        raw_hits: list[tuple[ReceiverEntry, list[dict], list[dict]]] = []
        matched_vid_pids: set[tuple[int, int]] = set()
        for receiver in receivers:
            vid_pid_devs = self.filter_for_receiver(snapshot, receiver)
            if not vid_pid_devs:
                continue
            matched = [
                d for d in vid_pid_devs
                if matches_hid_rule(d, receiver.hid_match, caps_probe=self._caps_probe)
            ]
            if matched:
                matched_vid_pids.add((receiver.vid, receiver.pid))
            raw_hits.append((receiver, vid_pid_devs, matched))

        hits: list[tuple[ReceiverEntry, list[dict], list[dict]]] = []
        seen_unmatched_vid_pids: set[tuple[int, int]] = set()
        for receiver, vid_pid_devs, matched in raw_hits:
            vp = (receiver.vid, receiver.pid)
            if vp in matched_vid_pids:
                if matched:
                    hits.append((receiver, vid_pid_devs, matched))
            elif vp not in seen_unmatched_vid_pids:
                seen_unmatched_vid_pids.add(vp)
                hits.append((receiver, vid_pid_devs, matched))
        return hits

    def transact(
        self,
        path: bytes | str,
        request: Sequence[int] | None,
        read_length: int,
        *,
        cancel_event: threading.Event | None = None,
        query_id: str = "",
        read_timeout_ms: int | None = None,
        write_delay_s: float | None = None,
    ) -> tuple[list[int] | None, str | None]:
        """Send one interrupt report and read one response within bounded timeout slices.

        Returns (raw_bytes, failure_reason). Always closes the HID handle in finally.
        """
        if not request:
            return None, "protocol_pending_capture"
        if cancel_event is not None and cancel_event.is_set():
            return None, "cancelled"

        effective_timeout_ms = self.read_timeout_ms if read_timeout_ms is None else int(read_timeout_ms)
        effective_delay_s = self.write_delay_s if write_delay_s is None else float(write_delay_s)

        dev = self._device_factory()
        phase = "open"
        try:
            dev.open_path(path)
            phase = "write"
            req_list = list(request)
            log.debug("query=%s HID write path_hash=%s len=%s", query_id, hash(path), len(req_list))
            written = dev.write(req_list)
            if written != len(req_list):
                return None, "short_write"

            if cancel_event is not None:
                if cancel_event.wait(effective_delay_s):
                    return None, "cancelled"
            else:
                time.sleep(effective_delay_s)

            phase = "read"
            deadline = time.monotonic() + effective_timeout_ms / 1000.0
            res: list[int] = []
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return None, "cancelled"
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    break
                res = list(dev.read(read_length, timeout_ms=min(self.slice_timeout_ms, remaining_ms)) or [])
                if res:
                    break
            if not res:
                return None, "timeout"
            log.debug("query=%s HID read len=%s", query_id, len(res))
            return res, None
        except OSError as exc:
            log.warning("query=%s HID %s failed: %s", query_id, phase, exc)
            return None, f"{phase}_error"
        finally:
            with suppress(Exception):
                dev.close()
