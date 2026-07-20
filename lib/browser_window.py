"""Browser window mode and Windows native-window helpers."""

from __future__ import annotations

import sys


WINDOW_MODE_HIDDEN = "hidden"
WINDOW_MODE_MINIMIZED = "minimized"
WINDOW_MODE_VISIBLE = "visible"
WINDOW_MODES = {
    WINDOW_MODE_HIDDEN,
    WINDOW_MODE_MINIMIZED,
    WINDOW_MODE_VISIBLE,
}


def normalize_browser_window_mode(value, *, platform: str | None = None) -> str:
    """Normalize the configured headed-window mode for the current platform."""
    current_platform = sys.platform if platform is None else str(platform)
    mode = str(value or "").strip().lower()
    if mode not in WINDOW_MODES:
        mode = (
            WINDOW_MODE_HIDDEN
            if current_platform == "win32"
            else WINDOW_MODE_VISIBLE
        )
    if mode == WINDOW_MODE_HIDDEN and current_platform != "win32":
        return WINDOW_MODE_VISIBLE
    return mode
