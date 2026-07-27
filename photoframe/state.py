"""Shared state between the web thread and the display loop.

Deliberately tiny: the web side pushes commands and reads status, the display
side drains commands and publishes status. One lock covers both.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

COMMANDS = {
    "next", "previous", "pause", "resume", "toggle_pause", "reload",
    # Temporary overrides of the overlay schedule, in both directions.
    "info_burst", "info_hide", "toggle_info",
}


MIRROR_DEMAND_SECONDS = 30  # how long one viewer request keeps frames flowing


class FrameState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame_ready = threading.Condition(self._lock)
        self._commands: deque[str] = deque(maxlen=32)
        self._status: dict[str, Any] = {
            "photo_id": None,
            "photo_filename": None,
            "shown_at": None,
            "paused": False,
            "quiet": False,
            "overlay_visible": False,
            "screen": None,
            "started_at": time.time(),
        }
        # The info button overrides the overlay schedule for a while, either
        # way round: show it when the schedule says no, hide it when the
        # schedule says yes. Muting wins over showing.
        self.info_burst_until: float = 0.0
        self.info_mute_until: float = 0.0

        # Latest composed frame for the web mirror, as raw RGB. Only produced
        # while someone is actually watching — see mirror_active().
        self._frame: bytes | None = None
        self._frame_size: tuple[int, int] = (0, 0)
        self._frame_generation = 0
        self._mirror_wanted_until = 0.0

    def send(self, command: str) -> bool:
        """Queue a command for the display loop. Returns False if unknown."""
        if command not in COMMANDS:
            return False
        with self._lock:
            self._commands.append(command)
        return True

    def drain(self) -> list[str]:
        with self._lock:
            commands = list(self._commands)
            self._commands.clear()
        return commands

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)

    def status(self) -> dict[str, Any]:
        with self._lock:
            status = dict(self._status)
            status["frame_generation"] = self._frame_generation
        status["uptime_seconds"] = int(time.time() - status["started_at"])
        return status

    # ----------------------------------------------------------- web mirror

    def request_mirror(self) -> None:
        """Called by the mirror endpoint; asks the display to keep publishing."""
        with self._lock:
            self._mirror_wanted_until = time.time() + MIRROR_DEMAND_SECONDS

    def mirror_active(self) -> bool:
        with self._lock:
            return time.time() < self._mirror_wanted_until

    def publish_frame(self, data: bytes, size: tuple[int, int]) -> None:
        with self._frame_ready:
            self._frame = data
            self._frame_size = size
            self._frame_generation += 1
            self._frame_ready.notify_all()

    def frame(self) -> tuple[int, bytes, tuple[int, int]] | None:
        with self._lock:
            if self._frame is None:
                return None
            return self._frame_generation, self._frame, self._frame_size

    def wait_for_frame(
        self, timeout: float, since: int | None = None
    ) -> tuple[int, bytes, tuple[int, int]] | None:
        """Block until a frame newer than `since` exists (long-polling).

        With `since` omitted, any frame will do — that's the first load.
        """
        deadline = time.monotonic() + timeout
        with self._frame_ready:
            while True:
                fresh = self._frame is not None and (since is None or self._frame_generation > since)
                if fresh:
                    return self._frame_generation, self._frame, self._frame_size
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._frame_ready.wait(remaining)
