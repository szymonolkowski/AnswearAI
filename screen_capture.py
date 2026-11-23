import io
from typing import Optional, Tuple
import mss
from PIL import Image, ImageChops


class ScreenCapturer:
    """Fast in-memory screen capture using mss."""

    def __init__(self):
        self._last_thumbnail: Optional[Image.Image] = None

    def capture_jpeg_bytes(
        self,
        monitor_index: int = 1,
        max_dimension: int = 1920,
        jpeg_quality: int = 80
    ) -> Tuple[bytes, Image.Image]:
        """
        Capture the screen into RAM and return compressed JPEG bytes
        together with a thumbnail (for screen-change detection).
        """
        with mss.mss() as sct:
            monitors = sct.monitors
            if monitor_index >= len(monitors):
                monitor_index = 1 if len(monitors) > 1 else 0
            monitor = monitors[monitor_index]
            sct_img = sct.grab(monitor)

            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        # 64x64 grayscale thumbnail for fast screen-change comparison
        thumbnail = img.resize((64, 64), Image.Resampling.BILINEAR).convert("L")

        # Downscale large displays (e.g. 4K -> Full HD) for faster API transfer (<5s)
        w, h = img.size
        if max(w, h) > max_dimension:
            scale = max_dimension / float(max(w, h))
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        return buf.getvalue(), thumbnail

    def has_screen_changed(self, current_thumb: Image.Image, threshold_percent: float = 3.0) -> bool:
        """
        Check whether the screen changed by at least threshold_percent % since the previous scan.
        """
        if self._last_thumbnail is None:
            self._last_thumbnail = current_thumb
            return True

        diff = ImageChops.difference(self._last_thumbnail, current_thumb)
        diff_bytes = diff.tobytes()
        avg_diff = sum(diff_bytes) / (len(diff_bytes) * 255.0) * 100.0

        if avg_diff >= threshold_percent:
            self._last_thumbnail = current_thumb
            return True
        return False

    def reset_change_tracker(self) -> None:
        self._last_thumbnail = None
