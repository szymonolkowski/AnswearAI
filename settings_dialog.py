from typing import Callable, Optional
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from config_manager import ConfigManager


class SettingsDialog(QDialog):
    """
    Application settings dialog:
    - Gemini API key and fast multimodal model selection
    - Source files folder path (PDF, TXT, DOCX, MD)
    - Overlay (X, Y) coordinates with interactive screen position calibration
    - Font size, color, opacity, and auto-hide timeout
    - Global hotkeys (Scan, Hide, Continuous mode)
    - Continuous mode settings (interval + screen change detection)
    """

    def __init__(
        self,
        config: ConfigManager,
        on_settings_saved: Callable[[], None],
        on_start_calibration: Callable[[], None],
        on_preview_overlay: Optional[Callable[[dict], None]] = None,
        parent=None
    ):
        super().__init__(parent)
        self.config = config
        self.on_settings_saved = on_settings_saved
        self.on_start_calibration = on_start_calibration
        self.on_preview_overlay = on_preview_overlay
        self._selected_color = str(self.config.get("font_color", "#909090"))

        self.setWindowTitle("AnswearPrint — Ustawienia")
        self.setMinimumWidth(480)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
            | Qt.WindowType.WindowStaysOnTopHint
        )

        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        # 1. AI & Knowledge Sources section
        ai_group = QGroupBox("1. Gemini AI oraz Pliki Źródłowe")
        ai_layout = QFormLayout(ai_group)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.api_key_input.setPlaceholderText("Wklej klucz Gemini API (lub użyj zmiennej GEMINI_API_KEY)")
        ai_layout.addRow("Klucz Gemini API:", self.api_key_input)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems([
            "gemini-4-hydrogen",
            "gemini-3.5-flash-lite",
            "gemini-4-argon"
        ])
        ai_layout.addRow("Model AI:", self.model_combo)

        self.thinking_combo = QComboBox()
        self.thinking_combo.addItems(["low", "medium", "high"])
        self.thinking_combo.setToolTip("Poziom 'low' zapewnia najszybszy czas odpowiedzi (< 5 sekund).")
        ai_layout.addRow("Poziom wnioskowania:", self.thinking_combo)

        folder_row = QHBoxLayout()
        self.folder_input = QLineEdit()
        self.folder_input.setPlaceholderText("Brak folderu (tylko Google Search)")
        browse_btn = QPushButton("Wybierz...")
        browse_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(self.folder_input)
        folder_row.addWidget(browse_btn)
        ai_layout.addRow("Folder źródłowy (PDF/DOCX/TXT/MD):", folder_row)

        self.google_search_cb = QCheckBox("Włącz Google Search Grounding (Priorytet 2, gdy brak w plikach)")
        ai_layout.addRow("", self.google_search_cb)

        main_layout.addWidget(ai_group)

        # 2. Answer Overlay Display section
        overlay_group = QGroupBox("2. Dyskretne Wyświetlanie Odpowiedzi (Overlay)")
        overlay_layout = QFormLayout(overlay_group)

        pos_row = QHBoxLayout()
        self.pos_x_spin = QSpinBox()
        self.pos_x_spin.setRange(-1, 10000)
        self.pos_x_spin.setSpecialValueText("Auto (Prawy dół)")
        self.pos_y_spin = QSpinBox()
        self.pos_y_spin.setRange(-1, 10000)
        self.pos_y_spin.setSpecialValueText("Auto (Prawy dół)")

        calibrate_btn = QPushButton("Ustaw pozycję (Kliknij na ekranie)")
        calibrate_btn.clicked.connect(self._trigger_calibration)

        pos_row.addWidget(QLabel("X:"))
        pos_row.addWidget(self.pos_x_spin)
        pos_row.addWidget(QLabel("Y:"))
        pos_row.addWidget(self.pos_y_spin)
        pos_row.addWidget(calibrate_btn)
        overlay_layout.addRow("Współrzędne:", pos_row)

        font_row = QHBoxLayout()
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 72)
        self.font_size_spin.setSuffix(" pt")

        self.color_btn = QPushButton("Kolor czcionki")
        self.color_btn.clicked.connect(self._pick_color)

        self.opacity_spin = QDoubleSpinBox()
        self.opacity_spin.setRange(0.10, 1.00)
        self.opacity_spin.setSingleStep(0.05)

        font_row.addWidget(self.font_size_spin)
        font_row.addWidget(self.color_btn)
        font_row.addWidget(QLabel("Widoczność:"))
        font_row.addWidget(self.opacity_spin)
        overlay_layout.addRow("Wygląd literki:", font_row)

        hide_row = QHBoxLayout()
        self.auto_hide_spin = QSpinBox()
        self.auto_hide_spin.setRange(1, 300)
        self.auto_hide_spin.setSuffix(" sek.")
        preview_btn = QPushButton("Pokaż podgląd literki")
        preview_btn.clicked.connect(self._trigger_preview)
        hide_row.addWidget(self.auto_hide_spin)
        hide_row.addWidget(preview_btn)
        overlay_layout.addRow("Ukryj automatycznie po:", hide_row)

        self.tray_answer_cb = QCheckBox("Wyświetlaj odpowiedź również na ikonie w zasobniku (zamiast 'A')")
        overlay_layout.addRow("", self.tray_answer_cb)

        main_layout.addWidget(overlay_group)

        # 3. Global Hotkeys & Continuous Mode section
        hotkey_group = QGroupBox("3. Skróty Klawiszowe (Hotkeys) i Tryb Ciągły")
        hotkey_layout = QFormLayout(hotkey_group)

        self.hk_scan_input = QLineEdit()
        self.hk_scan_input.setPlaceholderText("np. Ctrl+Shift+Q")
        hotkey_layout.addRow("Wykonaj skan ekranu:", self.hk_scan_input)

        self.hk_hide_input = QLineEdit()
        self.hk_hide_input.setPlaceholderText("np. Ctrl+Shift+X")
        hotkey_layout.addRow("Ukryj / wyczyść literkę:", self.hk_hide_input)

        self.hk_cont_input = QLineEdit()
        self.hk_cont_input.setPlaceholderText("np. Ctrl+Shift+C")
        hotkey_layout.addRow("Przełącz tryb ciągły:", self.hk_cont_input)

        self.continuous_cb = QCheckBox("Aktywuj tryb ciągły (auto-skanowanie w tle)")
        hotkey_layout.addRow("", self.continuous_cb)

        cont_row = QHBoxLayout()
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(4, 300)
        self.interval_spin.setSuffix(" sek.")
        self.only_on_change_cb = QCheckBox("Skanuj tylko po wykryciu zmiany na ekranie")
        cont_row.addWidget(self.interval_spin)
        cont_row.addWidget(self.only_on_change_cb)
        hotkey_layout.addRow("Interwał auto-skanu:", cont_row)

        self.startup_notif_cb = QCheckBox("Pokaż powiadomienie w zasobniku przy uruchomieniu")
        hotkey_layout.addRow("", self.startup_notif_cb)

        main_layout.addWidget(hotkey_group)

        # Save / Cancel buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("Zapisz ustawienia")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("Anuluj")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)

        main_layout.addLayout(btn_row)

    def _update_color_btn_style(self) -> None:
        self.color_btn.setText(self._selected_color)
        self.color_btn.setStyleSheet(
            f"background-color: {self._selected_color}; color: #000000; font-weight: bold; padding: 4px 8px;"
        )

    def _load_values(self) -> None:
        self.api_key_input.setText(str(self.config.get("api_key", "")))
        self.model_combo.setCurrentText(str(self.config.get("model", "gemini-4-hydrogen")))
        self.thinking_combo.setCurrentText(str(self.config.get("thinking_level", "low")))
        self.folder_input.setText(str(self.config.get("source_folder", "")))
        self.google_search_cb.setChecked(bool(self.config.get("use_google_search", True)))

        self.pos_x_spin.setValue(int(self.config.get("overlay_x", -1)))
        self.pos_y_spin.setValue(int(self.config.get("overlay_y", -1)))
        self.font_size_spin.setValue(int(self.config.get("font_size", 13)))
        self._selected_color = str(self.config.get("font_color", "#909090"))
        self._update_color_btn_style()
        self.opacity_spin.setValue(float(self.config.get("font_opacity", 0.75)))
        self.auto_hide_spin.setValue(int(self.config.get("auto_hide_seconds", 10)))
        self.tray_answer_cb.setChecked(bool(self.config.get("show_answer_on_tray_icon", True)))

        self.hk_scan_input.setText(str(self.config.get("hotkey_scan", "Ctrl+Shift+Q")))
        self.hk_hide_input.setText(str(self.config.get("hotkey_hide", "Ctrl+Shift+X")))
        self.hk_cont_input.setText(str(self.config.get("hotkey_toggle_continuous", "Ctrl+Shift+C")))
        self.continuous_cb.setChecked(bool(self.config.get("continuous_mode", False)))
        self.interval_spin.setValue(int(self.config.get("continuous_interval_sec", 12)))
        self.only_on_change_cb.setChecked(bool(self.config.get("continuous_only_on_change", True)))
        self.startup_notif_cb.setChecked(bool(self.config.get("show_startup_notification", True)))

    def _browse_folder(self) -> None:
        current = self.folder_input.text().strip()
        folder = QFileDialog.getExistingDirectory(
            self,
            "Wybierz folder z plikami źródłowymi (PDF, TXT, DOCX, MD)",
            current or ""
        )
        if folder:
            self.folder_input.setText(folder)

    def _pick_color(self) -> None:
        initial = QColor(self._selected_color)
        color = QColorDialog.getColor(initial, self, "Wybierz kolor wyświetlanej literki")
        if color.isValid():
            self._selected_color = color.name()
            self._update_color_btn_style()

    def _trigger_preview(self) -> None:
        if self.on_preview_overlay:
            self.on_preview_overlay({
                "overlay_x": self.pos_x_spin.value(),
                "overlay_y": self.pos_y_spin.value(),
                "font_size": self.font_size_spin.value(),
                "font_color": self._selected_color,
                "font_opacity": round(self.opacity_spin.value(), 2),
                "auto_hide_seconds": self.auto_hide_spin.value(),
                "show_answer_on_tray_icon": self.tray_answer_cb.isChecked(),
            })

    def _trigger_calibration(self) -> None:
        self.hide()
        if self.on_start_calibration:
            self.on_start_calibration()

    def update_calibrated_coordinates(self, x: int, y: int) -> None:
        self.pos_x_spin.setValue(x)
        self.pos_y_spin.setValue(y)
        self.show()
        self.raise_()
        self.activateWindow()

    def _save(self) -> None:
        hk_scan = self.hk_scan_input.text().strip() or "Ctrl+Shift+Q"
        hk_hide = self.hk_hide_input.text().strip() or "Ctrl+Shift+X"
        hk_cont = self.hk_cont_input.text().strip() or "Ctrl+Shift+C"

        self.config.update({
            "api_key": self.api_key_input.text().strip(),
            "model": self.model_combo.currentText().strip() or "gemini-4-hydrogen",
            "thinking_level": self.thinking_combo.currentText().strip() or "low",
            "source_folder": self.folder_input.text().strip(),
            "use_google_search": self.google_search_cb.isChecked(),
            "overlay_x": self.pos_x_spin.value(),
            "overlay_y": self.pos_y_spin.value(),
            "font_size": self.font_size_spin.value(),
            "font_color": self._selected_color,
            "font_opacity": round(self.opacity_spin.value(), 2),
            "auto_hide_seconds": self.auto_hide_spin.value(),
            "show_answer_on_tray_icon": self.tray_answer_cb.isChecked(),
            "hotkey_scan": hk_scan,
            "hotkey_hide": hk_hide,
            "hotkey_toggle_continuous": hk_cont,
            "continuous_mode": self.continuous_cb.isChecked(),
            "continuous_interval_sec": self.interval_spin.value(),
            "continuous_only_on_change": self.only_on_change_cb.isChecked(),
            "show_startup_notification": self.startup_notif_cb.isChecked(),
        }, auto_save=True)

        if self.on_settings_saved:
            self.on_settings_saved()
        self.accept()
