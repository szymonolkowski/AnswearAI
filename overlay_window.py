import ctypes
import ctypes.wintypes
import sys
from typing import Any, Callable, Dict, Optional
from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from config_manager import ConfigManager

HWND_TOPMOST = ctypes.wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

_win32_configured = False
_get_window_long_ptr = None
_set_window_long_ptr = None
_set_window_pos = None


def _ensure_win32_funcs() -> bool:
    """Configure Win32 API signatures (including 64-bit HWND pointers)."""
    global _win32_configured, _get_window_long_ptr, _set_window_long_ptr, _set_window_pos
    if _win32_configured:
        return _set_window_pos is not None
    _win32_configured = True

    if sys.platform != "win32":
        return False

    try:
        user32 = ctypes.windll.user32
        get_fn = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_fn = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        swp_fn = user32.SetWindowPos

        get_fn.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        get_fn.restype = ctypes.c_ssize_t

        set_fn.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_fn.restype = ctypes.c_ssize_t

        swp_fn.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.wintypes.UINT,
        ]
        swp_fn.restype = ctypes.wintypes.BOOL

        _get_window_long_ptr = get_fn
        _set_window_long_ptr = set_fn
        _set_window_pos = swp_fn
        return True
    except Exception:
        return False


class AnswerOverlay(QWidget):
    """
    Frameless, transparent Always-on-Top and Click-Through overlay window
    (WindowTransparentForInput + WindowStaysOnTopHint) displaying only a
    discreet lowercase answer letter (e.g. 'a', 'b', 'c', 'd', or '?').
    """

    answer_cleared = pyqtSignal()

    def __init__(self, config: ConfigManager):
        super().__init__(None)
        self.config = config
        self._current_text = ""

        # Window flags ensuring frameless, no taskbar entry, always-on-top, and click-through behavior
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.label = QLabel("", self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.clear_answer)

        self.topmost_timer = QTimer(self)
        self.topmost_timer.setInterval(500)
        self.topmost_timer.timeout.connect(self._ensure_topmost)

        self.apply_config()

    def _apply_win32_click_through(self) -> None:
        """
        Enforce Win32 extended styles (WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        and HWND_TOPMOST z-order so the overlay stays above the Windows 11 taskbar and passes all mouse clicks through.
        """
        if not _ensure_win32_funcs():
            return
        try:
            hwnd = int(self.winId())
            style = _get_window_long_ptr(hwnd, GWL_EXSTYLE)
            new_style = style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            _set_window_long_ptr(hwnd, GWL_EXSTYLE, new_style)
            _set_window_pos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
        except Exception:
            pass

    def _ensure_topmost(self) -> None:
        """Keep the overlay positioned above the Windows 11 taskbar and other windows."""
        if not self.isVisible() or not _ensure_win32_funcs():
            return
        try:
            hwnd = int(self.winId())
            _set_window_pos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except Exception:
            pass

    def get_box_size(self, overrides: Optional[Dict[str, Any]] = None) -> int:
        font_size = int(
            overrides["font_size"]
            if overrides and "font_size" in overrides
            else self.config.get("font_size", 13)
        )
        return max(font_size * 2 + 8, 28)

    def get_default_bottom_right_position(self, overrides: Optional[Dict[str, Any]] = None) -> QPoint:
        """
        Compute the default position in the bottom-right corner next to the taskbar clock.
        """
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return QPoint(1750, 1045)
        full_geo = screen.geometry()
        avail_geo = screen.availableGeometry()

        # When the taskbar is at the bottom, place the letter immediately to the left of the system clock
        box_size = self.get_box_size(overrides)

        x = full_geo.right() - 145
        if avail_geo.bottom() < full_geo.bottom():
            # Bottom taskbar: vertically center the letter next to the clock
            taskbar_height = full_geo.bottom() - avail_geo.bottom()
            y = avail_geo.bottom() + max((taskbar_height - box_size) // 2, 2)
        else:
            # Hidden or non-bottom taskbar: bottom-right corner of the screen
            y = full_geo.bottom() - box_size - 8

        return QPoint(x, y)

    def apply_config(self, overrides: Optional[Dict[str, Any]] = None) -> None:
        def _val(key: str, default: Any) -> Any:
            if overrides and key in overrides:
                return overrides[key]
            return self.config.get(key, default)

        font_size = int(_val("font_size", 13))
        font_color = str(_val("font_color", "#909090"))
        opacity = float(_val("font_opacity", 0.75))

        font = QFont("Segoe UI", font_size, QFont.Weight.DemiBold)
        self.label.setFont(font)

        color = QColor(font_color)
        if not color.isValid():
            color = QColor("#909090")
        rgba_str = f"rgba({color.red()}, {color.green()}, {color.blue()}, {int(max(0.1, min(1.0, opacity)) * 255)})"
        self.label.setStyleSheet(f"color: {rgba_str}; background: transparent;")

        box_size = self.get_box_size(overrides)
        self.resize(box_size, box_size)
        self.label.setGeometry(0, 0, box_size, box_size)

        x = int(_val("overlay_x", -1))
        y = int(_val("overlay_y", -1))
        if x < 0 or y < 0:
            pos = self.get_default_bottom_right_position(overrides)
            self.move(pos.x(), pos.y())
        else:
            self.move(x, y)

    def show_answer(self, letter: str, overrides: Optional[Dict[str, Any]] = None) -> None:
        """
        Display a single lowercase letter (or loading dot) and start the auto-hide timer.
        """
        self.hide_timer.stop()
        self.apply_config(overrides=overrides)

        self._current_text = letter
        self.label.setText(letter)
        self.show()
        self.raise_()
        self._apply_win32_click_through()
        self.topmost_timer.start()

        auto_hide_sec = float(
            overrides["auto_hide_seconds"]
            if overrides and "auto_hide_seconds" in overrides
            else self.config.get("auto_hide_seconds", 10)
        )
        if auto_hide_sec > 0 and letter != "·":
            self.hide_timer.start(int(auto_hide_sec * 1000))

    def clear_answer(self) -> None:
        """Immediately hide and clear the displayed answer letter."""
        self.hide_timer.stop()
        self.topmost_timer.stop()
        self._current_text = ""
        self.label.setText("")
        self.hide()
        self.answer_cleared.emit()


class PositionCalibrationOverlay(QWidget):
    """
    Full-screen translucent overlay for interactive (X, Y) position calibration.
    The user clicks anywhere on the screen to save the answer display coordinates.
    """

    def __init__(self, on_position_selected: Callable[[int, int], None]):
        super().__init__(None)
        self.on_position_selected = on_position_selected

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.CrossCursor)

        # Span the entire virtual desktop (multi-monitor support)
        virtual_geo = QGuiApplication.primaryScreen().virtualGeometry()
        self.setGeometry(virtual_geo)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Subtle screen dimming
        painter.fillRect(self.rect(), QColor(0, 0, 0, 75))

        # Centered on-screen instructions
        painter.setPen(QPen(QColor("#FFFFFF")))
        font = QFont("Segoe UI", 14, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(
            self.rect(),
            Qt.AlignmentFlag.AlignCenter,
            "TRYB KALIBRACJI POZYCJI\n\n"
            "Kliknij lewym przyciskiem myszy w dowolne miejsce na ekranie,\n"
            "w którym ma pojawiać się dyskretna literka z odpowiedzią.\n"
            "(Naciśnij ESC lub prawy przycisk myszy, aby anulować)"
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            global_pos = event.globalPosition().toPoint()
            self.close()
            if self.on_position_selected:
                self.on_position_selected(global_pos.x(), global_pos.y())
        elif event.button() == Qt.MouseButton.RightButton:
            self.close()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
