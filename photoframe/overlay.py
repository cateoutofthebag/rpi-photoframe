"""The clock/weather overlay drawn on top of the photo.

Composed onto its own translucent surface and cached: the text only changes
once a minute, so the steady-state cost is a single small blit.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pygame

FONT_CANDIDATES = {
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
}

TEXT = (255, 255, 255)
SHADOW = (0, 0, 0, 170)
SCRIM = (0, 0, 0, 105)

# A private-use codepoint no font defines, used to learn what this font's
# "missing glyph" box looks like. See OverlayRenderer._supports.
NOTDEF_PROBE = "\ue000"

# pygame renamed tostring -> tobytes in 2.1.3; Pi OS may ship either.
_to_bytes = getattr(pygame.image, "tobytes", None) or pygame.image.tostring


def _font_path(weight: str) -> str | None:
    for candidate in FONT_CANDIDATES[weight]:
        if Path(candidate).exists():
            return candidate
    return None


def format_temperature(value: Any, unit: str = "") -> str:
    try:
        return f"{round(float(value))}°{unit}"
    except (TypeError, ValueError):
        return ""


class OverlayRenderer:
    """Builds the overlay surface for a given screen size."""

    def __init__(self, screen_size: tuple[int, int]):
        self.screen_size = screen_size
        if not pygame.font.get_init():
            pygame.font.init()
        self._fonts: dict[tuple[str, int], pygame.font.Font] = {}
        self._cache: pygame.Surface | None = None
        self._glyph_support: dict[tuple[int, str], bool] = {}
        # Identifies the last composed content, so callers can tell when a
        # repaint is actually needed (the text only changes once a minute).
        self.key: tuple | None = None

    def _font(self, weight: str, size: int) -> pygame.font.Font:
        key = (weight, size)
        if key not in self._fonts:
            path = _font_path(weight)
            self._fonts[key] = (
                pygame.font.Font(path, size) if path else pygame.font.SysFont(None, size, bold=weight == "bold")
            )
        return self._fonts[key]

    def _supports(self, font: pygame.font.Font, glyph: str) -> bool:
        """Skip symbols the installed font can't draw (avoids tofu boxes).

        `font.metrics()` isn't trustworthy here — SDL_ttf happily reports
        metrics for the ".notdef" replacement box. So render the glyph and
        compare it against a codepoint nothing defines: identical pixels mean
        we'd be drawing a box.
        """
        if not glyph:
            return False
        key = (id(font), glyph)
        if key in self._glyph_support:
            return self._glyph_support[key]

        try:
            candidate = font.render(glyph, True, TEXT)
            notdef = font.render(NOTDEF_PROBE, True, TEXT)  # private use, always absent
            supported = (
                candidate.get_size() != notdef.get_size()
                or _to_bytes(candidate, "RGBA") != _to_bytes(notdef, "RGBA")
            )
        except Exception:
            supported = False

        self._glyph_support[key] = supported
        return supported

    # ------------------------------------------------------------- content

    def _lines(self, settings: dict, weather: dict | None, now: datetime) -> list[tuple[str, str, int]]:
        """Build (text, weight, size_px) rows from the current settings."""
        height = self.screen_size[1]
        scale = float(settings.get("scale", 1.0))
        big = max(28, int(height * 0.105 * scale))
        small = max(15, int(height * 0.034 * scale))
        rows: list[tuple[str, str, int]] = []

        if settings.get("clock", True):
            fmt = "%H:%M" if settings.get("clock_24h", True) else "%I:%M %p"
            clock = now.strftime(fmt)
            if not settings.get("clock_24h", True):
                clock = clock.lstrip("0")  # "09:05 AM" -> "9:05 AM"
            rows.append((clock, "bold", big))

        if settings.get("date", True):
            rows.append((now.strftime("%A %-d %B"), "regular", small))

        if settings.get("weather", True) and weather:
            temp = format_temperature(weather.get("temperature"), weather.get("unit", "").lstrip("°"))
            parts = []
            glyph = weather.get("glyph", "")
            if self._supports(self._font("regular", small), glyph):
                parts.append(glyph)
            if temp:
                parts.append(temp)
            if weather.get("text"):
                parts.append(weather["text"])
            high = format_temperature(weather.get("high"))
            low = format_temperature(weather.get("low"))
            if high and low:
                parts.append(f"{high} / {low}")
            line = "  ".join(parts)
            if weather.get("stale"):
                line += "  (offline)"
            if line:
                rows.append((line, "regular", small))

        return rows

    def compose(
        self, settings: dict, weather: dict | None, now: datetime
    ) -> tuple[pygame.Surface, tuple[int, int]] | None:
        """Return (surface, top-left position), or None if there's nothing to show."""
        rows = self._lines(settings, weather, now)
        if not rows:
            return None

        corner = settings.get("corner", "bottom-left")
        key = (tuple(rows), corner, self.screen_size)
        if key != self.key:
            self._cache = self._render(rows)
            self.key = key

        surface = self._cache
        assert surface is not None
        screen_w, screen_h = self.screen_size
        margin = int(min(screen_w, screen_h) * 0.05)
        x = margin if corner.endswith("left") else screen_w - surface.get_width() - margin
        y = margin if corner.startswith("top") else screen_h - surface.get_height() - margin
        return surface, (x, y)

    def _render(self, rows: list[tuple[str, str, int]]) -> pygame.Surface:
        rendered = []
        for text, weight, size in rows:
            font = self._font(weight, size)
            rendered.append((font.render(text, True, TEXT), font.render(text, True, SHADOW[:3])))

        gap = max(4, int(rows[0][2] * 0.12))
        pad_x = max(14, int(rows[0][2] * 0.45))
        pad_y = max(10, int(rows[0][2] * 0.30))
        width = max(surface.get_width() for surface, _ in rendered) + pad_x * 2
        height = sum(surface.get_height() for surface, _ in rendered) + gap * (len(rendered) - 1) + pad_y * 2

        panel = pygame.Surface((width, height), pygame.SRCALPHA)
        radius = max(8, int(min(width, height) * 0.12))
        pygame.draw.rect(panel, SCRIM, panel.get_rect(), border_radius=radius)

        offset = max(2, rows[0][2] // 40)
        y = pad_y
        for surface, shadow in rendered:
            panel.blit(shadow, (pad_x + offset, y + offset))
            panel.blit(surface, (pad_x, y))
            y += surface.get_height() + gap
        return panel
