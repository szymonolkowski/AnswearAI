import ctypes
import os
import signal
import sys
import threading
from typing import Dict, Optional
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QFileDialog, QMenu, QSystemTrayIcon

from ai_client import GeminiQuizAnalyzer
from config_manager import ConfigManager
from hotkey_manager import GlobalHotkeyManager
from overlay_window import AnswerOverlay, PositionCalibrationOverlay
from screen_capture import ScreenCapturer
from settings_dialog import SettingsDialog


def create_tray_icon(
    active_continuous: bool = False,
    display_char: str = "A",
    state: str = "idle",
) -> QIcon:
    """Render a multi-size, DPI-aware icon in RAM for the Windows System Tray."""
    icon = QIcon()
    if state == "scanning":
        bg_color = QColor("#546E7A")
    elif state == "answer":
        bg_color = QColor("#00897B")
    elif state == "unknown":
        bg_color = QColor("#757575")
    else:
        bg_color = QColor("#2E7D32") if active_continuous else QColor("#1E88E5")

    char = (display_char or "A").strip()[:1] or "A"

    for size in (16, 24, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        margin = max(1, size // 10)
        radius = max(3, size // 5)
        painter.setBrush(bg_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(margin, margin, size - 2 * margin, size - 2 * margin, radius, radius)

        painter.setPen(QColor("#FFFFFF"))
        font = QFont("Segoe UI")
        font.setBold(True)
        font.setPixelSize(max(10, int(size * 0.62)))
        font.setStyleStrategy(QFont.StyleStrategy.NoSubpixelAntialias)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, char)
        painter.end()

        icon.addPixmap(pixmap)

    return icon


class AnswearPrintController(QObject):
    """
    Main background controller for the System Tray application:
    - Manages in-memory screen captures and asynchronous Gemini API requests
    - Handles the System Tray context menu (Settings, Source Folder, Set Position, Continuous Mode, Quit)
    - Coordinates global hotkeys and the overlay window
    """

    scan_started = pyqtSignal()
    answer_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    status_updated = pyqtSignal(str)

    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.config = ConfigManager()
        self.capturer = ScreenCapturer()
        self.analyzer = GeminiQuizAnalyzer()
        self.overlay = AnswerOverlay(self.config)
        self.hotkey_manager = GlobalHotkeyManager(self.app)

        self._is_busy = False
        self._lock = threading.Lock()
        self._settings_dialog: Optional[SettingsDialog] = None
        self._calibration_overlay: Optional[PositionCalibrationOverlay] = None
        self._last_tray_icon_key: Optional[tuple] = None
        self._current_tray_state: str = "idle"
        self._calibration_from_settings: bool = False
        self._hotkey_statuses: Dict[str, str] = {}

        # Connect worker thread signals to the main UI thread
        self.scan_started.connect(self._on_scan_started)
        self.answer_ready.connect(self._on_answer_ready)
        self.error_occurred.connect(self._on_error_occurred)
        self.status_updated.connect(self._on_status_updated)
        self.overlay.answer_cleared.connect(self._reset_tray_icon_to_idle)

        # Connect global hotkeys
        self.hotkey_manager.scan_triggered.connect(lambda: self.trigger_scan(manual=True))
        self.hotkey_manager.hide_triggered.connect(self.overlay.clear_answer)
        self.hotkey_manager.toggle_continuous_triggered.connect(self.toggle_continuous_mode)

        # Continuous mode (auto-scan) timer
        self.continuous_timer = QTimer(self)
        self.continuous_timer.timeout.connect(lambda: self.trigger_scan(manual=False))

        self._init_tray()
        self.apply_all_settings()
        self._print_startup_diagnostics()

        if bool(self.config.get("show_startup_notification", True)):
            QTimer.singleShot(500, self._show_startup_notification)

    def _set_tray_icon(self, display_char: str = "A", state: str = "idle") -> None:
        is_continuous = bool(self.config.get("continuous_mode", False))
        key = (is_continuous, display_char, state)
        if key != self._last_tray_icon_key:
            icon = create_tray_icon(
                active_continuous=is_continuous,
                display_char=display_char,
                state=state,
            )
            self.app.setWindowIcon(icon)
            self.tray.setIcon(icon)
            self._last_tray_icon_key = key
            self._current_tray_state = state

    def _reset_tray_icon_to_idle(self) -> None:
        self._set_tray_icon(display_char="A", state="idle")

    def _on_scan_started(self) -> None:
        if bool(self.config.get("show_answer_on_tray_icon", True)):
            self._set_tray_icon(display_char="·", state="scanning")

    def _init_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[AnswearPrint] OSTRZEŻENIE: Zasobnik systemowy (System Tray) nie jest dostępny w tym środowisku!")

        self.tray = QSystemTrayIcon(self.app)
        self._set_tray_icon(display_char="A", state="idle")
        self.tray.setToolTip("AnswearPrint — Dyskretny asystent testowy AI")

        self.menu = QMenu()

        self.action_scan_now = QAction("Skanuj ekran teraz", self.menu)
        self.action_scan_now.triggered.connect(lambda: self.trigger_scan(manual=True))
        self.menu.addAction(self.action_scan_now)

        self.action_continuous = QAction("Tryb ciągły (Auto-skanowanie)", self.menu)
        self.action_continuous.setCheckable(True)
        self.action_continuous.triggered.connect(self._on_tray_continuous_toggled)
        self.menu.addAction(self.action_continuous)

        self.menu.addSeparator()

        # Required tray menu items: Settings, Select source folder, Set answer display position, Quit
        self.action_settings = QAction("Ustawienia", self.menu)
        self.action_settings.triggered.connect(self.open_settings)
        self.menu.addAction(self.action_settings)

        self.action_folder = QAction("Wybierz folder z plikami źródłowymi", self.menu)
        self.action_folder.triggered.connect(self.select_source_folder)
        self.menu.addAction(self.action_folder)

        self.action_position = QAction("Ustaw pozycję wyświetlania odpowiedzi", self.menu)
        self.action_position.triggered.connect(lambda: self.start_position_calibration(from_settings=False))
        self.menu.addAction(self.action_position)

        self.menu.addSeparator()

        self.action_quit = QAction("Zakończ", self.menu)
        self.action_quit.triggered.connect(self.quit_app)
        self.menu.addAction(self.action_quit)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(self.open_settings)
        self.tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_settings()

    def _print_startup_diagnostics(self) -> None:
        api_key = str(self.config.get("api_key", "")).strip() or os.environ.get("GEMINI_API_KEY", "").strip()
        api_status = "skonfigurowany" if api_key else "BRAK (ustaw w Ustawieniach)"
        print("[AnswearPrint] Aplikacja uruchomiona w zasobniku systemowym (System Tray).")
        print(f"[AnswearPrint] Tray dostępny: {QSystemTrayIcon.isSystemTrayAvailable()}, widoczny: {self.tray.isVisible()}")
        print(
            "[AnswearPrint] W Windows 11 nowa ikona 'A' może być domyślnie ukryta pod strzałką '^' "
            "(Pokaż ukryte ikony) obok zegara — kliknij '^' i przeciągnij ikonę 'A' na pasek zadań, aby była zawsze widoczna."
        )
        print(f"[AnswearPrint] Plik konfiguracji: {self.config.config_path}")
        print(f"[AnswearPrint] Klucz Gemini API: {api_status}")
        print(f"[AnswearPrint] Skróty klawiszowe: {self._hotkey_statuses}")

    def _show_startup_notification(self) -> None:
        if not QSystemTrayIcon.supportsMessages() or not self.tray.isVisible():
            return
        hk_scan = str(self.config.get("hotkey_scan", "Ctrl+Shift+Q"))
        api_key = str(self.config.get("api_key", "")).strip() or os.environ.get("GEMINI_API_KEY", "").strip()
        if api_key:
            msg = (
                f"Ikona 'A' znajduje się w zasobniku (sprawdź menu '^' obok zegara). "
                f"Skanuj: {hk_scan}. Kliknij to powiadomienie, aby otworzyć Ustawienia."
            )
        else:
            msg = (
                f"Ikona 'A' znajduje się w zasobniku (sprawdź menu '^' obok zegara). "
                f"Brak klucza Gemini API — kliknij to powiadomienie, aby otworzyć Ustawienia. Skan: {hk_scan}."
            )
        self.tray.showMessage("AnswearPrint działa w tle", msg, self.tray.icon(), 6000)

    def apply_all_settings(self) -> None:
        """Refresh global hotkeys, overlay position/styling, and the continuous mode timer."""
        hk_scan = str(self.config.get("hotkey_scan", "Ctrl+Shift+Q"))
        hk_hide = str(self.config.get("hotkey_hide", "Ctrl+Shift+X"))
        hk_cont = str(self.config.get("hotkey_toggle_continuous", "Ctrl+Shift+C"))

        self._hotkey_statuses = self.hotkey_manager.register_all(hk_scan, hk_hide, hk_cont)
        self.action_scan_now.setText(f"Skanuj ekran teraz ({hk_scan})")

        self.overlay.apply_config()

        is_continuous = bool(self.config.get("continuous_mode", False))
        self.action_continuous.setChecked(is_continuous)
        if self._current_tray_state == "idle" or not bool(self.config.get("show_answer_on_tray_icon", True)):
            self._set_tray_icon(display_char="A", state="idle")

        if is_continuous:
            interval_ms = max(4, int(self.config.get("continuous_interval_sec", 12))) * 1000
            self.continuous_timer.start(interval_ms)
        else:
            self.continuous_timer.stop()

    def open_settings(self) -> None:
        if self._settings_dialog is not None and self._settings_dialog.isVisible():
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return

        self._settings_dialog = SettingsDialog(
            config=self.config,
            on_settings_saved=self._on_settings_saved,
            on_start_calibration=lambda: self.start_position_calibration(from_settings=True),
            on_preview_overlay=self._preview_overlay,
        )
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _preview_overlay(self, overrides: dict) -> None:
        self.overlay.show_answer("a", overrides=overrides)
        if bool(overrides.get("show_answer_on_tray_icon", self.config.get("show_answer_on_tray_icon", True))):
            self._set_tray_icon(display_char="A", state="answer")

    def _on_settings_saved(self) -> None:
        self.analyzer.context_manager.invalidate()
        self.apply_all_settings()

    def select_source_folder(self) -> None:
        current = str(self.config.get("source_folder", ""))
        folder = QFileDialog.getExistingDirectory(
            None,
            "Wybierz folder z plikami źródłowymi (PDF, TXT, DOCX, MD)",
            current or ""
        )
        if folder:
            self.config.set("source_folder", folder, auto_save=True)
            self.analyzer.context_manager.invalidate()
            self.tray.setToolTip(f"AnswearPrint — Folder: {folder}")

    def start_position_calibration(self, from_settings: bool = False) -> None:
        self._calibration_from_settings = from_settings
        self._calibration_overlay = PositionCalibrationOverlay(
            on_position_selected=self._on_position_calibrated
        )
        self._calibration_overlay.show()
        self._calibration_overlay.raise_()
        self._calibration_overlay.activateWindow()

    def _on_position_calibrated(self, x: int, y: int) -> None:
        box_size = self.overlay.get_box_size()
        target_x = max(0, x - box_size // 2)
        target_y = max(0, y - box_size // 2)

        self.config.update({"overlay_x": target_x, "overlay_y": target_y}, auto_save=True)
        self.overlay.apply_config()
        # Briefly preview the letter 'a' at the newly selected position
        self.overlay.show_answer("a")

        if self._calibration_from_settings and self._settings_dialog is not None:
            self._settings_dialog.update_calibrated_coordinates(target_x, target_y)
        self._calibration_from_settings = False

    def _on_tray_continuous_toggled(self, checked: bool) -> None:
        self.config.set("continuous_mode", checked, auto_save=True)
        self.capturer.reset_change_tracker()
        self.apply_all_settings()

    def toggle_continuous_mode(self) -> None:
        new_state = not bool(self.config.get("continuous_mode", False))
        self.config.set("continuous_mode", new_state, auto_save=True)
        self.capturer.reset_change_tracker()
        self.apply_all_settings()

    def trigger_scan(self, manual: bool = True) -> None:
        """
        Run screen capture and AI analysis asynchronously on a background thread
        so the UI event loop and Windows stay responsive.
        """
        with self._lock:
            if self._is_busy:
                return
            self._is_busy = True

        # Clear the previous overlay letter before capturing the screen
        if manual:
            self.overlay.clear_answer()
            if bool(self.config.get("show_answer_on_tray_icon", True)):
                self._set_tray_icon(display_char="·", state="scanning")

        worker = threading.Thread(
            target=self._background_scan_worker,
            args=(manual,),
            daemon=True
        )
        worker.start()

    def _background_scan_worker(self, manual: bool) -> None:
        try:
            jpeg_bytes, thumbnail = self.capturer.capture_jpeg_bytes()

            # In continuous mode, skip analysis unless the screen image actually changed
            if not manual and bool(self.config.get("continuous_only_on_change", True)):
                threshold = float(self.config.get("change_threshold_percent", 3.0))
                if not self.capturer.has_screen_changed(thumbnail, threshold_percent=threshold):
                    return

            self.scan_started.emit()
            answer = self.analyzer.analyze_screenshot(
                jpeg_bytes=jpeg_bytes,
                api_key=str(self.config.get("api_key", "")),
                model=str(self.config.get("model", "gemini-4-hydrogen")),
                source_folder=str(self.config.get("source_folder", "")),
                thinking_level=str(self.config.get("thinking_level", "low")),
                use_google_search=bool(self.config.get("use_google_search", True)),
                timeout_seconds=30.0,
                status_callback=lambda msg: self.status_updated.emit(msg),
            )
            self.answer_ready.emit(answer)
        except Exception as e:
            self.error_occurred.emit(str(e))
        finally:
            with self._lock:
                self._is_busy = False

    def _on_answer_ready(self, letter: str) -> None:
        self.overlay.show_answer(letter)
        if bool(self.config.get("show_answer_on_tray_icon", True)):
            if letter == "?":
                self._set_tray_icon(display_char="?", state="unknown")
            else:
                self._set_tray_icon(display_char=letter.upper(), state="answer")
        self.tray.setToolTip(f"AnswearPrint — Ostatnia odpowiedź: {letter.upper()}")

    def _on_error_occurred(self, error_msg: str) -> None:
        print(f"[AnswearPrint] Błąd analizy: {error_msg}")
        self.overlay.show_answer("?")
        if bool(self.config.get("show_answer_on_tray_icon", True)):
            self._set_tray_icon(display_char="?", state="unknown")
        self.tray.setToolTip(f"AnswearPrint — Błąd: {error_msg[:80]}")

    def _on_status_updated(self, status_msg: str) -> None:
        self.tray.setToolTip(f"AnswearPrint — {status_msg}")

    def quit_app(self) -> None:
        self.continuous_timer.stop()
        self.hotkey_manager.unregister_all()
        self.overlay.close()
        self.tray.hide()
        self.app.quit()


def main() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AnswearPrint.Desktop.App")
        except Exception:
            pass

    app = QApplication(sys.argv)
    # Keep the app running in the System Tray even after closing the Settings dialog
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("AnswearPrint")

    _controller = AnswearPrintController(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
