#!/usr/bin/env python3
"""Entry point: starts the web server, the weather poller and the slideshow.

Everything lives in one process so the web thread and the display loop can
share the library and settings objects directly — on a Zero 2 W a second Python
interpreter is a real cost, and there's nothing here that needs one.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

from werkzeug.serving import make_server

from photoframe.config import Config
from photoframe.library import HEIF_SUPPORTED, PhotoLibrary
from photoframe.state import FrameState
from photoframe.sync import SyncManager
from photoframe.weather import WeatherService
from photoframe.web import create_app, local_ip


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Raspberry Pi photo frame")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(os.environ.get("PHOTOFRAME_DATA", "data")),
        help="directory for photos and settings (default: ./data)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="web bind address")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PHOTOFRAME_PORT", 8080)))
    parser.add_argument("--no-display", action="store_true", help="web only, no slideshow (handy off-Pi)")
    parser.add_argument("--windowed", action="store_true", help="run in a window instead of fullscreen")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="render to an off-screen buffer — the web mirror still works, so this "
             "is the way to try the frame on a machine with no display",
    )
    parser.add_argument("--no-sync", action="store_true", help="don't sync network sources")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = args.data.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    config = Config(data_dir / "config.json")
    library = PhotoLibrary(data_dir)
    state = FrameState()
    weather = WeatherService(config)
    weather.start()

    sync = None
    if not args.no_sync:
        sync = SyncManager(config, library, data_dir / "sync.json")
        sync.start()

    password = os.environ.get("PHOTOFRAME_PASSWORD") or None
    # Shown in the log and on the "no photos yet" screen. Inside a container
    # the detected address is the container's own, which nobody can reach, so
    # allow it to be overridden.
    url = os.environ.get("PHOTOFRAME_URL") or f"http://{local_ip()}:{args.port}"
    app = create_app(config, library, state, weather, password=password, sync=sync)

    try:
        server = make_server(args.host, args.port, app, threaded=True)
    except OSError as exc:
        print(f"cannot bind {args.host}:{args.port} — {exc}", file=sys.stderr)
        return 1
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()

    print(f"[frame] {len(library)} photo(s) in {data_dir}")
    print(f"[frame] remote control on {url}" + ("" if password else "  (no password set)"))
    print(f"[frame] live mirror on {url}/mirror")
    if sync is not None:
        enabled = [s for s in config.section("sources") if s["enabled"]]
        print(f"[frame] {len(enabled)} network source(s) enabled")
    if not HEIF_SUPPORTED:
        print("[frame] HEIC uploads disabled — pip install pillow-heif to enable")

    display = None
    stopping = threading.Event()

    def shutdown(signum, _frame):
        print(f"[frame] signal {signum}, shutting down")
        stopping.set()
        if display is not None:
            display.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        if args.no_display:
            stopping.wait()
        else:
            if args.headless:
                # SDL's dummy driver gives us a real surface with no screen, so
                # the loop (and the mirror) behave exactly as they do on the Pi.
                os.environ["SDL_VIDEODRIVER"] = "dummy"
            from photoframe.display import FrameDisplay  # imports pygame

            display = FrameDisplay(config, library, weather, state,
                                   web_url=url, windowed=args.windowed)
            display.run()
    finally:
        weather.stop()
        if sync is not None:
            sync.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
