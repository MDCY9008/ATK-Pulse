# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Clean-room HID protocols and offline device registry for ATK-Pulse."""
from __future__ import annotations

from .codecs import NanoFf05Codec, NearlinkCodec, Nordic52Codec, Nordic54Codec, ProtocolCodec, get_codec
from .hid_transport import HidTransport, matches_hid_rule, query_hid_caps
from .registry import BindingStore, DeviceRegistry, make_device_uid, resolve_binding
from .types import (
    CHARGING_STATE_UNKNOWN,
    VOLTAGE_MAX_MV,
    VOLTAGE_MIN_MV,
    DeviceBinding,
    HidMatchRule,
    ModelEntry,
    PacketReading,
    ReceiverEntry,
    ResolvedDevice,
)

__all__ = [
    "CHARGING_STATE_UNKNOWN",
    "VOLTAGE_MAX_MV",
    "VOLTAGE_MIN_MV",
    "BindingStore",
    "DeviceBinding",
    "DeviceRegistry",
    "HidMatchRule",
    "HidTransport",
    "ModelEntry",
    "NanoFf05Codec",
    "NearlinkCodec",
    "Nordic52Codec",
    "Nordic54Codec",
    "PacketReading",
    "ProtocolCodec",
    "ReceiverEntry",
    "ResolvedDevice",
    "get_codec",
    "make_device_uid",
    "matches_hid_rule",
    "query_hid_caps",
    "resolve_binding",
]
