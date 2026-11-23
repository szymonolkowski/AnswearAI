# AnswearPrint — Lightweight AI Multiple-Choice Overlay Assistant for Windows

**AnswearPrint** is a fast, lightweight Windows desktop application running quietly in the System Tray. It captures your screen directly in RAM using `mss`, analyzes multiple-choice questions via the **Google Gemini Interactions API** (`google-genai`), and discreetly displays the correct option letter (`a`–`f` or `?`) in a transparent, click-through overlay and on the System Tray icon.

---

## Key Features

1. **Background Operation (System Tray):**
   - Runs without a main taskbar window — sits cleanly as an icon in the Windows System Tray.
   - Right-click context menu:
     - **Scan screen now** (with active hotkey shown)
     - **Continuous mode (Auto-scan)**
     - **Settings**
     - **Select source files folder**
     - **Set answer display position**
     - **Quit**

2. **Global Hotkeys:**
   - `Ctrl+Shift+Q` (default) — immediately captures the screen in RAM and sends it for AI analysis.
   - `Ctrl+Shift+X` (default) — immediately hides/clears the displayed answer letter and resets the tray icon.
   - `Ctrl+Shift+C` (default) — toggles continuous auto-scan mode.
   - All shortcuts are configurable in **Settings** and registered natively via Win32 `RegisterHotKey` (with a `keyboard` library fallback).

3. **Fast In-Memory Screen Capture (`mss`):**
   - Captures the display directly into RAM (`io.BytesIO` JPEG) with zero temporary files written to disk.
   - Generates a `64x64` grayscale thumbnail diff to detect screen changes in continuous mode so idle screens do not trigger unnecessary API requests.

4. **Two-Tier AI Knowledge & Shuffled/Unlabeled Option Matching (`google-genai`):**
   - Uses `client.interactions.create` with the fast multimodal **`gemini-4-hydrogen`** model (and `thinking_level="low"` for fast responses under a strict 30-second timeout).
   - **Priority 1 (Local User Files):** Loads `PDF`, `TXT`, `DOCX`, and `MD` files from a chosen folder. PDFs are uploaded once via the **Gemini Files API** (`client.files.upload`) and cached in `.gemini_file_cache.json` for 48 hours; `TXT`, `MD`, and `DOCX` files (preserving bold `**...**` and highlighted `[*...*]` answer formatting) benefit from automatic implicit context caching.
   - **Priority 2 (Google Search Grounding):** When the answer is not present in your local files, the model automatically uses built-in `google_search` grounding.
   - **Structured Content-Based Matching:** Even when on-screen options have no `A/B/C/D` prefixes (only bullets, radio buttons, or plain lines) or appear in a different order than in your study files, the model outputs structured JSON (`screen_options` in exact on-screen order + `correct_answer_text`) and Python deterministically matches the answer text to its on-screen position (`a`–`f` or `?`).

5. **Discreet Click-Through Overlay, Calibration & Dynamic Tray Icon:**
   - Frameless, transparent `PyQt6` overlay using `WindowTransparentForInput`, `WindowStaysOnTopHint`, `Tool`, and 64-bit Win32 `WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE` + `HWND_TOPMOST`.
   - Default placement sits in the bottom-right corner next to the system clock, with an interactive full-screen click calibration mode and configurable font size, color, opacity, and auto-hide timeout.
   - **Dynamic System Tray Icon:** Displays `A` at idle (blue, or green in continuous mode), `·` while scanning, the uppercase answer letter (`A`–`F` on teal) when ready, and automatically reverts to `A` when cleared.

---

## Project Structure

- `main.py` — Application entry point, tray icon rendering, background worker threads, and `AnswearPrintController`.
- `ai_client.py` — `KnowledgeContextManager` and `GeminiQuizAnalyzer` (`google-genai` Interactions API + Files API + structured option matching).
- `screen_capture.py` — In-memory `mss` capture and `64x64` screen-change detection.
- `overlay_window.py` — Click-through `AnswerOverlay` and `PositionCalibrationOverlay`.
- `hotkey_manager.py` — Global Win32 `RegisterHotKey` + `keyboard` fallback manager.
- `settings_dialog.py` — Configuration GUI with live overlay preview and coordinate calibration.
- `config_manager.py` — Persistent JSON configuration manager.
- `config.example.json` — Safe template configuration file (no API key).
- `requirements.txt` — Python package dependencies.

---

## Setup & Running (Virtual Environment)

1. Create and activate a Python virtual environment:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install dependencies:
   ```powershell
   .\.venv\Scripts\pip.exe install -r requirements.txt
   ```

3. Create your local `config.json` from the template (never commit `config.json` — it is listed in `.gitignore` so your API key stays private):
   ```powershell
   Copy-Item config.example.json config.json
   ```
   Alternatively, set the `GEMINI_API_KEY` environment variable or paste your key in the **Settings** dialog after launch:
   ```powershell
   $env:GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
   ```

4. Run the application:
   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

---

## Building a Standalone `.exe` (PyInstaller)

To bundle AnswearPrint into a single background Windows executable (`--windowed`, no console window):

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --name "AnswearPrint" main.py
```

The executable will be created at:
```text
dist\AnswearPrint.exe
```

> **Note:** `config.json` is automatically read from and saved next to `AnswearPrint.exe`.

---

## Troubleshooting (Windows 11 — Hidden System Tray Icon)

By default, Windows 11 places newly registered tray icons inside the hidden icons overflow menu next to the taskbar clock:

1. Click the **`^`** (*Show hidden icons*) arrow on the right side of the taskbar to see the **`A`** icon.
2. To keep the icon permanently visible on the taskbar (so you can see answers directly on the tray icon):
   - **Drag** the **`A`** icon from the `^` popup directly onto the taskbar, **or**
   - Open **Windows Settings → Personalization → Taskbar → Other system tray icons** and toggle **On** for Python / `AnswearPrint`.

---

## License

This project is licensed under the [MIT License](LICENSE).
