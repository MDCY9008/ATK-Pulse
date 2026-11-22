"""Non-intrusive Win32 layered taskbar badge for ATK Battery Monitor V0.5.

Renders a crisp, DPI-aware mouse icon + estimated battery percent directly to the
left of the system tray chevron (TrayNotifyWnd) using Qt QPainter + UpdateLayeredWindow,
with zero modification to Explorer's child window tree and automatic safe degradation.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import sys
import winreg
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip

log = logging.getLogger(__name__)

_CLASS_NAME = "ATK_TaskbarBatteryBadge"
_REGISTERED = False
_INSTANCES: dict[int, "TaskbarBadge"] = {}

# Win32 constants
_WS_POPUP = 0x80000000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000

_WM_DESTROY = 0x0002
_WM_SETTINGCHANGE = 0x001A
_WM_MOUSEACTIVATE = 0x0021
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONUP = 0x0205
_WM_MOUSELEAVE = 0x02A3
_WM_DPICHANGED = 0x02E0

_MA_NOACTIVATE = 3
_TME_LEAVE = 0x00000002
_HOVER_DEFAULT = 0xFFFFFFFF

_SW_HIDE = 0
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_HWND_TOPMOST = wt.HWND(-1)
_GW_HWNDPREV = 3

_ULW_ALPHA = 0x00000002
_AC_SRC_OVER = 0x00
_AC_SRC_ALPHA = 0x01
_DIB_RGB_COLORS = 0
_MONITOR_DEFAULTTOPRIMARY = 1

_FULLSCREEN_IGNORED_CLASSES = frozenset({
    "",
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow",
    "Windows.UI.Core.CoreWindow",
    _CLASS_NAME,
})


@dataclass(frozen=True)
class BadgeStyle:
    text: str
    fg_color: QColor
    bg_color: QColor
    outline_color: QColor
    fill_color: QColor
    wheel_color: QColor
    low_battery: bool
    stale: bool


def read_system_uses_light_theme() -> bool:
    """Read Windows taskbar theme (1=light taskbar, 0=dark taskbar)."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            return bool(int(val))
    except OSError:
        return False


def resolve_badge_style(
    percent: int | None,
    *,
    stale: bool,
    light_theme: bool,
    hovered: bool,
    low_threshold: int = 20,
) -> BadgeStyle:
    """Resolve liquid-fill, outline, wheel-contrast, and hit-area colors for current state."""
    is_missing = percent is None
    is_dimmed = stale or is_missing
    thresh = max(1, min(99, int(low_threshold)))
    is_low = (not is_missing) and (not stale) and int(percent) <= thresh
    text = "--%" if is_missing else f"{int(percent)}%"
    wheel_covered = (not is_missing) and int(percent) >= 75

    if light_theme:
        bg = QColor(0, 0, 0, 14) if hovered else QColor(0, 0, 0, 1)
        if is_dimmed:
            outline = QColor(0x86, 0x7E, 0x96, 166)
            fg = QColor(0x86, 0x7E, 0x96, 166)
            fill = QColor(0x86, 0x7E, 0x96, 150)
        elif is_low:
            outline = QColor(0x1E, 0x1E, 0x24, 255)
            fg = QColor(0xC4, 0x55, 0x45, 255)
            fill = QColor(0xD6, 0x68, 0x53, 235)
        else:
            outline = QColor(0x1E, 0x1E, 0x24, 255)
            fg = QColor(0x1E, 0x1E, 0x24, 255)
            fill = QColor(0x7C, 0x64, 0xB8, 224)
        wheel = QColor(0xFF, 0xFF, 0xFF, 200 if is_dimmed else 255) if wheel_covered else outline
    else:
        bg = QColor(255, 255, 255, 20) if hovered else QColor(255, 255, 255, 1)
        if is_dimmed:
            outline = QColor(0x9A, 0x93, 0xA8, 166)
            fg = QColor(0x9A, 0x93, 0xA8, 166)
            fill = QColor(0x9A, 0x93, 0xA8, 150)
        elif is_low:
            outline = QColor(0xF3, 0xF4, 0xF6, 255)
            fg = QColor(0xF2, 0x8B, 0x76, 255)
            fill = QColor(0xD6, 0x68, 0x53, 235)
        else:
            outline = QColor(0xF3, 0xF4, 0xF6, 255)
            fg = QColor(0xF3, 0xF4, 0xF6, 255)
            fill = QColor(0xA6, 0x8E, 0xE6, 235)
        wheel = QColor(0x1E, 0x1E, 0x24, 200 if is_dimmed else 255) if wheel_covered else outline

    return BadgeStyle(
        text=text,
        fg_color=fg,
        bg_color=bg,
        outline_color=outline,
        fill_color=fill,
        wheel_color=wheel,
        low_battery=is_low,
        stale=is_dimmed,
    )


def render_badge_image(
    percent: int | None,
    *,
    stale: bool = False,
    light_theme: bool = True,
    hovered: bool = False,
    dpi: int = 96,
    height_px: int | None = None,
    low_threshold: int = 20,
) -> QImage:
    """Render a high-DPI premultiplied ARGB32 image with mouse liquid-level fill + %."""
    scale = max(1.0, float(dpi) / 96.0)
    h_px = int(height_px) if height_px is not None else max(28, round(36 * scale))
    style = resolve_badge_style(
        percent,
        stale=stale,
        light_theme=light_theme,
        hovered=hovered,
        low_threshold=low_threshold,
    )

    font = QFont("Segoe UI")
    font.setWeight(QFont.Weight.DemiBold)
    font.setPixelSize(max(11, round(13.5 * scale)))
    fm = QFontMetrics(font)

    icon_w = max(11.0, 11.5 * scale)
    icon_h = max(16.0, 17.5 * scale)
    pad_x = max(6, round(8 * scale))
    gap = max(5, round(5.5 * scale))
    text_w = fm.horizontalAdvance(style.text)
    # Keep steady width across 2-digit values; expand cleanly for "100%" or "--%".
    min_text_w = fm.horizontalAdvance("88%")
    draw_text_w = max(text_w, min_text_w)
    w_px = max(46, round(pad_x * 2 + icon_w + gap + draw_text_w))

    img = QImage(w_px, h_px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    # 1) Full-canvas hit pad (alpha >= 1 invariant, independent of stale/sleep opacity)
    hit_color = QColor(0, 0, 0, 1) if light_theme else QColor(255, 255, 255, 1)
    p.fillRect(QRectF(0.0, 0.0, float(w_px), float(h_px)), hit_color)

    # 2) Subtle Win11-style rounded capsule ONLY while hovered
    if hovered:
        inset = max(1.0, round(2.0 * scale))
        radius = max(4.0, 5.5 * scale)
        hover_rect = QRectF(inset, inset, w_px - 2 * inset, h_px - 2 * inset)
        border_col = QColor(0, 0, 0, 12) if light_theme else QColor(255, 255, 255, 18)
        p.setPen(QPen(border_col, max(1.0, 1.0 * scale)))
        p.setBrush(style.bg_color)
        p.drawRoundedRect(hover_rect, radius, radius)

    # 3) Mouse geometry & inner ~1px logical breathing gap clip
    stroke = max(1.3, 1.35 * scale)
    ix = float(pad_x)
    iy = (h_px - icon_h) / 2.0
    body_rect = QRectF(ix, iy, icon_w, icon_h)
    body_r = icon_w * 0.46

    gap_in = (stroke / 2.0) + (1.0 * scale)
    inner_rect = body_rect.adjusted(gap_in, gap_in, -gap_in, -gap_in)
    inner_r = max(1.5, body_r - gap_in)

    # 4) Liquid-level fill from bottom upward (clipped to inner rounded shape)
    if percent is not None and int(percent) > 0 and inner_rect.width() > 1.0 and inner_rect.height() > 1.0:
        frac = max(0.0, min(1.0, float(percent) / 100.0))
        min_fill_h = max(2.0, 2.0 * scale)
        fill_h = min(inner_rect.height(), max(min_fill_h, inner_rect.height() * frac))
        fill_rect = QRectF(
            inner_rect.left(),
            inner_rect.bottom() - fill_h,
            inner_rect.width(),
            fill_h,
        )
        clip_path = QPainterPath()
        clip_path.addRoundedRect(inner_rect, inner_r, inner_r)
        p.save()
        p.setClipPath(clip_path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(style.fill_color)
        top_r = min(1.2 * scale, fill_h * 0.35)
        p.drawRoundedRect(fill_rect, top_r, top_r)
        p.restore()

    # 5) Fine-line minimalist mouse outline (NO middle horizontal line)
    outline_pen = QPen(
        style.outline_color,
        stroke,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )
    p.setPen(outline_pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(body_rect, body_r, body_r)

    # 6) Upper scroll wheel tick with high-level contrast protection
    cx = ix + icon_w / 2.0
    wheel_top = iy + icon_h * 0.21
    wheel_bot = iy + icon_h * 0.39
    wheel_pen = QPen(style.wheel_color, max(1.25, stroke), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    p.setPen(wheel_pen)
    p.drawLine(QPointF(cx, wheel_top), QPointF(cx, wheel_bot))

    # 7) Right-aligned battery percentage text
    p.setFont(font)
    p.setPen(style.fg_color)
    tx = ix + icon_w + gap
    text_rect = QRectF(tx, 0.0, w_px - tx - pad_x + 1.0, float(h_px))
    p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, style.text)
    p.end()
    return img


def compute_badge_rect(
    tray_rect: tuple[int, int, int, int],
    notify_rect: tuple[int, int, int, int],
    rebar_rect: tuple[int, int, int, int] | None,
    badge_w: int,
    badge_h: int,
    dpi: int = 96,
) -> tuple[int, int, int, int] | None:
    """Compute physical screen (x, y, w, h) immediately left of TrayNotifyWnd."""
    tray_l, tray_t, tray_r, tray_b = tray_rect
    tray_w = tray_r - tray_l
    tray_h = tray_b - tray_t
    if tray_w <= tray_h or tray_h < 20:
        return None

    notify_l, _notify_t, notify_r, _notify_b = notify_rect
    if notify_r <= notify_l or notify_l <= tray_l:
        return None

    scale = max(1.0, float(dpi) / 96.0)
    gap = max(3, round(5 * scale))
    x = notify_l - gap - badge_w
    y = tray_t + max(0, (tray_h - badge_h) // 2)

    if x < tray_l + 2:
        return None

    if rebar_rect is not None:
        rebar_l, _rt, rebar_r, _rb = rebar_rect
        if rebar_r > rebar_l and rebar_r < notify_l:
            min_x = rebar_r + max(3, round(4 * scale))
            if x < min_x:
                return None

    return (x, y, badge_w, badge_h)


def should_hide_for_fullscreen(
    fg_rect: tuple[int, int, int, int] | None,
    fg_class: str,
    mon_rect: tuple[int, int, int, int] | None,
    quns_state: int | None = None,
) -> bool:
    """Return True when a fullscreen app/game or presentation mode is active."""
    if quns_state in (2, 3, 4):
        return True
    if fg_class in _FULLSCREEN_IGNORED_CLASSES:
        return False
    if fg_rect is None or mon_rect is None:
        return False
    fl, ft, fr, fb = fg_rect
    ml, mt, mr, mb = mon_rect
    if fr <= fl or fb <= ft or mr <= ml or mb <= mt:
        return False
    return fl <= ml and ft <= mt and fr >= mr and fb >= mb


# ---------------------------------------------------------------------------
# Win32 ctypes bindings & window host (Windows only)
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _LRESULT = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.UINT),
            ("style", wt.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wt.HINSTANCE),
            ("hIcon", wt.HICON),
            ("hCursor", wt.HANDLE),
            ("hbrBackground", wt.HBRUSH),
            ("lpszMenuName", wt.LPCWSTR),
            ("lpszClassName", wt.LPCWSTR),
            ("hIconSm", wt.HICON),
        ]

    class _TRACKMOUSEEVENT(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("dwFlags", wt.DWORD),
            ("hwndTrack", wt.HWND),
            ("dwHoverTime", wt.DWORD),
        ]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wt.LONG), ("y", wt.LONG)]

    class _SIZE(ctypes.Structure):
        _fields_ = [("cx", wt.LONG), ("cy", wt.LONG)]

    class _BLENDFUNCTION(ctypes.Structure):
        _fields_ = [
            ("BlendOp", wt.BYTE),
            ("BlendFlags", wt.BYTE),
            ("SourceConstantAlpha", wt.BYTE),
            ("AlphaFormat", wt.BYTE),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD),
            ("biWidth", wt.LONG),
            ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD),
            ("biBitCount", wt.WORD),
            ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD),
            ("biXPelsPerMeter", wt.LONG),
            ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD),
            ("biClrImportant", wt.DWORD),
        ]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("rcMonitor", wt.RECT),
            ("rcWork", wt.RECT),
            ("dwFlags", wt.DWORD),
        ]

    _kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
    _kernel32.GetModuleHandleW.restype = wt.HMODULE

    _user32.RegisterClassExW.argtypes = (ctypes.POINTER(_WNDCLASSEXW),)
    _user32.RegisterClassExW.restype = wt.ATOM
    _user32.RegisterWindowMessageW.argtypes = (wt.LPCWSTR,)
    _user32.RegisterWindowMessageW.restype = wt.UINT
    _user32.CreateWindowExW.argtypes = (
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
    )
    _user32.CreateWindowExW.restype = wt.HWND
    _user32.DestroyWindow.argtypes = (wt.HWND,)
    _user32.DestroyWindow.restype = wt.BOOL
    _user32.DefWindowProcW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    _user32.DefWindowProcW.restype = _LRESULT
    _user32.LoadCursorW.argtypes = (wt.HINSTANCE, ctypes.c_void_p)
    _user32.LoadCursorW.restype = wt.HANDLE
    _user32.TrackMouseEvent.argtypes = (ctypes.POINTER(_TRACKMOUSEEVENT),)
    _user32.TrackMouseEvent.restype = wt.BOOL
    _user32.FindWindowW.argtypes = (wt.LPCWSTR, wt.LPCWSTR)
    _user32.FindWindowW.restype = wt.HWND
    _user32.FindWindowExW.argtypes = (wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
    _user32.FindWindowExW.restype = wt.HWND
    _user32.GetWindowRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
    _user32.GetWindowRect.restype = wt.BOOL
    _user32.IsWindowVisible.argtypes = (wt.HWND,)
    _user32.IsWindowVisible.restype = wt.BOOL
    _user32.GetDpiForWindow.argtypes = (wt.HWND,)
    _user32.GetDpiForWindow.restype = wt.UINT
    _user32.ShowWindow.argtypes = (wt.HWND, ctypes.c_int)
    _user32.ShowWindow.restype = wt.BOOL
    _user32.SetWindowPos.argtypes = (wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
    _user32.SetWindowPos.restype = wt.BOOL
    _user32.GetWindow.argtypes = (wt.HWND, wt.UINT)
    _user32.GetWindow.restype = wt.HWND
    _user32.GetForegroundWindow.argtypes = ()
    _user32.GetForegroundWindow.restype = wt.HWND
    _user32.GetClassNameW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.MonitorFromWindow.argtypes = (wt.HWND, wt.DWORD)
    _user32.MonitorFromWindow.restype = wt.HMONITOR
    _user32.GetMonitorInfoW.argtypes = (wt.HMONITOR, ctypes.POINTER(_MONITORINFO))
    _user32.GetMonitorInfoW.restype = wt.BOOL
    _user32.GetDC.argtypes = (wt.HWND,)
    _user32.GetDC.restype = wt.HDC
    _user32.ReleaseDC.argtypes = (wt.HWND, wt.HDC)
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.UpdateLayeredWindow.argtypes = (
        wt.HWND, wt.HDC, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
        wt.HDC, ctypes.POINTER(_POINT), wt.COLORREF,
        ctypes.POINTER(_BLENDFUNCTION), wt.DWORD,
    )
    _user32.UpdateLayeredWindow.restype = wt.BOOL
    _user32.InvalidateRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT), wt.BOOL)
    _user32.InvalidateRect.restype = wt.BOOL

    _gdi32.CreateCompatibleDC.argtypes = (wt.HDC,)
    _gdi32.CreateCompatibleDC.restype = wt.HDC
    _gdi32.DeleteDC.argtypes = (wt.HDC,)
    _gdi32.DeleteDC.restype = wt.BOOL
    _gdi32.CreateDIBSection.argtypes = (
        wt.HDC, ctypes.POINTER(_BITMAPINFOHEADER), wt.UINT,
        ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD,
    )
    _gdi32.CreateDIBSection.restype = wt.HBITMAP
    _gdi32.SelectObject.argtypes = (wt.HDC, wt.HGDIOBJ)
    _gdi32.SelectObject.restype = wt.HGDIOBJ
    _gdi32.DeleteObject.argtypes = (wt.HGDIOBJ,)
    _gdi32.DeleteObject.restype = wt.BOOL

    _shell32.SHQueryUserNotificationState.argtypes = (ctypes.POINTER(ctypes.c_int),)
    _shell32.SHQueryUserNotificationState.restype = ctypes.c_long

    _WM_TASKBARCREATED = _user32.RegisterWindowMessageW("TaskbarCreated")

    def _dispatch_wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        badge = _INSTANCES.get(int(hwnd))
        if badge is not None:
            try:
                handled, res = badge._handle_win32_message(int(hwnd), int(msg), int(wparam), int(lparam))
                if handled:
                    return res
            except Exception:
                log.exception("TaskbarBadge message handler error")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    _GLOBAL_WNDPROC = _WNDPROC(_dispatch_wndproc)

    def _ensure_class_registered() -> None:
        global _REGISTERED
        if _REGISTERED:
            return
        hinst = _kernel32.GetModuleHandleW(None)
        cls = _WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        cls.style = 0
        cls.lpfnWndProc = _GLOBAL_WNDPROC
        cls.cbClsExtra = 0
        cls.cbWndExtra = 0
        cls.hInstance = hinst
        cls.hIcon = None
        cls.hCursor = _user32.LoadCursorW(None, ctypes.c_void_p(32512))  # IDC_ARROW
        cls.hbrBackground = None
        cls.lpszMenuName = None
        cls.lpszClassName = _CLASS_NAME
        cls.hIconSm = None
        atom = _user32.RegisterClassExW(ctypes.byref(cls))
        err = ctypes.get_last_error()
        if not atom and err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
            raise ctypes.WinError(err)
        _REGISTERED = True


class TaskbarBadge:
    """Decoupled transparent top-most taskbar badge with safe fallback."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        on_left_click=None,
        on_right_click=None,
        low_threshold: int = 20,
        parent=None,
    ) -> None:
        self.enabled = bool(enabled)
        self.on_left_click = on_left_click
        self.on_right_click = on_right_click
        self.percent: int | None = None
        self.stale: bool = True
        self.low_threshold: int = int(low_threshold)
        self.tooltip_text: str = "ATK-Pulse · 等待设备连接"
        self.degraded: bool = False

        self._hwnd: int | None = None
        self._visible: bool = False
        self._hovered: bool = False
        self._tracking_leave: bool = False
        self._light_theme: bool = read_system_uses_light_theme()
        self._last_render_key: tuple | None = None
        self._last_rect: tuple[int, int, int, int] | None = None

        self._sync_timer = QTimer(parent)
        self._sync_timer.setInterval(500)
        self._sync_timer.timeout.connect(self.sync)

        self._hover_timer = QTimer(parent)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(300)
        self._hover_timer.timeout.connect(self._show_tooltip)

    def start(self) -> bool:
        """Create Win32 layered window and start lightweight 500 ms sync."""
        if sys.platform != "win32":
            self.degraded = True
            return False
        if self.degraded:
            return False
        try:
            self._create_window()
            if self.enabled:
                self.sync()
            self._sync_timer.start()
            return True
        except Exception as exc:
            log.warning("TaskbarBadge disabled cleanly after init failure: %s", exc)
            self.degraded = True
            self.destroy()
            return False

    def _create_window(self) -> None:
        if self._hwnd:
            return
        _ensure_class_registered()
        hinst = _kernel32.GetModuleHandleW(None)
        ex_style = _WS_EX_LAYERED | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE | _WS_EX_TOPMOST
        hwnd = _user32.CreateWindowExW(
            ex_style,
            _CLASS_NAME,
            "ATK Taskbar Battery Badge",
            _WS_POPUP,
            0, 0, 1, 1,
            None, None, hinst, None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = int(hwnd)
        _INSTANCES[self._hwnd] = self

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self._hide_window()
        elif not self.degraded:
            if not self._hwnd:
                self.start()
            else:
                self.sync()

    def update_state(
        self,
        percent: int | None,
        *,
        stale: bool,
        tooltip_text: str | None = None,
        low_threshold: int | None = None,
    ) -> None:
        self.percent = percent
        self.stale = bool(stale)
        if low_threshold is not None:
            self.low_threshold = int(low_threshold)
        if tooltip_text is not None:
            self.tooltip_text = tooltip_text
        if self.enabled and not self.degraded and self._hwnd:
            self.sync()

    def set_low_threshold(self, threshold: int) -> None:
        self.low_threshold = int(threshold)
        if self.enabled and not self.degraded and self._hwnd:
            self.sync()

    def set_tooltip(self, tooltip_text: str) -> None:
        self.tooltip_text = tooltip_text
        if self._hovered and self._visible and QToolTip.isVisible():
            QToolTip.showText(QCursor.pos(), self.tooltip_text)

    def ensure_on_top(self) -> None:
        if not self._hwnd or not self._visible or not self.enabled or self.degraded:
            return
        if self._last_rect is not None:
            x, y, w, h = self._last_rect
            _user32.SetWindowPos(
                self._hwnd, _HWND_TOPMOST, x, y, w, h,
                _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
            )

    def _show_tooltip(self) -> None:
        if self._hovered and self._visible and self.tooltip_text:
            QToolTip.showText(QCursor.pos(), self.tooltip_text)

    def _on_theme_change(self) -> None:
        new_theme = read_system_uses_light_theme()
        if new_theme != self._light_theme:
            self._light_theme = new_theme
            self._last_render_key = None
            self.sync()

    def _rebind_after_explorer_restart(self) -> None:
        self._last_render_key = None
        self._last_rect = None
        self.sync()

    def _handle_win32_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> tuple[bool, int]:
        if msg == _WM_MOUSEACTIVATE:
            return True, _MA_NOACTIVATE
        if msg == _WM_LBUTTONUP:
            if self.on_left_click:
                QTimer.singleShot(0, self.on_left_click)
            return True, 0
        if msg == _WM_RBUTTONUP:
            if self.on_right_click:
                QTimer.singleShot(0, self.on_right_click)
            return True, 0
        if msg == _WM_MOUSEMOVE:
            if not self._tracking_leave:
                tme = _TRACKMOUSEEVENT(
                    ctypes.sizeof(_TRACKMOUSEEVENT), _TME_LEAVE, hwnd, _HOVER_DEFAULT
                )
                if _user32.TrackMouseEvent(ctypes.byref(tme)):
                    self._tracking_leave = True
            if not self._hovered:
                self._hovered = True
                self._hover_timer.start()
                self._last_render_key = None
                self.sync()
            return True, 0
        if msg == _WM_MOUSELEAVE:
            self._tracking_leave = False
            self._hover_timer.stop()
            QToolTip.hideText()
            if self._hovered:
                self._hovered = False
                self._last_render_key = None
                self.sync()
            return True, 0
        if msg == _WM_SETTINGCHANGE:
            QTimer.singleShot(80, self._on_theme_change)
            return False, 0
        if msg == _WM_DPICHANGED:
            self._last_render_key = None
            QTimer.singleShot(0, self.sync)
            return True, 0
        if _WM_TASKBARCREATED and msg == _WM_TASKBARCREATED:
            QTimer.singleShot(500, self._rebind_after_explorer_restart)
            return True, 0
        if msg == _WM_DESTROY:
            _INSTANCES.pop(hwnd, None)
            return False, 0
        return False, 0

    def _probe_taskbar_geometry(self) -> dict | None:
        tray_hwnd = _user32.FindWindowW("Shell_TrayWnd", None)
        if not tray_hwnd or not _user32.IsWindowVisible(tray_hwnd):
            return None
        notify_hwnd = _user32.FindWindowExW(tray_hwnd, None, "TrayNotifyWnd", None)
        if not notify_hwnd or not _user32.IsWindowVisible(notify_hwnd):
            return None
        rebar_hwnd = _user32.FindWindowExW(tray_hwnd, None, "ReBarWindow32", None)

        tray_rc = wt.RECT()
        notify_rc = wt.RECT()
        if not _user32.GetWindowRect(tray_hwnd, ctypes.byref(tray_rc)):
            return None
        if not _user32.GetWindowRect(notify_hwnd, ctypes.byref(notify_rc)):
            return None

        rebar_tuple = None
        if rebar_hwnd and _user32.IsWindowVisible(rebar_hwnd):
            rebar_rc = wt.RECT()
            if _user32.GetWindowRect(rebar_hwnd, ctypes.byref(rebar_rc)):
                rebar_tuple = (rebar_rc.left, rebar_rc.top, rebar_rc.right, rebar_rc.bottom)

        dpi = _user32.GetDpiForWindow(tray_hwnd) or 96

        # Fullscreen check on taskbar's monitor
        mon_tuple = None
        hmon = _user32.MonitorFromWindow(tray_hwnd, _MONITOR_DEFAULTTOPRIMARY)
        if hmon:
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcMonitor
                mon_tuple = (r.left, r.top, r.right, r.bottom)

        fg_hwnd = _user32.GetForegroundWindow()
        fg_tuple = None
        fg_cls = ""
        if fg_hwnd:
            buf = ctypes.create_unicode_buffer(128)
            _user32.GetClassNameW(fg_hwnd, buf, 128)
            fg_cls = buf.value
            fg_rc = wt.RECT()
            if _user32.GetWindowRect(fg_hwnd, ctypes.byref(fg_rc)):
                fg_tuple = (fg_rc.left, fg_rc.top, fg_rc.right, fg_rc.bottom)

        quns = ctypes.c_int(0)
        quns_val = quns.value if _shell32.SHQueryUserNotificationState(ctypes.byref(quns)) == 0 else None

        return {
            "tray_hwnd": int(tray_hwnd),
            "tray_rect": (tray_rc.left, tray_rc.top, tray_rc.right, tray_rc.bottom),
            "notify_rect": (notify_rc.left, notify_rc.top, notify_rc.right, notify_rc.bottom),
            "rebar_rect": rebar_tuple,
            "dpi": int(dpi),
            "mon_rect": mon_tuple,
            "fg_rect": fg_tuple,
            "fg_class": fg_cls,
            "quns_state": quns_val,
        }

    def _is_tray_above_badge(self, tray_hwnd: int) -> bool:
        """Walk GW_HWNDPREV from badge to check if Shell_TrayWnd rose above us."""
        if not self._hwnd:
            return False
        cur = _user32.GetWindow(self._hwnd, _GW_HWNDPREV)
        steps = 0
        while cur and steps < 64:
            if int(cur) == tray_hwnd:
                return True
            cur = _user32.GetWindow(cur, _GW_HWNDPREV)
            steps += 1
        return False

    def _present_image(self, img: QImage, x: int, y: int, *, raise_topmost: bool) -> None:
        if not self._hwnd:
            return
        w = img.width()
        h = img.height()
        screen_dc = _user32.GetDC(None)
        if not screen_dc:
            raise ctypes.WinError(ctypes.get_last_error())
        mem_dc = None
        hbmp = None
        old_obj = None
        try:
            mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
            if not mem_dc:
                raise ctypes.WinError(ctypes.get_last_error())
            bmi = _BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # top-down DIB matching QImage memory layout
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0
            bits = ctypes.c_void_p()
            hbmp = _gdi32.CreateDIBSection(
                screen_dc, ctypes.byref(bmi), _DIB_RGB_COLORS, ctypes.byref(bits), None, 0
            )
            if not hbmp or not bits.value:
                raise ctypes.WinError(ctypes.get_last_error())
            nbytes = img.sizeInBytes()
            dst_buf = (ctypes.c_ubyte * nbytes).from_address(bits.value)
            memoryview(dst_buf).cast("B")[:] = img.constBits()
            old_obj = _gdi32.SelectObject(mem_dc, hbmp)

            pt_dst = _POINT(x, y)
            sz = _SIZE(w, h)
            pt_src = _POINT(0, 0)
            blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
            ok = _user32.UpdateLayeredWindow(
                self._hwnd, screen_dc, ctypes.byref(pt_dst), ctypes.byref(sz),
                mem_dc, ctypes.byref(pt_src), 0, ctypes.byref(blend), _ULW_ALPHA,
            )
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if old_obj and mem_dc:
                _gdi32.SelectObject(mem_dc, old_obj)
            if hbmp:
                _gdi32.DeleteObject(hbmp)
            if mem_dc:
                _gdi32.DeleteDC(mem_dc)
            _user32.ReleaseDC(None, screen_dc)

        if raise_topmost or not self._visible:
            _user32.SetWindowPos(
                self._hwnd, _HWND_TOPMOST, x, y, w, h,
                _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
            )
            self._visible = True

    def _hide_window(self) -> None:
        if self._hwnd and self._visible:
            _user32.ShowWindow(self._hwnd, _SW_HIDE)
            self._visible = False
        self._hovered = False
        self._tracking_leave = False
        self._hover_timer.stop()
        QToolTip.hideText()

    def sync(self) -> None:
        """Lightweight position/visibility/theme/Z-order sync."""
        if not self.enabled or self.degraded or not self._hwnd:
            self._hide_window()
            return
        try:
            geo = self._probe_taskbar_geometry()
            if geo is None or should_hide_for_fullscreen(
                geo["fg_rect"], geo["fg_class"], geo["mon_rect"], geo["quns_state"]
            ):
                self._hide_window()
                return

            dpi = geo["dpi"]
            tray_h = geo["tray_rect"][3] - geo["tray_rect"][1]
            scale = max(1.0, float(dpi) / 96.0)
            target_h = min(max(24, tray_h - max(8, round(10 * scale))), max(28, round(36 * scale)))

            render_key = (
                self.percent,
                self.stale,
                self.low_threshold,
                self._light_theme,
                self._hovered,
                dpi,
                target_h,
            )
            if render_key != self._last_render_key:
                img = render_badge_image(
                    self.percent,
                    stale=self.stale,
                    light_theme=self._light_theme,
                    hovered=self._hovered,
                    dpi=dpi,
                    height_px=target_h,
                    low_threshold=self.low_threshold,
                )
                self._cached_img = img
                self._last_render_key = render_key
                rect_changed = True
            else:
                img = self._cached_img
                rect_changed = False

            rect = compute_badge_rect(
                geo["tray_rect"], geo["notify_rect"], geo["rebar_rect"],
                img.width(), img.height(), dpi,
            )
            if rect is None:
                self._hide_window()
                return

            need_topmost = (not self._visible) or self._is_tray_above_badge(geo["tray_hwnd"])
            if rect_changed or rect != self._last_rect:
                self._present_image(img, rect[0], rect[1], raise_topmost=need_topmost)
                self._last_rect = rect
            elif need_topmost:
                _user32.SetWindowPos(
                    self._hwnd, _HWND_TOPMOST, rect[0], rect[1], rect[2], rect[3],
                    _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
                )
                self._visible = True
        except Exception as exc:
            log.warning("TaskbarBadge sync degraded safely: %s", exc)
            self.degraded = True
            self._hide_window()

    def destroy(self) -> None:
        """Stop timers, hide tooltip, destroy Win32 window, and trigger taskbar repaint."""
        try:
            self._sync_timer.stop()
            self._hover_timer.stop()
            QToolTip.hideText()
            if sys.platform == "win32" and self._hwnd:
                hwnd = self._hwnd
                self._hwnd = None
                self._visible = False
                _INSTANCES.pop(hwnd, None)
                _user32.ShowWindow(hwnd, _SW_HIDE)
                _user32.DestroyWindow(hwnd)
                tray = _user32.FindWindowW("Shell_TrayWnd", None)
                if tray:
                    _user32.InvalidateRect(tray, None, False)
        except Exception:
            log.debug("TaskbarBadge destroy ignored error", exc_info=True)
