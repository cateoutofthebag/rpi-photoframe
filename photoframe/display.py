"""The slideshow itself: pygame straight to the framebuffer, no desktop needed.

Performance notes for the Zero 2 W, since they drive the design here:

* Photos are decoded on a worker thread, never in the render loop, so a slow
  JPEG delays the *change* by a moment instead of freezing the frame.
* The library hands back images already composed at exactly screen size, so
  the loop does no scaling.
* In the steady state only the overlay is repainted, once a minute, over a
  restored patch of the background.
"""

from __future__ import annotations

import os
import queue
import random
import subprocess
import threading
import time
from datetime import datetime
from typing import Any

import pygame

from .config import in_window
from .overlay import OverlayRenderer

# pygame renamed fromstring/tostring -> frombytes/tobytes in 2.1.3; Pi OS may
# ship either.
_frombytes = getattr(pygame.image, "frombytes", None) or pygame.image.fromstring
_tobytes = getattr(pygame.image, "tobytes", None) or pygame.image.tostring

FAILED = object()  # sentinel: this photo could not be rendered

# How long the info button's override of the overlay schedule lasts.
INFO_OVERRIDE_SECONDS = 600


class ImageLoader(threading.Thread):
    """Renders photos to pygame surfaces off the main thread."""

    def __init__(self, library, cache_size: int = 4):
        super().__init__(name="image-loader", daemon=True)
        self.library = library
        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._results: dict[tuple, Any] = {}
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._cache_size = cache_size
        self._stop = threading.Event()

    def request(self, key: tuple) -> None:
        """Queue a render. Cheap and idempotent."""
        with self._lock:
            if key in self._results:
                return
        try:
            self._queue.put_nowait(key)
        except queue.Full:
            pass

    def take(self, key: tuple, timeout: float = 0.0) -> Any:
        """Return the surface (or FAILED), or None if it isn't ready yet."""
        self.request(key)
        deadline = time.monotonic() + timeout
        with self._ready:
            while True:
                if key in self._results:
                    return self._results[key]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._ready.wait(remaining)

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)

    def run(self) -> None:
        while not self._stop.is_set():
            key = self._queue.get()
            if key is None:
                break
            with self._lock:
                if key in self._results:
                    continue
            surface = self._render(key)
            with self._ready:
                self._results[key] = surface
                # Bounded FIFO cache: keep the few photos around "now".
                while len(self._results) > self._cache_size:
                    self._results.pop(next(iter(self._results)))
                self._ready.notify_all()

    def _render(self, key: tuple) -> Any:
        photo_id, size, fit, rotation = key
        # For a rotated frame we render at the pre-rotation size, then turn it.
        render_size = (size[1], size[0]) if rotation in (90, 270) else size
        image = self.library.render(photo_id, render_size, fit)
        if image is None:
            return FAILED
        try:
            surface = _frombytes(image.tobytes(), image.size, "RGB")
            if rotation:
                surface = pygame.transform.rotate(surface, -rotation)
            return surface
        except Exception as exc:
            print(f"[display] surface conversion failed for {photo_id}: {exc}")
            return FAILED


class FrameDisplay:
    """Owns the screen and the main render loop."""

    def __init__(self, config, library, weather, state, web_url: str = "", windowed: bool = False):
        self.config = config
        self.library = library
        self.weather = weather
        self.state = state
        self.web_url = web_url
        self.windowed = windowed

        self.running = True
        self.paused = False
        self.screen: pygame.Surface | None = None
        self.size: tuple[int, int] = (0, 0)
        self.overlay: OverlayRenderer | None = None
        self.loader = ImageLoader(library)

        self._order: list[str] = []
        self._pos = 0
        self._library_version = -1
        self._cfg = config.snapshot()
        self._cfg_version = config.version

        self._base: pygame.Surface | None = None  # current photo, screen-sized
        self._current_id: str | None = None
        self._shown_at = 0.0
        self._pending_id: str | None = None
        self._overlay_rect: pygame.Rect | None = None
        self._overlay_key: tuple | None = None
        self._quiet = False
        self._powered_off = False

        # Bumped on every visible change; compared against what the mirror has
        # been given, so we only pay for a frame copy when it's both stale and
        # someone is watching.
        self._content_serial = 0
        self._published_serial = -1

    # ------------------------------------------------------------- lifecycle

    def setup(self) -> None:
        if "SDL_VIDEODRIVER" not in os.environ and not self.windowed:
            # Console boot with no X/Wayland: talk to KMS directly.
            if os.name == "posix" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
                os.environ["SDL_VIDEODRIVER"] = "kmsdrm"
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")  # no audio hardware needed

        pygame.display.init()
        pygame.font.init()
        if self.windowed:
            self.screen = pygame.display.set_mode((1280, 720))
        else:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            pygame.mouse.set_visible(False)
        pygame.display.set_caption("Photo Frame")

        self.size = self.screen.get_size()
        self.overlay = OverlayRenderer(self.size)
        self.state.update(screen=list(self.size))
        print(f"[display] {self.size[0]}x{self.size[1]} via {pygame.display.get_driver()}")

        self.loader.start()
        self.screen.fill((0, 0, 0))
        pygame.display.flip()
        self._start_cache_warmer()

    def teardown(self) -> None:
        self.loader.stop()
        self._set_power(True)
        pygame.quit()

    def run(self) -> None:
        self.setup()
        clock = pygame.time.Clock()
        try:
            while self.running:
                self._refresh_config()
                self._handle_events()
                self._handle_commands()

                now = datetime.now()
                if self._handle_quiet_hours(now):
                    self._publish_mirror_frame()
                    clock.tick(4)
                    continue

                self._sync_playlist()
                if not self._order:
                    self._draw_placeholder()
                    self._publish_mirror_frame()
                    clock.tick(2)
                    continue

                self._ensure_showing()
                self._maybe_advance()
                self._paint_overlay(now)
                self._publish_mirror_frame()
                clock.tick(10)
        except KeyboardInterrupt:
            pass
        finally:
            self.teardown()

    def stop(self) -> None:
        self.running = False

    # -------------------------------------------------------------- plumbing

    def _refresh_config(self) -> None:
        if self.config.version != self._cfg_version:
            previous_fit = self._cfg["slideshow"]["fit"]
            previous_rotation = self._cfg["display"]["rotation"]
            self._cfg = self.config.snapshot()
            self._cfg_version = self.config.version
            if (self._cfg["slideshow"]["fit"] != previous_fit
                    or self._cfg["display"]["rotation"] != previous_rotation):
                self._reload_current()

    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    self.running = False
                elif event.key in (pygame.K_RIGHT, pygame.K_SPACE):
                    self._advance(1)
                elif event.key == pygame.K_LEFT:
                    self._advance(-1)
                elif event.key == pygame.K_p:
                    self.paused = not self.paused
                    self.state.update(paused=self.paused)
                elif event.key == pygame.K_i:
                    self._show_info(not self._overlay_visible(datetime.now()))

    def _handle_commands(self) -> None:
        for command in self.state.drain():
            if command == "next":
                self._advance(1)
            elif command == "previous":
                self._advance(-1)
            elif command in ("pause", "resume", "toggle_pause"):
                self.paused = {"pause": True, "resume": False}.get(command, not self.paused)
                self.state.update(paused=self.paused)
                if not self.paused:
                    self._shown_at = time.time()
            elif command == "reload":
                self._library_version = -1  # forces a playlist rebuild
            elif command == "info_burst":
                self._show_info(True)
            elif command == "info_hide":
                self._show_info(False)
            elif command == "toggle_info":
                self._show_info(not self._overlay_visible(datetime.now()))

    def _sync_playlist(self) -> None:
        if self.library.version == self._library_version:
            return
        self._library_version = self.library.version
        ids = self.library.ids()
        if self._cfg["slideshow"]["shuffle"]:
            random.shuffle(ids)
        self._order = ids
        # Stay on the current photo if it survived the change.
        if self._current_id in self._order:
            self._pos = self._order.index(self._current_id)
        else:
            self._pos = 0
            if self._current_id is not None:
                self._current_id = None  # it was deleted; move on

    def _key_for(self, photo_id: str) -> tuple:
        return (photo_id, self.size, self._cfg["slideshow"]["fit"], self._cfg["display"]["rotation"])

    def _ensure_showing(self) -> None:
        """Make sure something is on screen, and keep the next photo warm."""
        if self._base is None and self._order:
            self._current_id = self._order[self._pos]
            surface = self.loader.take(self._key_for(self._current_id), timeout=8.0)
            if surface is None:
                return  # still decoding; try again next tick
            if surface is FAILED:
                self._skip_broken(self._current_id)
                return
            self._show(surface, self._current_id)
        if self._pending_id is None and len(self._order) > 1:
            self.loader.request(self._key_for(self._order[(self._pos + 1) % len(self._order)]))

    def _maybe_advance(self) -> None:
        if self.paused or not self._order:
            return
        if time.time() - self._shown_at < self._cfg["slideshow"]["interval_seconds"]:
            return
        self._advance(1)

    def _advance(self, step: int) -> None:
        if not self._order:
            return
        if len(self._order) == 1:
            self._shown_at = time.time()
            return

        self._pos = (self._pos + step) % len(self._order)
        # Reshuffle once we've been all the way round.
        if self._pos == 0 and step > 0 and self._cfg["slideshow"]["shuffle"] and len(self._order) > 2:
            last = self._order[-1]
            random.shuffle(self._order)
            if self._order[0] == last:
                self._order[0], self._order[-1] = self._order[-1], self._order[0]

        photo_id = self._order[self._pos]
        self._pending_id = photo_id
        surface = self.loader.take(self._key_for(photo_id), timeout=6.0)
        self._pending_id = None
        if surface is None:
            self._shown_at = time.time()  # give it a full interval to catch up
            return
        if surface is FAILED:
            self._skip_broken(photo_id)
            return

        self._transition(surface)
        self._show(surface, photo_id)
        if len(self._order) > 1:
            self.loader.request(self._key_for(self._order[(self._pos + 1) % len(self._order)]))

    def _skip_broken(self, photo_id: str) -> None:
        print(f"[display] skipping unreadable photo {photo_id}")
        if photo_id in self._order:
            self._order.remove(photo_id)
            self._pos = min(self._pos, max(0, len(self._order) - 1))
        self._shown_at = time.time()

    def _show(self, surface: pygame.Surface, photo_id: str) -> None:
        assert self.screen is not None
        self._base = surface.convert()
        self._current_id = photo_id
        self._shown_at = time.time()
        self._overlay_rect = None
        self._overlay_key = None
        self.screen.blit(self._base, (0, 0))
        pygame.display.flip()
        self._touch()

        entry = self.library.get(photo_id) or {}
        self.state.update(
            photo_id=photo_id,
            photo_filename=entry.get("filename"),
            shown_at=time.time(),
        )

    def _reload_current(self) -> None:
        """Re-render the current photo (fit mode or rotation changed)."""
        self._base = None
        self._overlay_rect = None

    def _transition(self, incoming: pygame.Surface) -> None:
        assert self.screen is not None
        seconds = self._cfg["slideshow"]["transition_seconds"]
        if seconds <= 0 or self._base is None:
            return

        outgoing = self._base
        incoming = incoming.convert()
        clock = pygame.time.Clock()
        start = time.monotonic()
        while True:
            progress = (time.monotonic() - start) / seconds
            if progress >= 1:
                break
            self.screen.blit(outgoing, (0, 0))
            incoming.set_alpha(int(255 * progress))
            self.screen.blit(incoming, (0, 0))
            pygame.display.flip()
            clock.tick(30)
        incoming.set_alpha(None)

    # --------------------------------------------------------------- overlay

    def _show_info(self, visible: bool) -> None:
        """Override the overlay schedule for a while, in either direction."""
        deadline = time.time() + INFO_OVERRIDE_SECONDS
        if visible:
            self.state.info_burst_until, self.state.info_mute_until = deadline, 0.0
        else:
            self.state.info_mute_until, self.state.info_burst_until = deadline, 0.0

    def _overlay_visible(self, now: datetime) -> bool:
        settings = self._cfg["overlay"]
        clock = time.time()
        if clock < self.state.info_mute_until:
            return False
        if clock < self.state.info_burst_until:
            return True
        mode = settings["mode"]
        if mode == "always":
            return True
        if mode == "scheduled":
            return in_window(now, settings["start"], settings["end"])
        return False

    def _paint_overlay(self, now: datetime) -> None:
        assert self.screen is not None
        if self._base is None:
            return

        composed = None
        if self._overlay_visible(now):
            composed = self.overlay.compose(self._cfg["overlay"], self.weather.current, now)
        self.state.update(overlay_visible=composed is not None)

        # overlay.key changes whenever the rendered text does, which is what
        # decides a repaint — the panel is usually the same size minute to minute.
        key = (self.overlay.key, composed[1]) if composed else None
        if key == self._overlay_key:
            return  # nothing has changed since the last repaint

        dirty = []
        if self._overlay_rect is not None:  # erase the old panel
            self.screen.blit(self._base, self._overlay_rect.topleft, self._overlay_rect)
            dirty.append(self._overlay_rect.copy())

        if composed is not None:
            surface, position = composed
            self.screen.blit(surface, position)
            self._overlay_rect = pygame.Rect(position, surface.get_size())
            dirty.append(self._overlay_rect.copy())
        else:
            self._overlay_rect = None

        self._overlay_key = key
        if dirty:
            pygame.display.update(dirty)
            self._touch()

    def _draw_placeholder(self) -> None:
        """Shown before any photos have been uploaded."""
        assert self.screen is not None and self.overlay is not None
        key = ("placeholder", self.web_url)
        if self._overlay_key == key:
            return
        self._overlay_key = key
        self._base = None

        self.screen.fill((16, 18, 22))
        height = self.size[1]
        lines = [
            ("No photos yet", "bold", int(height * 0.07)),
            ("Add some from any browser on your network:", "regular", int(height * 0.033)),
            (self.web_url or "http://<this-pi>:8080", "bold", int(height * 0.045)),
        ]
        rendered = [(self.overlay._font(weight, size).render(text, True, (235, 235, 235)), size)
                    for text, weight, size in lines]
        total = sum(surface.get_height() for surface, _ in rendered) + int(height * 0.03) * (len(rendered) - 1)
        y = (self.size[1] - total) // 2
        for surface, _ in rendered:
            self.screen.blit(surface, ((self.size[0] - surface.get_width()) // 2, y))
            y += surface.get_height() + int(height * 0.03)
        pygame.display.flip()
        self._touch()

    # ----------------------------------------------------------- quiet hours

    def _handle_quiet_hours(self, now: datetime) -> bool:
        settings = self._cfg["display"]["quiet_hours"]
        quiet = settings["enabled"] and in_window(now, settings["start"], settings["end"])
        if quiet == self._quiet:
            return quiet

        self._quiet = quiet
        self.state.update(quiet=quiet)
        self._touch()
        if quiet:
            assert self.screen is not None
            self.screen.fill((0, 0, 0))
            pygame.display.flip()
            if self._cfg["display"]["power_off_when_quiet"]:
                self._set_power(False)
        else:
            self._set_power(True)
            self._overlay_key = None
            self._overlay_rect = None
            if self._base is not None:
                assert self.screen is not None
                self.screen.blit(self._base, (0, 0))
                pygame.display.flip()
            self._shown_at = time.time()
        return quiet

    # ----------------------------------------------------------- web mirror

    def _touch(self) -> None:
        """Note that what's on screen has changed."""
        self._content_serial += 1

    def _publish_mirror_frame(self) -> None:
        """Hand the current screen to the web mirror, if anyone is watching.

        Reading the screen back costs a full-frame copy, so it only happens
        when the picture has actually changed and a viewer asked for it in the
        last half minute.
        """
        if self._published_serial == self._content_serial:
            return
        if self.screen is None or not self.state.mirror_active():
            return
        try:
            self.state.publish_frame(_tobytes(self.screen, "RGB"), self.size)
        except Exception as exc:  # a mirror problem must never stop the frame
            print(f"[display] could not publish mirror frame: {exc}")
        self._published_serial = self._content_serial

    def _set_power(self, on: bool) -> None:
        """Blank the HDMI output overnight. No-op off a Pi."""
        if on == (not self._powered_off):
            return
        try:
            subprocess.run(
                ["vcgencmd", "display_power", "1" if on else "0"],
                check=True, capture_output=True, timeout=5,
            )
            self._powered_off = not on
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            if not isinstance(exc, FileNotFoundError):
                print(f"[display] display_power failed: {exc}")
            self._powered_off = False

    # ------------------------------------------------------------ warm cache

    def _start_cache_warmer(self) -> None:
        """Pre-render photos in the background so the first pass isn't slow."""

        def warm() -> None:
            try:
                os.nice(10)  # stay out of the render loop's way
            except (AttributeError, OSError):
                pass
            time.sleep(20)
            while self.running:
                fit = self._cfg["slideshow"]["fit"]
                rotation = self._cfg["display"]["rotation"]
                size = (self.size[1], self.size[0]) if rotation in (90, 270) else self.size

                warmed = True
                for photo_id in self.library.uncached(size, fit):
                    if not self.running or fit != self._cfg["slideshow"]["fit"]:
                        warmed = False
                        break
                    self.library.render(photo_id, size, fit)
                    time.sleep(1.0)

                # Once every photo is cached for the settings in force, drop
                # renders left behind by an earlier screen size or fit mode.
                if warmed and self.running:
                    removed = self.library.prune_cache(size, fit)
                    if removed:
                        print(f"[display] pruned {removed} stale cached render(s)")
                time.sleep(60)

        threading.Thread(target=warm, name="cache-warmer", daemon=True).start()
