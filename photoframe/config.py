"""Persistent settings, stored as a single JSON file.

The web thread writes settings while the display thread reads them, so readers
always take a deep-copied snapshot and never see a half-applied update.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "slideshow": {
        "interval_seconds": 90,
        "shuffle": True,
        "transition_seconds": 0.8,
        "fit": "blur",  # contain | cover | blur
    },
    "overlay": {
        "mode": "scheduled",  # off | always | scheduled
        "start": "07:00",
        "end": "22:30",
        "corner": "bottom-left",  # bottom-left | bottom-right | top-left | top-right
        "clock": True,
        "clock_24h": True,
        "date": True,
        "weather": True,
        "scale": 1.0,
    },
    "weather": {
        "latitude": 51.5072,
        "longitude": -0.1276,
        "units": "metric",  # metric | imperial
        "refresh_minutes": 15,
    },
    "display": {
        "rotation": 0,  # 0 | 90 | 180 | 270
        "quiet_hours": {"enabled": False, "start": "23:30", "end": "07:00"},
        "power_off_when_quiet": True,
    },
    "mirror": {
        "enabled": True,
        "quality": 80,  # JPEG quality for /api/frame.jpg
        "max_width": 0,  # 0 = send at native screen width
    },
    "storage": {
        "warn_free_mb": 1024,  # say something
        "min_free_mb": 256,  # stop importing photos
        "pause_sync_when_low": True,
        "trim_cache_when_low": True,
    },
    # A list, not a dict — see sanitise_sources().
    "sources": [],
}

# Per-type fields for network photo sources, with their defaults. Anything not
# listed here is dropped, so a hand-edited config can't smuggle keys through.
SOURCE_FIELDS: dict[str, dict[str, Any]] = {
    "folder": {"path": ""},
    "smb": {
        "server": "", "share": "", "path": "", "domain": "",
        "username": "", "password": "", "port": 445,
    },
    "photoprism": {
        "url": "", "album": "", "username": "", "password": "",
        "token": "", "size": "fit_1920", "verify_tls": True,
    },
    "icloud": {"url": ""},
}

# Never returned by the HTTP API; see web.redact_config().
SECRET_FIELDS = {"password", "token"}

COMMON_SOURCE_FIELDS: dict[str, Any] = {
    "enabled": True,
    "interval_minutes": 30,
    "limit": 500,  # most photos to pull from one source
    "remove_deleted": True,
}

PHOTOPRISM_SIZES = {
    "fit_720", "fit_1280", "fit_1600", "fit_1920",
    "fit_2048", "fit_2560", "fit_3840", "original",
}

_CORNERS = {"bottom-left", "bottom-right", "top-left", "top-right"}
_FITS = {"contain", "cover", "blur"}
_MODES = {"off", "always", "scheduled"}
_UNITS = {"metric", "imperial"}


def parse_hhmm(value: Any, fallback: str = "00:00") -> int:
    """Return "HH:MM" as minutes past midnight, falling back on bad input."""
    for candidate in (value, fallback):
        try:
            hours, _, minutes = str(candidate).partition(":")
            h, m = int(hours), int(minutes)
            if 0 <= h < 24 and 0 <= m < 60:
                return h * 60 + m
        except (TypeError, ValueError):
            continue
    return 0


def in_window(now: datetime, start: str, end: str) -> bool:
    """Is `now` inside the daily window, handling windows that cross midnight?

    An empty window (start == end) is treated as "never".
    """
    start_m, end_m = parse_hhmm(start), parse_hhmm(end)
    now_m = now.hour * 60 + now.minute
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m  # wraps past midnight


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge `patch` into a copy of `base`."""
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _num(value: Any, fallback: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(value)))
    except (TypeError, ValueError):
        return fallback


def _choice(value: Any, allowed: set[str], fallback: str) -> str:
    return value if value in allowed else fallback


def sanitise_sources(value: Any) -> list[dict]:
    """Validate the source list, dropping entries we can't make sense of."""
    if not isinstance(value, list):
        return []

    cleaned: list[dict] = []
    used_ids: set[str] = set()
    for raw in value[:20]:  # a sane ceiling; each one costs a sync thread pass
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind not in SOURCE_FIELDS:
            continue

        entry: dict[str, Any] = {"type": kind}
        source_id = str(raw.get("id") or "").strip()[:32]
        if not source_id or source_id in used_ids:
            source_id = uuid.uuid4().hex[:8]
        used_ids.add(source_id)
        entry["id"] = source_id
        entry["name"] = (str(raw.get("name") or "").strip() or f"{kind} source")[:60]

        for key, default in COMMON_SOURCE_FIELDS.items():
            given = raw.get(key, default)
            if isinstance(default, bool):
                entry[key] = bool(given)
            else:
                entry[key] = int(_num(given, default, 1, 100000))

        for key, default in SOURCE_FIELDS[kind].items():
            given = raw.get(key, default)
            if isinstance(default, bool):
                entry[key] = bool(given)
            elif isinstance(default, int):
                entry[key] = int(_num(given, default, 1, 65535))
            else:
                entry[key] = str(given if given is not None else default).strip()[:500]

        if kind == "photoprism":
            entry["size"] = _choice(entry["size"], PHOTOPRISM_SIZES, "fit_1920")
            entry["url"] = entry["url"].rstrip("/")

        cleaned.append(entry)
    return cleaned


def sanitise(data: dict) -> dict:
    """Clamp/validate a settings dict so bad input can never wedge the display."""
    sources = sanitise_sources(data.get("sources", []))
    data = {k: v for k, v in data.items() if k != "sources"}
    out = _deep_merge({k: v for k, v in DEFAULTS.items() if k != "sources"}, data)

    slide, d = out["slideshow"], DEFAULTS["slideshow"]
    slide["interval_seconds"] = int(_num(slide["interval_seconds"], d["interval_seconds"], 5, 86400))
    slide["transition_seconds"] = round(_num(slide["transition_seconds"], d["transition_seconds"], 0, 5), 2)
    slide["shuffle"] = bool(slide["shuffle"])
    slide["fit"] = _choice(slide["fit"], _FITS, d["fit"])

    ov, d = out["overlay"], DEFAULTS["overlay"]
    ov["mode"] = _choice(ov["mode"], _MODES, d["mode"])
    ov["corner"] = _choice(ov["corner"], _CORNERS, d["corner"])
    ov["scale"] = round(_num(ov["scale"], d["scale"], 0.5, 2.5), 2)
    for key in ("start", "end"):
        minutes = parse_hhmm(ov[key], d[key])
        ov[key] = f"{minutes // 60:02d}:{minutes % 60:02d}"
    for key in ("clock", "clock_24h", "date", "weather"):
        ov[key] = bool(ov[key])

    wx, d = out["weather"], DEFAULTS["weather"]
    wx["latitude"] = round(_num(wx["latitude"], d["latitude"], -90, 90), 5)
    wx["longitude"] = round(_num(wx["longitude"], d["longitude"], -180, 180), 5)
    wx["units"] = _choice(wx["units"], _UNITS, d["units"])
    wx["refresh_minutes"] = int(_num(wx["refresh_minutes"], d["refresh_minutes"], 5, 720))

    disp, d = out["display"], DEFAULTS["display"]
    try:
        rotation = int(disp["rotation"]) % 360
    except (TypeError, ValueError):
        rotation = 0
    disp["rotation"] = rotation if rotation in (0, 90, 180, 270) else 0
    disp["power_off_when_quiet"] = bool(disp["power_off_when_quiet"])
    quiet = disp["quiet_hours"]
    quiet["enabled"] = bool(quiet["enabled"])
    for key in ("start", "end"):
        minutes = parse_hhmm(quiet[key], d["quiet_hours"][key])
        quiet[key] = f"{minutes // 60:02d}:{minutes % 60:02d}"

    mirror, d = out["mirror"], DEFAULTS["mirror"]
    mirror["enabled"] = bool(mirror["enabled"])
    mirror["quality"] = int(_num(mirror["quality"], d["quality"], 30, 95))
    mirror["max_width"] = int(_num(mirror["max_width"], d["max_width"], 0, 4096))

    store, d = out["storage"], DEFAULTS["storage"]
    store["min_free_mb"] = int(_num(store["min_free_mb"], d["min_free_mb"], 32, 1024 * 1024))
    store["warn_free_mb"] = int(_num(store["warn_free_mb"], d["warn_free_mb"], 32, 1024 * 1024))
    # A warning that fires later than the hard stop would never be seen.
    store["warn_free_mb"] = max(store["warn_free_mb"], store["min_free_mb"])
    store["pause_sync_when_low"] = bool(store["pause_sync_when_low"])
    store["trim_cache_when_low"] = bool(store["trim_cache_when_low"])

    # Drop any keys that aren't part of the schema.
    result = {section: {k: v for k, v in values.items() if k in DEFAULTS[section]}
              for section, values in out.items()
              if section in DEFAULTS and section != "sources"}
    result["sources"] = sources
    return result


class Config:
    """Thread-safe settings backed by a JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = sanitise({})
        self._version = 0
        self.load()

    def load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as fh:
                stored = json.load(fh)
        except FileNotFoundError:
            self.save()
            return
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] ignoring unreadable {self.path}: {exc}")
            return
        with self._lock:
            self._data = sanitise(stored if isinstance(stored, dict) else {})
            self._version += 1

    def save(self) -> None:
        """Write atomically so a power cut can't leave a truncated config."""
        with self._lock:
            payload = json.dumps(self._data, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        # Source credentials live in here, so keep it owner-only.
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    @property
    def version(self) -> int:
        """Bumped on every change, so readers can cheaply detect updates."""
        with self._lock:
            return self._version

    def update(self, patch: dict) -> dict:
        """Deep-merge `patch`, validate the result, persist it."""
        with self._lock:
            self._data = sanitise(_deep_merge(self._data, patch))
            self._version += 1
            result = copy.deepcopy(self._data)
        self.save()
        return result

    def section(self, name: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._data[name])
