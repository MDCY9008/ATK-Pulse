# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""Descriptive flags and cycle metrics: no capacity hallucination."""
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class Thresholds:
    rise_pct: int = 10
    drop_pct: int = 10
    short_window_s: int = 600
    comparable_mv: int = 20
    voltage_change_mv: int = 20
    plateau_window_s: int = 1800
    chart_gap_s: int = 450
    gap_rise_pct: int = 15

DEFAULTS = Thresholds()

def comparable(previous, current):
    # Unknown legacy identity is explicitly uncertain, never a confirmed match.
    u_prev = previous.get("device_uid") or previous.get("device_id")
    u_curr = current.get("device_uid") or current.get("device_id")
    if u_prev and u_curr and u_prev != u_curr:
        return False
    return True

def analyze(rows, thresholds=DEFAULTS):
    flags = []
    for a, b in zip(rows, rows[1:]):
        if not comparable(a, b) or a.get("source") != b.get("source"):
            continue
        delta, seconds = b["percent"] - a["percent"], (b["ts"] - a["ts"]).total_seconds()
        if seconds < 0:
            continue
        is_gap = seconds > thresholds.chart_gap_s
        labels = []
        if is_gap:
            if delta >= thresholds.gap_rise_pct:
                labels.append("断线后报告电量显著回升；充电状态 unknown")
        else:
            if delta >= thresholds.rise_pct:
                labels.append("设备报告电量回升；充电状态 unknown")
            if delta <= -thresholds.drop_pct and seconds <= thresholds.short_window_s:
                labels.append("短时报告电量下降")
            if b.get("recovered") and abs(delta) >= thresholds.rise_pct:
                labels.append("恢复响应后报告值变化")
            if a.get("mv") is not None and b.get("mv") is not None:
                dv = abs(b["mv"] - a["mv"])
                if abs(delta) >= thresholds.rise_pct and dv <= thresholds.comparable_mv:
                    labels.append("相近候选电压对应不同百分比")
        if labels:
            flags.append(dict(query_id=b.get("query_id"), at=b["ts"], labels=labels,
                              message=f"{a['percent']}% → {b['percent']}%：" + "；".join(labels),
                              identity_uncertain=not(a.get("device_id") and b.get("device_id"))))
    # Check cumulative voltage movement across an unchanged-percent plateau within a continuous segment.
    start = None
    prev = None
    for row in rows:
        if start is None or prev is None or row["percent"] != start["percent"] or chart_break(prev, row, thresholds):
            start = row
        elif (row["ts"] - start["ts"]).total_seconds() >= thresholds.plateau_window_s:
            if start.get("mv") is not None and row.get("mv") is not None and abs(row["mv"] - start["mv"]) >= thresholds.voltage_change_mv:
                flags.append(dict(query_id=row.get("query_id"), at=row["ts"],
                                  labels=["百分比平台期候选电压变化"], message="百分比平台期候选电压变化；字段语义待验证"))
                start = row
        prev = row
    return flags

def adaptive_sleep_gap_s(period_hours) -> int:
    """Return view-adaptive sleep/idle gap threshold (seconds) to avoid chart fragmentation."""
    if period_hours == "cycle":
        return 10800  # 3h
    if period_hours is None or (isinstance(period_hours, (int, float)) and period_hours >= 30 * 24):
        return 43200  # 12h
    if period_hours >= 7 * 24:
        return 10800  # 3h
    if period_hours >= 24:
        return 2700   # 45m
    return 450        # 7.5m (1h view / default)


def chart_break(a, b, thresholds=DEFAULTS, gap_threshold_s=None):
    gap_limit = thresholds.chart_gap_s if gap_threshold_s is None else gap_threshold_s
    return (b["ts"] - a["ts"]).total_seconds() > gap_limit or a.get("source") != b.get("source") or not comparable(a, b)


@dataclass(frozen=True)
class CycleMetrics:
    cycle_start: datetime
    cycle_rows: list
    elapsed_seconds: float
    active_seconds: float
    start_pct: int
    current_pct: int
    drop_pct: int
    avg_daily_drain: float | None
    sleep_gaps: list


def compute_sleep_gaps(rows, thresholds=DEFAULTS, gap_threshold_s=None):
    """Return contiguous idle/sleep gaps where time jump exceeds threshold."""
    gap_limit = thresholds.chart_gap_s if gap_threshold_s is None else gap_threshold_s
    gaps = []
    for a, b in zip(rows, rows[1:]):
        if not comparable(a, b):
            continue
        sec = (b["ts"] - a["ts"]).total_seconds()
        if sec > gap_limit:
            gaps.append((a["ts"], b["ts"]))
    return gaps


def compute_cycle_metrics(rows, estimates=None, thresholds=DEFAULTS, min_recharge_jump=15):
    """Identify the latest discharge cycle and compute natural-language metrics."""
    if not rows:
        return None
    pcts = []
    for idx, r in enumerate(rows):
        if estimates is not None and idx < len(estimates):
            pcts.append(int(estimates[idx].display_percent))
        else:
            pcts.append(int(r.get("display_percent", r["percent"])))

    cycle_idx = 0
    i = 1
    while i < len(rows):
        a, b = rows[i - 1], rows[i]
        if comparable(a, b) and (pcts[i] - pcts[i - 1]) >= min_recharge_jump:
            # Advance across rising/plateau charge steps to the recharge peak.
            peak = i
            while peak + 1 < len(rows) and comparable(rows[peak], rows[peak + 1]):
                next_pct = pcts[peak + 1]
                if next_pct >= pcts[peak] and (rows[peak + 1]["ts"] - rows[peak]["ts"]).total_seconds() <= 4 * 3600:
                    peak += 1
                else:
                    break
            cycle_idx = peak
            i = peak + 1
            continue
        i += 1

    cycle_rows = list(rows[cycle_idx:])
    cycle_pcts = pcts[cycle_idx:]
    start_ts = cycle_rows[0]["ts"]
    end_ts = cycle_rows[-1]["ts"]
    elapsed = max(0.0, (end_ts - start_ts).total_seconds())

    active = 0.0
    for a, b in zip(cycle_rows, cycle_rows[1:]):
        dt = (b["ts"] - a["ts"]).total_seconds()
        if 0 < dt <= thresholds.chart_gap_s and comparable(a, b):
            active += dt

    start_pct = cycle_pcts[0]
    curr_pct = cycle_pcts[-1]
    drop_pct = max(0, start_pct - curr_pct)

    avg_daily = None
    if elapsed >= 3600 and drop_pct >= 1:
        avg_daily = round(drop_pct / (elapsed / 86400.0), 1)

    return CycleMetrics(
        cycle_start=start_ts,
        cycle_rows=cycle_rows,
        elapsed_seconds=elapsed,
        active_seconds=active,
        start_pct=start_pct,
        current_pct=curr_pct,
        drop_pct=drop_pct,
        avg_daily_drain=avg_daily,
        sleep_gaps=compute_sleep_gaps(cycle_rows, thresholds),
    )


def format_cycle_elapsed(seconds):
    if seconds is None:
        return "暂无数据"
    total_mins = max(0, int(seconds // 60))
    days, rem_mins = divmod(total_mins, 1440)
    hours, mins = divmod(rem_mins, 60)
    if days > 0:
        return f"{days}天 {hours}小时"
    if hours > 0:
        return f"{hours}小时 {mins}分"
    return f"{max(1, mins)}分钟" if total_mins > 0 else "刚刚开始"


def format_daily_drain(drain_pct_per_day):
    if drain_pct_per_day is None:
        return "样本积累中"
    return f"每日约 {drain_pct_per_day:.1f}%"


def format_relative_age(ts, now=None):
    if ts is None:
        return "暂无更新"
    if now is None:
        now = datetime.now(ts.tzinfo) if ts.tzinfo is not None else datetime.now()
    sec = max(0, int((now - ts).total_seconds()))
    if sec < 60:
        return "刚刚"
    mins = sec // 60
    if mins < 60:
        return f"{mins} 分钟前"
    hours = mins // 60
    if hours < 24:
        return f"{hours} 小时前"
    days = hours // 24
    return f"{days} 天前"
