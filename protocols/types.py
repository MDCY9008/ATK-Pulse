# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Clean-room protocol types and runtime contracts for ATK-Pulse."""
from __future__ import annotations

from dataclasses import dataclass

CHARGING_STATE_UNKNOWN = "unknown"
VOLTAGE_MIN_MV = 2500
VOLTAGE_MAX_MV = 4500


@dataclass(frozen=True)
class HidMatchRule:
    """HID collection disambiguators used to select the battery interface."""

    usage_page: int | None = None
    usage: int | None = None
    interface_number: int | None = None


@dataclass(frozen=True)
class ReceiverEntry:
    """Whitelisted USB receiver/wired PID entry."""

    vid_pid: str
    vid: int
    pid: int
    link_role: str
    protocol_family: str
    hid_match: HidMatchRule
    candidate_model_ids: tuple[str, ...]
    shared_pid_collision: bool = False


@dataclass(frozen=True)
class ModelEntry:
    """Catalog definition for an ATK / VXE / VGN mouse model."""

    model_id: str
    brand: str
    display_name: str
    verification_tier: str
    calibration_status: str
    lut_profile_id: str | None
    protocol_family: str
    voltage_capability: str
    firmware_percent_step: int | None = None


@dataclass(frozen=True)
class DeviceBinding:
    """User-confirmed binding for a specific device_uid."""

    device_uid: str
    vid_pid: str
    selected_model_id: str
    identity_basis: str
    include_legacy_v1_v01_v02: bool = False
    bound_at: str = ""


@dataclass(frozen=True)
class ResolvedDevice:
    """Result of resolving a connected receiver against registry + user bindings."""

    receiver: ReceiverEntry
    device_uid: str
    identity_basis: str
    binding_state: str  # "bound" | "single_candidate" | "ambiguous_unbound" | "pending_protocol"
    bound_model: ModelEntry | None
    candidate_models: tuple[ModelEntry, ...]
    recommended_model_id: str | None = None


@dataclass(frozen=True)
class PacketReading:
    """Normalized packet-level reading; charging is always None / unknown."""

    present: bool
    percent: int | None = None
    voltage_mv: int | None = None
    voltage_raw: int | None = None
    voltage_validation: str = "unknown"
    packet_valid: bool = False
    percent_valid: bool = False
    status_byte: int | None = None
    link_byte: int | None = None
    asleep: bool = False
    failure_reason: str | None = None
    charging: None = None
    charging_state: str = CHARGING_STATE_UNKNOWN
