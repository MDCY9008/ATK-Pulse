# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Whitelist-driven multi-protocol HID queries. Host receipt time is not device measurement time."""
from __future__ import annotations

import ctypes
import hashlib
import logging
import time
from contextlib import contextmanager
from datetime import datetime

from data_store import BINDINGS_PATH, has_legacy_f1_v1_history
from protocols import (
    CHARGING_STATE_UNKNOWN,
    BindingStore,
    DeviceRegistry,
    HidTransport,
    get_codec,
    make_device_uid,
    resolve_binding,
)

log = logging.getLogger(__name__)
VID, PID = 0x373B, 0x11D9

_DEFAULT_REGISTRY = DeviceRegistry.load()
MODEL = _DEFAULT_REGISTRY.get_model("atk_f1_v2_ultimate")

ECOSYSTEM_VIDS = frozenset({0x373B, 0x3554, 0x391D})
ECOSYSTEM_KEYWORDS = ("atk", "vxe", "vgn", "dragonfly", "blazing")


def find_unregistered_ecosystem_vid_pids(all_devices: list[dict], registry: DeviceRegistry) -> list[str]:
    """Pure in-memory scan for ATK/VXE/VGN ecosystem devices missing from whitelist."""
    found: set[str] = set()
    for d in all_devices or ():
        vid = d.get("vendor_id")
        pid = d.get("product_id")
        if vid is None or pid is None:
            continue
        vid_i, pid_i = int(vid), int(pid)
        if registry.get_receivers(vid_i, pid_i) or registry.is_excluded(vid_i, pid_i):
            continue
        prod = str(d.get("product_string") or "").lower()
        mfg = str(d.get("manufacturer_string") or "").lower()
        text = f"{prod} {mfg}"
        if vid_i in ECOSYSTEM_VIDS or any(kw in text for kw in ECOSYSTEM_KEYWORDS):
            found.add(f"{vid_i:04X}:{pid_i:04X}")
    return sorted(found)


@contextmanager
def device_guard(mutex_name: str = "Local\\ATK_Battery_373B_11D9_query"):
    """Cooperative per-device lock; older apps and vendor hubs do not participate."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    kernel.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise OSError("Cannot create query mutex")
    owned = False
    try:
        code = kernel.WaitForSingleObject(handle, 0)
        owned = code in (0, 0x80)
        if not owned:
            if code == 0x102:
                raise BlockingIOError("Another cooperating process is querying")
            raise OSError(f"Device mutex wait failed: {code}")
        yield
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


class TargetQuery:
    """Clean-room whitelist HID query runner supporting nordic52, nordic54, evision_nearlink, and nano_ff05."""

    def __init__(
        self,
        enumerate_fn=None,
        device_factory=None,
        guard=None,
        *,
        registry: DeviceRegistry | None = None,
        binding_store: BindingStore | None = None,
        legacy_history_probe=None,
        transport: HidTransport | None = None,
    ):
        self.registry = registry or _DEFAULT_REGISTRY
        self.binding_store = binding_store or BindingStore(BINDINGS_PATH)
        self.legacy_history_probe = legacy_history_probe or has_legacy_f1_v1_history
        self.transport = transport or HidTransport(
            enumerate_fn=enumerate_fn,
            device_factory=device_factory,
        )
        self.guard = guard or device_guard
        self._cid_mid_cache: dict[tuple[str, str], tuple[int, int]] = {}

    def _select_receiver_hit(self, devices: list[dict]):
        """Select highest-priority whitelisted receiver present on the bus."""
        hits = self.transport.enumerate_whitelisted(self.registry.receiver_list(), devices=devices)
        if not hits:
            return None
        # Prefer hits that also have a bound device_uid in BindingStore, then ready protocol, then wireless-first order.
        bindings = self.binding_store.load_all()
        for receiver, vid_pid_devs, matched in hits:
            if len(matched) == 1:
                t = matched[0]
                uid, _ = make_device_uid(receiver.vid, receiver.pid, t.get("serial_number"), t["path"])
                if uid in bindings:
                    return receiver, vid_pid_devs, matched
        for receiver, vid_pid_devs, matched in hits:
            if get_codec(receiver.protocol_family).protocol_status == "ready" and len(matched) == 1:
                return receiver, vid_pid_devs, matched
        return hits[0]

    def __call__(self, query_id, mode, cancel):
        started = time.monotonic()
        event = dict(
            query_id=query_id,
            mode=mode,
            manual=mode == "manual",
            started_at=datetime.now().astimezone(),
            at=None,
            receiver_visibility="unknown",
            outcome="internal_error",
            connected=False,
            receiver=None,
            percent=None,
            voltage_mv=None,
            voltage_raw=None,
            voltage_validation="unknown",
            charging=None,
            charging_state=CHARGING_STATE_UNKNOWN,
            packet_valid=False,
            percent_valid=False,
            status_byte=None,
            link_byte=None,
            device_id=None,
            device_uid=None,
            device_identity_basis="unknown",
            vid_pid=None,
            model_id=None,
            model_display_name="未检测到已适配设备",
            brand="ATK-Pulse",
            name="未检测到已适配设备",
            verification_tier="catalog_only",
            calibration_status="uncalibrated",
            lut_profile_id=None,
            protocol_family=None,
            link_mode="unknown",
            binding_state="unknown",
            candidate_models=[],
            recommended_model_id=None,
            unregistered_ecosystem_vid_pids=[],
            cid_mid=None,
            battery_source_mode="firmware_reported",
            response_freshness="unverified",
            device_measured_at=None,
        )
        phase = "enumeration"
        try:
            if cancel.is_set():
                event["outcome"] = "cancelled"
                return event

            all_devices = self.transport.enumerate_all()
            hit = self._select_receiver_hit(all_devices)
            if hit is None:
                event.update(
                    receiver_visibility="absent",
                    receiver=False,
                    outcome="receiver_absent",
                    unregistered_ecosystem_vid_pids=find_unregistered_ecosystem_vid_pids(all_devices, self.registry),
                )
                return event

            receiver, vid_pid_devs, matches = hit
            event.update(
                receiver_visibility="present",
                receiver=True,
                vid_pid=receiver.vid_pid,
                protocol_family=receiver.protocol_family,
                link_mode=receiver.link_role,
            )
            log.info(
                "query=%s receiver=%s collections=%s matched=%s",
                query_id,
                receiver.vid_pid,
                len(vid_pid_devs),
                len(matches),
            )
            if len(matches) != 1:
                event["outcome"] = "interface_not_found" if not matches else "ambiguous_interface"
                return event

            target = matches[0]
            path = target["path"]
            path_bytes = path if isinstance(path, bytes) else str(path).encode("utf-8")
            event["collection_path_hash"] = hashlib.sha256(path_bytes).hexdigest()[:16]

            device_uid, identity_basis = make_device_uid(
                receiver.vid,
                receiver.pid,
                target.get("serial_number"),
                path,
            )
            resolved = resolve_binding(
                receiver,
                device_uid,
                identity_basis,
                self.registry,
                self.binding_store,
                has_legacy_f1_v1_history=bool(self.legacy_history_probe()),
            )
            candidates_payload = [
                dict(
                    model_id=m.model_id,
                    brand=m.brand,
                    display_name=m.display_name,
                    verification_tier=m.verification_tier,
                    calibration_status=m.calibration_status,
                    lut_profile_id=m.lut_profile_id,
                )
                for m in resolved.candidate_models
            ]
            bound_model = resolved.bound_model
            display_name = (
                bound_model.display_name
                if bound_model is not None
                else f"{receiver.vid_pid} (待确认型号)"
            )
            event.update(
                device_id=device_uid,
                device_uid=device_uid,
                device_identity_basis=identity_basis,
                binding_state=resolved.binding_state,
                candidate_models=candidates_payload,
                recommended_model_id=resolved.recommended_model_id,
                model_id=bound_model.model_id if bound_model is not None else None,
                model_display_name=display_name,
                name=display_name,
                brand=bound_model.brand if bound_model is not None else "ATK",
                verification_tier=bound_model.verification_tier if bound_model is not None else "catalog_only",
                calibration_status=bound_model.calibration_status if bound_model is not None else "uncalibrated",
                lut_profile_id=bound_model.lut_profile_id if bound_model is not None else None,
            )

            codec = get_codec(receiver.protocol_family)
            wired = receiver.link_role == "wired_usb"
            request = codec.build_request(wired=wired)
            if request is None or resolved.binding_state == "pending_protocol":
                event["outcome"] = "protocol_pending_capture"
                return event

            phase = "transaction"
            mutex_name = f"Local\\ATK_Pulse_{receiver.vid:04X}_{receiver.pid:04X}_query"
            with self.guard(mutex_name):
                raw_reply, tx_err = self.transport.transact(
                    path,
                    request,
                    codec.response_length,
                    cancel_event=cancel,
                    query_id=query_id,
                    drain_before_write=True,
                    response_matcher=codec.make_response_matcher(wired=wired),
                )
                received_at = datetime.now().astimezone()
                if cancel.is_set() or tx_err == "cancelled":
                    event["outcome"] = "cancelled"
                    return event

                phase = "enumeration"
                current_all = self.transport.enumerate_all()
                current_vid_pid = self.transport.filter_for_receiver(current_all, receiver)
                current_matches = self.transport.match_collections(current_all, receiver)
                if not any(d.get("path") == path for d in current_matches):
                    event.update(
                        receiver=bool(current_vid_pid),
                        receiver_visibility="present" if current_vid_pid else "absent",
                        outcome="device_changed" if current_vid_pid else "receiver_absent",
                    )
                    return event

                if tx_err is not None:
                    event["outcome"] = tx_err
                    return event

                reading = codec.parse_response(raw_reply, wired=wired)
                event.update(
                    packet_valid=reading.packet_valid,
                    percent_valid=reading.percent_valid,
                    voltage_raw=reading.voltage_raw,
                    voltage_validation=reading.voltage_validation,
                    status_byte=reading.status_byte,
                    link_byte=reading.link_byte,
                    charging=None,
                    charging_state=CHARGING_STATE_UNKNOWN,
                )
                if reading.present and reading.percent_valid:
                    if bound_model is not None and bound_model.lut_profile_id:
                        src_mode = (
                            "voltage_lut_1pct"
                            if reading.voltage_mv is not None
                            else "firmware_fallback_no_voltage"
                        )
                    else:
                        src_mode = "firmware_reported"

                    if (
                        receiver.protocol_family in ("nordic52", "nordic54")
                        and receiver.link_role == "wireless_dongle"
                        and not cancel.is_set()
                    ):
                        cache_key = (device_uid, event["collection_path_hash"])
                        cid_cmd = 0x10 if receiver.protocol_family == "nordic52" else 0x4A
                        if cache_key not in self._cid_mid_cache and hasattr(codec, "build_cid_mid_request"):
                            try:
                                req_id = codec.build_cid_mid_request()
                                raw_id, id_err = self.transport.transact(
                                    path,
                                    req_id,
                                    codec.response_length,
                                    cancel_event=cancel,
                                    query_id=query_id,
                                    read_timeout_ms=250,
                                    write_delay_s=0.02,
                                    drain_before_write=False,
                                    response_matcher=codec.make_response_matcher(wired=False, command=cid_cmd),
                                )
                                if id_err is None:
                                    parsed_cm = codec.parse_cid_mid_response(raw_id)
                                    if parsed_cm is not None:
                                        self._cid_mid_cache[cache_key] = parsed_cm
                            except Exception:
                                log.debug(
                                    "query=%s optional %#x CID/MID read skipped",
                                    query_id,
                                    cid_cmd,
                                    exc_info=True,
                                )

                        cached_cm = self._cid_mid_cache.get(cache_key)
                        if cached_cm is not None:
                            cid, mid = cached_cm
                            event["cid_mid"] = f"{cid},{mid}"
                            cid_model = (
                                self.registry.lookup_compx_cid_mid(cid, mid)
                                if receiver.protocol_family == "nordic52"
                                else self.registry.lookup_bitmouse_cid_mid(cid, mid)
                            )
                            if cid_model is not None:
                                if resolved.binding_state == "bound":
                                    pass
                                elif resolved.binding_state == "ambiguous_unbound":
                                    if cid_model.lut_profile_id is not None:
                                        event["recommended_model_id"] = cid_model.model_id
                                    else:
                                        event.update(
                                            model_id=cid_model.model_id,
                                            model_display_name=cid_model.display_name,
                                            name=cid_model.display_name,
                                            brand=cid_model.brand,
                                            verification_tier=cid_model.verification_tier,
                                            calibration_status=cid_model.calibration_status,
                                            lut_profile_id=None,
                                            binding_state="cid_mid_resolved",
                                        )
                                        src_mode = "firmware_reported"
                                else:
                                    event.update(
                                        model_id=cid_model.model_id,
                                        model_display_name=cid_model.display_name,
                                        name=cid_model.display_name,
                                        brand=cid_model.brand,
                                        verification_tier=cid_model.verification_tier,
                                        calibration_status=cid_model.calibration_status,
                                        lut_profile_id=cid_model.lut_profile_id,
                                    )
                                    if cid_model.lut_profile_id:
                                        src_mode = (
                                            "voltage_lut_1pct"
                                            if reading.voltage_mv is not None
                                            else "firmware_fallback_no_voltage"
                                        )
                                    else:
                                        src_mode = "firmware_reported"

                    event.update(
                        outcome="success",
                        connected=True,
                        percent=reading.percent,
                        voltage_mv=reading.voltage_mv,
                        battery_source_mode=src_mode,
                        at=received_at,
                    )
                else:
                    event["outcome"] = reading.failure_reason or "invalid_response"

        except BlockingIOError as exc:
            event.update(outcome="device_busy", error=str(exc))
        except OSError as exc:
            event.update(outcome="enumeration_error" if phase == "enumeration" else "io_error", error=str(exc))
            if phase == "enumeration":
                event.update(receiver_visibility="unknown", receiver=None)
        except Exception as exc:
            event.update(outcome="internal_error", error=f"{type(exc).__name__}: {exc}")
            log.exception("query=%s unexpected error", query_id)
        finally:
            event["completed_at"] = datetime.now().astimezone()
            if event["at"] is None:
                event["at"] = event["completed_at"]
            event["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
            log.info(
                "query=%s mode=%s visibility=%s outcome=%s elapsed_ms=%s",
                query_id,
                mode,
                event["receiver_visibility"],
                event["outcome"],
                event["elapsed_ms"],
            )
        return event
