# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Voltage-to-SoC causal estimator (V0.4 Scheme B, decoupled by LUT profile).

Pure runtime calculation only: never mutates or writes CSV storage.
Anchors from 4340 mV (100%) to 3722 mV (12%) are calibrated from Cycle 1
wireless discharge data for ATK F1 V2 Ultimate; < 3722 mV is conservative
extrapolation pending low-battery hardware verification.
"""
from __future__ import annotations

from dataclasses import dataclass

LOW_EXTRAPOLATION_MV = 3722
_UNSET = object()

LUT_ANCHORS: tuple[tuple[int, int], ...] = (
    (4340, 100),
    (4300, 95),
    (4255, 90),
    (4180, 82),
    (4105, 73),
    (4030, 64),
    (3960, 56),
    (3880, 47),
    (3820, 38),
    (3795, 32),
    (3770, 25),
    (3745, 19),
    (3722, 12),
    (3660, 5),   # conservative extrapolation (< 3722 mV)
    (3500, 0),   # conservative extrapolation (< 3722 mV)
)


@dataclass(frozen=True)
class LutProfile:
    """Explicit per-model voltage-to-SoC LUT calibration profile."""

    profile_id: str
    model_id: str
    anchors: tuple[tuple[int, int], ...]
    low_extrapolation_mv: int | None
    sag_gate_mv: int = 25
    sag_rebound_tol_mv: int = 15
    fast_unlock_mv: int = 25
    sustain_rise_mv: int = 15


ATK_F1_V2_ULTIMATE_PROFILE = LutProfile(
    profile_id="atk_f1_v2_ultimate_cycle1_v1",
    model_id="atk_f1_v2_ultimate",
    anchors=LUT_ANCHORS,
    low_extrapolation_mv=LOW_EXTRAPOLATION_MV,
)

LUT_PROFILES: dict[str, LutProfile] = {
    ATK_F1_V2_ULTIMATE_PROFILE.profile_id: ATK_F1_V2_ULTIMATE_PROFILE,
}


def get_lut_profile(profile_id: str | None) -> LutProfile | None:
    """Return calibrated LutProfile for profile_id, or None if uncalibrated."""
    if not profile_id:
        return None
    return LUT_PROFILES.get(profile_id)


@dataclass(frozen=True)
class SocEstimate:
    """Single-step voltage-based SoC estimate."""

    percent_est: int | None
    v_est_mv: float | None
    sag_pending: bool
    extrapolated_low: bool
    fallback_used: bool


def lut_mv_to_soc(
    mv: float,
    anchors: tuple[tuple[int, int], ...] = LUT_ANCHORS,
) -> float:
    """Piecewise linear interpolation from mV to SoC (%), clamped to [0.0, 100.0]."""
    if mv >= anchors[0][0]:
        return float(anchors[0][1])
    if mv <= anchors[-1][0]:
        return float(anchors[-1][1])
    for (v_hi, s_hi), (v_lo, s_lo) in zip(anchors, anchors[1:]):
        if v_lo <= mv <= v_hi:
            frac = (mv - v_lo) / (v_hi - v_lo)
            return max(0.0, min(100.0, s_lo + frac * (s_hi - s_lo)))
    return 0.0


class VoltageSocEstimator:
    """Causal Scheme B estimator: 1-step sag gate + monotone wireless hold + fast unlock."""

    def __init__(
        self,
        *,
        sag_gate_mv: int = 25,
        sag_rebound_tol_mv: int = 15,
        fast_unlock_mv: int = 25,
        sustain_rise_mv: int = 15,
        anchors: tuple[tuple[int, int], ...] = LUT_ANCHORS,
        low_extrapolation_mv: int | None = LOW_EXTRAPOLATION_MV,
    ) -> None:
        self.sag_gate_mv = sag_gate_mv
        self.sag_rebound_tol_mv = sag_rebound_tol_mv
        self.fast_unlock_mv = fast_unlock_mv
        self.sustain_rise_mv = sustain_rise_mv
        self.anchors = anchors
        self.low_extrapolation_mv = low_extrapolation_mv
        self.v_est: float | None = None
        self.pending_sag: float | None = None
        self.rise_streak: int = 0

    def step(
        self,
        voltage_mv: int | float | None,
        *,
        link_byte: int | None = None,
    ) -> SocEstimate:
        """Advance state machine by one sample; missing voltage gracefully falls back."""
        if voltage_mv is None:
            return SocEstimate(
                percent_est=None,
                v_est_mv=None,
                sag_pending=False,
                extrapolated_low=False,
                fallback_used=True,
            )

        v = float(voltage_mv)
        if self.v_est is None:
            self.v_est = v
        elif link_byte == 1 or v >= self.v_est + self.fast_unlock_mv:
            # Fast unlock on link_byte=1 or large upward jump.
            self.v_est = v
            self.pending_sag = None
            self.rise_streak = 0
        elif v >= self.v_est + self.sustain_rise_mv:
            self.rise_streak += 1
            self.pending_sag = None
            if self.rise_streak >= 2:
                self.v_est = v
                self.rise_streak = 0
        else:
            self.rise_streak = 0
            if self.pending_sag is not None:
                if v <= self.v_est - self.sag_gate_mv:
                    # Two consecutive low frames confirm genuine discharge step.
                    self.v_est = v
                elif v > self.v_est - self.sag_rebound_tol_mv:
                    # Rebounded within tolerance -> reject previous sag spike.
                    self.v_est = min(self.v_est, v)
                else:
                    self.v_est = min(self.v_est, v)
                self.pending_sag = None
            elif self.v_est - v >= self.sag_gate_mv:
                # Hold v_est on first abrupt drop frame until next sample confirms.
                self.pending_sag = v
            elif v < self.v_est:
                # Wireless discharge asymmetric monotone hold suppresses 1-LSB positive noise.
                self.v_est = v

        pct = max(0, min(100, round(lut_mv_to_soc(self.v_est, self.anchors))))
        extrap_low = (
            self.low_extrapolation_mv is not None
            and self.v_est < self.low_extrapolation_mv
        )
        return SocEstimate(
            percent_est=pct,
            v_est_mv=round(self.v_est, 1),
            sag_pending=self.pending_sag is not None,
            extrapolated_low=extrap_low,
            fallback_used=False,
        )


def create_estimator(profile_id: str | None) -> VoltageSocEstimator | None:
    """Create a VoltageSocEstimator for a calibrated profile_id, or None if uncalibrated."""
    profile = get_lut_profile(profile_id)
    if profile is None:
        return None
    return VoltageSocEstimator(
        sag_gate_mv=profile.sag_gate_mv,
        sag_rebound_tol_mv=profile.sag_rebound_tol_mv,
        fast_unlock_mv=profile.fast_unlock_mv,
        sustain_rise_mv=profile.sustain_rise_mv,
        anchors=profile.anchors,
        low_extrapolation_mv=profile.low_extrapolation_mv,
    )


def estimate_series(
    rows: list[dict],
    estimator: VoltageSocEstimator | None = None,
    *,
    lut_profile_id: str | None | object = _UNSET,
) -> tuple[list[dict], VoltageSocEstimator | None]:
    """Deterministically replay a time-ordered sample sequence without mutating inputs.

    If `lut_profile_id=None` (or unknown) is passed explicitly without `estimator`,
    LUT estimation is skipped and firmware_reported % is preserved cleanly.
    """
    if estimator is not None:
        est: VoltageSocEstimator | None = estimator
    elif lut_profile_id is _UNSET:
        est = create_estimator(ATK_F1_V2_ULTIMATE_PROFILE.profile_id)
    else:
        est = create_estimator(lut_profile_id if isinstance(lut_profile_id, str) else None)

    enriched: list[dict] = []
    for row in rows:
        out = dict(row)
        if est is None:
            out["percent_est"] = None
            out["display_percent"] = row["percent"]
            out["v_est_mv"] = None
            out["sag_pending"] = False
            out["extrapolated_low"] = False
            out["fallback_used"] = True
            out["battery_source_mode"] = "firmware_reported"
        else:
            res = est.step(row.get("mv"), link_byte=row.get("link_byte"))
            out["percent_est"] = res.percent_est
            out["display_percent"] = res.percent_est if res.percent_est is not None else row["percent"]
            out["v_est_mv"] = res.v_est_mv
            out["sag_pending"] = res.sag_pending
            out["extrapolated_low"] = res.extrapolated_low
            out["fallback_used"] = res.fallback_used
            out["battery_source_mode"] = (
                "firmware_fallback_no_voltage" if res.fallback_used else "voltage_lut_1pct"
            )
        enriched.append(out)
    return enriched, est
