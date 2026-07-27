"""Keeping an eye on the SD card.

A photo frame fills its disk slowly and then fails badly: syncs half-write,
the render cache can't be saved, and the index write that keeps everything
straight is the thing most likely to be interrupted. So rather than let that
happen, the frame watches free space, says so on the web page, and stops
importing before it runs out.

Two numbers matter, both configurable:

* **warn** — say something, keep working.
* **minimum** — stop importing photos. The render cache is dropped first,
  since every byte of it can be rebuilt from the originals.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

MB = 1024 * 1024
MEASURE_TTL = 60  # seconds; walking the tree is too slow for every status poll

# No threshold may exceed this share of the disk, so a floor bigger than the
# whole volume can't stop the frame working. See StorageMonitor._thresholds.
MAX_THRESHOLD_SHARE = 0.25


def directory_size(path: Path) -> int:
    """Bytes used under `path`. Missing files are skipped, not fatal."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue  # vanished mid-walk
        except (OSError, PermissionError):
            continue
    return total


class StorageMonitor:
    """Free-space reporting and the guard rails that go with it."""

    def __init__(self, config, library, data_dir: Path, ttl: float = MEASURE_TTL):
        self.config = config
        self.library = library
        self.data_dir = Path(data_dir)
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._measured_at = 0.0

    # ------------------------------------------------------------ thresholds

    def _thresholds(self) -> tuple[int, int, dict]:
        """The floor and warning level in bytes, scaled down on small disks.

        The settings are absolute, which suits an SD card but locks the frame
        out entirely if the volume is smaller than the floor — a 256 MB
        minimum on a 64 MB volume means nothing can ever be imported. So
        neither threshold is allowed past a quarter of the disk.
        """
        settings = self.config.section("storage")
        minimum = int(settings["min_free_mb"]) * MB
        warn = int(settings["warn_free_mb"]) * MB
        try:
            total = shutil.disk_usage(self.data_dir).total
        except OSError:
            total = 0
        if total:
            ceiling = int(total * MAX_THRESHOLD_SHARE)
            minimum = min(minimum, ceiling)
            warn = min(warn, ceiling)
        return minimum, warn, settings

    def free_bytes(self) -> int:
        """Cheap enough to call per photo — one statvfs, no directory walk."""
        try:
            return shutil.disk_usage(self.data_dir).free
        except OSError:
            return 0

    def has_room(self, needed: int = 0) -> bool:
        """Is there room to import another photo?"""
        minimum, _, _ = self._thresholds()
        return self.free_bytes() - needed >= minimum

    def level(self, free: int | None = None) -> str:
        free = self.free_bytes() if free is None else free
        minimum, warn, _ = self._thresholds()
        if free < minimum:
            return "critical"
        if free < warn:
            return "warn"
        return "ok"

    # -------------------------------------------------------------- reporting

    def summary(self) -> dict[str, Any]:
        """The cheap figures, safe to include in every status poll."""
        try:
            usage = shutil.disk_usage(self.data_dir)
            total, free = usage.total, usage.free
        except OSError:
            total = free = 0
        minimum, warn, _ = self._thresholds()
        return {
            "free_bytes": free,
            "total_bytes": total,
            "free_percent": round(100 * free / total, 1) if total else 0.0,
            "level": self.level(free),
            "min_free_bytes": minimum,
            "warn_free_bytes": warn,
        }

    def snapshot(self, refresh: bool = False) -> dict[str, Any]:
        """The full picture, including what the frame itself is using.

        Measuring means walking the photo directories, so the result is cached
        briefly — the web page polls far more often than this changes.
        """
        with self._lock:
            fresh = self._cached is not None and time.time() - self._measured_at < self._ttl
            if fresh and not refresh:
                return dict(self._cached, cached=True)

        breakdown = {
            "originals": directory_size(self.library.originals),
            "thumbnails": directory_size(self.library.thumbs),
            "cache": directory_size(self.library.cache),
        }
        frame_total = directory_size(self.data_dir)
        breakdown["other"] = max(0, frame_total - sum(breakdown.values()))

        report = self.summary()
        report.update({
            "path": str(self.data_dir),
            "frame_bytes": frame_total,
            "breakdown": breakdown,
            "photo_count": len(self.library),
            "bytes_per_photo": frame_total // max(1, len(self.library)),
            "measured_at": time.time(),
            "cached": False,
        })

        with self._lock:
            self._cached = report
            self._measured_at = time.time()
        return report

    def invalidate(self) -> None:
        """Called after anything that moves the numbers appreciably."""
        with self._lock:
            self._measured_at = 0.0

    # --------------------------------------------------------------- actions

    def trim_cache(self) -> int:
        """Delete every cached render, returning the bytes reclaimed.

        Safe at any time: renders are rebuilt on demand from the originals.
        The next few photo changes are slower, and that's all.
        """
        freed = directory_size(self.library.cache)
        removed = 0
        for path in self.library.cache.glob("*.jpg"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        self.invalidate()
        if removed:
            print(f"[storage] dropped {removed} cached render(s), freeing {freed / MB:.1f} MB")
        return freed

    def make_room(self) -> bool:
        """Try to get back above the minimum. True if there's room now."""
        if self.has_room():
            return True
        _, _, settings = self._thresholds()
        if settings["trim_cache_when_low"]:
            self.trim_cache()
        return self.has_room()

    def describe_shortage(self) -> str:
        free = self.free_bytes()
        minimum, _, _ = self._thresholds()
        return (
            f"Only {free // MB} MB free, below the {minimum // MB} MB minimum — "
            "delete some photos, or lower the minimum in Settings"
        )
