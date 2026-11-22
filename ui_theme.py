# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ATK-Pulse Contributors
"""ATK-Pulse V0.8 — "Iris Greenhouse" (鸢尾温室) UI Theme & Custom Components."""
from __future__ import annotations

import math
from pathlib import Path
from PySide6.QtCore import Qt, QRectF, QPointF, QSize, Signal
from PySide6.QtGui import (
    QColor, QFont, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QFrame, QHBoxLayout, QLabel, QMenu,
    QPushButton, QSizePolicy, QSpinBox, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QWidget,
)

# ---------------------------------------------------------------------------
# 1. "Iris Greenhouse" (鸢尾温室) 10-slot Palette (#AARRGGBB)
# ---------------------------------------------------------------------------
PALETTE = {
    # 1. Main canvas diagonal gradient
    "bg_stop_0": "#FFF9F7FA",
    "bg_stop_1": "#FFF8F6F3",
    "bg_stop_2": "#FFF2F7F5",
    # 2. 4 Radial soft glow fields (softened ~43% opacity for misty #F9F7FA base)
    "glow_apricot": "#34EEC99E",   # 暖杏
    "glow_iris": "#34C8B9E8",      # 浅鸢尾
    "glow_mint": "#36A4D4C4",      # 薄荷绿
    "glow_rose": "#31F0B2A2",      # 柔粉
    # 3. Secondary container / sub-card
    "sub_card_bg": "#A3FCFCFE",
    "sub_card_border": "#EEFFFFFF",
    # 4. Main card
    "card_bg": "#DCFFFFFF",
    "card_border": "#F7FFFFFF",
    # 5. Input / ComboBox / Button trough
    "slot_bg": "#A8F1EFF6",
    "slot_hover": "#CCEAE5F2",
    "slot_focus_border": "#FF7C64B8",
    # 6. Navigation capsule & device chip
    "nav_active_bg": "#F5FFFFFF",
    "nav_indicator": "#FFD66853",
    "device_chip_bg": "#CCFDFCFF",
    # 7. Typography hierarchy
    "text_primary": "#FF1B1922",
    "text_secondary": "#FF5B5768",
    "text_muted": "#FF867E96",
    # 8. Accent & data semantic colors
    "btn_grad_0": "#FFDE725C",
    "btn_grad_1": "#FFC45545",
    "btn_shadow": "#42D0604E",
    "iris_primary": "#FF7C64B8",
    "coral_low": "#FFD66853",
    "stale_gray": "#FF867E96",
    "fw_dash": "#66867E96",
    # 9. ToggleSwitch
    "toggle_off": "#FFE3E0EB",
    "toggle_on": "#FFD66853",
    "toggle_disabled": "#FFEDEBF2",
    "toggle_knob": "#FFFFFFFF",
    # 10. Badges
    "badge_coral_bg": "#22D66853",
    "badge_coral_fg": "#FFC45545",
    "badge_iris_bg": "#206E58AD",
    "badge_iris_fg": "#FF6E58AD",
}


def qcolor(argb_hex: str) -> QColor:
    """Parse #AARRGGBB or #RRGGBB hex string into QColor."""
    return QColor(argb_hex)


def qss_rgba(argb_hex: str) -> str:
    """Convert #AARRGGBB to CSS rgba(r, g, b, a) for reliable Qt stylesheet parsing."""
    c = QColor(argb_hex)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha()})"


IRIS_GREENHOUSE_QSS = f"""
QWidget {{
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    color: {qss_rgba(PALETTE["text_primary"])};
}}
QWidget#root {{
    background: transparent;
}}
QFrame#topbar {{
    background: {qss_rgba("#B8FAF8FC")};
    border-bottom: 1px solid {qss_rgba(PALETTE["sub_card_border"])};
}}
QFrame#statusbar {{
    background: {qss_rgba("#B0F6F4F9")};
    border-top: 1px solid {qss_rgba(PALETTE["sub_card_border"])};
}}
QFrame#nav_trough {{
    background: rgba(241, 239, 246, 195);
    border: 1px solid rgba(212, 205, 226, 120);
    border-radius: 13px;
}}
QFrame#device_chip {{
    background: {qss_rgba(PALETTE["device_chip_bg"])};
    border: 1px solid {qss_rgba(PALETTE["card_border"])};
    border-radius: 11px;
}}
QFrame#panel {{
    background: {qss_rgba(PALETTE["card_bg"])};
    border: 1px solid {qss_rgba(PALETTE["card_border"])};
    border-radius: 16px;
}}
QFrame#slot_tile {{
    background: {qss_rgba("#8CF4F2F8")};
    border: 1px solid {qss_rgba("#E4FFFFFF")};
    border-radius: 12px;
}}
QFrame#metric, QFrame#subcard {{
    background: {qss_rgba(PALETTE["sub_card_bg"])};
    border: 1px solid {qss_rgba(PALETTE["sub_card_border"])};
    border-radius: 10px;
}}
QLabel#brand {{
    font-size: 16px;
    font-weight: 700;
    color: {qss_rgba(PALETTE["text_primary"])};
}}
QLabel#heading {{
    font-size: 15px;
    font-weight: 700;
    color: {qss_rgba(PALETTE["text_primary"])};
}}
QLabel#cardTitle {{
    font-size: 14px;
    font-weight: 700;
    color: {qss_rgba(PALETTE["text_primary"])};
}}
QLabel#metricTitle {{
    font-size: 12px;
    font-weight: 600;
    color: {qss_rgba(PALETTE["text_muted"])};
}}
QLabel#big {{
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 44px;
    font-weight: 700;
    color: {qss_rgba(PALETTE["text_primary"])};
}}
QLabel#large {{
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 20px;
    font-weight: 700;
    color: {qss_rgba(PALETTE["text_primary"])};
}}
QLabel#body {{
    font-size: 12px;
    color: {qss_rgba(PALETTE["text_secondary"])};
}}
QLabel#muted {{
    font-size: 11px;
    color: {qss_rgba(PALETTE["text_muted"])};
}}
QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {qss_rgba(PALETTE["btn_grad_0"])},
        stop:1 {qss_rgba(PALETTE["btn_grad_1"])});
    border: 1px solid {qss_rgba(PALETTE["btn_shadow"])};
    border-radius: 10px;
    color: #FFFFFF;
    font-size: 12px;
    font-weight: 700;
    padding: 7px 14px;
}}
QPushButton#primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #FFE47B66,
        stop:1 #FFCC5C4B);
}}
QPushButton#primary:disabled {{
    background: {qss_rgba(PALETTE["toggle_off"])};
    border: 1px solid transparent;
    color: {qss_rgba(PALETTE["text_muted"])};
}}
QPushButton#subtle {{
    background: {qss_rgba(PALETTE["slot_bg"])};
    border: 0px;
    border-radius: 9px;
    color: {qss_rgba(PALETTE["text_primary"])};
    font-size: 12px;
    font-weight: 600;
    padding: 6px 12px;
}}
QPushButton#subtle:hover {{
    background: {qss_rgba(PALETTE["slot_hover"])};
}}
QComboBox, QSpinBox {{
    background: {qss_rgba(PALETTE["slot_bg"])};
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 4px 10px;
    color: {qss_rgba(PALETTE["text_primary"])};
    font-size: 12px;
    font-weight: 600;
    min-height: 18px;
}}
QComboBox:hover, QSpinBox:hover {{
    background: {qss_rgba(PALETTE["slot_hover"])};
}}
QComboBox:focus, QSpinBox:focus, QComboBox:on {{
    background: {qss_rgba(PALETTE["nav_active_bg"])};
    border: 1px solid {qss_rgba(PALETTE["slot_focus_border"])};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background: transparent;
    border: 0px;
    width: 14px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background: {qss_rgba(PALETTE["slot_hover"])};
    border-radius: 4px;
}}
QComboBox::drop-down {{
    border: 0px;
    background: transparent;
    width: 18px;
}}
QComboBox QAbstractItemView {{
    background: #FCFBFE;
    border: 1px solid {qss_rgba(PALETTE["slot_focus_border"])};
    border-radius: 8px;
    padding: 4px;
    color: {qss_rgba(PALETTE["text_primary"])};
    selection-background-color: {qss_rgba(PALETTE["slot_hover"])};
    selection-color: {qss_rgba(PALETTE["text_primary"])};
    outline: 0px;
}}
QTableWidget {{
    border: 0px;
    background: transparent;
    alternate-background-color: {qss_rgba("#66EAE5F2")};
    color: {qss_rgba(PALETTE["text_primary"])};
    gridline-color: transparent;
    selection-background-color: {qss_rgba(PALETTE["slot_hover"])};
    selection-color: {qss_rgba(PALETTE["text_primary"])};
    outline: 0px;
    font-size: 12px;
}}
QTableWidget::item {{
    padding: 6px 8px;
    border: 0px;
    border-radius: 6px;
}}
QTableWidget::item:hover {{
    background: {qss_rgba(PALETTE["slot_hover"])};
}}
QTableWidget::item:selected {{
    background: {qss_rgba(PALETTE["slot_hover"])};
    color: {qss_rgba(PALETTE["text_primary"])};
}}
QHeaderView {{
    background: transparent;
    border: 0px;
}}
QHeaderView::section {{
    border: 0px;
    background: {qss_rgba(PALETTE["slot_bg"])};
    color: {qss_rgba(PALETTE["text_secondary"])};
    padding: 8px 10px;
    font-size: 12px;
    font-weight: 700;
}}
QHeaderView::section:first {{
    border-top-left-radius: 8px;
    border-bottom-left-radius: 8px;
}}
QHeaderView::section:last {{
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
}}
QScrollBar:vertical {{
    border: 0px;
    background: transparent;
    width: 6px;
    margin: 2px 0px 2px 0px;
}}
QScrollBar::handle:vertical {{
    background: {qss_rgba("#99C8B9E8")};
    border-radius: 3px;
    min-height: 26px;
}}
QScrollBar::handle:vertical:hover {{
    background: {qss_rgba(PALETTE["iris_primary"])};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
    border: none;
    height: 0px;
}}
QScrollBar:horizontal {{
    border: 0px;
    background: transparent;
    height: 6px;
    margin: 0px 2px 0px 2px;
}}
QScrollBar::handle:horizontal {{
    background: {qss_rgba("#99C8B9E8")};
    border-radius: 3px;
    min-width: 26px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
    border: none;
    width: 0px;
}}
QToolTip {{
    background-color: #FCFBFE;
    color: {qss_rgba(PALETTE["text_primary"])};
    border: 1px solid {qss_rgba(PALETTE["slot_focus_border"])};
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 12px;
}}
"""


# ---------------------------------------------------------------------------
# 2. Offscreen Cached Background Canvas (Diagonal Gradient + 4 Radial Glows)
# ---------------------------------------------------------------------------
class GreenhouseCanvas(QWidget):
    """Root background widget with offscreen QPixmap caching for 4 radial soft glow fields."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("root")
        self._cache_pm: QPixmap | None = None
        self._cache_key: tuple[int, int, int] | None = None

    def _ensure_cache(self) -> QPixmap:
        dpr = self.devicePixelRatioF()
        w = max(1, self.width())
        h = max(1, self.height())
        key = (w, h, int(round(dpr * 100)))
        if self._cache_pm is not None and self._cache_key == key:
            return self._cache_pm

        pm = QPixmap(int(math.ceil(w * dpr)), int(math.ceil(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)

        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        # 1) Diagonal linear gradient base: #FFF9F7FA -> #FFF8F6F3 -> #FFF2F7F5
        base_grad = QLinearGradient(0, 0, w, h)
        base_grad.setColorAt(0.0, qcolor(PALETTE["bg_stop_0"]))
        base_grad.setColorAt(0.52, qcolor(PALETTE["bg_stop_1"]))
        base_grad.setColorAt(1.0, qcolor(PALETTE["bg_stop_2"]))
        p.fillRect(QRectF(0, 0, w, h), base_grad)

        # 2) 4 radial soft glow fields around corners/edges
        diag = math.hypot(w, h)
        glow_specs = (
            (w * 0.06, h * 0.08, diag * 0.42, PALETTE["glow_apricot"]),  # Top-left warm apricot
            (w * 0.94, h * 0.10, diag * 0.42, PALETTE["glow_iris"]),     # Top-right light iris
            (w * 0.90, h * 0.92, diag * 0.42, PALETTE["glow_mint"]),     # Bottom-right mint
            (w * 0.10, h * 0.92, diag * 0.42, PALETTE["glow_rose"]),     # Bottom-left soft rose
        )
        for cx, cy, radius, hex_color in glow_specs:
            rg = QRadialGradient(QPointF(cx, cy), max(10.0, radius))
            c0 = qcolor(hex_color)
            c1 = QColor(c0.red(), c0.green(), c0.blue(), 0)
            rg.setColorAt(0.0, c0)
            rg.setColorAt(1.0, c1)
            p.setBrush(rg)
            p.drawRect(QRectF(0, 0, w, h))

        p.end()
        self._cache_pm = pm
        self._cache_key = key
        return pm

    def resizeEvent(self, event):
        self._cache_pm = None
        super().resizeEvent(event)

    def paintEvent(self, _event):
        pm = self._ensure_cache()
        p = QPainter(self)
        p.drawPixmap(0, 0, pm)
        p.end()


# ---------------------------------------------------------------------------
# 3. Clean ComboBox & Hover Table Delegate (eliminates native Windows frames)
# ---------------------------------------------------------------------------
class CleanComboBox(QComboBox):
    """QComboBox with clean popup and custom chevron arrow (no native black box/shadow artifact)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        view = self.view()
        if view is not None:
            view.setFrameShape(QFrame.Shape.NoFrame)
            container = view.window()
            if container is not None and container is not self.window():
                container.setWindowFlags(
                    Qt.WindowType.Popup
                    | Qt.WindowType.FramelessWindowHint
                    | Qt.WindowType.NoDropShadowWindowHint
                )
                container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        super().paintEvent(event)
        # Draw a crisp minimalist chevron on the right side
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(qcolor(PALETTE["text_secondary"]), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        cx = self.width() - 13.0
        cy = self.height() / 2.0 + 0.5
        path = QPainterPath(QPointF(cx - 3.5, cy - 1.8))
        path.lineTo(QPointF(cx, cy + 1.8))
        path.lineTo(QPointF(cx + 3.5, cy - 1.8))
        p.drawPath(path)
        p.end()


class CleanSpinBox(QSpinBox):
    """QSpinBox with custom minimalist up/down chevrons (no native Windows 95 gray box)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(84)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(qcolor(PALETTE["text_secondary"]), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        cx = self.width() - 12.0
        cy_up = self.height() * 0.34
        cy_dn = self.height() * 0.66
        # Up chevron
        up_path = QPainterPath(QPointF(cx - 3.0, cy_up + 1.3))
        up_path.lineTo(QPointF(cx, cy_up - 1.5))
        up_path.lineTo(QPointF(cx + 3.0, cy_up + 1.3))
        p.drawPath(up_path)
        # Down chevron
        dn_path = QPainterPath(QPointF(cx - 3.0, cy_dn - 1.3))
        dn_path.lineTo(QPointF(cx, cy_dn + 1.5))
        dn_path.lineTo(QPointF(cx + 3.0, cy_dn - 1.3))
        p.drawPath(dn_path)
        p.end()


class HoverRowTableDelegate(QStyledItemDelegate):
    """Removes focus dotted rectangles and draws unified #CCEAE5F2 row hover / selection."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        opt = QStyleOptionViewItem(option)
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        if hovered or selected:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(PALETTE["slot_hover"]))
            painter.drawRect(opt.rect)
            painter.restore()
            opt.state &= ~QStyle.StateFlag.State_Selected
            opt.state &= ~QStyle.StateFlag.State_MouseOver
        super().paint(painter, opt, index)


# ---------------------------------------------------------------------------
# 4. Self-drawn ToggleSwitch (QCheckBox-compatible API)
# ---------------------------------------------------------------------------
class ToggleSwitch(QAbstractButton):
    """Self-drawn 'Iris Greenhouse' ToggleSwitch with optional inline label.

    Off track: #FFE3E0EB, On track: #FFD66853, Disabled: #FFEDEBF2, Pure white knob: #FFFFFFFF.
    Compatible with QCheckBox API (.isChecked(), .setChecked(), .toggled).
    """

    def __init__(self, label: str = "", parent: QWidget | None = None, *, compact: bool = False):
        super().__init__(parent)
        self.setCheckable(True)
        self.setText(label)
        self.compact = compact
        self._hovered = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._track_w = 28 if compact else 38
        self._track_h = 16 if compact else 20
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def _label_font(self) -> QFont:
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(12)
        f.setWeight(QFont.Weight.Medium if self.compact else QFont.Weight.Normal)
        return f

    def sizeHint(self) -> QSize:
        label = self.text()
        if not label:
            return QSize(self._track_w + 2, self._track_h + 2)
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self._label_font())
        tw = fm.horizontalAdvance(label)
        return QSize(self._track_w + 7 + tw + 3, max(self._track_h + 2, fm.height() + 4))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        tw, th = float(self._track_w), float(self._track_h)
        ty = (self.height() - th) / 2.0
        track_rect = QRectF(1.0, ty, tw, th)

        if not self.isEnabled():
            track_color = qcolor(PALETTE["toggle_disabled"])
        elif self.isChecked():
            track_color = qcolor(PALETTE["toggle_on"])
        else:
            track_color = qcolor(PALETTE["toggle_off"])

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track_color)
        p.drawRoundedRect(track_rect, th / 2.0, th / 2.0)

        if self._hovered and self.isEnabled():
            p.setPen(QPen(qcolor(PALETTE["slot_focus_border"]), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track_rect.adjusted(0.5, 0.5, -0.5, -0.5), th / 2.0, th / 2.0)

        margin = 2.0 if self.compact else 2.5
        knob_d = th - margin * 2.0
        kx = (1.0 + tw - margin - knob_d) if self.isChecked() else (1.0 + margin)
        ky = ty + margin
        knob_rect = QRectF(kx, ky, knob_d, knob_d)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(PALETTE["toggle_knob"]))
        p.drawEllipse(knob_rect)

        label = self.text()
        if label:
            p.setPen(qcolor(PALETTE["text_primary"] if self.isEnabled() else PALETTE["text_muted"]))
            p.setFont(self._label_font())
            text_rect = QRectF(tw + 7.0, 0, self.width() - tw - 7.0, self.height())
            p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)

        p.end()


# ---------------------------------------------------------------------------
# 5. Navigation Capsule Button & Lightweight Badges / Progress Bar
# ---------------------------------------------------------------------------
class NavCapsuleButton(QPushButton):
    """Top bar capsule tab button: #F5FFFFFF active pill with bottom coral underline indicator."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("nav")
        self._active = False
        self._hovered = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(30)
        self.setMinimumWidth(86)

    def set_active(self, active: bool):
        self._active = bool(active)
        self.setProperty("active", self._active)
        self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)

        if self._active:
            p.setPen(QPen(QColor(124, 100, 184, 42), 1.0))
            p.setBrush(qcolor(PALETTE["nav_active_bg"]))
            p.drawRoundedRect(r, 9.0, 9.0)
            # Short horizontal coral underline inside bottom edge of active capsule
            ind_w = 20.0
            ind_h = 2.5
            ind_rect = QRectF(r.center().x() - ind_w / 2.0, r.bottom() - 4.0, ind_w, ind_h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(PALETTE["nav_indicator"]))
            p.drawRoundedRect(ind_rect, 1.25, 1.25)
        elif self._hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(PALETTE["slot_hover"]))
            p.drawRoundedRect(r, 9.0, 9.0)

        p.setPen(qcolor(PALETTE["text_primary"] if self._active else PALETTE["text_secondary"]))
        f = QFont("Microsoft YaHei UI", 10, QFont.Weight.Bold if self._active else QFont.Weight.DemiBold)
        p.setFont(f)
        text_r = r.adjusted(0, -1, 0, -2) if self._active else r
        p.drawText(text_r, Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


class PillBadge(QLabel):
    """Lightweight tinted badge: kind='iris' (#206E58AD / #FF6E58AD) or 'coral' (#22D66853 / #FFC45545)."""

    def __init__(self, text: str = "", *, kind: str = "iris", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.kind = kind
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.apply_kind(kind)

    def apply_kind(self, kind: str):
        self.kind = kind
        if kind == "coral":
            bg = qss_rgba(PALETTE["badge_coral_bg"])
            fg = qss_rgba(PALETTE["badge_coral_fg"])
        elif kind == "muted":
            bg = qss_rgba(PALETTE["slot_bg"])
            fg = qss_rgba(PALETTE["text_muted"])
        else:
            bg = qss_rgba(PALETTE["badge_iris_bg"])
            fg = qss_rgba(PALETTE["badge_iris_fg"])
        self.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 8px; "
            f"padding: 3px 9px; font-size: 11px; font-weight: 700;"
        )


class BatteryProgressBar(QWidget):
    """8px rounded horizontal battery progress bar (trough #FFE3E0EB, iris #FF7C64B8 / coral #FFD66853 / stale #FF867E96)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.value: int | None = None
        self.stale: bool = False
        self.setFixedHeight(8)
        self.setMinimumWidth(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_state(self, value: int | None, *, stale: bool = False):
        self.value = value
        self.stale = bool(stale)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(PALETTE["toggle_off"]))
        p.drawRoundedRect(r, 4.0, 4.0)

        if self.value is not None and self.value > 0:
            pct = max(0.0, min(100.0, float(self.value)))
            if self.stale:
                fill_color = qcolor(PALETTE["stale_gray"])
            elif pct <= 20:
                fill_color = qcolor(PALETTE["coral_low"])
            else:
                fill_color = qcolor(PALETTE["iris_primary"])
            fw = max(8.0, r.width() * (pct / 100.0))
            p.setBrush(fill_color)
            p.drawRoundedRect(QRectF(0, 0, min(r.width(), fw), r.height()), 4.0, 4.0)
        p.end()


# ---------------------------------------------------------------------------
# 6. Chart Y-Axis Auto-Scaling & Area Fill Guard Helpers
# ---------------------------------------------------------------------------
def should_fill_chart_area(segments: list[list[QPointF]], period_hours: int | None = 24) -> bool:
    """Disable area gradient fill for long ranges (7d/30d/all), >12 segments, or narrow segments (<12px)."""
    if period_hours is None or period_hours >= 7 * 24:
        return False
    if len(segments) > 12:
        return False
    multi = [seg for seg in segments if len(seg) >= 2]
    if not multi:
        return False
    widths = [abs(seg[-1].x() - seg[0].x()) for seg in multi]
    avg_w = sum(widths) / len(widths)
    return max(widths) >= 12.0 and avg_w >= 12.0


def default_auto_scale_for_hours(hours: int | None) -> bool:
    """Return default Y-axis auto-scale state: True for 1h/24h, False for 7d/30d/all."""
    return hours in (1, 24)


def compute_y_axis_bounds(values: list[float], mode: str, auto_scale: bool) -> tuple[float, float]:
    """Compute (minimum, maximum) Y-axis range for TrendChart.

    - When mode == 'percent':
      - If not auto_scale or no values: returns (0.0, 100.0)
      - If auto_scale: pads visible data range by ~8%-10% (min span 12% so 4 unit steps are clean integers)
        and rounds bounds to clean integer percentages within [0, 100].
    - When mode != 'percent' (voltage V):
      - If no values: returns (3.2, 4.4)
      - If not auto_scale: returns full lithium voltage window (3.3, 4.4)
      - If auto_scale: pads visible voltage range by ~10% (at least ±0.06 V).
    """
    if mode == "percent":
        if not auto_scale or not values:
            return 0.0, 100.0
        vmin = float(min(values))
        vmax = float(max(values))
        span = max(8.0, vmax - vmin)
        pad = max(2.0, math.ceil(span * 0.10))
        low = max(0.0, float(math.floor((vmin - pad) / 2.0) * 2))
        high = min(100.0, float(math.ceil((vmax + pad) / 2.0) * 2))
        if high - low < 12.0:
            mid = (vmin + vmax) / 2.0
            low = max(0.0, float(math.floor((mid - 6.0) / 2.0) * 2))
            high = min(100.0, low + 12.0)
            if high - low < 12.0:
                low = max(0.0, high - 12.0)
        # Make (high - low) divisible by 4 for crisp integer grid labels
        rem = int(round(high - low)) % 4
        if rem != 0:
            add = 4 - rem
            if high + add <= 100.0:
                high += add
            elif low - add >= 0.0:
                low -= add
        return low, high

    # Voltage mode (V)
    if not values:
        return 3.2, 4.4
    if not auto_scale:
        return 3.3, 4.4
    vmin = float(min(values))
    vmax = float(max(values))
    span = vmax - vmin
    pad = max(0.04, span * 0.10)
    low = max(2.5, round(vmin - pad, 2))
    high = min(4.5, round(vmax + pad, 2))
    if high - low < 0.16:
        mid = (vmin + vmax) / 2.0
        low = max(2.5, round(mid - 0.08, 2))
        high = min(4.5, round(mid + 0.08, 2))
    return low, high


# ---------------------------------------------------------------------------
# 7. V0.8 Segmented Control, Taskbar Status Pill & Ultra-Thin Battery Ring
# ---------------------------------------------------------------------------
class SegmentedControl(QFrame):
    """Compact pill segmented control with translucent trough & white floating active selection."""

    changed = Signal(str)

    def __init__(self, items: list[tuple[str, str]] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._items: list[tuple[str, str]] = []
        self._buttons: dict[str, QPushButton] = {}
        self._current: str = ""
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(3, 3, 3, 3)
        self._layout.setSpacing(2)
        self.setObjectName("segmented_trough")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QFrame#segmented_trough { background: rgba(241, 239, 246, 166); "
            "border: 1px solid rgba(212, 205, 226, 125); border-radius: 10px; }"
        )
        self.setFixedHeight(30)
        if items:
            self.set_items(items)

    def set_items(self, items: list[tuple[str, str]]):
        while self._layout.count():
            child = self._layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._buttons.clear()
        self._items = list(items)
        for key, text in self._items:
            btn = QPushButton(text, self)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(22)
            btn.clicked.connect(lambda _checked=False, k=key: self.set_current(k, emit_signal=True))
            self._layout.addWidget(btn)
            self._buttons[key] = btn
        if self._items and self._current not in self._buttons:
            self._current = self._items[0][0]
        self._refresh_styles()

    def current_key(self) -> str:
        return self._current

    def set_current(self, key: str, *, emit_signal: bool = False):
        if key not in self._buttons:
            return
        changed = self._current != key
        self._current = key
        self._refresh_styles()
        if emit_signal and changed:
            self.changed.emit(key)

    def _refresh_styles(self):
        for key, btn in self._buttons.items():
            active = key == self._current
            btn.blockSignals(True)
            btn.setChecked(active)
            btn.blockSignals(False)
            if active:
                btn.setStyleSheet(
                    "QPushButton { background: rgba(255, 255, 255, 248); color: #1B1922; "
                    "border: 1px solid rgba(124, 100, 184, 50); "
                    "border-bottom: 1.5px solid rgba(124, 100, 184, 75); border-radius: 7px; "
                    "padding: 2px 10px; font-size: 11px; font-weight: 700; }"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton { background: transparent; color: #5B5768; "
                    "border: none; border-radius: 7px; padding: 2px 10px; font-size: 11px; font-weight: 600; }"
                    "QPushButton:hover { color: #1B1922; background: rgba(255, 255, 255, 115); }"
                )


class WatermarkCardFrame(QFrame):
    """Left device glass panel with faint 90-degree rotated 'MOUSE' watermark along right edge."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("panel")

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.save()
        p.translate(self.width() - 18.0, self.height() * 0.43)
        p.rotate(90.0)
        p.setPen(QColor(91, 87, 104, 12))  # ~4.7% opacity, completely clear of center ring
        wm_font = QFont("Microsoft YaHei UI", 11, QFont.Weight.Bold)
        wm_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 6.0)
        p.setFont(wm_font)
        p.drawText(QRectF(-70.0, -14.0, 140.0, 28.0), Qt.AlignmentFlag.AlignCenter, "MOUSE")
        p.restore()
        p.end()


class TaskbarStatusPill(QPushButton):
    """Topbar toggle pill showing live status of the floating taskbar badge."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(30)
        self.toggled.connect(self._sync_appearance)
        self._sync_appearance(self.isChecked())

    def _sync_appearance(self, checked: bool):
        if checked:
            self.setText("● 任务栏直显")
            self.setStyleSheet(
                "QPushButton { background: rgba(124, 100, 184, 24); color: #5E489D; "
                "border: 1px solid rgba(124, 100, 184, 72); border-radius: 10px; "
                "padding: 0 11px; font-size: 11px; font-weight: 700; }"
                "QPushButton:hover { background: rgba(124, 100, 184, 38); }"
            )
        else:
            self.setText("○ 任务栏直显")
            self.setStyleSheet(
                "QPushButton { background: rgba(241, 239, 246, 190); color: #867E96; "
                "border: 1px solid rgba(212, 205, 226, 120); border-radius: 10px; "
                "padding: 0 11px; font-size: 11px; font-weight: 600; }"
                "QPushButton:hover { background: rgba(234, 229, 242, 220); color: #5B5768; }"
            )


class BatteryRingWidget(QWidget):
    """Pure-data 3.8px ultra-thin battery ring with faint 'MOUSE' watermark or optional user PNG."""

    customImageSelected = Signal(str)
    customImageReset = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.percent: int | None = None
        self.mv: int | None = None
        self.stale: bool = False
        self.disconnected: bool = False
        self.sub_text: str = ""
        self.custom_image_path: str | None = None
        self._custom_pm: QPixmap | None = None
        self.setMinimumSize(198, 198)
        self.setMaximumSize(224, 224)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setToolTip("右键可导入本地自定义鼠标图 (PNG) 或恢复纯数据圆环")

    def set_state(
        self,
        percent: int | None,
        mv: int | None = None,
        *,
        stale: bool = False,
        disconnected: bool = False,
        sub_text: str = "",
    ):
        self.percent = percent
        self.mv = mv
        self.stale = bool(stale)
        self.disconnected = bool(disconnected)
        self.sub_text = sub_text or ""
        self.update()

    def set_custom_image(self, image_path: str | None):
        clean = (image_path or "").strip()
        if not clean:
            self.custom_image_path = None
            self._custom_pm = None
            self.update()
            return
        pm = QPixmap(clean)
        if pm.isNull():
            self.custom_image_path = None
            self._custom_pm = None
        else:
            self.custom_image_path = clean
            self._custom_pm = pm
        self.update()

    def has_custom_image(self) -> bool:
        return self._custom_pm is not None and not self._custom_pm.isNull()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        act_import = menu.addAction("导入本地自定义图片 (PNG)...")
        act_reset = menu.addAction("恢复纯数据圆环")
        act_reset.setEnabled(self.has_custom_image())
        chosen = menu.exec(event.globalPos())
        if chosen == act_import:
            from PySide6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                self, "选择自定义鼠标图片", "", "PNG/图片 (*.png *.jpg *.jpeg *.webp)"
            )
            if path:
                self.set_custom_image(path)
                self.customImageSelected.emit(path)
        elif chosen == act_reset:
            self.set_custom_image(None)
            self.customImageReset.emit()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        size = min(self.width(), self.height()) - 14.0
        cx, cy = self.width() / 2.0, self.height() / 2.0
        ring_rect = QRectF(cx - size / 2.0, cy - size / 2.0, size, size)
        inner_r = size / 2.0 - 10.0

        # Inner backdrop: only optional user PNG (zero built-in illustration or overlapping center watermark)
        if self._custom_pm is not None and not self._custom_pm.isNull():
            p.save()
            clip = QPainterPath()
            clip.addEllipse(QPointF(cx, cy), inner_r, inner_r)
            p.setClipPath(clip)
            scaled = self._custom_pm.scaled(
                int(inner_r * 1.55),
                int(inner_r * 1.55),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            p.setOpacity(0.24)
            p.drawPixmap(int(cx - scaled.width() / 2), int(cy - scaled.height() / 2), scaled)
            p.restore()

        # Background track (3.8px ultra-thin)
        track_pen = QPen(QColor(227, 224, 235, 220), 3.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(track_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(ring_rect)

        # Progress arc
        if self.percent is not None and self.percent > 0:
            pct = max(0.0, min(100.0, float(self.percent)))
            if self.stale or self.disconnected:
                arc_pen = QPen(qcolor(PALETTE["stale_gray"]), 3.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            elif pct <= 20:
                grad = QLinearGradient(ring_rect.topLeft(), ring_rect.bottomRight())
                grad.setColorAt(0.0, qcolor(PALETTE["btn_grad_0"]))
                grad.setColorAt(1.0, qcolor(PALETTE["btn_grad_1"]))
                arc_pen = QPen(grad, 3.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            else:
                arc_pen = QPen(qcolor(PALETTE["iris_primary"]), 3.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(arc_pen)
            start_angle = 90 * 16
            span_angle = int(round(-360.0 * (pct / 100.0) * 16))
            p.drawArc(ring_rect, start_angle, span_angle)

        # Center typography stack
        pct_text = "--%" if (self.percent is None or self.disconnected) else f"{int(self.percent)}%"
        pct_color = qcolor(PALETTE["text_muted"] if (self.percent is None or self.disconnected) else PALETTE["text_primary"])
        p.setPen(pct_color)
        hero_font = QFont("Microsoft YaHei UI", 34, QFont.Weight.DemiBold)
        p.setFont(hero_font)
        hero_rect = QRectF(ring_rect.left() + 8, cy - 30, ring_rect.width() - 16, 44)
        p.drawText(hero_rect, Qt.AlignmentFlag.AlignCenter, pct_text)

        # Voltage line directly beneath percentage (3 decimal places for table consistency)
        if self.mv is not None:
            v_text = f"{self.mv / 1000.0:.3f} V"
        else:
            v_text = "电压待获取"
        p.setPen(qcolor(PALETTE["text_secondary"]))
        v_font = QFont("Microsoft YaHei UI", 11, QFont.Weight.DemiBold)
        p.setFont(v_font)
        v_rect = QRectF(ring_rect.left() + 12, cy + 18, ring_rect.width() - 24, 20)
        p.drawText(v_rect, Qt.AlignmentFlag.AlignCenter, v_text)

        if self.sub_text:
            p.setPen(qcolor(PALETTE["text_muted"]))
            sub_font = QFont("Microsoft YaHei UI", 9, QFont.Weight.Medium)
            p.setFont(sub_font)
            sub_rect = QRectF(ring_rect.left() + 16, cy + 40, ring_rect.width() - 32, 18)
            p.drawText(sub_rect, Qt.AlignmentFlag.AlignCenter, self.sub_text)

        p.end()
