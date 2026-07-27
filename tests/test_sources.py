"""Tests for network sources, syncing and the web mirror."""

from __future__ import annotations

import io
import json
import os
import time

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

from photoframe.config import Config, sanitise_sources
from photoframe.library import PhotoLibrary
from photoframe.sources import RemoteItem, SourceError, build_source
from photoframe.sources.icloud import extract_token
from photoframe.state import FrameState
from photoframe.sync import SyncManager
from photoframe.web import SECRET_PLACEHOLDER, create_app, redact_config, restore_secrets

from test_photoframe import FakeWeather, make_image


# ------------------------------------------------------------ config schema


def test_sources_are_validated_and_given_ids():
    cleaned = sanitise_sources([
        {"type": "folder", "name": "Pictures", "path": "/photos"},
        {"type": "nonsense", "name": "nope"},
        "not a dict",
        {"type": "photoprism", "url": "http://pp.local/", "size": "enormous", "album": "Trip"},
    ])
    assert [s["type"] for s in cleaned] == ["folder", "photoprism"]
    assert cleaned[0]["path"] == "/photos"
    assert cleaned[0]["enabled"] is True
    assert len(cleaned[0]["id"]) == 8              # generated
    assert cleaned[1]["size"] == "fit_1920"        # invalid value replaced
    assert cleaned[1]["url"] == "http://pp.local"  # trailing slash trimmed


def test_source_ids_are_unique():
    cleaned = sanitise_sources([
        {"type": "folder", "id": "same", "path": "/a"},
        {"type": "folder", "id": "same", "path": "/b"},
    ])
    assert cleaned[0]["id"] != cleaned[1]["id"]


def test_unknown_source_fields_are_dropped():
    cleaned = sanitise_sources([{"type": "folder", "path": "/p", "evil": "rm -rf"}])
    assert "evil" not in cleaned[0]


def test_sources_survive_a_config_roundtrip(tmp_path):
    config = Config(tmp_path / "config.json")
    config.update({"sources": [{"type": "smb", "name": "NAS", "server": "nas.local", "share": "photos"}]})
    reloaded = Config(tmp_path / "config.json").section("sources")
    assert reloaded[0]["server"] == "nas.local"
    assert reloaded[0]["port"] == 445  # default filled in


def test_config_file_is_not_world_readable(tmp_path):
    config = Config(tmp_path / "config.json")
    config.update({"sources": [{"type": "smb", "server": "nas", "share": "s", "password": "hunter2"}]})
    assert (tmp_path / "config.json").stat().st_mode & 0o077 == 0


# ---------------------------------------------------------------- redaction


def test_secrets_never_reach_the_browser():
    settings = {"sources": [
        {"id": "a", "type": "smb", "password": "hunter2", "token": ""},
    ]}
    redacted = redact_config(settings)
    assert redacted["sources"][0]["password"] == SECRET_PLACEHOLDER
    assert redacted["sources"][0]["token"] == ""  # empty stays empty, not a fake value


def test_placeholder_restores_the_stored_secret():
    current = [{"id": "a", "type": "smb", "password": "hunter2"}]
    patch = restore_secrets({"sources": [{"id": "a", "password": SECRET_PLACEHOLDER}]}, current)
    assert patch["sources"][0]["password"] == "hunter2"


def test_a_real_new_password_is_kept():
    current = [{"id": "a", "type": "smb", "password": "old"}]
    patch = restore_secrets({"sources": [{"id": "a", "password": "new"}]}, current)
    assert patch["sources"][0]["password"] == "new"


def test_config_endpoint_hides_and_preserves_passwords(tmp_path):
    config = Config(tmp_path / "config.json")
    library = PhotoLibrary(tmp_path)
    app = create_app(config, library, FrameState(), FakeWeather())
    http = app.test_client()

    http.put("/api/config", json={"sources": [
        {"id": "s1", "type": "smb", "name": "NAS", "server": "nas", "share": "p", "password": "hunter2"},
    ]})
    body = http.get("/api/config").get_json()
    assert body["sources"][0]["password"] == SECRET_PLACEHOLDER

    # Saving the page back unchanged must not wipe the stored password.
    http.put("/api/config", json={"sources": body["sources"]})
    assert config.section("sources")[0]["password"] == "hunter2"


# ------------------------------------------------------------ folder source


def test_folder_source_lists_images_recursively(tmp_path):
    root = tmp_path / "photos"
    (root / "nested").mkdir(parents=True)
    (root / "one.jpg").write_bytes(make_image())
    (root / "nested" / "two.png").write_bytes(make_image(colour=(10, 120, 30), fmt="PNG"))
    (root / "notes.txt").write_text("ignore me")

    source = build_source({"id": "f", "type": "folder", "name": "F", "path": str(root), "limit": 500})
    items = source.list_items()
    assert sorted(item.filename for item in items) == ["one.jpg", "two.png"]
    assert source.fetch(items[0])[:2] in (b"\xff\xd8", b"\x89P")


def test_folder_source_reports_a_missing_directory(tmp_path):
    source = build_source({"id": "f", "type": "folder", "name": "F", "path": str(tmp_path / "nope")})
    with pytest.raises(SourceError, match="does not exist"):
        source.list_items()


def test_folder_key_changes_when_the_file_does(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    target = root / "a.jpg"
    target.write_bytes(make_image())
    source = build_source({"id": "f", "type": "folder", "name": "F", "path": str(root)})
    before = source.list_items()[0].key

    time.sleep(1.1)  # mtime has one-second resolution
    target.write_bytes(make_image(colour=(9, 9, 200)))
    assert source.list_items()[0].key != before


def test_unknown_source_type_is_rejected():
    with pytest.raises(SourceError, match="Unknown source type"):
        build_source({"id": "x", "type": "wat", "name": "x"})


# --------------------------------------------------------------- smb source


class FakeSmbEntry:
    def __init__(self, name, directory=False):
        self.name = name
        self._dir = directory

    def is_dir(self):
        return self._dir

    def stat(self):
        return type("S", (), {"st_size": 10, "st_mtime": 1000})()


class FakeSmbClient:
    """Records the kwargs each call receives, so we can assert on the port."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def register_session(self, server, **kwargs):
        self.calls.append(("register_session", kwargs))

    def scandir(self, path, **kwargs):
        self.calls.append(("scandir", kwargs))
        if path.endswith("PHOTOS"):
            return [FakeSmbEntry("holiday.jpg"), FakeSmbEntry("notes.txt")]
        return []

    def open_file(self, path, mode="rb", **kwargs):
        self.calls.append(("open_file", kwargs))

        class Handle:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                return b"jpeg-bytes"

        return Handle()


def test_smb_passes_the_port_to_every_call(monkeypatch):
    """A UNC path carries no port, so each call must be told it explicitly —
    otherwise a non-standard port silently reconnects on 445."""
    import photoframe.sources.smb as smb_module

    fake = FakeSmbClient()
    monkeypatch.setattr(smb_module, "_smbclient", lambda: fake)

    source = build_source({
        "id": "s", "type": "smb", "name": "NAS", "limit": 500,
        "server": "nas.local", "share": "PHOTOS", "path": "", "port": 4445,
        "username": "u", "password": "p", "domain": "",
    })
    items = source.list_items()
    assert [item.filename for item in items] == ["holiday.jpg"]  # .txt filtered out
    assert source.fetch(items[0]) == b"jpeg-bytes"

    for name, kwargs in fake.calls:
        assert kwargs.get("port") == 4445, f"{name} lost the port"


def test_smb_needs_a_server_and_share():
    source = build_source({"id": "s", "type": "smb", "name": "NAS", "server": "", "share": ""})
    with pytest.raises(SourceError, match="server and a share"):
        source.list_items()


# ----------------------------------------------------- testing before saving


@pytest.fixture
def sourced_client(tmp_path, monkeypatch):
    """A client whose sources can be tested, with a stubbed source builder."""
    import photoframe.web as web_module

    config = Config(tmp_path / "config.json")
    library = PhotoLibrary(tmp_path)
    manager = SyncManager(config, library, tmp_path / "sync.json")

    seen: dict = {}

    class Probe:
        def __init__(self, cfg):
            seen.clear()
            seen.update(cfg)

        def check(self):
            return f"Connected to {seen.get('url') or seen.get('path')}"

    monkeypatch.setattr(web_module, "build_source", lambda cfg: Probe(cfg))
    app = create_app(config, library, FrameState(), FakeWeather(), sync=manager)
    app.config.update(TESTING=True)
    return app.test_client(), config, seen


def test_an_unsaved_source_can_still_be_tested(sourced_client):
    """The regression: a source added in the browser isn't on the server yet,
    so testing by id alone could only ever say 'No such source'."""
    http, config, seen = sourced_client
    assert config.section("sources") == []  # nothing saved

    response = http.post("/api/sources/new-abc123/test", json={
        "id": "new-abc123", "type": "photoprism", "name": "PhotoFrame",
        "url": "https://photoprism.example.com", "album": "PhotoFrame", "token": "secret",
    })
    body = response.get_json()
    assert body["ok"] is True
    assert "photoprism.example.com" in body["message"]
    assert seen["token"] == "secret"       # the typed-in values were used
    assert config.section("sources") == []  # and nothing was saved as a side effect


def test_testing_an_edited_source_keeps_the_stored_password(sourced_client):
    http, config, seen = sourced_client
    config.update({"sources": [{
        "type": "photoprism", "id": "pp1", "name": "PP",
        "url": "https://pp.example.com", "album": "A", "token": "stored-secret",
    }]})

    # The browser echoes the placeholder back, never the real credential.
    http.post("/api/sources/pp1/test", json={
        "id": "pp1", "type": "photoprism", "name": "PP",
        "url": "https://pp.example.com", "album": "B", "token": SECRET_PLACEHOLDER,
    })
    assert seen["token"] == "stored-secret"
    assert seen["album"] == "B"  # the edit under test is honoured


def test_testing_without_a_body_falls_back_to_stored_settings(sourced_client, tmp_path):
    """No body means "test what's saved" — the path an API caller takes."""
    http, config, _ = sourced_client
    folder = tmp_path / "stored"
    folder.mkdir()
    (folder / "a.jpg").write_bytes(make_image())
    config.update({"sources": [{"type": "folder", "id": "f1", "name": "F", "path": str(folder)}]})

    body = http.post("/api/sources/f1/test").get_json()

    assert body["ok"] is True
    assert "1 photo" in body["message"]


def test_testing_an_unknown_id_without_a_body_explains_itself(sourced_client):
    http, _, _ = sourced_client
    body = http.post("/api/sources/nope/test").get_json()
    assert body["ok"] is False
    assert "Save settings" in body["message"]


def test_syncing_an_unsaved_source_is_rejected(sourced_client):
    """Unlike Test, this writes to the library, so it needs a saved source —
    and an unknown id must not sit in the queue forever."""
    http, _, _ = sourced_client
    response = http.post("/api/sources/new-abc123/sync")
    assert response.status_code == 404
    assert "Save settings" in response.get_json()["error"]


def test_photoprism_401_names_the_credential():
    from photoframe.sources.photoprism import PhotoPrismSource

    class Response:
        status_code = 401

    source = PhotoPrismSource({"id": "p", "type": "photoprism", "name": "P", "token": "abc"})
    with pytest.raises(SourceError, match="rejected that app password"):
        source._raise_for_status(Response(), "read data")

    source = PhotoPrismSource({"id": "p", "type": "photoprism", "name": "P",
                               "username": "u", "password": "p", "token": ""})
    with pytest.raises(SourceError, match="rejected that username and password"):
        source._raise_for_status(Response(), "read data")


# ------------------------------------------------------------ icloud helper


@pytest.mark.parametrize("value,expected", [
    ("https://www.icloud.com/sharedalbum/#B0dGvVKq3G0Qxyz", "B0dGvVKq3G0Qxyz"),
    ("B0dGvVKq3G0Qxyz", "B0dGvVKq3G0Qxyz"),
    ("https://www.icloud.com/sharedalbum/#B0dGvVKq3G0Qxyz?utm=1", "B0dGvVKq3G0Qxyz"),
])
def test_icloud_token_extraction(value, expected):
    assert extract_token(value) == expected


@pytest.mark.parametrize("value", ["", "https://example.com/", "short"])
def test_icloud_rejects_rubbish_links(value):
    with pytest.raises(SourceError):
        extract_token(value)


# ------------------------------------------------------------------- syncing


class FakeSource:
    """A source we can drive from a test, registered via monkeypatch."""

    def __init__(self, config):
        self.config = config
        self.id = config["id"]
        self.name = config["name"]

    items: list[RemoteItem] = []
    payloads: dict[str, bytes] = {}
    fail_on: set[str] = set()

    def list_items(self):
        return list(FakeSource.items)

    def fetch(self, item):
        if item.key in FakeSource.fail_on:
            raise SourceError("nope")
        return FakeSource.payloads[item.key]

    def check(self):
        return f"Connected — {len(FakeSource.items)} photo(s)"


@pytest.fixture
def synced(tmp_path, monkeypatch):
    """A SyncManager wired to FakeSource, plus its config and library."""
    import photoframe.sync as sync_module

    monkeypatch.setattr(sync_module, "build_source", lambda cfg: FakeSource(cfg))
    monkeypatch.setattr(sync_module, "DOWNLOAD_PAUSE", 0)

    FakeSource.items = []
    FakeSource.payloads = {}
    FakeSource.fail_on = set()

    config = Config(tmp_path / "config.json")
    config.update({"sources": [{
        "type": "folder", "id": "src1", "name": "Fake", "path": "/x",
        "enabled": True, "interval_minutes": 30, "limit": 500, "remove_deleted": True,
    }]})
    library = PhotoLibrary(tmp_path)
    manager = SyncManager(config, library, tmp_path / "sync.json")
    return manager, config, library


def stock(manager, keys_to_colour):
    FakeSource.items = [RemoteItem(key=key, filename=f"{key}.jpg") for key in keys_to_colour]
    FakeSource.payloads = {key: make_image(colour=colour) for key, colour in keys_to_colour.items()}


def test_sync_imports_new_photos(synced):
    manager, config, library = synced
    stock(manager, {"a": (10, 20, 30), "b": (40, 50, 60)})

    manager._sync_one(config.section("sources")[0])

    assert len(library) == 2
    assert all(photo["origins"] == ["src1"] for photo in library.list_photos())
    assert manager.status()[0]["last_added"] == 2
    assert manager.status()[0]["last_error"] is None


def test_sync_is_incremental(synced):
    manager, config, library = synced
    source = config.section("sources")[0]
    stock(manager, {"a": (10, 20, 30)})
    manager._sync_one(source)

    stock(manager, {"a": (10, 20, 30), "b": (40, 50, 60)})
    manager._sync_one(source)

    assert len(library) == 2
    assert manager.status()[0]["last_added"] == 1  # only the new one


def test_photos_removed_upstream_are_deleted(synced):
    manager, config, library = synced
    source = config.section("sources")[0]
    stock(manager, {"a": (10, 20, 30), "b": (40, 50, 60)})
    manager._sync_one(source)

    stock(manager, {"a": (10, 20, 30)})
    manager._sync_one(source)

    assert len(library) == 1


def test_remove_deleted_can_be_switched_off(synced):
    manager, config, library = synced
    config.update({"sources": [dict(config.section("sources")[0], remove_deleted=False)]})
    source = config.section("sources")[0]

    stock(manager, {"a": (10, 20, 30), "b": (40, 50, 60)})
    manager._sync_one(source)
    stock(manager, {"a": (10, 20, 30)})
    manager._sync_one(source)

    assert len(library) == 2


def test_uploads_survive_a_source_dropping_the_same_photo(synced):
    manager, config, library = synced
    source = config.section("sources")[0]
    shared = make_image(colour=(77, 88, 99))

    library.add(shared, "mine.jpg")  # uploaded by hand
    FakeSource.items = [RemoteItem(key="a", filename="a.jpg")]
    FakeSource.payloads = {"a": shared}
    manager._sync_one(source)

    photo = library.list_photos()[0]
    assert sorted(photo["origins"]) == ["src1", "upload"]

    FakeSource.items = []  # vanishes upstream
    manager._sync_one(source)

    assert len(library) == 1  # the manual upload keeps it alive
    assert library.list_photos()[0]["origins"] == ["upload"]


def test_one_bad_photo_does_not_stop_the_batch(synced):
    manager, config, library = synced
    stock(manager, {"a": (10, 20, 30), "b": (40, 50, 60), "c": (70, 80, 90)})
    FakeSource.fail_on = {"b"}

    manager._sync_one(config.section("sources")[0])

    assert len(library) == 2
    assert manager.status()[0]["last_error"] is None  # per-item failures aren't source failures


def test_source_failure_is_recorded_not_raised(synced, monkeypatch):
    manager, config, library = synced

    def explode(self):
        raise SourceError("NAS is asleep")

    monkeypatch.setattr(FakeSource, "list_items", explode)
    manager._sync_one(config.section("sources")[0])

    assert "NAS is asleep" in manager.status()[0]["last_error"]
    assert len(library) == 0


def test_limit_caps_what_is_imported(synced):
    manager, config, library = synced
    config.update({"sources": [dict(config.section("sources")[0], limit=2)]})
    stock(manager, {"a": (1, 2, 3), "b": (4, 5, 6), "c": (7, 8, 9)})

    manager._sync_one(config.section("sources")[0])

    assert len(library) == 2


def test_removing_a_source_drops_its_photos(synced):
    manager, config, library = synced
    stock(manager, {"a": (10, 20, 30)})
    manager._sync_one(config.section("sources")[0])
    assert len(library) == 1

    config.update({"sources": []})
    manager._prune_removed()

    assert len(library) == 0
    assert manager.status() == []


def test_a_manually_deleted_photo_is_not_re_fetched(synced):
    """Deleting one in the web grid means "not on my frame", so a later sync
    must not quietly bring it back."""
    manager, config, library = synced
    source = config.section("sources")[0]
    stock(manager, {"a": (10, 20, 30)})
    manager._sync_one(source)

    library.delete(library.ids()[0])
    manager._sync_one(source)

    assert len(library) == 0


def test_progress_is_reported_while_a_sync_runs(synced, monkeypatch):
    """A few hundred photos take minutes; the page needs to show movement
    rather than the previous run's outcome."""
    manager, config, library = synced
    stock(manager, {"a": (1, 2, 3), "b": (4, 5, 6), "c": (7, 8, 9)})

    seen: list[tuple] = []
    real_add = library.add

    def add_and_peek(*args, **kwargs):
        entry = real_add(*args, **kwargs)
        row = manager.status()[0]
        seen.append((row["syncing"], row["progress"]["added"], row["progress"]["total"]))
        return entry

    monkeypatch.setattr(library, "add", add_and_peek)
    manager._sync_one(config.section("sources")[0])

    # Reported mid-run, counting up towards the number actually being fetched.
    assert [row[0] for row in seen] == [True, True, True]
    assert [row[1] for row in seen] == [0, 1, 2]
    assert all(row[2] == 3 for row in seen)

    # And cleared once it's over.
    finished = manager.status()[0]
    assert finished["syncing"] is False
    assert finished["progress"] is None


def test_progress_counts_only_what_needs_fetching(synced):
    manager, config, library = synced
    stock(manager, {"a": (1, 2, 3)})
    manager._sync_one(config.section("sources")[0])

    # Second run: nothing new, so nothing to report.
    stock(manager, {"a": (1, 2, 3), "b": (4, 5, 6)})
    totals: list[int] = []
    real_add = library.add
    manager.library.add = lambda *a, **k: (totals.append(manager._progress[1]), real_add(*a, **k))[1]
    manager._sync_one(config.section("sources")[0])

    assert totals == [1]  # only "b" counted, not the already-known "a"


def test_sync_state_persists(synced, tmp_path):
    manager, config, library = synced
    stock(manager, {"a": (10, 20, 30)})
    manager._sync_one(config.section("sources")[0])

    stored = json.loads((tmp_path / "sync.json").read_text())
    assert list(stored["sources"]["src1"]["items"]) == ["a"]

    # A fresh manager doesn't re-import what's already here.
    again = SyncManager(config, library, tmp_path / "sync.json")
    again._sync_one(config.section("sources")[0])
    assert again.status()[0]["last_added"] == 0


def test_due_check_respects_the_interval(synced):
    manager, config, _ = synced
    source = config.section("sources")[0]
    assert manager._is_due(source) is True  # never run

    manager._sync_one(source)
    assert manager._is_due(source) is False

    manager._record("src1")["last_run"] = time.time() - 31 * 60
    assert manager._is_due(source) is True


# ------------------------------------------------------------------- mirror


@pytest.fixture
def mirror_client(tmp_path):
    config = Config(tmp_path / "config.json")
    state = FrameState()
    app = create_app(config, PhotoLibrary(tmp_path), state, FakeWeather())
    app.config.update(TESTING=True)
    return app.test_client(), state, config


def test_frame_endpoint_serves_a_jpeg(mirror_client):
    http, state, _ = mirror_client
    state.publish_frame(bytes(4 * 3 * 3), (4, 3))  # 4x3 black RGB

    response = http.get("/api/frame.jpg")
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert response.data[:2] == b"\xff\xd8"
    assert response.headers["X-Frame-Generation"] == "1"


def test_frame_endpoint_reports_when_nothing_is_rendering(mirror_client):
    http, _, _ = mirror_client
    assert http.get("/api/frame.jpg").status_code == 503


def test_frame_endpoint_respects_the_off_switch(mirror_client):
    http, state, config = mirror_client
    state.publish_frame(bytes(4 * 3 * 3), (4, 3))
    config.update({"mirror": {"enabled": False}})
    assert http.get("/api/frame.jpg").status_code == 403


def test_long_poll_returns_304_when_nothing_changed(mirror_client, monkeypatch):
    import photoframe.web as web_module

    monkeypatch.setattr(web_module, "LONG_POLL_SECONDS", 1.0)  # don't wait the full 25s
    http, state, _ = mirror_client
    state.publish_frame(bytes(4 * 3 * 3), (4, 3))

    started = time.monotonic()
    response = http.get("/api/frame.jpg?since=1")
    assert response.status_code == 304
    assert time.monotonic() - started >= 1  # it really did park rather than return at once


def test_long_poll_wakes_on_a_new_frame(mirror_client):
    import threading

    http, state, _ = mirror_client
    state.publish_frame(bytes(4 * 3 * 3), (4, 3))
    threading.Timer(0.3, lambda: state.publish_frame(bytes(4 * 3 * 3), (4, 3))).start()

    started = time.monotonic()
    response = http.get("/api/frame.jpg?since=1")
    assert response.status_code == 200
    assert response.headers["X-Frame-Generation"] == "2"
    assert time.monotonic() - started < 5  # woke on the event, didn't time out


def test_max_width_shrinks_the_mirror(mirror_client):
    from PIL import Image

    http, state, config = mirror_client
    state.publish_frame(bytes(200 * 100 * 3), (200, 100))
    config.update({"mirror": {"max_width": 50}})

    image = Image.open(io.BytesIO(http.get("/api/frame.jpg").data))
    assert image.size == (50, 25)


def test_requesting_a_frame_signals_demand(mirror_client):
    http, state, _ = mirror_client
    assert state.mirror_active() is False
    http.get("/api/frame.jpg")  # 503, but demand is still registered
    assert state.mirror_active() is True


def test_mirror_page_renders(mirror_client):
    http, _, _ = mirror_client
    response = http.get("/mirror")
    assert response.status_code == 200
    assert b"mirror.js" in response.data


def test_sources_api_without_sync_is_harmless(mirror_client):
    http, _, _ = mirror_client
    assert http.get("/api/sources").get_json()["sources"] == []
    assert http.post("/api/sources/x/test").status_code == 503
