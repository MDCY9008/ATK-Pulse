# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""ATK-Pulse V0.8.3 — "Iris Greenhouse" (鸢尾温室) Windows dashboard."""
from __future__ import annotations

import ctypes
import gc
import logging
import os
import subprocess
import sys
import time
import winreg
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer, QRectF, QPointF, QSize, QUrl
from PySide6.QtGui import (
    QColor, QCursor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QDesktopServices,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSizePolicy, QSpinBox, QStackedWidget, QSystemTrayIcon,
    QScrollArea, QLayout, QTableWidget, QTableWidgetItem, QToolTip,
    QVBoxLayout, QWidget,
)

from battery_worker import BatteryWorker
from build_info import (
    APP_NAME, AUTHOR_HANDLE, AUTHOR_NAME, DEBUG_ENV_VAR,
    REPO_URL, VERSION_LABEL, resource_path,
)
from data_store import (
    APP_DIR, BINDINGS_PATH, HISTORY_PATH, READ_WARNINGS,
    SAMPLES_PATH, SAMPLES_V2_PATH, V01_HISTORY, read_samples,
)
from exporter import (
    collect_sanitized_ecosystem_hid_descriptors, export_cycle_csv,
    export_redacted_diagnostic_zip,
)
from protocols import BindingStore, DeviceRegistry
from research import (
    adaptive_sleep_gap_s, analyze, chart_break, compute_cycle_metrics,
    compute_sleep_gaps, format_cycle_elapsed, format_daily_drain, format_relative_age,
)
from taskbar_badge import TaskbarBadge
from voltage_soc import (
    ATK_F1_V2_ULTIMATE_PROFILE, SocEstimate, VoltageSocEstimator,
    create_estimator, estimate_series,
)
from ui_theme import (
    PALETTE, IRIS_GREENHOUSE_QSS, qcolor, qss_rgba,
    GreenhouseCanvas, CleanComboBox, CleanSpinBox, HoverRowTableDelegate,
    ToggleSwitch, NavCapsuleButton, PillBadge, BatteryProgressBar,
    SegmentedControl, TaskbarStatusPill, BatteryRingWidget, WatermarkCardFrame,
    compute_y_axis_bounds, default_auto_scale_for_hours, should_fill_chart_area,
)

ROOT = Path(__file__).resolve().parent
STARTUP_KEY = "ATK_Battery_Monitor_V02"
MODEL = "ATK F1 V2 Ultimate"
CONTENT_MAX_WIDTH = 1240
STATUS_RIGHT_TEXT = f"{APP_NAME} {VERSION_LABEL} · by {AUTHOR_HANDLE} · 永久免费开源"
_UNSET = object()
log = logging.getLogger("atk_battery_v02")


def reported_charge_rebound(rows: list[dict]) -> str | None:
    """Flag unexplained device percent increases; keep all raw values unchanged."""
    flags = analyze(rows)
    if flags:
        return flags[-1]["message"] + "；仅描述变化，原因待验证"
    return None


def text(label: str, *, role: str = "body", size: int | None = None, wrap: bool | None = None) -> QLabel:
    w = QLabel(label)
    w.setObjectName(role)
    if wrap is not None:
        w.setWordWrap(wrap)
    elif role == "body":
        w.setWordWrap(True)
    if size:
        f = w.font()
        f.setPointSize(size)
        w.setFont(f)
    return w


def panel(layout=None, kind="panel"):
    box = QFrame()
    box.setObjectName(kind)
    if layout:
        box.setLayout(layout)
    return box


def vertical(*widgets, spacing=9, margins=(16, 14, 16, 14)):
    lay = QVBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for w in widgets:
        lay.addWidget(w)
    return lay


def tiny_button(label: str, primary=False):
    b = QPushButton(label)
    b.setObjectName("primary" if primary else "subtle")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def trim_process_working_set() -> bool:
    """Safely trim idle Windows working set memory when minimized/hidden to tray."""
    try:
        gc.collect()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetProcessWorkingSetSize.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t)
        kernel.SetProcessWorkingSetSize.restype = ctypes.c_int
        proc = kernel.GetCurrentProcess()
        if not proc:
            return False
        limit = ctypes.c_size_t(-1).value
        return bool(kernel.SetProcessWorkingSetSize(proc, limit, limit))
    except Exception:
        return False


def load_app_icon() -> QIcon:
    """Load the official ATK-Pulse brand icon from assets without code redrawing."""
    for rel in ("assets/atk_pulse.ico", "assets/atk_pulse.png"):
        p = resource_path(rel)
        if p.is_file():
            icon = QIcon(str(p))
            if not icon.isNull():
                return icon
    return QIcon()


def make_icon(percent: int | None = None, *, stale: bool = False, size: int = 64) -> QIcon:
    """Backward-compatible wrapper returning the official ATK-Pulse brand icon."""
    return load_app_icon()


class TrendChart(QWidget):
    """Canvas-only chart in Iris Greenhouse palette; supports translucent sleep bands & crisp hover."""

    def __init__(self, mode="percent"):
        super().__init__()
        self.rows: list[dict] = []
        self.mode = mode
        self.dark = False
        self.show_firmware = True
        self.auto_scale = True
        self.period_hours: int | None = 24
        self.gap_threshold_s: int = adaptive_sleep_gap_s(24)
        self.sleep_gaps: list[tuple] = []
        self.last_area_filled: bool = False
        self.last_y_bounds: tuple[float, float] = (0.0, 100.0)
        self.has_lut: bool = True
        self._plot_points: list[tuple[QPointF, dict]] = []
        self._hover_point: tuple[QPointF, dict] | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(175)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_data(self, rows, mode=None, show_firmware=None, auto_scale=None, period_hours=_UNSET, sleep_gaps=None, gap_threshold_s=None, has_lut=None):
        self.rows = rows
        if mode is not None:
            self.mode = mode
        if show_firmware is not None:
            self.show_firmware = bool(show_firmware)
        if auto_scale is not None:
            self.auto_scale = bool(auto_scale)
        if has_lut is not None:
            self.has_lut = bool(has_lut)
        if period_hours is not _UNSET:
            self.period_hours = period_hours
        self.gap_threshold_s = (
            int(gap_threshold_s)
            if gap_threshold_s is not None
            else adaptive_sleep_gap_s(self.period_hours)
        )
        self.sleep_gaps = (
            list(sleep_gaps)
            if sleep_gaps is not None
            else compute_sleep_gaps(rows, gap_threshold_s=self.gap_threshold_s)
        )
        self._hover_point = None
        self.update()

    def leaveEvent(self, event):
        if self._hover_point is not None:
            self._hover_point = None
            self.update()
        QToolTip.hideText()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        if not self._plot_points:
            if self._hover_point is not None:
                self._hover_point = None
                self.update()
            QToolTip.hideText()
            return super().mouseMoveEvent(event)
        pos = event.position()
        px, py = pos.x(), pos.y()
        # Allow convenient horizontal snapping within 24px or direct distance within 18px
        best_score = 1e9
        best_item: tuple[QPointF, dict] | None = None
        for pt, row in self._plot_points:
            dx = abs(pt.x() - px)
            dy = abs(pt.y() - py)
            if dx <= 22.0 and dy <= 64.0:
                score = dx * dx + (dy * 0.45) ** 2
                if score < best_score:
                    best_score = score
                    best_item = (pt, row)
        if best_item is not None:
            if self._hover_point is None or self._hover_point[0] != best_item[0]:
                self._hover_point = best_item
                self.update()
            best_row = best_item[1]
            mv = best_row.get("mv")
            mv_text = f"{mv} mV ({mv / 1000:.3f} V)" if mv is not None else "—"
            v_est = best_row.get("v_est_mv")
            v_est_text = f"{v_est:.1f} mV" if v_est is not None else "—"
            est_pct = best_row.get("percent_est")
            if est_pct is not None:
                est_line = f"电压估算：{est_pct}%" + ("（低段外推）" if best_row.get("extrapolated_low") else "")
                fw_line = f"\n固件原报：{best_row['percent']}%"
                volt_line = f"\n原始电压：{mv_text} · 滤波电压：{v_est_text}"
            elif not self.has_lut:
                est_line = f"当前口径：固件原报 {best_row['percent']}%"
                fw_line = ""
                volt_line = f"\n原始电压：{mv_text}" if mv is not None else "\n原始电压：当前协议不支持"
            else:
                est_line = f"电压估算：—（回退固件 {best_row['percent']}%）"
                fw_line = f"\n固件原报：{best_row['percent']}%"
                volt_line = f"\n原始电压：{mv_text} · 滤波电压：{v_est_text}"
            mode_text = best_row.get("query_mode") or "unknown"
            tip = (
                f"时间：{best_row['ts'].strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"{est_line}{fw_line}{volt_line}\n"
                f"来源：{best_row.get('source', '')} · {mode_text}"
            )
            tags = []
            if best_row.get("recovered"):
                tags.append("恢复后首帧")
            if best_row.get("sag_pending"):
                tags.append("压降门控过滤")
            if tags:
                tip += "\n标记：" + " / ".join(tags)
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        else:
            if self._hover_point is not None:
                self._hover_point = None
                self.update()
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        ink = qcolor(PALETTE["text_muted"])
        line = qcolor(PALETTE["iris_primary"])
        fw_line = qcolor(PALETTE["fw_dash"])
        grid = qcolor("#38D8D2E4")
        rec_ring = qcolor(PALETTE["coral_low"])
        left, top, right, bottom = 46, 12, w - 14, h - 24
        if right - left < 60 or bottom - top < 30:
            self._plot_points = []
            p.end()
            return
        rows = (
            [r for r in self.rows if r.get("display_percent", r["percent"]) is not None]
            if self.mode == "percent"
            else [r for r in self.rows if r["mv"] is not None]
        )
        unit = "%" if self.mode == "percent" else "V"
        values = [float(r.get("display_percent", r["percent"])) if self.mode == "percent" else r["mv"] / 1000.0 for r in rows]

        # Include firmware values in auto-scale bounds if overlay is active in percent mode
        bound_values = list(values)
        if self.mode == "percent" and self.show_firmware and rows:
            bound_values.extend(float(r["percent"]) for r in rows if r.get("percent") is not None)

        minimum, maximum = compute_y_axis_bounds(bound_values, self.mode, self.auto_scale)
        self.last_y_bounds = (minimum, maximum)

        for i in range(5):
            y = top + (bottom - top) * i / 4.0
            p.setPen(QPen(grid, 1.0))
            p.drawLine(QPointF(left, y), QPointF(right, y))
            p.setPen(ink)
            p.setFont(QFont("Segoe UI", 8))
            value = maximum - (maximum - minimum) * i / 4.0
            p.drawText(
                QRectF(0, y - 8, left - 7, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{value:.0f}{unit}" if unit == "%" else f"{value:.2f}V",
            )
        if not values:
            self._plot_points = []
            self.last_area_filled = False
            p.setPen(ink)
            p.setFont(QFont("Microsoft YaHei UI", 10))
            empty_msg = (
                "当前设备协议不上报电池电压，仅记录固件电量"
                if (self.mode == "mv" and self.rows)
                else "尚无有效记录，连接鼠标后会自动采集"
            )
            p.drawText(
                QRectF(left, top, right - left, bottom - top),
                Qt.AlignmentFlag.AlignCenter,
                empty_msg,
            )
            p.end()
            return

        ts = [r["ts"].timestamp() for r in rows]
        t0, t1 = min(ts), max(ts)
        span_y = max(1e-6, maximum - minimum)

        # 0) Misty translucent sleep/disconnect bands behind curves
        if self.sleep_gaps and t1 > t0:
            p.save()
            for gap_start, gap_end in self.sleep_gaps:
                gs, ge = gap_start.timestamp(), gap_end.timestamp()
                if ge <= t0 or gs >= t1:
                    continue
                gx0 = left + (right - left) * max(0.0, (gs - t0) / (t1 - t0))
                gx1 = left + (right - left) * min(1.0, (ge - t0) / (t1 - t0))
                bw = gx1 - gx0
                if bw >= 3.0:
                    band_rect = QRectF(gx0, top, bw, bottom - top)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(134, 126, 150, 22))
                    p.drawRect(band_rect)
                    if bw >= 38.0:
                        p.setPen(QColor(134, 126, 150, 145))
                        p.setFont(QFont("Microsoft YaHei UI", 8))
                        p.drawText(band_rect.adjusted(2, 4, -2, -4), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter, "休眠")
            p.restore()

        points: list[QPointF] = []
        for t, value in zip(ts, values):
            x = left + (right - left) * ((t - t0) / (t1 - t0) if t1 != t0 else 0.5)
            clamped = max(minimum, min(maximum, value))
            y = bottom - (clamped - minimum) / span_y * (bottom - top)
            points.append(QPointF(x, y))

        # 1) Firmware reference dashed line (#66867E96) — strictly split into open per-segment paths
        if self.mode == "percent" and self.show_firmware and len(points) > 1:
            fw_segments: list[list[QPointF]] = []
            cur_fw: list[QPointF] = []
            for idx, (t, row) in enumerate(zip(ts, rows)):
                x = left + (right - left) * ((t - t0) / (t1 - t0) if t1 != t0 else 0.5)
                clamped_fw = max(minimum, min(maximum, float(row["percent"])))
                y = bottom - (clamped_fw - minimum) / span_y * (bottom - top)
                pt = QPointF(x, y)
                if idx == 0 or chart_break(rows[idx - 1], row, gap_threshold_s=self.gap_threshold_s):
                    if cur_fw:
                        fw_segments.append(cur_fw)
                    cur_fw = [pt]
                else:
                    cur_fw.append(pt)
            if cur_fw:
                fw_segments.append(cur_fw)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(fw_line, 1.2, Qt.PenStyle.DashLine))
            for seg in fw_segments:
                if len(seg) >= 2:
                    seg_path = QPainterPath(seg[0])
                    for pt in seg[1:]:
                        seg_path.lineTo(pt)
                    p.drawPath(seg_path)

        # 2) Build continuous segments by chart_break
        segments: list[list[QPointF]] = [[points[0]]]
        for i, pt in enumerate(points[1:], 1):
            if chart_break(rows[i - 1], rows[i], gap_threshold_s=self.gap_threshold_s):
                segments.append([pt])
            else:
                segments[-1].append(pt)

        # Step A: Pure vertical gradient fill (NO stroke on closed area path)
        fill_area = should_fill_chart_area(segments, self.period_hours)
        self.last_area_filled = fill_area
        if fill_area:
            for seg in segments:
                if len(seg) >= 2 and abs(seg[-1].x() - seg[0].x()) >= 12.0:
                    area_path = QPainterPath(QPointF(seg[0].x(), bottom))
                    for pt in seg:
                        area_path.lineTo(pt)
                    area_path.lineTo(QPointF(seg[-1].x(), bottom))
                    area_path.closeSubpath()
                    seg_top = min(pt.y() for pt in seg)
                    area_grad = QLinearGradient(0, seg_top, 0, bottom)
                    area_grad.setColorAt(0.0, QColor(line.red(), line.green(), line.blue(), 36))  # ~14% alpha
                    area_grad.setColorAt(1.0, QColor(line.red(), line.green(), line.blue(), 0))
                    p.fillPath(area_path, area_grad)

        # Step B: Pure open polyline stroke (NO fill, NO closed bottom edge)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(
            QPen(
                line,
                2.0 if fill_area else 1.8,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        for seg in segments:
            if len(seg) >= 2:
                seg_path = QPainterPath(seg[0])
                for pt in seg[1:]:
                    seg_path.lineTo(pt)
                p.drawPath(seg_path)

        # Step C: Isolated single-point segments rendered as r=2.5px dots (never vertical lines)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(line)
        for seg in segments:
            if len(seg) == 1:
                p.drawEllipse(seg[0], 2.5, 2.5)

        # Preserve full fidelity below 2,500 points; bucket per X pixel above that to keep painting & hover fast.
        if len(points) <= 2500:
            visible = list(zip(points, rows))
        else:
            buckets: dict[int, list[tuple[QPointF, dict]]] = {}
            for pt, row in zip(points, rows):
                bx = int(pt.x())
                buckets.setdefault(bx, []).append((pt, row))
            visible = []
            for bx in sorted(buckets):
                grp = buckets[bx]
                if len(grp) <= 3:
                    visible.extend(grp)
                else:
                    picked = {0, len(grp) - 1}
                    picked.add(min(range(len(grp)), key=lambda idx: grp[idx][0].y()))
                    picked.add(max(range(len(grp)), key=lambda idx: grp[idx][0].y()))
                    for idx, (_, r) in enumerate(grp):
                        if r.get("recovered"):
                            picked.add(idx)
                    for idx in sorted(picked):
                        visible.append(grp[idx])

        self._plot_points = visible

        # Dense sample dots are hidden by default to eliminate "candied hawthorn" noise.
        # Recovered first-frame markers are surfaced cleanly in the hover tooltip and a highlighted ring on hover.
        if self._hover_point is not None:
            hpt, hrow = self._hover_point
            # Subtle vertical guide line
            guide_pen = QPen(qcolor("#557C64B8"), 1.0, Qt.PenStyle.DashLine)
            p.setPen(guide_pen)
            p.drawLine(QPointF(hpt.x(), top), QPointF(hpt.x(), bottom))
            # Outer soft halo + inner crisp dot
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor("#337C64B8"))
            p.drawEllipse(hpt, 7.0, 7.0)
            p.setBrush(qcolor("#FFFFFFFF"))
            p.setPen(QPen(rec_ring if hrow.get("recovered") else line, 2.2))
            p.drawEllipse(hpt, 4.0, 4.0)

        p.setFont(QFont("Segoe UI", 8))
        p.setPen(ink)
        p.drawText(
            QRectF(left, bottom + 5, 160, 16),
            Qt.AlignmentFlag.AlignLeft,
            rows[0]["ts"].strftime("%m/%d %H:%M"),
        )
        p.drawText(
            QRectF(right - 160, bottom + 5, 160, 16),
            Qt.AlignmentFlag.AlignRight,
            rows[-1]["ts"].strftime("%m/%d %H:%M"),
        )
        p.end()


class _WinGUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_uint8 * 8),
    ]


class _DEV_BROADCAST_DEVICEINTERFACE_W(ctypes.Structure):
    _fields_ = [
        ("dbcc_size", ctypes.c_uint32),
        ("dbcc_devicetype", ctypes.c_uint32),
        ("dbcc_reserved", ctypes.c_uint32),
        ("dbcc_classguid", _WinGUID),
        ("dbcc_name", ctypes.c_wchar * 1),
    ]


# {4D1E55B2-F16F-11CF-88CB-001111000030} = GUID_DEVINTERFACE_HID
_GUID_DEVINTERFACE_HID = _WinGUID(
    0x4D1E55B2,
    0xF16F,
    0x11CF,
    (ctypes.c_uint8 * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30),
)
_DBT_DEVTYP_DEVICEINTERFACE = 5
_DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000


class Dashboard(QMainWindow):
    POWER_DEBOUNCE_S = 1.0
    DEVICE_CHANGE_DEBOUNCE_S = 4.0
    DEVICE_CHANGE_TRAILING_MS = 1200
    REPLUG_RETRY_DELAYS_MS = (2000, 4000)

    def __init__(self, start_worker=True):
        super().__init__()
        self.settings = QSettings("PersonalTools", "ATKBatteryMonitorV02")
        self.dark = False  # V0.6 uses unified Iris Greenhouse light translucent palette
        self.allow_quit = False
        self.last_reading = None
        self.warned = False
        self.manual_pending = False
        self.pending_query_id = None
        self._closing = False
        self._hidden_to_tray = False
        self._history_dirty = False
        self._last_tick = time.time()
        self._last_native_refresh = 0.0
        self._last_power_refresh = -1e9
        self._last_device_change_refresh = -1e9
        self._dev_notify_handle = None
        self._replug_retry_idx = 0
        self._replug_retries_remaining = 0
        self._device_change_trailing_timer = QTimer(self)
        self._device_change_trailing_timer.setSingleShot(True)
        self._device_change_trailing_timer.timeout.connect(self._on_device_change_trailing_timeout)
        self._replug_retry_timer = QTimer(self)
        self._replug_retry_timer.setSingleShot(True)
        self._replug_retry_timer.timeout.connect(self._on_replug_retry_timeout)
        self.estimator = VoltageSocEstimator()
        self.estimators: dict[str, VoltageSocEstimator | None] = {}
        self.registry = DeviceRegistry.load()
        self.binding_store = BindingStore(BINDINGS_PATH)
        self.active_device_uid: str | None = None
        self.active_lut_profile_id: str | None = ATK_F1_V2_ULTIMATE_PROFILE.profile_id
        self._pending_collision_event: dict | None = None
        self._current_display_model: str = "未检测到已适配设备"
        self._last_known_identity: dict | None = None
        self._unregistered_ecosystem_vid_pids: list[str] = []
        self._last_estimated_qid = None
        self._last_estimate = None
        self.badge = None
        self._badge_pos_mode, self._badge_custom_x = self._load_badge_position_settings()

        self.setWindowTitle(f"{APP_NAME} · {VERSION_LABEL}")
        self.setWindowIcon(load_app_icon())
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.setFixedSize(1080, 700)
        self._center_on_screen()

        # Hidden compatibility labels/holders so existing diagnostics & tests still find full text
        self.refresh_status = QLabel("等待首次电量查询 · 点击刷新可查看查询结果")
        self.storage_status = QLabel("采样与查询诊断分别记录；旧历史只读")
        self.main_pct_title = QLabel("电压估算电量")
        self.main_pct_sub = QLabel("基于 ADC 电压 1% 估算 · 等待读数")
        self.main_pct = text("--", role="big")
        self.main_pct_caption = text("ADC 1% 标定曲线 · 等待读数", role="muted")
        self.main_voltage = text("--", role="large")
        self.main_voltage_sub = text("等待有效电压", role="muted")
        self.main_fw_pct = text("--", role="large")
        self.main_fw_sub = text("5% 阶梯对照", role="muted")
        self.main_last = text("等待首次读取", role="large")
        self.main_hint = text("充电状态 unknown", role="muted")
        self.badge_low_extrap = PillBadge("低段外推", kind="coral")
        self.badge_low_extrap.hide()
        self.gauge = BatteryProgressBar()
        self.battery_bar = self.gauge
        self.state_dot = QLabel("●")
        self.state_chip = QLabel(f"{MODEL}")

        root = GreenhouseCanvas()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 1. Single compact top navigation bar (~56px, aligned to CONTENT_MAX_WIDTH)
        self.topbar = self._build_topbar()
        outer.addWidget(self.topbar)

        # 2. Main centered content container (max width 1240px for 2K/4K screens)
        body_wrap = QWidget()
        body_wrap.setAutoFillBackground(False)
        body_lay = QHBoxLayout(body_wrap)
        body_lay.setContentsMargins(20, 12, 20, 10)
        body_lay.setSpacing(0)
        body_lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.pages = QStackedWidget()
        self.pages.setMaximumWidth(CONTENT_MAX_WIDTH)
        self.pages.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body_lay.addWidget(self.pages)
        outer.addWidget(body_wrap, 1)

        # 3. Ultra-slim bottom status bar (~28px, aligned to CONTENT_MAX_WIDTH)
        self.statusbar_frame = self._build_statusbar()
        outer.addWidget(self.statusbar_frame)

        self._home()
        self._history()
        self._settings_page()
        self.select_page(0)
        self.apply_theme()

        # System Tray & Taskbar Badge setup
        self.tray = QSystemTrayIcon(load_app_icon(), self)
        menu = QMenu(self)
        show_action = menu.addAction("打开主界面")
        show_action.triggered.connect(self.show_dashboard)
        refresh_action = menu.addAction("立即刷新")
        refresh_action.triggered.connect(self.refresh)
        badge_enabled = str(self.settings.value("taskbar_badge", "true")).lower() == "true"
        self.badge_action = menu.addAction("任务栏直显")
        self.badge_action.setCheckable(True)
        self.badge_action.setChecked(badge_enabled)
        self.badge_action.toggled.connect(self.toggle_taskbar_badge)
        self.badge_drag_action = menu.addAction("解锁拖动位置")
        self.badge_drag_action.setEnabled(badge_enabled)
        self.badge_drag_action.triggered.connect(self.toggle_badge_drag_lock)
        self.badge_reset_pos_action = menu.addAction("恢复默认位置")
        self.badge_reset_pos_action.setEnabled(badge_enabled)
        self.badge_reset_pos_action.triggered.connect(self.reset_badge_position)
        folder_action = menu.addAction("打开记录文件夹")
        folder_action.triggered.connect(self.open_history_folder)
        menu.addSeparator()
        quit_action = menu.addAction("退出程序")
        quit_action.triggered.connect(self.exit_app)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

        try:
            self.badge = TaskbarBadge(
                enabled=badge_enabled,
                on_left_click=self.toggle_dashboard,
                on_right_click=self._show_badge_menu,
                on_position_changed=self._on_badge_position_changed,
                low_threshold=self.threshold.value(),
                position_mode=self._badge_pos_mode,
                custom_x=self._badge_custom_x,
                parent=self,
            )
            self.badge.start()
            self.destroyed.connect(lambda *_, b=self.badge: b.destroy())
        except Exception:
            log.warning("TaskbarBadge setup failed; continuing with tray icon only", exc_info=True)
            self.badge = None
        self._sync_badge_position_ui()

        saved_custom_img = str(self.settings.value("card_custom_image", "") or "").strip()
        if saved_custom_img and Path(saved_custom_img).is_file():
            self.ring_widget.set_custom_image(saved_custom_img)
        self._refresh_device_selector()

        self.worker = BatteryWorker(int(self.settings.value("interval", 300))) if start_worker else None
        if self.worker:
            self.worker.result.connect(self.on_result)
            self.worker.checking.connect(self.on_checking)
            self.worker.finished.connect(self._worker_finished)
            self.worker.start()
        self.refresh_history()
        self.tick = QTimer(self)
        self.tick.timeout.connect(self.update_clock)
        self.tick.start(1000)
        self.update_clock()
        self._register_hid_device_notification()

    def _center_on_screen(self):
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = geo.x() + max(0, (geo.width() - self.width()) // 2)
            y = geo.y() + max(0, (geo.height() - self.height()) // 2)
            self.move(x, y)

    # -----------------------------------------------------------------------
    # Top Single-Row Bar (~56px) & Bottom Slim Status Bar (~28px)
    # -----------------------------------------------------------------------
    def _build_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(56)
        wrap_lay = QHBoxLayout(bar)
        wrap_lay.setContentsMargins(20, 0, 20, 0)
        wrap_lay.setSpacing(0)
        wrap_lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.topbar_inner = QWidget()
        self.topbar_inner.setMaximumWidth(CONTENT_MAX_WIDTH)
        self.topbar_inner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        outer = QHBoxLayout(self.topbar_inner)
        outer.setContentsMargins(0, 8, 0, 8)
        outer.setSpacing(10)

        # Left: Diamond icon + ATK-Pulse brand + compact device selector dropdown
        brand_dot = QLabel("❖")
        brand_dot.setStyleSheet(f"color: {qss_rgba(PALETTE['iris_primary'])}; font-size: 16px; font-weight: 700;")
        outer.addWidget(brand_dot)

        brand = text(APP_NAME, role="brand")
        outer.addWidget(brand)

        self.device_selector = CleanComboBox()
        self.device_selector.setMinimumWidth(180)
        self.device_selector.setMaximumWidth(260)
        self.device_selector.currentIndexChanged.connect(self._on_device_selected)
        outer.addWidget(self.device_selector)
        outer.addStretch(1)

        # Center: Floating capsule segmented navigation with bottom coral underline
        nav_trough = QFrame()
        nav_trough.setObjectName("nav_trough")
        tl = QHBoxLayout(nav_trough)
        tl.setContentsMargins(4, 3, 4, 3)
        tl.setSpacing(4)
        self.nav_buttons: list[NavCapsuleButton] = []
        for idx, name in enumerate(("总览", "历史趋势", "偏好设置")):
            btn = NavCapsuleButton(name)
            btn.clicked.connect(lambda _, i=idx: self.select_page(i))
            self.nav_buttons.append(btn)
            tl.addWidget(btn)
        outer.addWidget(nav_trough)
        outer.addStretch(1)

        # Right: Taskbar badge status pill toggle + crisp primary refresh button
        badge_enabled = str(self.settings.value("taskbar_badge", "true")).lower() == "true"
        self.taskbar_pill = TaskbarStatusPill()
        self.taskbar_pill.setChecked(badge_enabled)
        self.taskbar_pill.toggled.connect(self.toggle_taskbar_badge)
        outer.addWidget(self.taskbar_pill)

        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.setFixedHeight(30)
        self.refresh_button.setStyleSheet(
            "QPushButton { background: rgba(255, 255, 255, 210); color: #5B5768; "
            "border: 1px solid rgba(212, 205, 226, 155); border-radius: 10px; "
            "padding: 0 14px; font-size: 11px; font-weight: 700; }"
            "QPushButton:hover { background: rgba(255, 255, 255, 250); color: #1B1922; "
            "border: 1px solid rgba(124, 100, 184, 85); }"
            "QPushButton:pressed { background: rgba(241, 239, 246, 235); }"
            "QPushButton:disabled { color: #867E96; background: rgba(241, 239, 246, 140); }"
        )
        self.refresh_button.clicked.connect(self.refresh)
        outer.addWidget(self.refresh_button)

        wrap_lay.addWidget(self.topbar_inner)
        return bar

    def _build_statusbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("statusbar")
        bar.setFixedHeight(28)
        self.statusbar_frame = bar
        wrap_lay = QHBoxLayout(bar)
        wrap_lay.setContentsMargins(20, 0, 20, 0)
        wrap_lay.setSpacing(0)
        wrap_lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.statusbar_inner = QWidget()
        self.statusbar_inner.setMaximumWidth(CONTENT_MAX_WIDTH)
        self.statusbar_inner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QHBoxLayout(self.statusbar_inner)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(10)

        self.status_left = text("● 充电状态 unknown · 自动轮询中", role="muted")
        self.status_right = text(STATUS_RIGHT_TEXT, role="muted")
        self.status_right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.status_left, 1)
        lay.addWidget(self.status_right)
        wrap_lay.addWidget(self.statusbar_inner)
        self._sync_statusbar_tooltip()
        return bar

    def _sync_statusbar_tooltip(self):
        full_diag = (
            f"查询状态：{self.refresh_status.text()}\n"
            f"存储与契约：{self.storage_status.text()}\n"
            f"说明：设备测量时间未知，response_freshness=unverified；充电状态保持 unknown。"
        )
        self.statusbar_frame.setToolTip(full_diag)
        self.status_left.setToolTip(full_diag)
        self.status_right.setToolTip(full_diag)

    def _add_page(self, page: QWidget):
        page.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(page)
        page.setAutoFillBackground(False)
        self.pages.addWidget(scroll)

    # -----------------------------------------------------------------------
    # Page 1: Overview (总览页 — V0.8 Minimalist Dual-Column Layout)
    # -----------------------------------------------------------------------
    def _home(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(12)

        # Lightweight shared-PID collision disambiguation banner (hidden unless ambiguous_unbound)
        self.collision_banner = panel(kind="subcard")
        cbl = QHBoxLayout(self.collision_banner)
        cbl.setContentsMargins(16, 10, 16, 10)
        cbl.setSpacing(10)
        self.collision_label = text("检测到共用接收器，请确认您的鼠标型号：", wrap=False)
        self.collision_combo = CleanComboBox()
        self.collision_combo.setMinimumWidth(240)
        self.collision_confirm_btn = tiny_button("确认型号", primary=True)
        self.collision_confirm_btn.clicked.connect(lambda: self.confirm_ambiguous_model())
        cbl.addWidget(self.collision_label)
        cbl.addWidget(self.collision_combo)
        cbl.addStretch(1)
        cbl.addWidget(self.collision_confirm_btn)
        self.collision_banner.hide()
        l.addWidget(self.collision_banner)

        # Dual-column body: fixed 340px Left Device & Ring Card + Expanding Right Trend Card
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)

        # Left core card (340px) with faint 90-degree 'MOUSE' watermark along right edge
        self.core_left_card = WatermarkCardFrame()
        self.core_left_card.setFixedWidth(340)
        self.core_left_wrap = self.core_left_card
        lc_l = QVBoxLayout(self.core_left_card)
        lc_l.setContentsMargins(20, 18, 20, 18)
        lc_l.setSpacing(12)

        top_head = QHBoxLayout()
        top_head.setContentsMargins(0, 0, 0, 0)
        top_head.setSpacing(8)
        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(2)
        self.card_brand_label = text("ATK-Pulse", role="cardTitle")
        self.card_brand_label.setStyleSheet(
            f"color: {qss_rgba(PALETTE['text_primary'])}; font-size: 22px; font-weight: 800; letter-spacing: 0.4px;"
        )
        self.card_model_label = text("未检测到已适配设备", role="body", wrap=False)
        self.card_model_label.setStyleSheet(
            f"color: {qss_rgba(PALETTE['text_secondary'])}; font-size: 13px; font-weight: 600;"
        )
        title_col.addWidget(self.card_brand_label)
        title_col.addWidget(self.card_model_label)
        top_head.addLayout(title_col, 1)

        # Strictly single metric pill top-right (metric-only, never duplicated into low-battery warning)
        self.badge_est_type = PillBadge("● 固件原报", kind="muted")
        top_head.addWidget(self.badge_est_type, 0, Qt.AlignmentFlag.AlignTop)
        lc_l.addLayout(top_head)
        lc_l.addStretch(1)

        # Center pure-data ultra-thin battery ring (no built-in mouse SVG/PNG)
        ring_wrap = QHBoxLayout()
        ring_wrap.setContentsMargins(0, 2, 0, 2)
        self.ring_widget = BatteryRingWidget()
        self.ring_widget.customImageSelected.connect(self._on_custom_image_selected)
        self.ring_widget.customImageReset.connect(self._on_custom_image_reset)
        ring_wrap.addStretch(1)
        ring_wrap.addWidget(self.ring_widget)
        ring_wrap.addStretch(1)
        lc_l.addLayout(ring_wrap)
        lc_l.addStretch(1)

        # Bottom 2x2 natural-language metric slots
        slot_grid = QGridLayout()
        slot_grid.setContentsMargins(0, 0, 0, 0)
        slot_grid.setHorizontalSpacing(8)
        slot_grid.setVerticalSpacing(8)

        def _make_slot(title_txt: str, default_val: str):
            tile = panel(kind="slot_tile")
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(12, 9, 12, 9)
            tl.setSpacing(3)
            hdr = text(title_txt, role="muted")
            hdr.setStyleSheet(f"color: {qss_rgba(PALETTE['text_muted'])}; font-size: 11px; font-weight: 600;")
            val = text(default_val, role="body", wrap=False)
            val.setStyleSheet(f"color: {qss_rgba(PALETTE['text_primary'])}; font-size: 13px; font-weight: 700;")
            tl.addWidget(hdr)
            tl.addWidget(val)
            return tile, tl, val

        tile_link, _, self.slot_link_val = _make_slot("连接方式", "未连接")
        tile_cycle, _, self.slot_cycle_val = _make_slot("本周期已用", "样本积累中")
        tile_drain, _, self.slot_drain_val = _make_slot("耗电参考", "样本积累中")

        tile_upd = panel(kind="slot_tile")
        tu_l = QVBoxLayout(tile_upd)
        tu_l.setContentsMargins(12, 9, 10, 9)
        tu_l.setSpacing(3)
        upd_hdr = text("最近更新", role="muted")
        upd_hdr.setStyleSheet(f"color: {qss_rgba(PALETTE['text_muted'])}; font-size: 11px; font-weight: 600;")
        tu_l.addWidget(upd_hdr)
        upd_bot = QHBoxLayout()
        upd_bot.setContentsMargins(0, 0, 0, 0)
        upd_bot.setSpacing(4)
        self.slot_updated_val = text("暂无更新", role="body", wrap=False)
        self.slot_updated_val.setStyleSheet(f"color: {qss_rgba(PALETTE['text_primary'])}; font-size: 13px; font-weight: 700;")
        self.slot_state_pill = PillBadge("未连接", kind="muted")
        upd_bot.addWidget(self.slot_updated_val, 1)
        upd_bot.addWidget(self.slot_state_pill, 0)
        tu_l.addLayout(upd_bot)

        slot_grid.addWidget(tile_link, 0, 0)
        slot_grid.addWidget(tile_cycle, 0, 1)
        slot_grid.addWidget(tile_drain, 1, 0)
        slot_grid.addWidget(tile_upd, 1, 1)
        lc_l.addLayout(slot_grid)

        self.core_subcards = [tile_link, tile_cycle, tile_drain, tile_upd]
        body.addWidget(self.core_left_card, 0)

        # Right full-height single Trend Card ("电量走势", zero clutter, zero export button)
        self.core_right_card = panel(kind="panel")
        self.core_right_wrap = self.core_right_card
        tl = QVBoxLayout(self.core_right_card)
        tl.setContentsMargins(20, 18, 20, 14)
        tl.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(text("电量走势", role="cardTitle"))
        header.addStretch(1)

        self.home_period_seg = SegmentedControl([("24", "24h"), ("168", "7天"), ("cycle", "本周期")])
        self.home_period_seg.set_current("24")
        self.home_period_seg.changed.connect(self._on_home_seg_period_changed)

        self.home_mode_seg = SegmentedControl([("percent", "电量"), ("mv", "电压")])
        self.home_mode_seg.set_current("percent")
        self.home_mode_seg.changed.connect(self._on_home_seg_mode_changed)

        # Compatibility controls synced with SegmentedControls
        self.home_mode = CleanComboBox()
        self.home_mode.addItems(["电量 %", "电压 V"])
        self.home_mode.currentIndexChanged.connect(self._on_home_combo_mode_changed)
        self.home_mode.hide()

        self.home_period = CleanComboBox()
        for label, hours in (("最近 1 小时", 1), ("最近 24 小时", 24), ("最近 7 天", 7 * 24), ("本周期", "cycle")):
            self.home_period.addItem(label, hours)
        self.home_period.setCurrentIndex(1)
        self.home_period.currentIndexChanged.connect(self._on_home_period_changed)
        self.home_period.hide()

        self.home_auto_scale = ToggleSwitch("Y 轴自动缩放", compact=True)
        self.home_auto_scale.setChecked(default_auto_scale_for_hours(24))
        self.home_auto_scale.toggled.connect(lambda _: self.refresh_history())
        self.home_auto_scale.hide()

        self.home_fw_overlay = ToggleSwitch("固件对照", compact=True)
        self.home_fw_overlay.setChecked(False)
        self.home_fw_overlay.toggled.connect(lambda _: self.refresh_history())
        self.home_fw_overlay.hide()

        header.addWidget(self.home_period_seg)
        header.addWidget(self.home_mode_seg)
        tl.addLayout(header)

        self.home_chart = TrendChart()
        self.home_chart.show_firmware = False
        tl.addWidget(self.home_chart, 1)

        note_row = QHBoxLayout()
        note_row.setSpacing(8)
        self.home_chart_note = text("持续记录中", role="muted")
        self.home_legend = text("— 电量变化   ■ 休眠空窗", role="muted")
        self.home_legend.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        note_row.addWidget(self.home_chart_note, 1)
        note_row.addWidget(self.home_legend, 0)
        tl.addLayout(note_row)

        body.addWidget(self.core_right_card, 1)
        l.addLayout(body, 1)
        self._add_page(page)

    # -----------------------------------------------------------------------
    # Page 2: History & Trends (历史趋势页)
    # -----------------------------------------------------------------------
    def _history(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(12)

        trend_card = panel(kind="panel")
        tl = QVBoxLayout(trend_card)
        tl.setContentsMargins(18, 14, 18, 12)
        tl.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(text("历史走势与对照", role="cardTitle"))
        top.addStretch(1)

        export_cycle_btn = tiny_button("导出本周期 CSV")
        export_cycle_btn.clicked.connect(self.export_cycle_csv_action)
        export_btn = tiny_button("导出全部 CSV")
        export_btn.clicked.connect(self.export_csv)
        folder_btn = tiny_button("打开记录文件夹")
        folder_btn.clicked.connect(self.open_history_folder)
        top.addWidget(export_cycle_btn)
        top.addWidget(export_btn)
        top.addWidget(folder_btn)
        tl.addLayout(top)

        sub_top = QHBoxLayout()
        sub_top.setSpacing(8)
        self.history_period = CleanComboBox()
        for label, hours in (
            ("最近 1 小时", 1),
            ("最近 24 小时", 24),
            ("最近 7 天", 7 * 24),
            ("最近 30 天", 30 * 24),
            ("全部记录", None),
        ):
            self.history_period.addItem(label, hours)
        self.history_period.setCurrentIndex(3)  # Default: 最近 30 天
        self.history_period.currentIndexChanged.connect(self._on_history_period_changed)

        self.history_mode = CleanComboBox()
        self.history_mode.addItems(["电量 %", "电压 V"])
        self.history_mode.currentIndexChanged.connect(lambda _: self.refresh_history())

        self.history_auto_scale = ToggleSwitch("Y 轴自动缩放", compact=True)
        self.history_auto_scale.setChecked(default_auto_scale_for_hours(30 * 24))
        self.history_auto_scale.toggled.connect(lambda _: self.refresh_history())

        self.show_fw_overlay = ToggleSwitch("固件对照", compact=True)
        self.show_fw_overlay.setChecked(True)
        self.show_fw_overlay.toggled.connect(self._on_history_fw_toggled)

        sub_top.addWidget(self.history_period)
        sub_top.addWidget(self.history_mode)
        sub_top.addWidget(self.history_auto_scale)
        sub_top.addWidget(self.show_fw_overlay)
        sub_top.addStretch(1)
        tl.addLayout(sub_top)

        self.history_chart = TrendChart()
        self.history_chart.setMinimumHeight(205)
        tl.addWidget(self.history_chart, 1)

        desc_row = QHBoxLayout()
        desc_row.setSpacing(8)
        self.history_desc = text("暂无有效记录", role="muted")
        self.history_legend = text("— 电压估算   - - 固件原报   ■ 休眠空窗", role="muted")
        self.history_legend.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        desc_row.addWidget(self.history_desc, 1)
        desc_row.addWidget(self.history_legend, 0)
        tl.addLayout(desc_row)
        l.addWidget(trend_card, 3)

        # Lower table card: clean 5-column table without heavy gridlines
        panel_table = panel(kind="panel")
        table_l = QVBoxLayout(panel_table)
        table_l.setContentsMargins(18, 12, 18, 12)
        table_l.setSpacing(8)

        t_head = QHBoxLayout()
        t_head.addWidget(text("最近 50 条采集明细", role="cardTitle"))
        t_head.addStretch()
        table_l.addLayout(t_head)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["本机接收时间", "电压估算", "固件原报", "原始电压", "来源"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setMouseTracking(True)
        self.table.setItemDelegate(HoverRowTableDelegate(self.table))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setHighlightSections(False)
        table_l.addWidget(self.table, 1)
        l.addWidget(panel_table, 2)
        self._add_page(page)

    # -----------------------------------------------------------------------
    # Page 3: Preferences (偏好设置页 — V0.8 4 Glass Cards, Balanced Grid)
    # -----------------------------------------------------------------------
    def _settings_page(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(12)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        # Card 1: 显示与任务栏
        card_display = panel(kind="panel")
        card_display.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        card_display.setMinimumWidth(0)
        cd_l = vertical(spacing=10, margins=(18, 14, 18, 14))
        cd_l.addWidget(text("显示与任务栏", role="cardTitle"))

        row_badge = QHBoxLayout()
        row_badge.addWidget(text("Win11 任务栏无侵入透明直显", wrap=False))
        row_badge.addStretch()
        badge_on = str(self.settings.value("taskbar_badge", "true")).lower() == "true"
        self.taskbar_badge_switch = ToggleSwitch()
        self.taskbar_badge_switch.setChecked(badge_on)
        self.taskbar_badge_switch.toggled.connect(self.toggle_taskbar_badge)
        row_badge.addWidget(self.taskbar_badge_switch)
        row_badge_w = QWidget()
        row_badge_w.setMinimumHeight(26)
        row_badge_w.setLayout(row_badge)
        row_badge.setContentsMargins(0, 0, 0, 0)
        cd_l.addWidget(row_badge_w)

        row_badge_pos_mode = QHBoxLayout()
        row_badge_pos_mode.setContentsMargins(0, 0, 0, 0)
        row_badge_pos_mode.setSpacing(6)
        row_badge_pos_mode.addWidget(text("任务栏贴片位置", wrap=False))
        row_badge_pos_mode.addStretch()
        self.badge_pos_mode_pill = PillBadge(
            "自定义" if self._badge_pos_mode == "custom" else "自动默认",
            kind="iris" if self._badge_pos_mode == "custom" else "muted",
        )
        row_badge_pos_mode.addWidget(self.badge_pos_mode_pill)
        self.badge_drag_btn = tiny_button("解锁拖动")
        self.badge_drag_btn.setEnabled(badge_on)
        self.badge_drag_btn.clicked.connect(self.toggle_badge_drag_lock)
        row_badge_pos_mode.addWidget(self.badge_drag_btn)
        row_badge_pos_mode_w = QWidget()
        row_badge_pos_mode_w.setMinimumHeight(26)
        row_badge_pos_mode_w.setLayout(row_badge_pos_mode)
        cd_l.addWidget(row_badge_pos_mode_w)

        row_badge_pos_adj = QHBoxLayout()
        row_badge_pos_adj.setContentsMargins(0, 0, 0, 0)
        row_badge_pos_adj.setSpacing(6)
        row_badge_pos_adj.addWidget(text("水平偏移微调", wrap=False))
        row_badge_pos_adj.addStretch()
        self.badge_pos_spin = CleanSpinBox()
        self.badge_pos_spin.setRange(0, 3840)
        self.badge_pos_spin.setSingleStep(10)
        self.badge_pos_spin.setSuffix(" px")
        self.badge_pos_spin.setValue(int(self._badge_custom_x or 0))
        self.badge_pos_spin.setEnabled(badge_on)
        self.badge_pos_spin.valueChanged.connect(self._on_badge_pos_spin_changed)
        row_badge_pos_adj.addWidget(self.badge_pos_spin)
        self.badge_reset_pos_btn = tiny_button("恢复默认")
        self.badge_reset_pos_btn.setEnabled(badge_on)
        self.badge_reset_pos_btn.clicked.connect(self.reset_badge_position)
        row_badge_pos_adj.addWidget(self.badge_reset_pos_btn)
        row_badge_pos_adj_w = QWidget()
        row_badge_pos_adj_w.setMinimumHeight(26)
        row_badge_pos_adj_w.setLayout(row_badge_pos_adj)
        cd_l.addWidget(row_badge_pos_adj_w)

        row_alert = QHBoxLayout()
        row_alert.setContentsMargins(0, 0, 0, 0)
        row_alert.addWidget(text("启用低电量系统托盘通知", wrap=False))
        row_alert.addStretch()
        self.alert_enabled = ToggleSwitch()
        self.alert_enabled.setChecked(str(self.settings.value("alerts", "true")).lower() == "true")
        self.alert_enabled.toggled.connect(lambda on: self.settings.setValue("alerts", on))
        row_alert.addWidget(self.alert_enabled)
        row_alert_w = QWidget()
        row_alert_w.setMinimumHeight(26)
        row_alert_w.setLayout(row_alert)
        cd_l.addWidget(row_alert_w)

        row_thresh = QHBoxLayout()
        row_thresh.setContentsMargins(0, 0, 0, 0)
        row_thresh.addWidget(text("低电量提醒阈值", wrap=False))
        row_thresh.addStretch()
        self.threshold = CleanSpinBox()
        self.threshold.setRange(5, 50)
        self.threshold.setSuffix(" %")
        self.threshold.setValue(int(self.settings.value("threshold", 20)))
        self.threshold.valueChanged.connect(self._on_threshold_changed)
        row_thresh.addWidget(self.threshold)
        row_thresh_w = QWidget()
        row_thresh_w.setMinimumHeight(26)
        row_thresh_w.setLayout(row_thresh)
        cd_l.addWidget(row_thresh_w)

        row_theme = QHBoxLayout()
        row_theme.setContentsMargins(0, 0, 0, 0)
        row_theme.addWidget(text("界面视觉主题", wrap=False))
        row_theme.addStretch()
        row_theme.addWidget(PillBadge("鸢尾温室 · 浅色", kind="iris"))
        row_theme_w = QWidget()
        row_theme_w.setMinimumHeight(26)
        row_theme_w.setLayout(row_theme)
        cd_l.addWidget(row_theme_w)

        cd_l.addWidget(text("右键贴片可解锁水平拖动或恢复默认，位置越界自动钳制在任务栏内；自适应深浅任务栏与全屏隐藏。", role="muted", wrap=True))
        cd_l.addStretch()
        card_display.setLayout(cd_l)
        grid.addWidget(card_display, 0, 0)

        # Card 2: 采样与自启
        card_sample = panel(kind="panel")
        card_sample.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        card_sample.setMinimumWidth(0)
        cs_l = vertical(spacing=10, margins=(18, 14, 18, 14))
        cs_l.addWidget(text("采样与自启", role="cardTitle"))

        row_int = QHBoxLayout()
        row_int.setContentsMargins(0, 0, 0, 0)
        row_int.addWidget(text("后台自动采样间隔", wrap=False))
        row_int.addStretch()
        self.interval = CleanComboBox()
        self.interval.addItem("30 秒", 30)
        self.interval.addItem("1 分钟", 60)
        self.interval.addItem("2 分钟", 120)
        self.interval.addItem("5 分钟（推荐）", 300)
        saved = int(self.settings.value("interval", 300))
        self.interval.setCurrentIndex(max(0, self.interval.findData(saved)))
        self.interval.currentIndexChanged.connect(self.interval_changed)
        row_int.addWidget(self.interval)
        row_int_w = QWidget()
        row_int_w.setMinimumHeight(26)
        row_int_w.setLayout(row_int)
        cs_l.addWidget(row_int_w)

        row_auto = QHBoxLayout()
        row_auto.setContentsMargins(0, 0, 0, 0)
        row_auto.addWidget(text("登录 Windows 时自动启动", wrap=False))
        row_auto.addStretch()
        self.autostart = ToggleSwitch()
        self.autostart.setChecked(self._autostart_enabled())
        self.autostart.toggled.connect(self.change_autostart)
        row_auto.addWidget(self.autostart)
        row_auto_w = QWidget()
        row_auto_w.setMinimumHeight(26)
        row_auto_w.setLayout(row_auto)
        cs_l.addWidget(row_auto_w)

        cs_l.addWidget(text("无响应按 15s -> 60s -> 5min 退避；接收器缺席每 30 秒枚举。", role="muted", wrap=True))
        cs_l.addWidget(text("关闭主窗口自动最小化至托盘；右键托盘图标可安全关闭。", role="muted", wrap=True))
        cs_l.addStretch()
        card_sample.setLayout(cs_l)
        grid.addWidget(card_sample, 0, 1)

        # Card 3: 设备档案与维护
        card_profile = panel(kind="panel")
        card_profile.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        card_profile.setMinimumWidth(0)
        cp_l = vertical(spacing=10, margins=(18, 14, 18, 14))
        cp_l.addWidget(text("设备档案与维护", role="cardTitle"))
        self.profile_device_label = text(
            "当前档案：未检测到已适配接收器或鼠标",
            role="body",
            wrap=True,
        )
        cp_l.addWidget(self.profile_device_label)
        cp_l.addWidget(text(
            "新采样仅追加至 battery_samples.v2.csv，v1/V0.2/V0.1 原始历史严格只读。",
            role="muted",
            wrap=True,
        ))

        btn_grid = QHBoxLayout()
        btn_grid.setSpacing(6)
        self.btn_reselect_model = tiny_button("重选型号")
        self.btn_reselect_model.clicked.connect(self._prompt_reselect_model)
        self.btn_custom_img = tiny_button("自定义图片")
        self.btn_custom_img.clicked.connect(self._choose_custom_card_image)
        self.btn_reset_img = tiny_button("纯数据圆环")
        self.btn_reset_img.clicked.connect(self._on_custom_image_reset)
        btn_grid.addWidget(self.btn_reselect_model)
        btn_grid.addWidget(self.btn_custom_img)
        btn_grid.addWidget(self.btn_reset_img)
        btn_grid.addStretch()
        cp_l.addLayout(btn_grid)

        btn_grid2 = QHBoxLayout()
        btn_grid2.setSpacing(6)
        btn_folder = tiny_button("打开数据目录")
        btn_folder.clicked.connect(self.open_history_folder)
        self.btn_diag_zip = tiny_button("导出脱敏诊断包 (ZIP)", primary=True)
        self.btn_diag_zip.clicked.connect(self.export_diagnostic_zip_action)
        btn_grid2.addWidget(btn_folder)
        btn_grid2.addWidget(self.btn_diag_zip)
        btn_grid2.addStretch()
        cp_l.addLayout(btn_grid2)
        cp_l.addStretch()
        card_profile.setLayout(cp_l)
        grid.addWidget(card_profile, 1, 0)

        # Card 4: 开源与声明
        card_oss = panel(kind="panel")
        card_oss.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        card_oss.setMinimumWidth(0)
        co_l = vertical(spacing=10, margins=(18, 14, 18, 14))
        oss_hdr = QHBoxLayout()
        oss_hdr.addWidget(text("开源与声明", role="cardTitle"))
        oss_hdr.addStretch()
        oss_hdr.addWidget(PillBadge("永久免费开源 · 谨防倒卖", kind="coral"))
        co_l.addLayout(oss_hdr)
        co_l.addWidget(text(
            f"• {APP_NAME} {VERSION_LABEL} by {AUTHOR_NAME} ({AUTHOR_HANDLE})，以 GNU GPL-3.0-or-later 协议永久免费开源。\n"
            f"• 项目主页：{REPO_URL} · 100% 本地离线运行，无联网与遥测。\n"
            "• 协议兼容研究致谢：NabuCasa / atk-f1-battery / VGN Hub 社区逆向资料与自主实机采样。",
            role="body",
            wrap=True,
        ))
        co_l.addWidget(text(
            "充电状态严守 unknown（不以 link_byte 臆断充电）；电压估算采用 15 点 LUT + 压降门控。",
            role="muted",
            wrap=True,
        ))
        co_l.addStretch()
        card_oss.setLayout(co_l)
        grid.addWidget(card_oss, 1, 1)

        l.addLayout(grid)
        l.addStretch(1)
        self._add_page(page)

    # -----------------------------------------------------------------------
    # Navigation, Theme & Chart Control Sync
    # -----------------------------------------------------------------------
    def _on_home_seg_period_changed(self, key: str):
        data = "cycle" if key == "cycle" else int(key)
        idx = self.home_period.findData(data)
        if idx >= 0 and self.home_period.currentIndex() != idx:
            self.home_period.blockSignals(True)
            self.home_period.setCurrentIndex(idx)
            self.home_period.blockSignals(False)
        self.home_auto_scale.blockSignals(True)
        self.home_auto_scale.setChecked(True if key == "cycle" else default_auto_scale_for_hours(data))
        self.home_auto_scale.blockSignals(False)
        self.refresh_history()

    def _on_home_seg_mode_changed(self, key: str):
        idx = 0 if key == "percent" else 1
        if self.home_mode.currentIndex() != idx:
            self.home_mode.blockSignals(True)
            self.home_mode.setCurrentIndex(idx)
            self.home_mode.blockSignals(False)
        self.refresh_history()

    def _on_home_combo_mode_changed(self, idx: int):
        key = "percent" if idx == 0 else "mv"
        if hasattr(self, "home_mode_seg"):
            self.home_mode_seg.set_current(key, emit_signal=False)
        self.refresh_history()

    def _on_home_period_changed(self, _index: int):
        data = self.home_period.currentData()
        if hasattr(self, "home_period_seg"):
            seg_key = "cycle" if data == "cycle" else str(data)
            if seg_key in ("24", "168", "cycle"):
                self.home_period_seg.set_current(seg_key, emit_signal=False)
        self.home_auto_scale.blockSignals(True)
        self.home_auto_scale.setChecked(True if data == "cycle" else default_auto_scale_for_hours(data))
        self.home_auto_scale.blockSignals(False)
        self.refresh_history()

    def _refresh_device_selector(self, event: dict | None = None):
        if not hasattr(self, "device_selector"):
            return
        self.device_selector.blockSignals(True)
        self.device_selector.clear()
        seen_uids: set[str] = set()
        for b in self.binding_store.load_all().values():
            m = self.registry.get_model(b.selected_model_id)
            name = m.display_name if m is not None else b.selected_model_id
            self.device_selector.addItem(name, b.device_uid)
            seen_uids.add(b.device_uid)
        if event:
            uid = event.get("device_uid") or event.get("device_id")
            if uid and uid not in seen_uids and uid not in ("__default__",):
                name = event.get("model_display_name") or event.get("name") or "未检测到已适配设备"
                self.device_selector.addItem(name, uid)
                seen_uids.add(uid)
        if self.device_selector.count() == 0:
            self.device_selector.addItem("未检测到已适配设备", "__default__")
        if self.active_device_uid:
            idx = self.device_selector.findData(self.active_device_uid)
            if idx >= 0:
                self.device_selector.setCurrentIndex(idx)
        self.device_selector.blockSignals(False)

    def _on_device_selected(self, _idx: int):
        uid = self.device_selector.currentData()
        if not uid or uid == self.active_device_uid:
            return
        self.active_device_uid = uid
        b = self.binding_store.get(uid)
        if b is not None:
            m = self.registry.get_model(b.selected_model_id)
            if m is not None:
                self.active_lut_profile_id = m.lut_profile_id
                self._apply_card_identity(
                    brand=m.brand,
                    model_name=m.display_name,
                    vid_pid=b.vid_pid,
                    protocol_family=m.protocol_family,
                    tier=m.verification_tier,
                    lut_id=m.lut_profile_id,
                )
        self.refresh_history()

    def _apply_card_identity(
        self,
        *,
        brand: str = "ATK",
        model_name: str = MODEL,
        vid_pid: str = "373B:11D9",
        protocol_family: str = "nordic52",
        tier: str = "verified",
        lut_id: str | None = ATK_F1_V2_ULTIMATE_PROFILE.profile_id,
    ):
        clean_brand = (brand or "ATK").strip()
        short_model = (model_name or MODEL).strip()
        if short_model.lower().startswith(clean_brand.lower() + " "):
            short_model = short_model[len(clean_brand) + 1:].strip()
        if hasattr(self, "card_brand_label"):
            self.card_brand_label.setText(clean_brand)
        if hasattr(self, "card_model_label"):
            self.card_model_label.setText(short_model)
        if hasattr(self, "profile_device_label"):
            lut_txt = lut_id or "未标定 (仅固件原报)"
            self.profile_device_label.setText(
                f"当前档案：{clean_brand} {short_model} · VID:PID {vid_pid} · 协议 {protocol_family} · tier={tier} · LUT={lut_txt}"
            )

    def _on_threshold_changed(self, value: int):
        self.settings.setValue("threshold", int(value))
        if getattr(self, "badge", None) is not None:
            self.badge.set_low_threshold(int(value))

    def _on_custom_image_selected(self, path: str):
        self.settings.setValue("card_custom_image", path)

    def _on_custom_image_reset(self):
        self.settings.setValue("card_custom_image", "")
        if hasattr(self, "ring_widget"):
            self.ring_widget.set_custom_image(None)

    def _choose_custom_card_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择自定义鼠标图片", "", "PNG/图片 (*.png *.jpg *.jpeg *.webp)"
        )
        if path and hasattr(self, "ring_widget"):
            self.ring_widget.set_custom_image(path)
            self.settings.setValue("card_custom_image", path)

    def _prompt_reselect_model(self):
        ev = self._pending_collision_event or self.last_reading
        vid_pid = ev.get("vid_pid") if ev else None
        if not vid_pid:
            QMessageBox.information(self, "当前设备档案", "当前尚未检测到已适配设备。")
            return
        rec = self.registry.receivers.get(vid_pid.upper())
        candidates = self.registry.candidate_models_for(rec) if rec is not None else ()
        if not candidates:
            QMessageBox.information(self, "当前设备档案", f"当前设备 ({vid_pid}) 无多机型共用冲突。")
            return
        if len(candidates) == 1:
            QMessageBox.information(self, "唯一匹配型号", f"{vid_pid} 仅对应型号：{candidates[0].display_name}。")
            return
        synthetic = dict(ev or {})
        synthetic["binding_state"] = "ambiguous_unbound"
        synthetic["vid_pid"] = vid_pid
        synthetic["device_uid"] = self.active_device_uid or synthetic.get("device_uid") or f"{vid_pid.lower()}:manual"
        synthetic["candidate_models"] = [{"model_id": c.model_id, "display_name": c.display_name} for c in candidates]
        self._update_collision_banner(synthetic)
        self.select_page(0)

    def _on_history_period_changed(self, _index: int):
        hours = self.history_period.currentData()
        self.history_auto_scale.blockSignals(True)
        self.history_auto_scale.setChecked(default_auto_scale_for_hours(hours))
        self.history_auto_scale.blockSignals(False)
        self.refresh_history()

    def _on_home_fw_toggled(self, checked: bool):
        if hasattr(self, "show_fw_overlay") and self.show_fw_overlay.isChecked() != checked:
            self.show_fw_overlay.blockSignals(True)
            self.show_fw_overlay.setChecked(checked)
            self.show_fw_overlay.blockSignals(False)
        self.refresh_history()

    def _on_history_fw_toggled(self, checked: bool):
        if hasattr(self, "home_fw_overlay") and self.home_fw_overlay.isChecked() != checked:
            self.home_fw_overlay.blockSignals(True)
            self.home_fw_overlay.setChecked(checked)
            self.home_fw_overlay.blockSignals(False)
        self.refresh_history()

    def select_page(self, index: int):
        self.pages.setCurrentIndex(index)
        for i, b in enumerate(self.nav_buttons):
            b.set_active(i == index)
        if index == 1:
            self.refresh_history()

    def apply_theme(self):
        root = self.centralWidget()
        root.setStyleSheet(IRIS_GREENHOUSE_QSS)
        self.home_chart.dark = False
        self.history_chart.dark = False
        self.home_chart.update()
        self.history_chart.update()

    def change_theme(self, value: str):
        self.dark = False
        self.settings.setValue("theme", "浅色")
        self.apply_theme()

    def interval_changed(self):
        interval = int(self.interval.currentData() or 300)
        self.settings.setValue("interval", interval)
        if getattr(self, "worker", None):
            self.worker.set_interval(interval)

    def _get_or_create_estimator(self, device_uid: str, lut_profile_id: str | None) -> VoltageSocEstimator | None:
        if not lut_profile_id:
            self.estimators.pop(device_uid, None)
            return None
        key = f"{device_uid}:{lut_profile_id}"
        est = self.estimators.get(key)
        if est is None:
            est = create_estimator(lut_profile_id)
            if est is not None:
                self.estimators[key] = est
        return est

    def _update_collision_banner(self, event: dict):
        if event.get("binding_state") == "ambiguous_unbound" and event.get("candidate_models"):
            candidates = event["candidate_models"]
            rec_id = event.get("recommended_model_id")
            vid_pid = event.get("vid_pid") or "共用接收器"
            self.collision_label.setText(f"检测到共用接收器 {vid_pid}，请确认鼠标型号：")
            self.collision_combo.blockSignals(True)
            self.collision_combo.clear()
            rec_idx = 0
            for idx, cand in enumerate(candidates):
                label = cand["display_name"]
                if cand["model_id"] == rec_id:
                    label += "（推荐 · 检测到历史记录）"
                    rec_idx = idx
                self.collision_combo.addItem(label, cand["model_id"])
            self.collision_combo.setCurrentIndex(rec_idx)
            self.collision_combo.blockSignals(False)
            self._pending_collision_event = dict(event)
            self.collision_banner.show()
        else:
            self._pending_collision_event = None
            self.collision_banner.hide()

    def confirm_ambiguous_model(self, model_id: str | None = None):
        selected_id = model_id or self.collision_combo.currentData()
        ev = self._pending_collision_event or self.last_reading
        if not selected_id or not ev:
            return None
        dev_uid = ev.get("device_uid") or ev.get("device_id")
        if not dev_uid:
            return None
        vid_pid = (ev.get("vid_pid") or "373B:11D9").upper()
        identity_basis = ev.get("device_identity_basis") or "unknown"
        binding = self.binding_store.set_binding(
            device_uid=dev_uid,
            vid_pid=vid_pid,
            selected_model_id=selected_id,
            identity_basis=identity_basis,
            include_legacy_v1_v01_v02=(selected_id == "atk_f1_v2_ultimate"),
        )
        model = self.registry.get_model(selected_id)
        self.active_device_uid = dev_uid
        self.active_lut_profile_id = model.lut_profile_id if model else None
        self._pending_collision_event = None
        self.collision_banner.hide()

        updated = dict(ev)
        updated["binding_state"] = "bound"
        updated["model_id"] = selected_id
        if model is not None:
            updated["model_display_name"] = model.display_name
            updated["name"] = model.display_name
            updated["brand"] = model.brand
            updated["verification_tier"] = model.verification_tier
            updated["calibration_status"] = model.calibration_status
            updated["lut_profile_id"] = model.lut_profile_id
        if updated.get("last_success"):
            ls = dict(updated["last_success"])
            ls["model_id"] = selected_id
            ls["lut_profile_id"] = updated.get("lut_profile_id")
            updated["last_success"] = ls
        self._last_estimated_qid = None
        self._last_estimate = None
        self.estimators.clear()
        self.on_result(updated)
        self.refresh_history()
        return binding

    # -----------------------------------------------------------------------
    # Worker Signal Handlers & State Updates
    # -----------------------------------------------------------------------
    def on_checking(self, event):
        if self._closing:
            return
        if event.get("manual") and event.get("request_id", event["query_id"]) == self.pending_query_id:
            self.refresh_status.setText(f"正在查询 · {event['query_id']}")
            self.status_left.setText("● 正在手动查询 · 充电状态 unknown")
        elif not self.manual_pending:
            self.refresh_status.setText(f"正在自动查询 · {event['query_id']}")
            self.status_left.setText("● 正在自动查询 · 充电状态 unknown")
        self.status_right.setText(STATUS_RIGHT_TEXT)
        self._sync_statusbar_tooltip()

    def on_result(self, event):
        if self._closing:
            return
        self.last_reading = event
        checked_at = event["completed_at"].strftime("%H:%M:%S")
        matching_manual = event.get("manual") and event.get("request_id", event["query_id"]) == self.pending_query_id
        if matching_manual:
            self.manual_pending = False
            self.pending_query_id = None
            self.refresh_button.setEnabled(True)
            self.refresh_button.setText("刷新")
        reasons = {
            "timeout": "接收器可见，本次未收到回复；原因待验证",
            "receiver_absent": "本次枚举未发现接收器",
            "enumeration_error": "设备枚举失败，接收器状态未知",
            "interface_not_found": "接收器可见，未找到已验证的电量接口",
            "ambiguous_interface": "发现多个匹配接口，未发送查询",
            "device_changed": "查询期间设备路径变化，已丢弃旧结果",
            "device_busy": "其他协作进程正在查询",
            "device_asleep": "接收器在线，鼠标当前处于休眠或未应答",
            "protocol_pending_capture": "已识别未核实协议槽位，待实机抓包后启用",
            "open_error": "接口打开失败",
            "write_error": "发送失败",
            "short_write": "发送字节数异常",
            "read_error": "读取失败",
            "cancelled": "查询已取消",
        }
        outcome = event["outcome"]
        if event["connected"]:
            self._replug_retries_remaining = 0
            self._replug_retry_idx = 0
            self._replug_retry_timer.stop()
        elif outcome == "receiver_absent":
            if (
                self._replug_retries_remaining > 0
                and not self._closing
                and getattr(self, "worker", None)
            ):
                delay_ms = self.REPLUG_RETRY_DELAYS_MS[
                    min(self._replug_retry_idx, len(self.REPLUG_RETRY_DELAYS_MS) - 1)
                ]
                self._replug_retry_idx += 1
                self._replug_retries_remaining -= 1
                self._replug_retry_timer.start(delay_ms)
        elif outcome not in ("cancelled", "device_changed"):
            self._replug_retries_remaining = 0
            self._replug_retry_idx = 0
            self._replug_retry_timer.stop()
        mode = "手动" if event.get("manual") else "自动"
        if matching_manual or not self.manual_pending:
            detail = "取得有效回复" if event["connected"] else reasons.get(outcome, f"响应不可用：{outcome}")
            self.refresh_status.setText(
                f"{checked_at} {mode}查询完成 · {detail} · {event['elapsed_ms']:.0f} ms · {event['query_id']}"
            )

        warnings = []
        if event.get("storage_error"):
            warnings.append("最近采样存储失败：" + event["storage_error"])
        if event.get("diagnostic_error"):
            warnings.append("诊断记录未能保存：" + event["diagnostic_error"])
        self.storage_status.setText(
            "；".join(warnings)
            if warnings
            else (
                ("本次有效读数已保存" if event.get("storage_attempted") else "本次未写电量样本")
                + "；充电状态 unknown；回复新鲜度未验证"
            )
        )

        # Bottom slim statusbar text
        save_tag = "存储失败" if warnings else ("已保存" if event.get("storage_attempted") else "未写入样本")
        query_tag = "自动轮询中" if event["connected"] else reasons.get(outcome, f"休眠/待命中 ({outcome})")
        self.status_left.setText(f"● 充电状态 unknown · {query_tag} · {save_tag}")
        self.status_right.setText(STATUS_RIGHT_TEXT)
        self._sync_statusbar_tooltip()

        last = event.get("last_success")
        is_disconnected_cached = (
            (not event["connected"])
            and (last is not None)
            and (not event.get("vid_pid"))
        )

        # Resolve per-device LUT binding state (keeps synthetic test events 100% compatible)
        if is_disconnected_cached:
            dev_uid = last.get("device_uid") or last.get("device_id") or self.active_device_uid or "__default__"
        else:
            dev_uid = event.get("device_uid") or event.get("device_id") or "__default__"
        self.active_device_uid = dev_uid
        binding_state = event.get("binding_state", "unknown")
        has_v07_fields = (
            ("lut_profile_id" in event)
            or (binding_state in ("bound", "single_candidate", "ambiguous_unbound", "pending_protocol", "cid_mid_resolved"))
            or ("vid_pid" in event)
            or ("unregistered_ecosystem_vid_pids" in event)
        )
        if is_disconnected_cached:
            lut_pid = last.get("lut_profile_id")
            if not lut_pid and self._last_known_identity is not None:
                lut_pid = self._last_known_identity.get("lut_id")
            if not lut_pid and last.get("model_id"):
                cached_model = self.registry.get_model(last["model_id"])
                if cached_model is not None and (
                    last.get("binding_state") in ("bound", "single_candidate", "cid_mid_resolved")
                    or cached_model.lut_profile_id == ATK_F1_V2_ULTIMATE_PROFILE.profile_id
                ):
                    lut_pid = cached_model.lut_profile_id
            if not lut_pid and not has_v07_fields:
                lut_pid = ATK_F1_V2_ULTIMATE_PROFILE.profile_id
        else:
            lut_pid = event.get("lut_profile_id") if has_v07_fields else ATK_F1_V2_ULTIMATE_PROFILE.profile_id
        self.active_lut_profile_id = lut_pid
        self._update_collision_banner(event)
        self._refresh_device_selector(event)

        unreg = list(event.get("unregistered_ecosystem_vid_pids") or [])
        self._unregistered_ecosystem_vid_pids = unreg
        is_unmatched_no_history = (
            (not event["connected"])
            and (last is None)
            and (event.get("receiver_visibility") == "absent")
            and (event.get("vid_pid") is None)
        )

        if is_unmatched_no_history:
            if unreg:
                brand = "ATK 生态"
                display_model = f"未收录 {unreg[0]} · 请导诊断包"
                self.profile_device_label.setText(
                    f"检测到未收录 ATK/VXE/VGN 设备：{', '.join(unreg)} · 请点击下方「导出诊断包」反馈适配"
                )
            else:
                brand = "ATK-Pulse"
                display_model = "未检测到已适配设备"
                self.profile_device_label.setText("当前档案：未检测到已适配接收器或鼠标")
            self._current_display_model = display_model
            self.card_brand_label.setText(brand)
            self.card_model_label.setText(display_model)
            self.state_dot.setStyleSheet(f"color: {qss_rgba(PALETTE['coral_low'])}; font-size: 11px;")
            self.state_chip.setText("未发现已登记接收器")
            self.state_chip.setToolTip(self.refresh_status.text())
        else:
            if event.get("vid_pid"):
                brand = event.get("brand") or "ATK"
                display_model = event.get("model_display_name") or event.get("name") or "ATK Wireless Mouse"
                ident = dict(
                    brand=brand,
                    model_name=display_model,
                    vid_pid=event.get("vid_pid"),
                    protocol_family=event.get("protocol_family") or "unknown",
                    tier=event.get("verification_tier") or "catalog_only",
                    lut_id=lut_pid,
                )
                if event["connected"] or last is not None:
                    self._last_known_identity = ident
            elif (not event["connected"]) and self._last_known_identity is not None:
                ident = dict(self._last_known_identity)
                brand = ident["brand"]
                display_model = ident["model_name"]
            elif is_disconnected_cached and (
                last.get("vid_pid") or last.get("model_display_name") or last.get("model_id")
            ):
                cached_model = self.registry.get_model(last["model_id"]) if last.get("model_id") else None
                brand = (cached_model.brand if cached_model else None) or "ATK"
                display_model = (
                    last.get("model_display_name")
                    or (cached_model.display_name if cached_model else None)
                    or "ATK Wireless Mouse"
                )
                ident = dict(
                    brand=brand,
                    model_name=display_model,
                    vid_pid=last.get("vid_pid") or (cached_model.vid_pid if cached_model else "unknown"),
                    protocol_family=last.get("protocol_family") or (cached_model.protocol_family if cached_model else "unknown"),
                    tier=(cached_model.verification_tier if cached_model else "catalog_only"),
                    lut_id=lut_pid,
                )
            else:
                brand = event.get("brand") or "ATK"
                display_model = event.get("model_display_name") or event.get("name") or MODEL
                ident = dict(
                    brand=brand,
                    model_name=display_model,
                    vid_pid=event.get("vid_pid") or "373B:11D9",
                    protocol_family=event.get("protocol_family") or "nordic52",
                    tier=event.get("verification_tier") or "verified",
                    lut_id=lut_pid,
                )
            self._current_display_model = display_model
            self._apply_card_identity(**ident)

            # Compat device status indicators
            if event["connected"]:
                self.state_dot.setStyleSheet(f"color: {qss_rgba(PALETTE['iris_primary'])}; font-size: 11px;")
                self.state_chip.setText(display_model)
                self.state_chip.setToolTip("最近查询有效")
            else:
                vis_text = {
                    "present": f"{display_model} · 暂未响应",
                    "absent": "未发现已登记接收器",
                }.get(event["receiver_visibility"], "接收器状态未知")
                self.state_dot.setStyleSheet(f"color: {qss_rgba(PALETTE['coral_low'])}; font-size: 11px;")
                self.state_chip.setText(vis_text)
                self.state_chip.setToolTip(self.refresh_status.text())

        pct, mv = (last["percent"], last["voltage_mv"]) if last else (None, None)
        est_obj = self._get_or_create_estimator(dev_uid, lut_pid)
        if est_obj is None:
            est = None
            self._last_estimate = None
            if event["connected"]:
                self._last_estimated_qid = event.get("query_id")
        elif event["connected"]:
            est = est_obj.step(mv, link_byte=event.get("link_byte"))
            self._last_estimate = est
            self._last_estimated_qid = event.get("query_id")
        elif self._last_estimate is None and last is not None:
            est = est_obj.step(mv, link_byte=last.get("link_byte"))
            self._last_estimate = est
            self._last_estimated_qid = last.get("query_id")
        else:
            est = self._last_estimate

        display_pct = est.percent_est if (est is not None and est.percent_est is not None) else pct
        if is_unmatched_no_history:
            self.main_pct_title.setText("未收录 ATK 生态设备" if unreg else "未检测到已适配设备")
            self.badge_est_type.setText("● 固件原报")
            self.badge_est_type.apply_kind("muted")
            self.badge_low_extrap.hide()
            self.main_pct_sub.setText(
                f"检测到 {', '.join(unreg)}，请到设置页导出离线诊断包"
                if unreg
                else "请插入已适配的 ATK / VXE / VGN 接收器或有线鼠标"
            )
            self.main_pct_caption.setText("待适配反馈" if unreg else "等待设备连接")
        elif est is not None and est.percent_est is not None:
            self.main_pct_title.setText("电压估算电量 · 低段外推" if est.extrapolated_low else "电压估算电量")
            # Metric-only pill: MUST NOT turn into a low-battery warning pill
            self.badge_est_type.setText("● 电压估算")
            self.badge_est_type.apply_kind("iris")
            self.badge_low_extrap.hide()
            sub_parts = [f"基于 ADC 电压 1% 估算 · 固件原报 {pct}%"]
            if est.sag_pending:
                sub_parts.append("压降待确认")
            self.main_pct_sub.setText(" · ".join(sub_parts))
            self.main_pct_caption.setText("ADC 1% 标定曲线" + (" · 压降门控保持" if est.sag_pending else ""))
        elif pct is not None and binding_state == "ambiguous_unbound":
            self.main_pct_title.setText("固件原报电量（待确认型号）")
            self.badge_est_type.setText("● 固件原报")
            self.badge_est_type.apply_kind("coral")
            self.badge_low_extrap.hide()
            self.main_pct_sub.setText("共用接收器待确认型号，暂显示固件原报百分比")
            self.main_pct_caption.setText("共用接收器待确认型号 · 暂显示固件原报")
        elif pct is not None and not lut_pid:
            self.main_pct_title.setText("固件原报电量（未标定 LUT）")
            self.badge_est_type.setText("● 固件原报")
            self.badge_est_type.apply_kind("muted")
            self.badge_low_extrap.hide()
            self.main_pct_sub.setText("当前机型未绑定标定曲线，展示固件原报百分比")
            self.main_pct_caption.setText("未标定型号 · 后台继续记录原始电压" if mv is not None else "仅固件百分比 · 无候选电压")
        elif pct is not None:
            self.main_pct_title.setText("设备报告电量（无电压回退）")
            self.badge_est_type.setText("● 固件原报")
            self.badge_est_type.apply_kind("coral")
            self.badge_low_extrap.hide()
            self.main_pct_sub.setText("缺少候选电压，回退显示固件原报百分比")
            self.main_pct_caption.setText("缺少候选电压 · 回退固件原报")
        else:
            self.main_pct_title.setText("固件原报电量" if not lut_pid else "电压估算电量")
            self.badge_est_type.setText("● 固件原报" if not lut_pid else "● 电压估算")
            self.badge_est_type.apply_kind("muted" if not lut_pid else "iris")
            self.badge_low_extrap.hide()
            self.main_pct_sub.setText("等待设备首次读取" if not lut_pid else "基于 ADC 电压 1% 估算 · 等待读数")
            self.main_pct_caption.setText("等待设备读数" if not lut_pid else "ADC 1% 标定曲线 · 等待读数")

        self.main_pct.setText(f"{display_pct}%" if display_pct is not None else "--")
        self.main_voltage.setText(f"{mv / 1000:.3f} V" if mv is not None else "--")
        self.main_fw_pct.setText(f"{pct}%" if pct is not None else "--")
        self.main_fw_sub.setText("5% 阶梯对照" if pct is not None else "等待设备原报")

        if mv is not None:
            if est and est.v_est_mv is not None:
                if round(est.v_est_mv) == mv:
                    self.main_voltage_sub.setText(f"滤波 {est.v_est_mv:.0f} mV")
                else:
                    self.main_voltage_sub.setText(f"{mv} mV · 滤波 {est.v_est_mv:.0f}")
            else:
                self.main_voltage_sub.setText(f"{mv} mV")
        elif pct is not None:
            self.main_voltage_sub.setText(f"无候选电压 · 原报 {pct}%")
        else:
            self.main_voltage_sub.setText("等待有效电压")

        # Update left-card V0.8 ring + 2x2 natural-language slots
        if is_unmatched_no_history and unreg:
            ring_sub = "未收录设备"
        elif est and est.extrapolated_low:
            ring_sub = "低段外推"
        elif est is None and pct is not None:
            ring_sub = "固件原报" if mv is not None else ""
        else:
            ring_sub = ""
        self.ring_widget.set_state(
            display_pct,
            mv,
            stale=not event["connected"],
            disconnected=(display_pct is None),
            sub_text=ring_sub,
        )

        link_mode = event.get("link_mode") or "wireless_dongle"
        if not event["connected"] and event.get("receiver_visibility") == "absent":
            self.slot_link_val.setText("未连接")
        elif link_mode == "wired_usb":
            self.slot_link_val.setText("有线 USB")
        elif link_mode == "wireless_dongle":
            self.slot_link_val.setText("2.4G 无线")
        else:
            self.slot_link_val.setText("待确认")

        if is_unmatched_no_history:
            self.slot_state_pill.setText("未适配" if unreg else "未连接")
            self.slot_state_pill.apply_kind("coral" if unreg else "muted")
        elif not event["connected"]:
            self.slot_state_pill.setText("未连接" if event.get("receiver_visibility") == "absent" else "休眠")
            self.slot_state_pill.apply_kind("muted")
        elif display_pct is not None and display_pct <= self.threshold.value():
            self.slot_state_pill.setText("低电")
            self.slot_state_pill.apply_kind("coral")
        else:
            self.slot_state_pill.setText("实时")
            self.slot_state_pill.apply_kind("iris")

        self.battery_bar.set_state(display_pct, stale=not event["connected"])
        self.update_clock()
        if self.badge is not None:
            self.badge.update_state(
                display_pct,
                stale=not event["connected"],
                tooltip_text=self.tray.toolTip(),
                low_threshold=self.threshold.value(),
            )
        # Notifications are triggered only by a successful query, never history.
        if event["connected"] and display_pct is not None:
            if self.alert_enabled.isChecked() and display_pct <= self.threshold.value():
                if not self.warned and self.tray.isVisible():
                    if est is not None and est.percent_est is not None:
                        warn_body = f"电压估算电量 {display_pct}%（固件原报 {pct}%），请留意充电。"
                    else:
                        warn_body = f"当前电量 {display_pct}%（固件原报），请留意充电。"
                    self.tray.showMessage(
                        "ATK 鼠标低电量",
                        warn_body,
                        QSystemTrayIcon.MessageIcon.Warning,
                        4000,
                    )
                self.warned = True
            elif display_pct > self.threshold.value():
                self.warned = False
            if self._hidden_to_tray:
                self._history_dirty = True
            else:
                self.refresh_history()
        else:
            if not self._hidden_to_tray:
                self._update_home_chart_note()

    def refresh(self):
        if self._closing:
            return
        if self.worker and not self.manual_pending:
            self.pending_query_id = self.worker.refresh()
            self.manual_pending = self.pending_query_id is not None
            self.refresh_button.setEnabled(not self.manual_pending)
            self.refresh_button.setText("刷新中…" if self.manual_pending else "刷新")
            self.refresh_status.setText("刷新请求已排队；重复点击合并，等待当前查询完成")
            self.status_left.setText("● 刷新请求已排队 · 重复点击自动合并")
            self._sync_statusbar_tooltip()
        elif not self.worker:
            self.refresh_status.setText("界面预览模式，没有读取设备")
            self.status_left.setText("● 界面预览模式 · 未连接采集线程")

    def refresh_history(self):
        dev_uid = self.active_device_uid
        if dev_uid and dev_uid not in ("__default__", "fake"):
            binding = self.binding_store.get(dev_uid)
            include_legacy = (
                bool(binding.include_legacy_v1_v01_v02)
                if binding is not None
                else (self.active_lut_profile_id == ATK_F1_V2_ULTIMATE_PROFILE.profile_id)
            )
            all_rows = read_samples(None, device_uid=dev_uid, include_legacy_f1=include_legacy)
            lut_pid = self.active_lut_profile_id
        else:
            all_rows = read_samples(None)
            lut_pid = self.active_lut_profile_id

        enriched, replay_est = estimate_series(all_rows, lut_profile_id=lut_pid)
        if enriched and self._last_estimated_qid is None and replay_est is not None:
            last_row = enriched[-1]
            self.estimator = replay_est
            self._last_estimated_qid = last_row.get("query_id")
            self._last_estimate = SocEstimate(
                percent_est=last_row["percent_est"],
                v_est_mv=last_row["v_est_mv"],
                sag_pending=last_row["sag_pending"],
                extrapolated_low=last_row["extrapolated_low"],
                fallback_used=last_row["fallback_used"],
            )

        cycle = compute_cycle_metrics(enriched)
        self._last_cycle_metrics = cycle
        if cycle is not None:
            self.slot_cycle_val.setText(format_cycle_elapsed(cycle.elapsed_seconds))
            self.slot_drain_val.setText(format_daily_drain(cycle.avg_daily_drain))
        else:
            self.slot_cycle_val.setText("样本积累中")
            self.slot_drain_val.setText("样本积累中")

        now = datetime.now().astimezone()

        def _filter_hours(rows_list, hours):
            if not hours:
                return rows_list
            cutoff = now - timedelta(hours=hours)
            return [r for r in rows_list if r["ts"] >= cutoff]

        home_period_data = self.home_period.currentData()
        if home_period_data == "cycle":
            home_rows = cycle.cycle_rows if cycle is not None else enriched
            effective_home_hours = (
                max(24, int(cycle.elapsed_seconds // 3600) + 1)
                if cycle is not None
                else 24
            )
        else:
            home_rows = _filter_hours(enriched, home_period_data)
            effective_home_hours = home_period_data

        hist_period_hours = self.history_period.currentData()
        hist_rows = _filter_hours(enriched, hist_period_hours)
        show_fw_home = self.home_fw_overlay.isChecked() if hasattr(self, "home_fw_overlay") else False
        show_fw_hist = self.show_fw_overlay.isChecked() if hasattr(self, "show_fw_overlay") else True
        auto_home = self.home_auto_scale.isChecked() if hasattr(self, "home_auto_scale") else True
        auto_hist = self.history_auto_scale.isChecked() if hasattr(self, "history_auto_scale") else False

        home_gap_s = adaptive_sleep_gap_s(home_period_data)
        hist_gap_s = adaptive_sleep_gap_s(hist_period_hours)
        home_gaps = compute_sleep_gaps(home_rows, gap_threshold_s=home_gap_s)
        hist_gaps = compute_sleep_gaps(hist_rows, gap_threshold_s=hist_gap_s)
        home_mode_str = "percent" if self.home_mode.currentIndex() == 0 else "mv"
        hist_mode_str = "percent" if self.history_mode.currentIndex() == 0 else "mv"
        has_lut = lut_pid is not None
        effective_show_fw_home = show_fw_home and has_lut
        effective_show_fw_hist = show_fw_hist and has_lut

        self.home_chart.set_data(
            home_rows,
            home_mode_str,
            show_firmware=effective_show_fw_home,
            auto_scale=auto_home,
            period_hours=effective_home_hours,
            sleep_gaps=home_gaps,
            gap_threshold_s=home_gap_s,
            has_lut=has_lut,
        )
        self.history_chart.set_data(
            hist_rows,
            hist_mode_str,
            show_firmware=effective_show_fw_hist,
            auto_scale=auto_hist,
            period_hours=hist_period_hours,
            sleep_gaps=hist_gaps,
            gap_threshold_s=hist_gap_s,
            has_lut=has_lut,
        )

        self.home_legend.setText(
            "— 电量变化   ■ 休眠空窗" if home_mode_str == "percent" else "— 电压变化   ■ 休眠空窗"
        )
        if hist_mode_str == "percent" and not has_lut:
            self.history_legend.setText("— 固件电量   ■ 休眠空窗")
        elif hist_mode_str == "percent" and effective_show_fw_hist:
            self.history_legend.setText("— 电压估算   - - 固件原报   ■ 休眠空窗")
        else:
            self.history_legend.setText("— 电量变化   ■ 休眠空窗" if hist_mode_str == "percent" else "— 电压变化   ■ 休眠空窗")

        self._last_home_rows = home_rows
        self._last_home_period = home_period_data
        self._update_home_chart_note()

        period_label = self.history_period.currentText()
        if hist_rows:
            h_vals = [int(r.get("display_percent", r["percent"])) for r in hist_rows]
            self.history_desc.setText(
                f"{period_label} 共 {len(hist_rows)} 条记录 · 峰谷 {max(h_vals)}%/{min(h_vals)}%"
            )
        else:
            self.history_desc.setText(f"{period_label} 暂无有效记录")
        if READ_WARNINGS:
            self.history_desc.setText(self.history_desc.text() + " · 读取警告：" + "；".join(READ_WARNINGS))

        rows = list(reversed(enriched[-50:]))
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            src = f'{r["source"]} · 恢复首帧' if r.get("recovered") else r["source"]
            if r.get("percent_est") is not None:
                est_txt = f'{r["percent_est"]}%' + (" (外推)" if r.get("extrapolated_low") else "")
            else:
                est_txt = "— (回退)" if has_lut else "—"
            vals = [
                r["ts"].strftime("%m/%d %H:%M:%S"),
                est_txt,
                f'{r["percent"]}%',
                f'{r["mv"] / 1000:.3f} V' if r["mv"] is not None else "—",
                src,
            ]
            for j, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if j > 0:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(i, j, it)

    def _update_home_chart_note(self):
        if not hasattr(self, "home_chart_note"):
            return
        home_rows = getattr(self, "_last_home_rows", self.home_chart.rows) or []
        period_data = getattr(self, "_last_home_period", self.home_period.currentData())
        win_label = {
            "cycle": "本周期",
            1: "近 1 小时",
            168: "近 7 天",
        }.get(period_data, "近 24 小时")
        sleeping = bool(self.last_reading and not self.last_reading.get("connected"))
        prefix = "休眠前 " if sleeping else ""
        if len(home_rows) >= 2:
            vals = [int(r.get("display_percent", r["percent"])) for r in home_rows]
            if vals[0] >= vals[-1]:
                self.home_chart_note.setText(f"{prefix}{win_label}耗电 {vals[0] - vals[-1]}%")
            else:
                self.home_chart_note.setText(f"{prefix}{win_label}区间 {min(vals)}% ~ {max(vals)}%")
        else:
            self.home_chart_note.setText("持续记录中")

    def update_clock(self):
        now = time.time()
        if now - self._last_tick > 15 and self.worker and not self._closing:
            log.info("UI timer gap detected; invalidate old query and poll once")
            self.worker.resume()
        self._last_tick = now
        event = self.last_reading
        last = event.get("last_success") if event else None
        if not last:
            self.main_last.setText("暂无读数")
            if hasattr(self, "slot_updated_val"):
                self.slot_updated_val.setText("暂无更新")
            if self._unregistered_ecosystem_vid_pids:
                tip = (
                    f"{APP_NAME} · 检测到未收录设备 "
                    f"{', '.join(self._unregistered_ecosystem_vid_pids)}（可导出离线诊断包反馈）"
                )
            else:
                tip = f"{APP_NAME} · 未检测到已适配设备"
            self.tray.setToolTip(tip)
            if getattr(self, "badge", None) is not None:
                self.badge.set_tooltip(tip)
            return
        at = last["at"]
        age = max(0, int((datetime.now().astimezone() - at).total_seconds()))
        self.main_last.setText(at.strftime("%H:%M:%S"))
        if hasattr(self, "slot_updated_val"):
            self.slot_updated_val.setText(format_relative_age(at))
        elapsed_ms = event.get("elapsed_ms")
        ms_part = f" · {elapsed_ms:.0f} ms" if elapsed_ms is not None else ""
        if event["connected"]:
            state = "最近取得的回复"
            self.main_hint.setText(f"距今 {age} 秒{ms_part}")
        else:
            state = "历史读数，本次查询未成功"
            self.main_hint.setText(f"历史读数 · 距今 {age} 秒")

        fw_pct = last.get("percent")
        mv = last.get("voltage_mv")
        est = self._last_estimate
        model_title = self._current_display_model or MODEL
        if est is not None and est.percent_est is not None:
            head = f"电压估算 {est.percent_est}%" + ("（低段外推）" if est.extrapolated_low else "")
            mv_str = f"{mv / 1000:.3f} V ({mv} mV)" if mv is not None else "—"
            detail_line = f"原始电压 {mv_str} · 固件原报 {fw_pct}%"
        elif mv is not None:
            head = f"固件原报 {fw_pct}%"
            detail_line = f"原始电压 {mv / 1000:.3f} V ({mv} mV) · 未标定曲线"
        else:
            head = f"固件电量 {fw_pct}%"
            detail_line = "当前协议仅上报固件电量（无电压字段）"
        tip = (
            f"{model_title} · {head}\n"
            f"{detail_line}\n"
            f"接收时间 {at.strftime('%m/%d %H:%M:%S')} · 距今 {age} 秒 · {state}；充电状态 unknown"
        )
        self.tray.setToolTip(tip)
        if getattr(self, "badge", None) is not None:
            self.badge.set_tooltip(tip)

    def _arm_replug_retries(self):
        self._replug_retry_idx = 0
        self._replug_retries_remaining = len(self.REPLUG_RETRY_DELAYS_MS)
        self._replug_retry_timer.stop()

    def _on_device_change_trailing_timeout(self, now: float | None = None):
        if self._closing or not getattr(self, "worker", None):
            return
        now = time.monotonic() if now is None else now
        self._last_device_change_refresh = now
        self._last_native_refresh = now
        self._arm_replug_retries()
        log.info("Trailing Win32 device change debounce fired; resuming worker")
        self.worker.resume()

    def _on_replug_retry_timeout(self):
        if self._closing or not getattr(self, "worker", None):
            return
        log.info("Bounded replug retry firing; resuming worker")
        self.worker.resume()

    def handle_native_event(self, msg_id: int, wparam: int, now: float | None = None) -> bool:
        resume = msg_id == 0x0218 and int(wparam) in (6, 7, 18)
        device_change = msg_id == 0x0219 and int(wparam) in (0x8000, 0x8004, 7)
        if not (resume or device_change) or not getattr(self, "worker", None) or self._closing:
            return False
        now = time.monotonic() if now is None else now
        if device_change:
            if now - self._last_device_change_refresh <= self.DEVICE_CHANGE_DEBOUNCE_S:
                self._device_change_trailing_timer.start(self.DEVICE_CHANGE_TRAILING_MS)
                return False
            self._device_change_trailing_timer.stop()
            self._last_device_change_refresh = now
            self._arm_replug_retries()
        elif resume:
            if now - self._last_power_refresh <= self.POWER_DEBOUNCE_S:
                return False
            self._last_power_refresh = now
        self._last_native_refresh = now
        log.info("Native power/device event: %s/%s", msg_id, wparam)
        self.worker.resume()
        return True

    def nativeEvent(self, event_type, message):
        try:
            from ctypes.wintypes import MSG
            msg = MSG.from_address(int(message))
            self.handle_native_event(msg.message, int(msg.wParam))
        except (ValueError, TypeError, OSError):
            log.exception("Native event decoding failed")
        return super().nativeEvent(event_type, message)

    @staticmethod
    def _autostart_target_marker() -> str:
        if getattr(sys, "frozen", False):
            return str(Path(sys.executable).resolve())
        return str(ROOT / "main.py")

    @staticmethod
    def _autostart_command() -> str:
        if getattr(sys, "frozen", False):
            return f'"{Path(sys.executable).resolve()}" --minimized'
        executable = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        return f'"{executable}" "{ROOT / "main.py"}" --minimized'

    def _autostart_enabled(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                value, _ = winreg.QueryValueEx(k, STARTUP_KEY)
                return self._autostart_target_marker().lower() in value.lower()
        except OSError:
            return False

    def change_autostart(self, enabled):
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                if enabled:
                    winreg.SetValueEx(key, STARTUP_KEY, 0, winreg.REG_SZ, self._autostart_command())
                else:
                    try:
                        winreg.DeleteValue(key, STARTUP_KEY)
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            self.autostart.blockSignals(True)
            self.autostart.setChecked(not enabled)
            self.autostart.blockSignals(False)
            QMessageBox.warning(self, "无法修改开机启动", str(exc))

    def export_csv(self):
        import csv
        rows = read_samples(None, limit=None)
        if not rows:
            QMessageBox.information(self, "没有数据", "当前没有有效电池记录可导出。")
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "导出全部有效电池历史",
            str(Path.home() / "Desktop" / "ATK_Battery_History.csv"),
            "CSV Files (*.csv)",
        )
        if not target:
            return
        if Path(target).resolve() in {p.resolve() for p in (HISTORY_PATH, V01_HISTORY, SAMPLES_PATH, SAMPLES_V2_PATH)}:
            QMessageBox.warning(self, "无法导出到原始历史", "请选择新的导出文件，不能覆盖已有采样文件。")
            return
        try:
            with open(target, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "原时间戳", "时区依据", "型号", "设备报告百分比", "候选电压原始值",
                    "电压验证状态", "充电状态", "来源", "来源文件", "来源行", "查询编号",
                ])
                for r in rows:
                    writer.writerow([
                        r["timestamp_raw"], r["timezone_status"], r["model"], r["percent"],
                        r["voltage_raw"], r["voltage_validation"], "unknown", r["source"],
                        r["source_file"], r["source_line"], r["query_id"],
                    ])
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def export_cycle_csv_action(self):
        dev_uid = self.active_device_uid
        rows = read_samples(None, limit=None, device_uid=dev_uid if dev_uid not in ("__default__", "fake") else None)
        enriched, _ = estimate_series(rows, lut_profile_id=self.active_lut_profile_id)
        cycle = compute_cycle_metrics(enriched)
        cycle_rows = cycle.cycle_rows if cycle is not None else enriched
        if not cycle_rows:
            QMessageBox.information(self, "没有数据", "当前设备暂无可导出的放电周期数据。")
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "导出脱敏本周期 CSV",
            str(Path.home() / "Desktop" / "ATK_Pulse_Cycle_Redacted.csv"),
            "CSV Files (*.csv)",
        )
        if not target:
            return
        try:
            export_cycle_csv(cycle_rows, target)
            QMessageBox.information(self, "导出完成", f"已生成 100% 离线脱敏 CSV：\n{target}")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def export_diagnostic_zip_action(self):
        dev_uid = self.active_device_uid
        rows = read_samples(None, limit=2000, device_uid=dev_uid if dev_uid not in ("__default__", "fake") else None)
        enriched, _ = estimate_series(rows, lut_profile_id=self.active_lut_profile_id)
        target, _ = QFileDialog.getSaveFileName(
            self,
            "导出脱敏诊断包 (ZIP)",
            str(Path.home() / "Desktop" / "ATK_Pulse_Diagnostic_Redacted.zip"),
            "ZIP Archives (*.zip)",
        )
        if not target:
            return
        ev = dict(self.last_reading or {})
        has_matched_dev = bool(ev.get("vid_pid"))
        dev_info = {
            "device_uid": self.active_device_uid if self.active_device_uid not in (None, "__default__") else (ev.get("device_uid") or None),
            "vid_pid": ev.get("vid_pid") or None,
            "model_id": ev.get("model_id") or None,
            "model_display_name": ev.get("model_display_name") or self._current_display_model,
            "protocol_family": ev.get("protocol_family") or None,
            "verification_tier": ev.get("verification_tier") or None,
            "lut_profile_id": (self.active_lut_profile_id or "") if has_matched_dev else None,
            "binding_state": ev.get("binding_state") or "unknown",
            "outcome": ev.get("outcome") or "none",
            "receiver_visibility": ev.get("receiver_visibility") or "unknown",
            "unregistered_ecosystem_vid_pids": list(ev.get("unregistered_ecosystem_vid_pids") or self._unregistered_ecosystem_vid_pids),
        }
        hid_descs = collect_sanitized_ecosystem_hid_descriptors()
        try:
            export_redacted_diagnostic_zip(
                target,
                rows=enriched,
                device_info=dev_info,
                hid_descriptors=hid_descs,
                extra_notes=f"refresh_status={self.refresh_status.text()}\nstorage_status={self.storage_status.text()}",
            )
            QMessageBox.information(self, "导出完成", f"已生成 100% 离线脱敏诊断包（无用户名/本机路径/序列号）：\n{target}")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def open_history_folder(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DIR)))

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_dashboard()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_dashboard()

    def toggle_dashboard(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_dashboard()

    def toggle_taskbar_badge(self, checked):
        enabled = bool(checked)
        self.settings.setValue("taskbar_badge", enabled)
        if hasattr(self, "badge_action") and self.badge_action.isChecked() != enabled:
            self.badge_action.blockSignals(True)
            self.badge_action.setChecked(enabled)
            self.badge_action.blockSignals(False)
        if hasattr(self, "taskbar_badge_switch") and self.taskbar_badge_switch.isChecked() != enabled:
            self.taskbar_badge_switch.blockSignals(True)
            self.taskbar_badge_switch.setChecked(enabled)
            self.taskbar_badge_switch.blockSignals(False)
        if hasattr(self, "taskbar_pill") and self.taskbar_pill.isChecked() != enabled:
            self.taskbar_pill.blockSignals(True)
            self.taskbar_pill.setChecked(enabled)
            self.taskbar_pill.blockSignals(False)
        if getattr(self, "badge", None) is not None:
            self.badge.set_enabled(enabled)
        self._sync_badge_position_ui()

    def _load_badge_position_settings(self) -> tuple[str, int | None]:
        mode = str(self.settings.value("badge_position_mode", "auto") or "auto").strip().lower()
        raw_x = self.settings.value("badge_custom_x", None)
        if mode == "custom" and raw_x is not None and str(raw_x).strip() != "":
            try:
                return ("custom", max(0, int(raw_x)))
            except (TypeError, ValueError):
                pass
        return ("auto", None)

    def _sync_badge_position_ui(self):
        enabled = bool(self.badge_action.isChecked()) if hasattr(self, "badge_action") else True
        badge = getattr(self, "badge", None)
        unlocked = bool(badge.drag_unlocked) if badge is not None else False
        is_custom = self._badge_pos_mode == "custom" and self._badge_custom_x is not None

        if hasattr(self, "badge_pos_mode_pill"):
            self.badge_pos_mode_pill.setText("自定义" if is_custom else "自动默认")
            self.badge_pos_mode_pill.apply_kind("iris" if is_custom else "muted")
        if hasattr(self, "badge_drag_action"):
            self.badge_drag_action.setText("锁定位置" if unlocked else "解锁拖动位置")
            self.badge_drag_action.setEnabled(enabled)
        if hasattr(self, "badge_reset_pos_action"):
            self.badge_reset_pos_action.setEnabled(enabled)
        if hasattr(self, "badge_drag_btn"):
            self.badge_drag_btn.setText("锁定位置" if unlocked else "解锁拖动")
            self.badge_drag_btn.setEnabled(enabled)
        if hasattr(self, "badge_reset_pos_btn"):
            self.badge_reset_pos_btn.setEnabled(enabled)
        if hasattr(self, "badge_pos_spin"):
            spin_val = (
                int(self._badge_custom_x)
                if is_custom
                else (int(badge.current_logical_x) if (badge is not None and badge.current_logical_x is not None) else 0)
            )
            self.badge_pos_spin.blockSignals(True)
            self.badge_pos_spin.setValue(max(0, min(3840, spin_val)))
            self.badge_pos_spin.blockSignals(False)
            self.badge_pos_spin.setEnabled(enabled)

    def toggle_badge_drag_lock(self):
        badge = getattr(self, "badge", None)
        if badge is None:
            return
        badge.set_drag_unlocked(not badge.drag_unlocked)
        self._sync_badge_position_ui()

    def _on_badge_position_changed(self, mode: str, custom_x: int | None):
        if mode == "custom" and custom_x is not None:
            self._badge_pos_mode = "custom"
            self._badge_custom_x = max(0, int(custom_x))
            self.settings.setValue("badge_position_mode", "custom")
            self.settings.setValue("badge_custom_x", int(self._badge_custom_x))
        else:
            self._badge_pos_mode = "auto"
            self._badge_custom_x = None
            self.settings.setValue("badge_position_mode", "auto")
            self.settings.remove("badge_custom_x")
        self._sync_badge_position_ui()

    def _on_badge_pos_spin_changed(self, value: int):
        val = max(0, int(value))
        self._badge_pos_mode = "custom"
        self._badge_custom_x = val
        self.settings.setValue("badge_position_mode", "custom")
        self.settings.setValue("badge_custom_x", val)
        if getattr(self, "badge", None) is not None:
            self.badge.set_position("custom", val)
        self._sync_badge_position_ui()

    def reset_badge_position(self):
        self._badge_pos_mode = "auto"
        self._badge_custom_x = None
        self.settings.setValue("badge_position_mode", "auto")
        self.settings.remove("badge_custom_x")
        if getattr(self, "badge", None) is not None:
            self.badge.set_drag_unlocked(False)
            self.badge.set_position("auto", None)
        self._sync_badge_position_ui()

    def _show_badge_menu(self):
        self.tray_menu.exec(QCursor.pos())
        if getattr(self, "badge", None) is not None:
            QTimer.singleShot(120, self.badge.ensure_on_top)

    def _stop_replug_timers(self):
        for attr in ("_device_change_trailing_timer", "_replug_retry_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                timer.stop()
        self._replug_retries_remaining = 0
        self._replug_retry_idx = 0

    def _register_hid_device_notification(self) -> bool:
        if sys.platform != "win32" or getattr(self, "_dev_notify_handle", None):
            return bool(getattr(self, "_dev_notify_handle", None))
        app = QApplication.instance()
        if app is None or app.platformName() != "windows":
            return False
        try:
            hwnd = int(self.winId())
            if not hwnd:
                return False
            flt = _DEV_BROADCAST_DEVICEINTERFACE_W()
            flt.dbcc_size = ctypes.sizeof(_DEV_BROADCAST_DEVICEINTERFACE_W)
            flt.dbcc_devicetype = _DBT_DEVTYP_DEVICEINTERFACE
            flt.dbcc_reserved = 0
            flt.dbcc_classguid = _GUID_DEVINTERFACE_HID
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.RegisterDeviceNotificationW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
            ]
            user32.RegisterDeviceNotificationW.restype = ctypes.c_void_p
            handle = user32.RegisterDeviceNotificationW(
                ctypes.c_void_p(hwnd),
                ctypes.byref(flt),
                _DEVICE_NOTIFY_WINDOW_HANDLE,
            )
            if handle:
                self._dev_notify_handle = int(handle)
                log.info("Registered Win32 HID device notification handle=%#x", self._dev_notify_handle)
                return True
            err = ctypes.get_last_error()
            log.warning("RegisterDeviceNotificationW returned NULL (last_error=%s)", err)
        except Exception:
            log.warning("Failed to register HID device notification", exc_info=True)
        return False

    def _unregister_hid_device_notification(self) -> None:
        handle = getattr(self, "_dev_notify_handle", None)
        if not handle or sys.platform != "win32":
            self._dev_notify_handle = None
            return
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.UnregisterDeviceNotification.argtypes = [ctypes.c_void_p]
            user32.UnregisterDeviceNotification.restype = ctypes.c_int
            user32.UnregisterDeviceNotification(ctypes.c_void_p(handle))
        except Exception:
            log.warning("UnregisterDeviceNotification failed", exc_info=True)
        finally:
            self._dev_notify_handle = None

    def _cleanup_badge(self):
        self._stop_replug_timers()
        self._unregister_hid_device_notification()
        if getattr(self, "badge", None) is not None:
            self.badge.destroy()

    def show_dashboard(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _trim_if_hidden(self):
        if self._hidden_to_tray and not self._closing:
            trim_process_working_set()

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self._closing:
            self._hidden_to_tray = True
            canvas = self.centralWidget()
            if isinstance(canvas, GreenhouseCanvas):
                canvas._cache_pm = None
                canvas._cache_key = None
            if hasattr(self, "tick") and self.tick.interval() != 5000:
                self.tick.setInterval(5000)
            QTimer.singleShot(150, self._trim_if_hidden)
            if getattr(self, "badge", None) is not None:
                self.badge.nudge_topmost()

    def changeEvent(self, event):
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.WindowStateChange
            and not self._closing
            and self.isMinimized()
            and getattr(self, "badge", None) is not None
        ):
            self.badge.nudge_topmost()

    def showEvent(self, event):
        super().showEvent(event)
        self._hidden_to_tray = False
        if hasattr(self, "tick") and self.tick.interval() != 1000:
            self.tick.setInterval(1000)
        if self._history_dirty:
            self._history_dirty = False
            self.refresh_history()
        self.update_clock()

    def closeEvent(self, event):
        if not self.allow_quit and (self.tray.isVisible() or getattr(self, "badge", None) is not None):
            self.hide()
            event.ignore()
        elif self.worker and self.worker.isRunning():
            self._closing = True
            self.refresh_button.setEnabled(False)
            self.refresh_status.setText("正在停止查询并关闭设备句柄…")
            self.status_left.setText("正在停止查询并关闭设备句柄…")
            self.worker.request_stop()
            event.ignore()
            QTimer.singleShot(1800, self._exit_status)
        else:
            self._cleanup_badge()
            event.accept()

    def _exit_status(self):
        if self._closing and self.worker and self.worker.isRunning():
            self.refresh_status.setText("仍在等待系统 I/O 返回；保留进程以安全释放句柄")
            self.status_left.setText("仍在等待系统 I/O 返回；保留进程以安全释放句柄")
            log.warning("Shutdown still waiting for native I/O; no forced thread termination")

    def _worker_finished(self):
        if self._closing:
            self._complete_exit()

    def _complete_exit(self):
        self._cleanup_badge()
        self.allow_quit = True
        self.close()
        self.tray.hide()
        QApplication.instance().quit()

    def exit_app(self):
        self.allow_quit = True
        self.close()
        if not self.worker or not self.worker.isRunning():
            self._complete_exit()


def singleton(name="Local\\ATK_Battery_Monitor_V02_single_instance"):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return None
    return kernel, handle


def main():
    # Only the personal V0.2+ instance is guarded; V0.1 remains untouched.
    lock = singleton()
    if lock is None:
        return 0
    kernel, handle = lock
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(APP_DIR / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        logging.basicConfig(
            handlers=[handler],
            level=logging.DEBUG if os.environ.get(DEBUG_ENV_VAR) == "1" else logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            force=True,
        )
        log.info("%s %s starting from %s", APP_NAME, VERSION_LABEL, ROOT)
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        app.setWindowIcon(load_app_icon())
        window = Dashboard(start_worker="--smoke" not in sys.argv)
        if "--minimized" not in sys.argv:
            window.show()
        else:
            window._hidden_to_tray = True
            if hasattr(window, "tick"):
                window.tick.setInterval(5000)
            QTimer.singleShot(300, window._trim_if_hidden)
        if "--smoke" in sys.argv:
            # Smoke mode renders the exact UI with unavailable values into isolated output path.
            out = Path(os.environ.get("ATK_SMOKE_OUT") or (APP_DIR / "atk_pulse_smoke.png"))
            out.parent.mkdir(parents=True, exist_ok=True)
            QTimer.singleShot(1000, lambda: (window.grab().save(str(out)), window.exit_app()))
        code = app.exec()
        return code
    except Exception:
        log.exception("Application startup failure")
        raise
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    sys.exit(main())
