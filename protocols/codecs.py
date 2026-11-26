# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Clean-room HID packet codecs for nordic52, nordic54, evision_nearlink, and nano_ff05."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Sequence

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
    feature_request: bool = False

    @abstractmethod
    def build_request(self, *, wired: bool = False) -> list[int] | None:
        """Return request bytes, or None if the protocol is pending capture."""

    @abstractmethod
    def parse_response(self, data: Sequence[int] | None, *, wired: bool = False) -> PacketReading:
        """Decode raw HID response bytes into a PacketReading."""

    def make_response_matcher(self, *, wired: bool = False) -> Callable[[Sequence[int]], bool] | None:
        """Return an optional frame filter that skips known async/unrelated HID reports."""
        return None


class Nordic52Codec(ProtocolCodec):
    """Compx / Nordic nRF52840 17-byte protocol on usage_page=0xFF02, usage=0x0002."""

    family = "nordic52"
    protocol_status = "ready"
    response_length = 17

    _REPORT_ID = 0x08
    _CMD_BATTERY = 0x04
    _CMD_ASYNC_STATUS = 0x0A
    _CMD_CID_MID = 0x10
    _CHECKSUM_TARGET = 0x55

    def make_response_matcher(
        self, *, wired: bool = False, command: int = _CMD_BATTERY
    ) -> Callable[[Sequence[int]], bool]:
        """Skip async CompX status pushes (0x0A) and mismatched command frames."""
        def _match(data: Sequence[int]) -> bool:
            if len(data) == 17 and data[0] == self._REPORT_ID:
                if data[1] != command and data[1] in (self._CMD_BATTERY, self._CMD_ASYNC_STATUS, self._CMD_CID_MID):
                    return False
            return True

        return _match

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
    _CMD_ASYNC_PUSH = 0x73
    _REQ_LEN = 0x02
    _SUB_BATTERY = 0x07
    _SUB_CHARGING = 0x0F
    _SUB_CID_MID = 0x4A
    _SUB_CID_MID_DONGLE = 0x89
    _STATUS_OK = 0x00
    _STATUS_BUSY_SKIP = 0x02

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

    def build_cid_mid_request(self, *, wired: bool = False) -> list[int]:
        """Build read-only MouseCidMid (0x4A) 64-byte frame for BITMOUSE mice/receivers."""
        pkt = [0] * 64
        pkt[0] = self._REPORT_ID
        pkt[2] = self._CMD_READ
        pkt[3] = 0x08  # paramLen = 8
        pkt[5] = 0x00 if wired else 0x01
        pkt[6] = self._SUB_CID_MID
        pkt[7] = 0x06  # cmdLen = 6
        pkt[8] = 0x00  # operate = 0 (read)
        pkt[1] = sum(pkt[2:]) & 0xFF  # 0xCB wireless, 0xCA wired
        return pkt

    def make_response_matcher(
        self, *, wired: bool = False, command: int = _SUB_BATTERY
    ) -> Callable[[Sequence[int]], bool]:
        """Skip async BITMOUSE notifications (0x73), intermediate status 0x02, and other sub-commands."""
        known_cmds = (0x00, self._SUB_BATTERY, self._SUB_CHARGING, self._SUB_CID_MID, self._SUB_CID_MID_DONGLE)

        def _match(data: Sequence[int]) -> bool:
            if len(data) >= 6 and data[0] == self._REPORT_ID:
                if data[1] == self._CMD_ASYNC_PUSH:
                    return False
                if data[2] == self._STATUS_BUSY_SKIP:
                    return False
                if data[5] != command and data[5] in known_cmds:
                    return False
            return True

        return _match

    def parse_cid_mid_response(self, data: Sequence[int] | None) -> tuple[int, int] | None:
        """Decode read-only 0x4A reply into (CID, MID), or None if invalid/zero."""
        if not data or len(data) < 13:
            return None
        if data[0] != self._REPORT_ID or data[1] == self._CMD_ASYNC_PUSH:
            return None
        if data[2] != self._STATUS_OK or data[5] != self._SUB_CID_MID or int(data[6]) < 6:
            return None
        cid = int(data[8])
        mid = int.from_bytes(bytes(data[9:13]), "little")
        if cid == 0 and mid == 0:
            return None
        return cid, mid

    def parse_response(self, data: Sequence[int] | None, *, wired: bool = False) -> PacketReading:
        if not data:
            return PacketReading(present=False, failure_reason="no_response")
        if len(data) < 8:
            return PacketReading(present=False, failure_reason="invalid_length")
        if data[0] != self._REPORT_ID:
            return PacketReading(present=False, failure_reason="invalid_report_id")
        if data[1] == self._CMD_ASYNC_PUSH or data[5] != self._SUB_BATTERY:
            return PacketReading(present=False, failure_reason="invalid_command")
        if data[2] != self._STATUS_OK:
            return PacketReading(
                present=True,
                packet_valid=True,
                percent_valid=False,
                asleep=True,
                failure_reason="device_asleep",
                status_byte=int(data[2]),
                link_byte=1 if wired else 0,
                charging=None,
                charging_state=CHARGING_STATE_UNKNOWN,
            )
        payload_len = int(data[6])
        if payload_len < 1 or payload_len > len(data) - 7:
            return PacketReading(
                present=False,
                packet_valid=True,
                percent_valid=False,
                failure_reason="invalid_length",
            )

        pct = int(data[7])
        if pct == 0:
            # Hardware sleep/not-ready replies on BITMOUSE dongles can report 0 with status=0.
            return PacketReading(
                present=True,
                packet_valid=True,
                percent_valid=False,
                asleep=True,
                failure_reason="device_asleep",
                status_byte=int(data[2]),
                link_byte=1 if wired else 0,
                charging=None,
                charging_state=CHARGING_STATE_UNKNOWN,
            )
        if not 1 <= pct <= 100:
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


class NearlinkCodec(ProtocolCodec):
    """EVISION Nearlink 64-byte protocol (373B:108C 0xFFB3/r05 wireless, 373B:108B 0xFFB2/r04 wired)."""

    family = "evision_nearlink"
    protocol_status = "ready"
    response_length = 64

    _RID_WIRELESS = 0x05
    _RID_WIRED = 0x04
    _CMD_POWER = 0x1A
    _CMD_ASLEEP = 0xFF

    def __init__(self) -> None:
        self._request_code = 0

    def _next_code(self) -> int:
        self._request_code = (self._request_code + 1) % 65536 or 1
        return self._request_code

    def build_request(self, *, wired: bool = False) -> list[int]:
        rid = self._RID_WIRED if wired else self._RID_WIRELESS
        seq = self._next_code()
        return [rid, seq & 0xFF, (seq >> 8) & 0xFF, self._CMD_POWER] + [0] * 60

    def make_response_matcher(self, *, wired: bool = False) -> Callable[[Sequence[int]], bool]:
        rid = self._RID_WIRED if wired else self._RID_WIRELESS

        def _match(data: Sequence[int]) -> bool:
            if len(data) >= 4 and data[0] == rid and data[3] not in (self._CMD_POWER, self._CMD_ASLEEP):
                return False
            return True

        return _match

    def parse_response(self, data: Sequence[int] | None, *, wired: bool = False) -> PacketReading:
        rid = self._RID_WIRED if wired else self._RID_WIRELESS
        if not data:
            return PacketReading(present=False, failure_reason="no_response")
        if len(data) < 64:
            return PacketReading(present=False, failure_reason="invalid_length")
        if data[0] != rid:
            return PacketReading(present=False, failure_reason="invalid_report_id")
        if data[3] == self._CMD_ASLEEP:
            return PacketReading(
                present=True,
                packet_valid=True,
                percent_valid=False,
                asleep=True,
                failure_reason="device_asleep",
                status_byte=int(data[3]),
                link_byte=int(data[9]),
                charging=None,
                charging_state=CHARGING_STATE_UNKNOWN,
            )
        if data[3] != self._CMD_POWER:
            return PacketReading(present=False, failure_reason="invalid_command")

        pct = int(data[8])
        if not 0 <= pct <= 100:
            return PacketReading(
                present=False,
                packet_valid=True,
                percent_valid=False,
                failure_reason="invalid_percent",
            )

        # EVISION Nearlink reports percent without voltage; charging_state stays unknown per project contract.
        return PacketReading(
            present=True,
            percent=pct,
            voltage_mv=None,
            voltage_raw=None,
            voltage_validation="not_supported_on_protocol",
            packet_valid=True,
            percent_valid=True,
            status_byte=int(data[3]),
            link_byte=int(data[9]),
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


class HoltekNano8kCodec(ProtocolCodec):
    """Read firmware-reported battery percentage from the Holtek 8K Nano receiver."""

    family = "holtek_nano8k"
    response_length = 65
    feature_request = True

    def build_request(self, *, wired: bool = False) -> list[int]:
        # Feature report ID 0 followed by the read-only GetConfigData command.
        return [0, 0x82] + [0] * 7

    def make_response_matcher(self, *, wired: bool = False) -> Callable[[Sequence[int]], bool]:
        # The reply is an input report; ignore unsolicited DPI/rate notifications.
        return lambda data: len(data) >= 10 and data[0] == 0x82

    def parse_response(self, data: Sequence[int] | None, *, wired: bool = False) -> PacketReading:
        if not data or not self.make_response_matcher()(data):
            return PacketReading(present=False, failure_reason="invalid_report")
        link = data[9]
        if not link:
            return PacketReading(present=False, packet_valid=True, asleep=True,
                                 link_byte=link, failure_reason="mouse_asleep")
        percent = data[8] & 0x7F  # Bit 7 is the charging flag.
        if not 1 <= percent <= 100:
            return PacketReading(present=False, packet_valid=True, link_byte=link,
                                 failure_reason="invalid_percent")
        return PacketReading(present=True, percent=percent, packet_valid=True,
                             percent_valid=True, link_byte=link)


_CODECS: dict[str, type[ProtocolCodec]] = {
    "holtek_nano8k": HoltekNano8kCodec,
    "nordic52": Nordic52Codec,
    "nordic54": Nordic54Codec,
    "evision_nearlink": NearlinkCodec,
    "nano_ff05": NanoFf05Codec,
}


def get_codec(family: str) -> ProtocolCodec:
    """Instantiate a clean-room protocol codec by family name."""
    cls = _CODECS.get(family)
    if cls is None:
        raise ValueError(f"Unsupported protocol family: {family}")
    return cls()
