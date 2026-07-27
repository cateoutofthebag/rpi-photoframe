"""Weather for the overlay, from Open-Meteo (no API key, no account).

Runs as a daemon thread that refreshes on an interval and hands the display a
plain dict. Network failures are never fatal: the last good reading is kept and
flagged stale, and the frame simply shows the clock if there's nothing yet.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests

API_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes, condensed to something that fits a frame.
WMO_CODES: dict[int, str] = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}

# Kept to characters DejaVu can render, so this works on a stock Pi OS image.
WMO_GLYPHS: dict[int, str] = {
    0: "☀", 1: "☀", 2: "⛅", 3: "☁",
    45: "☁", 48: "☁",
    51: "☂", 53: "☂", 55: "☂", 56: "☂", 57: "☂",
    61: "☂", 63: "☂", 65: "☂", 66: "☂", 67: "☂",
    71: "❄", 73: "❄", 75: "❄", 77: "❄",
    80: "☂", 81: "☂", 82: "☂", 85: "❄", 86: "❄",
    95: "⚡", 96: "⚡", 99: "⚡",
}


def describe(code: Any) -> tuple[str, str]:
    """Map a WMO code to (text, glyph)."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return ("", "")
    return (WMO_CODES.get(code, "—"), WMO_GLYPHS.get(code, ""))


class WeatherService(threading.Thread):
    """Polls Open-Meteo in the background; `current` is safe to read anytime."""

    def __init__(self, config, session: requests.Session | None = None):
        super().__init__(name="weather", daemon=True)
        self._config = config
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._current: dict[str, Any] | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._failures = 0

    @property
    def current(self) -> dict[str, Any] | None:
        """Last reading, or None if we've never had a successful fetch."""
        with self._lock:
            if self._current is None:
                return None
            reading = dict(self._current)
        age = time.time() - reading["fetched_at"]
        # Two refresh periods without an update and we stop trusting it.
        reading["stale"] = age > max(3600, self._settings()["refresh_minutes"] * 120)
        reading["age_seconds"] = int(age)
        return reading

    def refresh_now(self) -> None:
        """Ask for an immediate refetch (e.g. the location was changed)."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _settings(self) -> dict[str, Any]:
        return self._config.section("weather")

    def run(self) -> None:
        while not self._stop.is_set():
            settings = self._settings()
            ok = self._fetch(settings)

            if ok:
                self._failures = 0
                delay = settings["refresh_minutes"] * 60
            else:
                # Back off on failure, but keep retrying at least every 10 min.
                self._failures = min(self._failures + 1, 6)
                delay = min(600, 15 * 2 ** self._failures)

            self._wake.wait(delay)
            self._wake.clear()

    def _fetch(self, settings: dict[str, Any]) -> bool:
        metric = settings["units"] == "metric"
        query = {
            "latitude": settings["latitude"],
            "longitude": settings["longitude"],
            "current": "temperature_2m,weather_code,is_day",
            "daily": "temperature_2m_max,temperature_2m_min",
            "forecast_days": 1,
            "timezone": "auto",
            "temperature_unit": "celsius" if metric else "fahrenheit",
        }
        try:
            response = self._session.get(f"{API_URL}?{urlencode(query)}", timeout=10)
            response.raise_for_status()
            payload = response.json()
            current = payload["current"]
            daily = payload.get("daily", {})
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(f"[weather] fetch failed: {exc}")
            return False

        text, glyph = describe(current.get("weather_code"))
        reading = {
            "temperature": current.get("temperature_2m"),
            "code": current.get("weather_code"),
            "text": text,
            "glyph": glyph,
            "is_day": bool(current.get("is_day", 1)),
            "unit": "°C" if metric else "°F",
            "high": _first(daily.get("temperature_2m_max")),
            "low": _first(daily.get("temperature_2m_min")),
            "fetched_at": time.time(),
            "fetched_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with self._lock:
            self._current = reading
        return True


def _first(values: Any) -> float | None:
    if isinstance(values, list) and values:
        return values[0]
    return None
