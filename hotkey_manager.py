import ctypes
import ctypes.wintypes
import sys
from typing import Callable, Dict, Tuple
from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, pyqtSignal
from PyQt6.QtWidgets import QWidget

try:
    import keyboard
except ImportError:
    keyboard = None

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_win32_hk_configured = False


def _configure_win32_hotkey_api() -> bool:
    global _win32_hk_configured
    if sys.platform != "win32":
        return False
    if not _win32_hk_configured:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.RegisterHotKey.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.c_int,
            ctypes.wintypes.UINT,
            ctypes.wintypes.UINT,
        ]
        user32.RegisterHotKey.restype = ctypes.wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.c_int,
        ]
        user32.UnregisterHotKey.restype = ctypes.wintypes.BOOL
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = ctypes.wintypes.DWORD
        _win32_hk_configured = True
    return True

VK_MAP: Dict[str, int] = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "DEL": 0x2E,
}
# Digits 0-9 and letters A-Z
for c in range(ord("0"), ord("9") + 1):
    VK_MAP[chr(c)] = c
for c in range(ord("A"), ord("Z") + 1):
    VK_MAP[chr(c)] = c
# Function keys F1-F12
for i in range(1, 13):
    VK_MAP[f"F{i}"] = 0x6F + i


def parse_win32_hotkey(hotkey_str: str) -> Tuple[int, int]:
    """Convert a string like 'Ctrl+Shift+Q' into (modifiers, vk_code) for Win32 RegisterHotKey."""
    parts = [p.strip().upper() for p in hotkey_str.replace(" ", "").split("+") if p.strip()]
    mods = MOD_NOREPEAT
    vk = 0
    for part in parts:
        if part in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif part == "SHIFT":
            mods |= MOD_SHIFT
        elif part == "ALT":
            mods |= MOD_ALT
        elif part in ("WIN", "WINDOWS", "SUPER"):
            mods |= MOD_WIN
        elif part in VK_MAP:
            vk = VK_MAP[part]
    return mods, vk


class Win32HotkeyFilter(QAbstractNativeEventFilter):
    """Intercept WM_HOTKEY messages directly from the Windows event loop."""

    def __init__(self, callback_map: Dict[int, Callable[[], None]]):
        super().__init__()
        self.callback_map = callback_map

    def nativeEventFilter(self, event_type, message):
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY:
                    hotkey_id = int(msg.wParam)
                    cb = self.callback_map.get(hotkey_id)
                    if cb:
                        cb()
                        return True, 0
            except Exception:
                pass
        return False, 0


class GlobalHotkeyManager(QObject):
    """
    Register system-wide background hotkeys on Windows.
    Combines native Win32 RegisterHotKey with an optional `keyboard` library fallback,
    emitting Qt signals safely onto the main UI thread.
    """

    scan_triggered = pyqtSignal()
    hide_triggered = pyqtSignal()
    toggle_continuous_triggered = pyqtSignal()

    def __init__(self, app):
        super().__init__()
        self.app = app
        self._registered_win32_ids = []
        self._hotkey_window = QWidget()
        self._hwnd = int(self._hotkey_window.winId())
        self._callback_map: Dict[int, Callable[[], None]] = {
            101: self.scan_triggered.emit,
            102: self.hide_triggered.emit,
            103: self.toggle_continuous_triggered.emit,
        }
        self._event_filter = Win32HotkeyFilter(self._callback_map)
        self.app.installNativeEventFilter(self._event_filter)

    def register_all(self, hk_scan: str, hk_hide: str, hk_continuous: str) -> Dict[str, str]:
        self.unregister_all()
        bindings = [
            (101, hk_scan, self.scan_triggered.emit),
            (102, hk_hide, self.hide_triggered.emit),
            (103, hk_continuous, self.toggle_continuous_triggered.emit),
        ]

        statuses: Dict[str, str] = {}
        win32_ok = _configure_win32_hotkey_api()
        user32 = ctypes.windll.user32 if win32_ok else None
        kernel32 = ctypes.windll.kernel32 if win32_ok else None

        for hk_id, hk_str, signal_emitter in bindings:
            if not hk_str:
                continue
            mods, vk = parse_win32_hotkey(hk_str)
            registered = False
            win32_err = 0

            if win32_ok and vk != 0:
                try:
                    if user32.RegisterHotKey(self._hwnd, hk_id, mods, vk):
                        self._registered_win32_ids.append(hk_id)
                        statuses[hk_str] = "Win32"
                        registered = True
                    else:
                        win32_err = int(kernel32.GetLastError())
                except Exception as e:
                    win32_err = str(e)

            # Fall back to the `keyboard` library if Win32 RegisterHotKey failed (e.g. shortcut already in use)
            if not registered:
                if keyboard is not None:
                    normalized_hk = hk_str.replace(" ", "").lower()
                    try:
                        keyboard.add_hotkey(normalized_hk, signal_emitter)
                        statuses[hk_str] = "keyboard (fallback)"
                        registered = True
                    except Exception as e:
                        statuses[hk_str] = f"błąd: Win32={win32_err}, keyboard={e}"
                        print(f"[GlobalHotkeyManager] Nie udało się zarejestrować skrótu '{hk_str}': {e}")
                else:
                    statuses[hk_str] = f"błąd: Win32={win32_err}"

        return statuses

    def unregister_all(self) -> None:
        if _configure_win32_hotkey_api():
            user32 = ctypes.windll.user32
            for hk_id in self._registered_win32_ids:
                try:
                    user32.UnregisterHotKey(self._hwnd, hk_id)
                except Exception:
                    pass
        self._registered_win32_ids.clear()

        if keyboard is not None:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
