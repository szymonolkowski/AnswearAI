import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_FILE = "config.json"


def get_app_dir() -> Path:
    """Return the application directory (the executable folder when frozen or the script folder)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DEFAULT_CONFIG: Dict[str, Any] = {
    "api_key": "",
    "model": "gemini-4-hydrogen",
    "source_folder": "",
    "overlay_x": -1,  # -1 means auto-calculate bottom-right near system clock
    "overlay_y": -1,
    "font_size": 13,
    "font_color": "#909090",
    "font_opacity": 0.75,
    "auto_hide_seconds": 10,
    "hotkey_scan": "Ctrl+Shift+Q",
    "hotkey_hide": "Ctrl+Shift+X",
    "hotkey_toggle_continuous": "Ctrl+Shift+C",
    "continuous_mode": False,
    "continuous_interval_sec": 12,
    "continuous_only_on_change": True,
    "change_threshold_percent": 3.0,
    "thinking_level": "low",
    "use_google_search": True,
    "show_startup_notification": True,
    "show_answer_on_tray_icon": True,
}


class ConfigManager:
    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = get_app_dir() / CONFIG_FILE
        self.data: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> Dict[str, Any]:
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.data.update(loaded)
            except Exception as e:
                print(f"[ConfigManager] Błąd wczytywania config.json: {e}")
        else:
            self.save()
        return self.data

    def save(self) -> None:
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[ConfigManager] Błąd zapisu config.json: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULT_CONFIG.get(key, default))

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.data[key] = value
        if auto_save:
            self.save()

    def update(self, new_values: Dict[str, Any], auto_save: bool = True) -> None:
        self.data.update(new_values)
        if auto_save:
            self.save()
