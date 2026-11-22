# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Clean-room HID packet codecs for nordic52, nordic54, and nano_ff05."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from .types import (
    CHARGING_STATE_UNKNOWN,
    VOLTAGE_MAX_MV,
    VOLTAGE_MIN_MV,
    PacketReading,
)


class ProtocolCodec(ABC):
    """Base contract for protocol frame builders and parsers."""

    family: str = ""
    protocol_status: str = "ready"
    response_length: int = 0

    @abstractmethod
    def build_request(self, *, wired: bool = False) -> list[int] | None:
        """Return request bytes, or None if the protocol is pending capture."""

    @abstractmethod
    def parse_response(self, data: Sequence[int] | None, *, wired: bool = False) -> PacketReading:
        """Decode raw HID response bytes into a PacketReading."""


class Nordic52Codec(ProtocolCodec):
    """Compx / Nordic nRF52840 17-byte protocol on usage_page=0xFF02, usage=0x0002."""

    family = "nordic52"
    protocol_status = "ready"
    response_length = 17

    _REPORT_ID = 0x08
    _CMD_BATTERY = 0x04
    _CMD_CID_MID = 0x10
    _CHECKSUM_TARGET = 0x55

    def build_request(self, *, wired: bool = False) -> list[int]:
        pkt = [0] * 17
        pkt[0] = self._REPORT_ID
        pkt[1] = self._CMD_BATTERY
        pkt[16] = (self._CHECKSUM_TARGET - (pkt[0] + pkt[1])) % 256  # 0x49
        return pkt

    def build_cid_mid_request(self) -> list[int]:
        """Build read-only GetMouseCIDMID (0x10) frame for CompX mice/receivers."""
        pkt = [0] * 17
        pkt[0] = self._REPORT_ID
        pkt[1] = self._CMD_CID_MID
        pkt[16] = (self._CHECKSUM_TARGET - (pkt[0] + pkt[1])) % 256  # 0x3D
        return pkt

    def parse_cid_mid_response(self, data: Sequence[int] | None) -> tuple[int, int] | None:
        """Decode read-only 0x10 reply into (CID, MID), or None if invalid/zero."""
        if not data or len(data) != 17:
            return None
        if data[0] != self._REPORT_ID or data[1] != self._CMD_CID_MID:
            return None
        if sum(data) % 256 != self._CHECKSUM_TARGET:
            return None
        if data[2] != 0x00:
            return None
        cid, mid = int(data[6]), int(data[7])
        if cid == 0 and mid == 0:
            return None
        return cid, mid

    def parse_response(self, data: Sequence[int] | None, *, wired: bool = False) -> PacketReading:
        if not data:
            return PacketReading(present=False, failure_reason="no_response")
        if len(data) != 17:
            return PacketReading(present=False, failure_reason="invalid_length")
        if data[0] != self._REPORT_ID:
            return PacketReading(present=False, failure_reason="invalid_report_id")
        if data[1] != self._CMD_BATTERY:
            return PacketReading(present=False, failure_reason="invalid_command")
        if sum(data) % 256 != self._CHECKSUM_TARGET:
            return PacketReading(present=False, failure_reason="invalid_checksum")

        pct = int(data[6])
        if not 0 <= pct <= 100:
            return PacketReading(
                present=False,
                packet_valid=True,
                percent_valid=False,
                failure_reason="invalid_percent",
            )

        raw_mv = (int(data[8]) << 8) | int(data[9])
        in_range = VOLTAGE_MIN_MV <= raw_mv <= VOLTAGE_MAX_MV
        voltage_mv = raw_mv if in_range else None
        validation = "candidate_in_range" if in_range else "candidate_out_of_range"

        # charging_state strictly stays unknown regardless of link_byte (offset 7).
        return PacketReading(
            present=True,
            percent=pct,
            voltage_mv=voltage_mv,
            voltage_raw=raw_mv,
            voltage_validation=validation,
            packet_valid=True,
            percent_valid=True,
            status_byte=int(data[5]),
            link_byte=int(data[7]),
            asleep=False,
            failure_reason=None,
            charging=None,
            charging_state=CHARGING_STATE_UNKNOWN,
        )


class Nordic54Codec(ProtocolCodec):
    """Compx / Nordic nRF54L15 & ATK BITMOUSE 64-byte protocol on usage_page=0xFF05, usage=0x0001."""

    family = "nordic54"
    protocol_status = "ready"
    response_length = 64

    _REPORT_ID = 0x08
    _CMD_READ = 0x72
    _REQ_LEN = 0x02
    _SUB_BATTERY = 0x07

    def build_request(self, *, wired: bool = False) -> list[int]:
        pkt = [0] * 64
        pkt[0] = self._REPORT_ID
        pkt[2] = self._CMD_READ
        pkt[3] = self._REQ_LEN
        pkt[5] = 0x00 if wired else 0x01
        pkt[6] = self._SUB_BATTERY
        pkt[7] = 0x01
        pkt[1] = sum(pkt[2:]) & 0xFF  # 0x7D wireless, 0x7C wired
        return pkt

    def parse_response(self, data: Sequence[int] | None, *, wired: bool = False) -> PacketReading:
        if not data:
            return PacketReading(present=False, failure_reason="no_response")
        if len(data) < 8:
            return PacketReading(present=False, failure_reason="invalid_length")
        if data[0] != self._REPORT_ID:
            return PacketReading(present=False, failure_reason="invalid_report_id")
        if data[1] != self._CMD_READ or data[5] != self._SUB_BATTERY:
            return PacketReading(present=False, failure_reason="invalid_command")
        if data[2] != 0x00:
            return PacketReading(
                present=True,
                packet_valid=True,
                percent_valid=False,
                asleep=True,
                failure_reason="device_asleep",
                charging=None,
                charging_state=CHARGING_STATE_UNKNOWN,
            )
        if int(data[6]) < 1:
            return PacketReading(
                present=False,
                packet_valid=True,
                percent_valid=False,
                failure_reason="invalid_length",
            )

        pct = int(data[7])
        if not 0 <= pct <= 100:
            return PacketReading(
                present=False,
                packet_valid=True,
                percent_valid=False,
                failure_reason="invalid_percent",
            )

        # BITMOUSE 0x07 GetBatteryLevel returns 1 payload byte (offset 7) only;
        # trailing bytes 8..9 are buffer leftovers on both wireless and wired links.
        return PacketReading(
            present=True,
            percent=pct,
            voltage_mv=None,
            voltage_raw=None,
            voltage_validation="not_supported_on_link",
            packet_valid=True,
            percent_valid=True,
            status_byte=int(data[2]),
            link_byte=1 if wired else 0,
            asleep=False,
            failure_reason=None,
            charging=None,
            charging_state=CHARGING_STATE_UNKNOWN,
        )


class NanoFf05Codec(ProtocolCodec):
    """Reserved codec for 0xFF05 64-byte NANO receiver variants pending hardware capture."""

    family = "nano_ff05"
    protocol_status = "pending_capture"
    response_length = 64

    def build_request(self, *, wired: bool = False) -> None:
        # Never transmit unverified 64-byte write commands without hardware packet capture.
        return None

    def parse_response(self, data: Sequence[int] | None, *, wired: bool = False) -> PacketReading:
        return PacketReading(
            present=False,
            failure_reason="protocol_pending_capture",
            charging=None,
            charging_state=CHARGING_STATE_UNKNOWN,
        )


_CODECS: dict[str, type[ProtocolCodec]] = {
    "nordic52": Nordic52Codec,
    "nordic54": Nordic54Codec,
    "nano_ff05": NanoFf05Codec,
}


def get_codec(family: str) -> ProtocolCodec:
    """Instantiate a clean-room protocol codec by family name."""
    cls = _CODECS.get(family)
    if cls is None:
        raise ValueError(f"Unsupported protocol family: {family}")
    return cls()
