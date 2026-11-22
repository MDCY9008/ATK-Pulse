# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Clean-room HID transport using hidapi directly with strict whitelist filtering."""
from __future__ import annotations

import logging
import threading
import time
from contextlib import suppress
from typing import Callable, Iterable, Sequence

import hid

from .types import HidMatchRule, ReceiverEntry

log = logging.getLogger(__name__)


def matches_hid_rule(desc: dict, rule: HidMatchRule) -> bool:
    """Return True if a hid.enumerate() descriptor satisfies the collection match rule."""
    if rule.usage_page is not None and desc.get("usage_page") != rule.usage_page:
        return False
    if rule.usage is not None and desc.get("usage") != rule.usage:
        return False
    if rule.interface_number is not None and desc.get("interface_number") != rule.interface_number:
        return False
    return True


class HidTransport:
    """Single-transaction interrupt HID transport with bounded read slices and cancellation."""

    def __init__(
        self,
        *,
        enumerate_fn: Callable[[], list[dict]] | None = None,
        device_factory: Callable[[], object] | None = None,
        read_timeout_ms: int = 1000,
        slice_timeout_ms: int = 200,
        write_delay_s: float = 0.1,
    ) -> None:
        self._enumerate = enumerate_fn or (lambda: hid.enumerate(0, 0))
        self._device_factory = device_factory or hid.device
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
            if matches_hid_rule(d, receiver.hid_match)
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
        unregistered 0xFF05 interfaces) are completely excluded.
        """
        snapshot = list(devices) if devices is not None else self.enumerate_all()
        hits: list[tuple[ReceiverEntry, list[dict], list[dict]]] = []
        for receiver in receivers:
            vid_pid_devs = self.filter_for_receiver(snapshot, receiver)
            if not vid_pid_devs:
                continue
            matched = [d for d in vid_pid_devs if matches_hid_rule(d, receiver.hid_match)]
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
    ) -> tuple[list[int] | None, str | None]:
        """Send one interrupt report and read one response within bounded timeout slices.

        Returns (raw_bytes, failure_reason). Always closes the HID handle in finally.
        """
        if not request:
            return None, "protocol_pending_capture"
        if cancel_event is not None and cancel_event.is_set():
            return None, "cancelled"

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
                if cancel_event.wait(self.write_delay_s):
                    return None, "cancelled"
            else:
                time.sleep(self.write_delay_s)

            phase = "read"
            deadline = time.monotonic() + self.read_timeout_ms / 1000.0
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
