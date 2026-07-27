"""Tests for disk-space monitoring and the guard rails it drives."""

from __future__ import annotations

import os
from collections import namedtuple

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

from photoframe.config import Config
from photoframe.library import PhotoLibrary
from photoframe.state import FrameState
from photoframe.storage import MB, StorageMonitor, directory_size
from photoframe.sync import SyncManager
from photoframe.web import create_app

from test_photoframe import FakeWeather, make_image
from test_sources import FakeSource, stock  # noqa: F401 - stock() is used below

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def monitor(tmp_path):
    config = Config(tmp_path / "config.json")
    library = PhotoLibrary(tmp_path)
    return StorageMonitor(config, library, tmp_path, ttl=0), config, library


def fake_disk(monkeypatch, free_mb, total_mb=8192):
    """Pretend the disk has `free_mb` left."""
    import photoframe.storage as storage_module

    monkeypatch.setattr(
        storage_module.shutil, "disk_usage",
        lambda _path: Usage(total_mb * MB, (total_mb - free_mb) * MB, free_mb * MB),
    )


# ------------------------------------------------------------------ measuring


def test_directory_size_adds_up_recursively(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.bin").write_bytes(b"x" * 1000)
    (tmp_path / "nested" / "b.bin").write_bytes(b"x" * 500)
    assert directory_size(tmp_path) == 1500


def test_directory_size_of_a_missing_path_is_zero(tmp_path):
    assert directory_size(tmp_path / "nope") == 0


def test_snapshot_breaks_down_what_the_frame_uses(monitor):
    storage, _, library = monitor
    library.add(make_image(size=(1600, 1200)), "one.jpg")
    library.render(library.ids()[0], (800, 600), "contain")

    report = storage.snapshot()
    assert report["photo_count"] == 1
    assert report["breakdown"]["originals"] > 0
    assert report["breakdown"]["thumbnails"] > 0
    assert report["breakdown"]["cache"] > 0
    # Everything measured should be accounted for in the total.
    assert report["frame_bytes"] >= sum(report["breakdown"].values()) - report["breakdown"]["other"]
    assert report["bytes_per_photo"] > 0


def test_snapshot_is_cached_until_invalidated(tmp_path):
    config = Config(tmp_path / "config.json")
    library = PhotoLibrary(tmp_path)
    storage = StorageMonitor(config, library, tmp_path, ttl=300)

    assert storage.snapshot()["cached"] is False
    assert storage.snapshot()["cached"] is True      # served from the cache
    storage.invalidate()
    assert storage.snapshot()["cached"] is False     # measured again


def test_summary_survives_an_unreadable_path(tmp_path, monkeypatch):
    import photoframe.storage as storage_module

    config = Config(tmp_path / "config.json")
    storage = StorageMonitor(config, PhotoLibrary(tmp_path), tmp_path)
    monkeypatch.setattr(storage_module.shutil, "disk_usage",
                        lambda _p: (_ for _ in ()).throw(OSError("gone")))

    report = storage.summary()
    assert report["free_bytes"] == 0
    assert report["free_percent"] == 0.0


# ----------------------------------------------------------------- thresholds


@pytest.mark.parametrize("free_mb,expected", [(5000, "ok"), (500, "warn"), (100, "critical")])
def test_level_follows_the_thresholds(monitor, monkeypatch, free_mb, expected):
    storage, _, _ = monitor  # defaults: warn 1024 MB, minimum 256 MB
    fake_disk(monkeypatch, free_mb)
    assert storage.level() == expected


def test_has_room_respects_the_minimum(monitor, monkeypatch):
    storage, _, _ = monitor
    fake_disk(monkeypatch, 300)
    assert storage.has_room() is True
    fake_disk(monkeypatch, 200)
    assert storage.has_room() is False


def test_a_warning_below_the_hard_stop_is_lifted(tmp_path):
    """A warning that fired later than the stop would never be seen."""
    config = Config(tmp_path / "config.json")
    config.update({"storage": {"min_free_mb": 500, "warn_free_mb": 100}})
    assert config.section("storage")["warn_free_mb"] == 500


def test_a_floor_bigger_than_the_disk_cannot_lock_the_frame_out(monitor, monkeypatch):
    """A 256 MB floor on a 64 MB volume would refuse every photo forever."""
    storage, config, _ = monitor
    config.update({"storage": {"min_free_mb": 256, "warn_free_mb": 1024}})
    fake_disk(monkeypatch, free_mb=60, total_mb=64)

    minimum, warn, _ = storage._thresholds()
    assert minimum == int(64 * MB * 0.25)   # capped at a quarter of the disk
    assert warn == minimum
    assert storage.has_room() is True       # 60 MB free clears a 16 MB floor
    assert storage.level() == "ok"


def test_the_cap_leaves_a_normal_sd_card_alone(monitor, monkeypatch):
    storage, _, _ = monitor
    fake_disk(monkeypatch, free_mb=20000, total_mb=32000)
    minimum, warn, _ = storage._thresholds()
    assert minimum == 256 * MB    # defaults, untouched
    assert warn == 1024 * MB


def test_thresholds_are_clamped(tmp_path):
    config = Config(tmp_path / "config.json")
    config.update({"storage": {"min_free_mb": 0, "warn_free_mb": -5}})
    assert config.section("storage")["min_free_mb"] == 32


# --------------------------------------------------------------------- actions


def test_trim_cache_reclaims_renders_only(monitor):
    storage, _, library = monitor
    library.add(make_image(), "keep.jpg")
    photo_id = library.ids()[0]
    library.render(photo_id, (640, 480), "contain")
    assert list(library.cache.glob("*.jpg"))

    freed = storage.trim_cache()

    assert freed > 0
    assert list(library.cache.glob("*.jpg")) == []
    assert len(library) == 1                                   # photo still there
    assert library.render(photo_id, (640, 480), "contain")     # and rebuildable


def test_make_room_trims_when_that_is_enough(monitor, monkeypatch):
    storage, _, library = monitor
    library.add(make_image(), "a.jpg")
    library.render(library.ids()[0], (640, 480), "contain")

    # Start below the floor; trimming genuinely recovers space, so model that
    # rather than counting how many times the disk is inspected.
    import photoframe.storage as storage_module

    disk = {"free": 200}
    monkeypatch.setattr(storage_module.shutil, "disk_usage",
                        lambda _p: Usage(8192 * MB, 0, disk["free"] * MB))
    real_trim = storage.trim_cache

    def trim_and_recover():
        freed = real_trim()
        disk["free"] = 400
        return freed

    monkeypatch.setattr(storage, "trim_cache", trim_and_recover)

    assert storage.make_room() is True
    assert list(library.cache.glob("*.jpg")) == []


def test_make_room_gives_up_when_trimming_is_not_enough(monitor, monkeypatch):
    storage, _, _ = monitor
    fake_disk(monkeypatch, 10)
    assert storage.make_room() is False


def test_make_room_leaves_the_cache_alone_when_told_to(monitor, monkeypatch):
    storage, config, library = monitor
    config.update({"storage": {"trim_cache_when_low": False}})
    library.add(make_image(), "a.jpg")
    library.render(library.ids()[0], (640, 480), "contain")
    fake_disk(monkeypatch, 100)

    assert storage.make_room() is False
    assert list(library.cache.glob("*.jpg"))  # untouched


# ----------------------------------------------------------------- web surface


@pytest.fixture
def client(tmp_path):
    config = Config(tmp_path / "config.json")
    library = PhotoLibrary(tmp_path)
    storage = StorageMonitor(config, library, tmp_path, ttl=0)
    app = create_app(config, library, FrameState(), FakeWeather(), storage=storage)
    app.config.update(TESTING=True)
    return app.test_client(), config, library, storage


def test_status_carries_a_storage_summary(client):
    http, *_ = client
    disk = http.get("/api/status").get_json()["storage"]
    assert disk["level"] in {"ok", "warn", "critical"}
    assert disk["total_bytes"] > 0
    assert "breakdown" not in disk  # the expensive part stays out of status


def test_storage_endpoint_returns_the_breakdown(client):
    http, _, library, _ = client
    library.add(make_image(), "a.jpg")
    body = http.get("/api/storage?refresh=1").get_json()
    assert body["photo_count"] == 1
    assert set(body["breakdown"]) == {"originals", "thumbnails", "cache", "other"}


def test_trim_endpoint_frees_the_cache(client):
    http, _, library, _ = client
    library.add(make_image(), "a.jpg")
    library.render(library.ids()[0], (320, 240), "contain")

    body = http.post("/api/storage/trim").get_json()
    assert body["ok"] is True
    assert body["freed_bytes"] > 0
    assert list(library.cache.glob("*.jpg")) == []


def test_upload_is_refused_when_the_disk_is_full(client, monkeypatch):
    import io

    http, _, library, _ = client
    fake_disk(monkeypatch, 10)

    response = http.post(
        "/api/photos",
        data={"photos": (io.BytesIO(make_image()), "big.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 507  # Insufficient Storage
    assert "MB free" in response.get_json()["error"]
    assert len(library) == 0


def test_storage_routes_are_harmless_without_a_monitor(tmp_path):
    config = Config(tmp_path / "config.json")
    app = create_app(config, PhotoLibrary(tmp_path), FrameState(), FakeWeather())
    http = app.test_client()
    assert http.get("/api/storage").status_code == 503
    assert http.post("/api/storage/trim").status_code == 503
    assert http.get("/api/status").get_json()["storage"] is None


# ---------------------------------------------------------------- sync guard


@pytest.fixture
def synced_with_disk(tmp_path, monkeypatch):
    import photoframe.sync as sync_module

    monkeypatch.setattr(sync_module, "build_source", lambda cfg: FakeSource(cfg))
    monkeypatch.setattr(sync_module, "DOWNLOAD_PAUSE", 0)
    FakeSource.items, FakeSource.payloads, FakeSource.fail_on = [], {}, set()

    config = Config(tmp_path / "config.json")
    config.update({"sources": [{
        "type": "folder", "id": "src1", "name": "Fake", "path": "/x",
        "enabled": True, "interval_minutes": 30, "limit": 500, "remove_deleted": True,
    }]})
    library = PhotoLibrary(tmp_path)
    storage = StorageMonitor(config, library, tmp_path, ttl=0)
    manager = SyncManager(config, library, tmp_path / "sync.json", storage=storage)
    return manager, config, library, storage


def test_sync_stops_importing_when_space_runs_out(synced_with_disk, monkeypatch):
    manager, config, library, _ = synced_with_disk
    stock(manager, {"a": (1, 2, 3), "b": (4, 5, 6)})
    fake_disk(monkeypatch, 10)

    manager._sync_one(config.section("sources")[0])

    assert len(library) == 0
    assert "below the" in manager.status()[0]["last_error"]


def test_the_shortage_message_is_not_overwritten_by_the_success_path(synced_with_disk, monkeypatch):
    """The tidy-up after the import loop must not clear the error."""
    manager, config, library, _ = synced_with_disk
    stock(manager, {"a": (1, 2, 3)})
    fake_disk(monkeypatch, 10)

    manager._sync_one(config.section("sources")[0])

    assert manager.status()[0]["last_error"] is not None


def test_sync_proceeds_when_the_guard_is_switched_off(synced_with_disk, monkeypatch):
    manager, config, library, _ = synced_with_disk
    config.update({"storage": {"pause_sync_when_low": False}})
    stock(manager, {"a": (1, 2, 3)})
    fake_disk(monkeypatch, 10)

    manager._sync_one(config.section("sources")[0])

    assert len(library) == 1
    assert manager.status()[0]["last_error"] is None


def test_sync_without_a_monitor_is_unaffected(tmp_path, monkeypatch):
    import photoframe.sync as sync_module

    monkeypatch.setattr(sync_module, "build_source", lambda cfg: FakeSource(cfg))
    monkeypatch.setattr(sync_module, "DOWNLOAD_PAUSE", 0)
    config = Config(tmp_path / "config.json")
    config.update({"sources": [{"type": "folder", "id": "s", "name": "F", "path": "/x"}]})
    library = PhotoLibrary(tmp_path)
    manager = SyncManager(config, library, tmp_path / "sync.json")  # no storage

    stock(manager, {"a": (1, 2, 3)})
    manager._sync_one(config.section("sources")[0])

    assert len(library) == 1
