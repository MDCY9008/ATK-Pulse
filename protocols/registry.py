# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Offline device catalog registry and per-instance binding persistence."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from build_info import resource_path

from .codecs import get_codec
from .types import DeviceBinding, HidMatchRule, ModelEntry, ReceiverEntry, ResolvedDevice

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().with_name("device_registry.json")


def _parse_int(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    return int(str(val), 0)


def _parse_hid_rule(raw: dict | None, fallback: HidMatchRule) -> HidMatchRule:
    if not raw:
        return fallback
    return HidMatchRule(
        usage_page=_parse_int(raw.get("usage_page")) if "usage_page" in raw else fallback.usage_page,
        usage=_parse_int(raw.get("usage")) if "usage" in raw else fallback.usage,
        interface_number=raw.get("interface_number", fallback.interface_number),
        output_report_length=raw.get("output_report_length", fallback.output_report_length),
        input_report_length=raw.get("input_report_length", fallback.input_report_length),
    )


def make_device_uid(vid: int, pid: int, serial: str | None, path: bytes | str) -> tuple[str, str]:
    """Build deterministic device_uid + identity basis compatible with V0.2.1 device_id."""
    serial_clean = (serial or "").strip()
    if serial_clean:
        identity = serial_clean.encode("utf-8")
        basis = "receiver_serial_hash"
    else:
        identity = path if isinstance(path, bytes) else str(path).encode("utf-8")
        basis = "host_path_hash_not_stable"
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return f"{vid:04x}:{pid:04x}:{digest}", basis


class DeviceRegistry:
    """Read-only offline catalog of ATK / VXE / VGN receivers and mouse models."""

    def __init__(
        self,
        *,
        receivers: dict[str, ReceiverEntry],
        models: dict[str, ModelEntry],
        protocol_statuses: dict[str, str],
        multi_protocol_receivers: list[ReceiverEntry] | tuple[ReceiverEntry, ...] = (),
        excluded_vid_pids: set[str] | frozenset[str] = frozenset(),
        compx_cid_mid_catalog: dict[str, dict[str, str]] | None = None,
        bitmouse_cid_mid_catalog: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.receivers = receivers
        self.models = models
        self.protocol_statuses = protocol_statuses
        self.multi_protocol_receivers = list(multi_protocol_receivers)
        self._all_receivers = list(receivers.values()) + self.multi_protocol_receivers
        self.excluded_vid_pids = frozenset(vp.upper() for vp in excluded_vid_pids)
        self.compx_cid_mid_catalog = dict(compx_cid_mid_catalog or {})
        self.bitmouse_cid_mid_catalog = dict(bitmouse_cid_mid_catalog or {})

    @classmethod
    def load(cls, path: Path | None = None) -> "DeviceRegistry":
        if path is not None:
            target = Path(path)
        elif DEFAULT_REGISTRY_PATH.is_file():
            target = DEFAULT_REGISTRY_PATH
        else:
            target = resource_path("protocols/device_registry.json")
        raw = json.loads(target.read_text(encoding="utf-8"))

        proto_defaults: dict[str, HidMatchRule] = {}
        proto_statuses: dict[str, str] = {}
        for fam, spec in raw.get("protocol_families", {}).items():
            proto_statuses[fam] = spec.get("protocol_status", "pending_capture")
            hm = spec.get("hid_match") or {}
            proto_defaults[fam] = HidMatchRule(
                usage_page=_parse_int(hm.get("usage_page")),
                usage=_parse_int(hm.get("usage")),
                interface_number=hm.get("interface_number"),
                output_report_length=hm.get("output_report_length"),
                input_report_length=hm.get("input_report_length"),
            )

        models: dict[str, ModelEntry] = {}
        for mid, mraw in raw.get("models", {}).items():
            models[mid] = ModelEntry(
                model_id=mid,
                brand=mraw["brand"],
                display_name=mraw["display_name"],
                verification_tier=mraw["verification_tier"],
                calibration_status=mraw["calibration_status"],
                lut_profile_id=mraw.get("lut_profile_id"),
                protocol_family=mraw["protocol_family"],
                voltage_capability=mraw.get("voltage_capability", "unknown"),
                firmware_percent_step=mraw.get("firmware_percent_step"),
            )

        def _parse_receiver_entry(vp_str: str, rraw: dict) -> ReceiverEntry:
            fam = rraw["protocol_family"]
            rule = _parse_hid_rule(rraw.get("hid_match_override"), proto_defaults.get(fam, HidMatchRule()))
            vid = _parse_int(rraw["vid"])
            pid = _parse_int(rraw["pid"])
            assert vid is not None and pid is not None
            return ReceiverEntry(
                vid_pid=vp_str.upper(),
                vid=vid,
                pid=pid,
                link_role=rraw["link_role"],
                protocol_family=fam,
                hid_match=rule,
                candidate_model_ids=tuple(rraw["candidate_model_ids"]),
                shared_pid_collision=bool(rraw.get("shared_pid_collision", False)),
            )

        receivers: dict[str, ReceiverEntry] = {}
        for vp, rraw in raw.get("receivers", {}).items():
            receivers[vp.upper()] = _parse_receiver_entry(vp, rraw)

        multi_receivers: list[ReceiverEntry] = []
        for rraw in raw.get("multi_protocol_receivers", []):
            vp = rraw["vid_pid"]
            multi_receivers.append(_parse_receiver_entry(vp, rraw))

        excluded = frozenset(str(vp).upper() for vp in raw.get("excluded_vid_pids", []))
        cid_mid_catalog = dict(raw.get("compx_cid_mid_catalog") or {})
        bitmouse_cid_mid_catalog = dict(raw.get("bitmouse_cid_mid_catalog") or {})

        return cls(
            receivers=receivers,
            models=models,
            protocol_statuses=proto_statuses,
            multi_protocol_receivers=multi_receivers,
            excluded_vid_pids=excluded,
            compx_cid_mid_catalog=cid_mid_catalog,
            bitmouse_cid_mid_catalog=bitmouse_cid_mid_catalog,
        )

    def receiver_list(self) -> list[ReceiverEntry]:
        """Return whitelisted receivers ordered wireless-first for probing."""
        return sorted(
            self._all_receivers,
            key=lambda r: (0 if r.link_role == "wireless_dongle" else 1, r.vid_pid, r.protocol_family),
        )

    def get_receiver(self, vid: int, pid: int) -> ReceiverEntry | None:
        return self.receivers.get(f"{vid:04X}:{pid:04X}")

    def get_receivers(self, vid: int, pid: int) -> list[ReceiverEntry]:
        vp = f"{vid:04X}:{pid:04X}"
        return [r for r in self._all_receivers if r.vid_pid == vp]

    def is_excluded(self, vid: int, pid: int) -> bool:
        return f"{vid:04X}:{pid:04X}" in self.excluded_vid_pids

    def lookup_compx_cid_mid(self, cid: int, mid: int) -> ModelEntry | None:
        """Resolve a CompX (CID, MID) pair to a ModelEntry without inventing any LUT."""
        item = self.compx_cid_mid_catalog.get(f"{int(cid)},{int(mid)}")
        if not item:
            return None
        model_id = item["model_id"]
        existing = self.models.get(model_id)
        if existing is not None:
            return existing
        return ModelEntry(
            model_id=model_id,
            brand=item.get("brand", "ATK"),
            display_name=item.get("display_name", f"ATK Mouse ({cid},{mid})"),
            verification_tier="catalog_only",
            calibration_status="uncalibrated",
            lut_profile_id=None,
            protocol_family="nordic52",
            voltage_capability="wireless_candidate_mv_unverified",
            firmware_percent_step=5,
        )

    def lookup_bitmouse_cid_mid(self, cid: int, mid: int) -> ModelEntry | None:
        """Resolve a BITMOUSE (CID, MID) pair to a ModelEntry (no voltage LUT)."""
        item = self.bitmouse_cid_mid_catalog.get(f"{int(cid)},{int(mid)}")
        if not item:
            return None
        model_id = item["model_id"]
        existing = self.models.get(model_id)
        if existing is not None:
            return existing
        return ModelEntry(
            model_id=model_id,
            brand=item.get("brand", "ATK"),
            display_name=item.get("display_name", f"ATK Mouse ({cid},{mid})"),
            verification_tier="catalog_only",
            calibration_status="no_voltage",
            lut_profile_id=None,
            protocol_family="nordic54",
            voltage_capability="none",
            firmware_percent_step=None,
        )

    def get_model(self, model_id: str) -> ModelEntry | None:
        return self.models.get(model_id)

    def candidate_models_for(self, receiver: ReceiverEntry) -> tuple[ModelEntry, ...]:
        return tuple(self.models[mid] for mid in receiver.candidate_model_ids if mid in self.models)


class BindingStore:
    """Persist per-device_uid user model selections in device_bindings.json."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load_all(self) -> dict[str, DeviceBinding]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        bindings: dict[str, DeviceBinding] = {}
        for uid, item in (raw.get("bindings") or {}).items():
            try:
                bindings[uid] = DeviceBinding(
                    device_uid=uid,
                    vid_pid=item["vid_pid"],
                    selected_model_id=item["selected_model_id"],
                    identity_basis=item.get("identity_basis", "unknown"),
                    include_legacy_v1_v01_v02=bool(item.get("include_legacy_v1_v01_v02", False)),
                    bound_at=item.get("bound_at", ""),
                )
            except (KeyError, TypeError):
                continue
        return bindings

    def get(self, device_uid: str) -> DeviceBinding | None:
        return self.load_all().get(device_uid)

    def set_binding(
        self,
        *,
        device_uid: str,
        vid_pid: str,
        selected_model_id: str,
        identity_basis: str,
        include_legacy_v1_v01_v02: bool = False,
    ) -> DeviceBinding:
        bindings = self.load_all()
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        binding = DeviceBinding(
            device_uid=device_uid,
            vid_pid=vid_pid.upper(),
            selected_model_id=selected_model_id,
            identity_basis=identity_basis,
            include_legacy_v1_v01_v02=include_legacy_v1_v01_v02,
            bound_at=now_iso,
        )
        bindings[device_uid] = binding
        payload = {
            "schema_version": 1,
            "active_device_uid": device_uid,
            "bindings": {
                uid: {
                    "vid_pid": b.vid_pid,
                    "selected_model_id": b.selected_model_id,
                    "identity_basis": b.identity_basis,
                    "include_legacy_v1_v01_v02": b.include_legacy_v1_v01_v02,
                    "bound_at": b.bound_at,
                }
                for uid, b in bindings.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        return binding


def resolve_binding(
    receiver: ReceiverEntry,
    device_uid: str,
    identity_basis: str,
    registry: DeviceRegistry,
    binding_store: BindingStore,
    *,
    has_legacy_f1_v1_history: bool = False,
) -> ResolvedDevice:
    """Resolve model identity without ever auto-persisting ambiguous PID bindings."""
    candidates = registry.candidate_models_for(receiver)
    codec = get_codec(receiver.protocol_family)
    if codec.protocol_status != "ready":
        return ResolvedDevice(
            receiver=receiver,
            device_uid=device_uid,
            identity_basis=identity_basis,
            binding_state="pending_protocol",
            bound_model=candidates[0] if len(candidates) == 1 else None,
            candidate_models=candidates,
            recommended_model_id=None,
        )

    existing = binding_store.get(device_uid)
    if existing is not None and existing.selected_model_id in receiver.candidate_model_ids:
        model = registry.get_model(existing.selected_model_id)
        if model is not None:
            return ResolvedDevice(
                receiver=receiver,
                device_uid=device_uid,
                identity_basis=identity_basis,
                binding_state="bound",
                bound_model=model,
                candidate_models=candidates,
                recommended_model_id=model.model_id,
            )

    if len(candidates) == 1 and not receiver.shared_pid_collision:
        return ResolvedDevice(
            receiver=receiver,
            device_uid=device_uid,
            identity_basis=identity_basis,
            binding_state="single_candidate",
            bound_model=candidates[0],
            candidate_models=candidates,
            recommended_model_id=candidates[0].model_id,
        )

    recommended = None
    if has_legacy_f1_v1_history and "atk_f1_v2_ultimate" in receiver.candidate_model_ids:
        recommended = "atk_f1_v2_ultimate"

    # Shared PID collision without explicit user confirmation:
    # NEVER guess or apply F1 LUT; expose basic firmware_reported reading safely.
    return ResolvedDevice(
        receiver=receiver,
        device_uid=device_uid,
        identity_basis=identity_basis,
        binding_state="ambiguous_unbound",
        bound_model=None,
        candidate_models=candidates,
        recommended_model_id=recommended,
    )
