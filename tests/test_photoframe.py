"""Smoke tests: run with `pytest` from the repo root."""

from __future__ import annotations

import io
import os
import time
from datetime import datetime

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # no screen needed for these

from PIL import Image

from photoframe.config import Config, in_window, sanitise
from photoframe.library import LibraryError, PhotoLibrary
from photoframe.state import FrameState
from photoframe.web import create_app


def make_image(size=(1200, 800), colour=(180, 90, 40), fmt="JPEG") -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", size, colour)
    image.putpixel((0, 0), (0, 255, 0))  # keeps hashes distinct per colour
    image.save(buffer, fmt)
    return buffer.getvalue()


class FakeWeather:
    """Stands in for WeatherService — no network in tests."""

    current = {
        "temperature": 21.4, "unit": "°C", "text": "Partly cloudy", "glyph": "",
        "high": 24.0, "low": 15.0, "stale": False, "is_day": True, "code": 2,
    }
    refreshed = False

    def refresh_now(self):
        self.refreshed = True


@pytest.fixture
def library(tmp_path):
    return PhotoLibrary(tmp_path)


@pytest.fixture
def client(tmp_path):
    config = Config(tmp_path / "config.json")
    lib = PhotoLibrary(tmp_path)
    state = FrameState()
    weather = FakeWeather()
    app = create_app(config, lib, state, weather)
    app.config.update(TESTING=True)
    return app.test_client(), config, lib, state, weather


# ------------------------------------------------------------------- config


def test_sanitise_clamps_and_rejects_junk():
    result = sanitise({
        "slideshow": {"interval_seconds": 0, "fit": "nonsense", "transition_seconds": 99},
        "overlay": {"mode": "sometimes", "start": "25:99", "corner": "middle"},
        "display": {"rotation": 45},
        "bogus_section": {"x": 1},
    })
    assert result["slideshow"]["interval_seconds"] == 5      # clamped up
    assert result["slideshow"]["transition_seconds"] == 5    # clamped down
    assert result["slideshow"]["fit"] == "blur"              # back to default
    assert result["overlay"]["mode"] == "scheduled"
    assert result["overlay"]["start"] == "07:00"
    assert result["overlay"]["corner"] == "bottom-left"
    assert result["display"]["rotation"] == 0
    assert "bogus_section" not in result


def test_config_roundtrip_and_deep_merge(tmp_path):
    path = tmp_path / "config.json"
    config = Config(path)
    version = config.version

    config.update({"overlay": {"corner": "top-right"}})
    assert config.version > version
    # A partial patch must not wipe its siblings.
    assert config.section("overlay")["mode"] == "scheduled"
    assert config.section("overlay")["corner"] == "top-right"
    assert Config(path).section("overlay")["corner"] == "top-right"


def test_config_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ this is not json")
    assert Config(path).section("slideshow")["fit"] == "blur"


@pytest.mark.parametrize(
    "hour,minute,expected",
    [(8, 0, True), (22, 29, True), (23, 0, False), (6, 59, False)],
)
def test_in_window_same_day(hour, minute, expected):
    now = datetime(2026, 7, 26, hour, minute)
    assert in_window(now, "07:00", "22:30") is expected


@pytest.mark.parametrize(
    "hour,expected",
    [(23, True), (2, True), (6, True), (7, False), (12, False)],
)
def test_in_window_crossing_midnight(hour, expected):
    assert in_window(datetime(2026, 7, 26, hour, 30), "22:30", "07:00") is expected


def test_empty_window_is_never():
    assert in_window(datetime(2026, 7, 26, 12, 0), "09:00", "09:00") is False


# ------------------------------------------------------------------ library


def test_add_photo_creates_thumbnail_and_index(library):
    entry = library.add(make_image(), "Holiday Photo.jpg")
    assert entry["filename"] == "Holiday Photo.jpg"
    assert (library.thumbs / f"{entry['id']}.jpg").exists()
    assert len(library) == 1
    assert PhotoLibrary(library.root).ids() == [entry["id"]]  # index persisted


def test_reupload_is_deduplicated(library):
    data = make_image()
    first = library.add(data, "one.jpg")
    second = library.add(data, "again.jpg")
    assert first["id"] == second["id"]
    assert len(library) == 1


def test_rejects_unsupported_and_corrupt_files(library):
    with pytest.raises(LibraryError, match="unsupported"):
        library.add(b"data", "notes.txt")
    with pytest.raises(LibraryError, match="could not read"):
        library.add(b"not really a jpeg", "broken.jpg")
    with pytest.raises(LibraryError):
        library.add(b"", "empty.jpg")


@pytest.mark.parametrize("fit", ["contain", "cover", "blur"])
def test_render_matches_screen_size_exactly(library, fit):
    entry = library.add(make_image(size=(1600, 900)), "wide.jpg")
    rendered = library.render(entry["id"], (1024, 600), fit)
    assert rendered.size == (1024, 600)
    # Second call comes from the on-disk cache.
    assert library.render(entry["id"], (1024, 600), fit).size == (1024, 600)
    assert not list(library.uncached((1024, 600), fit))


def test_render_handles_portrait_source(library):
    entry = library.add(make_image(size=(600, 1400)), "tall.jpg")
    assert library.render(entry["id"], (1920, 1080), "blur").size == (1920, 1080)


def test_delete_removes_every_trace(library):
    entry = library.add(make_image(), "gone.jpg")
    library.render(entry["id"], (320, 240), "contain")
    assert library.delete(entry["id"]) is True
    assert library.delete(entry["id"]) is False
    assert len(library) == 0
    assert list(library.cache.glob("*.jpg")) == []
    assert library.thumb_path(entry["id"]) is None


def test_prune_cache_keeps_only_current_size(library):
    entry = library.add(make_image(), "p.jpg")
    library.render(entry["id"], (800, 600), "contain")
    library.render(entry["id"], (1920, 1080), "blur")
    assert library.prune_cache((1920, 1080), "blur") == 1
    assert len(list(library.cache.glob("*.jpg"))) == 1


def test_version_tracks_changes(library):
    start = library.version
    entry = library.add(make_image(), "v.jpg")
    assert library.version > start
    after_add = library.version
    library.delete(entry["id"])
    assert library.version > after_add


# ---------------------------------------------------------------------- web


def test_index_renders(client):
    http, *_ = client
    response = http.get("/")
    assert response.status_code == 200
    assert b"Photo Frame" in response.data


def test_upload_list_thumb_delete(client):
    http, _, library, *_ = client

    upload = http.post(
        "/api/photos",
        data={"photos": (io.BytesIO(make_image()), "beach.jpg")},
        content_type="multipart/form-data",
    )
    assert upload.status_code == 200
    photo_id = upload.get_json()["added"][0]["id"]
    assert len(library) == 1

    listing = http.get("/api/photos").get_json()
    assert [p["id"] for p in listing["photos"]] == [photo_id]

    thumb = http.get(f"/thumbs/{photo_id}.jpg")
    assert thumb.status_code == 200
    assert thumb.data[:2] == b"\xff\xd8"  # JPEG magic

    assert http.delete(f"/api/photos/{photo_id}").status_code == 200
    assert http.delete(f"/api/photos/{photo_id}").status_code == 404
    assert http.get(f"/thumbs/{photo_id}.jpg").status_code == 404


def test_upload_reports_per_file_errors_without_failing_the_batch(client):
    http, _, library, *_ = client
    response = http.post(
        "/api/photos",
        data={"photos": [
            (io.BytesIO(make_image()), "good.jpg"),
            (io.BytesIO(b"junk"), "bad.jpg"),
            (io.BytesIO(b"junk"), "notes.txt"),
        ]},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    assert response.status_code == 200
    assert len(body["added"]) == 1
    assert {e["filename"] for e in body["errors"]} == {"bad.jpg", "notes.txt"}
    assert len(library) == 1


def test_upload_with_no_files_is_a_400(client):
    http, *_ = client
    assert http.post("/api/photos", data={}, content_type="multipart/form-data").status_code == 400


def test_config_patch_is_partial_and_triggers_weather_refresh(client):
    http, config, _, _, weather = client

    response = http.put("/api/config", json={"slideshow": {"interval_seconds": 30}})
    assert response.status_code == 200
    assert config.section("slideshow")["interval_seconds"] == 30
    assert config.section("slideshow")["fit"] == "blur"  # untouched
    assert weather.refreshed is False

    http.put("/api/config", json={"weather": {"latitude": 55.9533}})
    assert weather.refreshed is True

    assert http.put("/api/config", json=[1, 2]).status_code == 400


def test_config_rejects_out_of_range_values(client):
    http, config, *_ = client
    http.put("/api/config", json={"display": {"rotation": 42}, "overlay": {"scale": 99}})
    assert config.section("display")["rotation"] == 0
    assert config.section("overlay")["scale"] == 2.5


def test_control_actions(client):
    http, _, _, state, _ = client
    assert http.post("/api/control", json={"action": "next"}).status_code == 200
    assert http.post("/api/control", json={"action": "toggle_pause"}).status_code == 200
    assert http.post("/api/control", json={"action": "explode"}).status_code == 400
    assert state.drain() == ["next", "toggle_pause"]


def test_status_includes_weather_and_counts(client):
    http, *_ = client
    body = http.get("/api/status").get_json()
    assert body["photo_count"] == 0
    assert body["weather"]["temperature"] == 21.4
    assert "uptime_seconds" in body


def test_password_gates_every_route(tmp_path):
    config = Config(tmp_path / "config.json")
    app = create_app(config, PhotoLibrary(tmp_path), FrameState(), FakeWeather(), password="hunter2")
    http = app.test_client()

    assert http.get("/api/status").status_code == 401
    assert http.get("/", headers=_basic("wrong")).status_code == 401
    assert http.get("/api/status", headers=_basic("hunter2")).status_code == 200


def _basic(password: str) -> dict[str, str]:
    import base64

    token = base64.b64encode(f"frame:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ------------------------------------------------------- overlay and loader


def test_overlay_composes_and_positions_by_corner():
    from photoframe.config import DEFAULTS
    from photoframe.overlay import OverlayRenderer

    renderer = OverlayRenderer((1920, 1080))
    settings = dict(DEFAULTS["overlay"])
    now = datetime(2026, 7, 26, 14, 32)

    surface, (x, y) = renderer.compose(settings, FakeWeather.current, now)
    assert surface.get_width() > 0
    assert x < 960 and y > 540  # bottom-left by default

    settings["corner"] = "top-right"
    _, (x2, y2) = renderer.compose(settings, FakeWeather.current, now)
    assert x2 > 960 and y2 < 540


def test_overlay_key_changes_only_when_the_text_does():
    from photoframe.config import DEFAULTS
    from photoframe.overlay import OverlayRenderer

    renderer = OverlayRenderer((1280, 720))
    settings = dict(DEFAULTS["overlay"])

    renderer.compose(settings, None, datetime(2026, 7, 26, 14, 32, 10))
    first = renderer.key
    renderer.compose(settings, None, datetime(2026, 7, 26, 14, 32, 50))
    assert renderer.key == first  # same minute, no repaint needed
    renderer.compose(settings, None, datetime(2026, 7, 26, 14, 33, 0))
    assert renderer.key != first


def test_overlay_returns_none_when_everything_is_switched_off():
    from photoframe.overlay import OverlayRenderer

    renderer = OverlayRenderer((800, 480))
    settings = {"clock": False, "date": False, "weather": False, "scale": 1.0, "corner": "top-left"}
    assert renderer.compose(settings, FakeWeather.current, datetime.now()) is None


@pytest.mark.parametrize("rotation,expected", [(0, (800, 480)), (90, (800, 480)), (180, (800, 480))])
def test_image_loader_returns_screen_sized_surface(library, rotation, expected):
    from photoframe.display import FAILED, ImageLoader

    entry = library.add(make_image(size=(1000, 700)), "loader.jpg")
    loader = ImageLoader(library)
    loader.start()
    try:
        surface = loader.take((entry["id"], (800, 480), "blur", rotation), timeout=20)
        assert surface is not FAILED and surface is not None
        assert surface.get_size() == expected
    finally:
        loader.stop()


def test_image_loader_flags_missing_photos(library):
    from photoframe.display import FAILED, ImageLoader

    loader = ImageLoader(library)
    loader.start()
    try:
        assert loader.take(("does-not-exist", (320, 240), "contain", 0), timeout=10) is FAILED
    finally:
        loader.stop()


# ------------------------------------------------------------- info override


@pytest.fixture
def frame(tmp_path):
    """A FrameDisplay wired up enough to exercise the overlay logic."""
    import photoframe.display as display_module
    from photoframe.config import Config

    config = Config(tmp_path / "config.json")
    library = PhotoLibrary(tmp_path)
    state = FrameState()

    display = display_module.FrameDisplay.__new__(display_module.FrameDisplay)
    display.config, display.library, display.state = config, library, state
    display.weather = FakeWeather()
    display._cfg = config.snapshot()
    return display, config, state


def test_info_shows_the_overlay_when_the_schedule_says_no(frame):
    display, config, _ = frame
    config.update({"overlay": {"mode": "off"}})
    display._cfg = config.snapshot()
    assert display._overlay_visible(datetime.now()) is False

    display._show_info(True)

    assert display._overlay_visible(datetime.now()) is True


def test_info_hides_the_overlay_when_the_schedule_says_yes(frame):
    """The reported bug: with the overlay already up, the button had no
    effect at all and there was no way to turn it off."""
    display, config, _ = frame
    config.update({"overlay": {"mode": "always"}})
    display._cfg = config.snapshot()
    assert display._overlay_visible(datetime.now()) is True

    display._show_info(False)

    assert display._overlay_visible(datetime.now()) is False


def test_the_info_override_expires(frame):
    import photoframe.display as display_module

    display, config, state = frame
    config.update({"overlay": {"mode": "off"}})
    display._cfg = config.snapshot()

    display._show_info(True)
    assert display._overlay_visible(datetime.now()) is True

    state.info_burst_until = time.time() - 1  # as if the ten minutes elapsed
    assert display._overlay_visible(datetime.now()) is False
    assert display_module.INFO_OVERRIDE_SECONDS == 600


@pytest.mark.parametrize("mode,starts_visible", [("always", True), ("off", False)])
def test_toggle_info_flips_whichever_way_it_is(frame, mode, starts_visible):
    display, config, _ = frame
    config.update({"overlay": {"mode": mode}})
    display._cfg = config.snapshot()
    assert display._overlay_visible(datetime.now()) is starts_visible

    for expected in (not starts_visible, starts_visible, not starts_visible):
        display.state.send("toggle_info")
        display._handle_commands()
        assert display._overlay_visible(datetime.now()) is expected


def test_showing_and_hiding_cancel_each_other(frame):
    display, config, state = frame
    config.update({"overlay": {"mode": "off"}})
    display._cfg = config.snapshot()

    display._show_info(True)
    display._show_info(False)
    assert state.info_burst_until == 0.0   # not left pending behind the mute
    assert display._overlay_visible(datetime.now()) is False

    display._show_info(True)
    assert state.info_mute_until == 0.0
    assert display._overlay_visible(datetime.now()) is True


def test_the_control_api_accepts_the_new_actions(client):
    http, _, _, state, _ = client
    for action in ("info_burst", "info_hide", "toggle_info"):
        assert http.post("/api/control", json={"action": action}).status_code == 200
    assert state.drain() == ["info_burst", "info_hide", "toggle_info"]
