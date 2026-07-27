"""Background sync between network sources and the local library.

One thread services every source in turn. Sources are polled on their own
interval, downloads are paced so a big first sync doesn't monopolise the Pi,
and a failure anywhere is recorded against that source rather than raised —
a NAS that's switched off must never stop the slideshow.

What each source has already contributed is tracked in `sync.json`, mapping
the source's own key for a photo to the library id it became. That's what lets
us notice a photo has been removed upstream and drop it here too.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .library import LibraryError
from .sources import SourceError, build_source

DOWNLOAD_PAUSE = 0.4  # seconds between fetches, to leave the frame responsive
TICK = 20  # how often to look for sources that are due


class SyncManager(threading.Thread):
    def __init__(self, config, library, state_path: Path, storage=None):
        super().__init__(name="sync", daemon=True)
        self.config = config
        self.library = library
        self.storage = storage
        self.state_path = Path(state_path)
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._load()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._forced: set[str] = set()
        self._active: str | None = None

    # ---------------------------------------------------------------- state

    def _load(self) -> dict[str, Any]:
        try:
            with self.state_path.open(encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict) and isinstance(stored.get("sources"), dict):
                return stored
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[sync] state unreadable ({exc}); starting fresh")
        return {"sources": {}}

    def _save(self) -> None:
        with self._lock:
            payload = json.dumps(self._state, indent=2, sort_keys=True)
        tmp = self.state_path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(payload + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self.state_path)
        except OSError as exc:
            print(f"[sync] could not save state: {exc}")

    def _record(self, source_id: str) -> dict[str, Any]:
        with self._lock:
            return self._state["sources"].setdefault(
                source_id, {"items": {}, "last_run": None, "last_error": None, "last_added": 0}
            )

    # ------------------------------------------------------------------ api

    def status(self) -> list[dict[str, Any]]:
        """One row per configured source, for the settings page."""
        rows = []
        for source in self.config.section("sources"):
            record = self._record(source["id"])
            rows.append({
                "id": source["id"],
                "name": source["name"],
                "type": source["type"],
                "enabled": source["enabled"],
                "photos": len(record.get("items", {})),
                "last_run": record.get("last_run"),
                "last_error": record.get("last_error"),
                "last_added": record.get("last_added", 0),
                "syncing": self._active == source["id"],
            })
        return rows

    def sync_now(self, source_id: str | None = None) -> bool:
        """Run one or all sources at the next tick.

        False means there's no such source — usually a source added in the
        browser but not saved yet. Unknown ids are rejected rather than
        queued, since nothing would ever pick them up.
        """
        known = {source["id"] for source in self.config.section("sources")}
        with self._lock:
            if source_id is not None:
                if source_id not in known:
                    return False
                self._forced.add(source_id)
            else:
                self._forced.update(known)
        self._wake.set()
        return True

    def check(self, source_id: str) -> str:
        """Test a source's settings without importing anything."""
        for source in self.config.section("sources"):
            if source["id"] == source_id:
                return build_source(source).check()
        raise SourceError("No such source — press Save settings first")

    def forget(self, source_id: str) -> None:
        """Drop a removed source's photos and its sync record."""
        for photo_id in self.library.ids_for_origin(source_id):
            self.library.remove_origin(photo_id, source_id)
        with self._lock:
            self._state["sources"].pop(source_id, None)
        self._save()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ----------------------------------------------------------------- loop

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._pass()
            except Exception as exc:  # a bug here must not kill the thread
                print(f"[sync] unexpected error: {exc}")
            self._wake.wait(TICK)
            self._wake.clear()

        # Sources removed from the config while we were asleep.
        self._prune_removed()

    def _pass(self) -> None:
        configured = self.config.section("sources")
        self._prune_removed(configured)

        for source in configured:
            if self._stop.is_set():
                return
            with self._lock:
                forced = source["id"] in self._forced
                self._forced.discard(source["id"])
            if not source["enabled"]:
                continue
            if not forced and not self._is_due(source):
                continue
            self._sync_one(source)

    def _is_due(self, source: dict) -> bool:
        record = self._record(source["id"])
        last = record.get("last_run")
        if not last:
            return True
        return time.time() - last >= max(60, int(source["interval_minutes"]) * 60)

    def _prune_removed(self, configured: list[dict] | None = None) -> None:
        """Forget sources that have been deleted from the settings."""
        configured = configured if configured is not None else self.config.section("sources")
        known = {source["id"] for source in configured}
        with self._lock:
            stale = [sid for sid in self._state["sources"] if sid not in known]
        for source_id in stale:
            print(f"[sync] source {source_id} removed; dropping its photos")
            self.forget(source_id)

    def _sync_one(self, config: dict) -> None:
        source_id, name = config["id"], config["name"]
        record = self._record(source_id)
        self._active = source_id
        added = 0
        shortage: str | None = None
        try:
            source = build_source(config)
            items = source.list_items()[: int(config["limit"])]
            seen = {item.key for item in items}
            known: dict[str, str] = dict(record.get("items", {}))

            for item in items:
                if self._stop.is_set():
                    break
                if item.key in known:
                    continue
                # Checked per photo, not once per sync: a long import can fill
                # the card halfway through.
                if not self._room_for_more():
                    shortage = self.storage.describe_shortage()
                    print(f"[sync] {name}: stopping — {shortage}")
                    break
                try:
                    data = source.fetch(item)
                    entry = self.library.add(data, item.filename, origin=source_id)
                except (SourceError, LibraryError) as exc:
                    print(f"[sync] {name}: skipping {item.filename} — {exc}")
                    continue
                known[item.key] = entry["id"]
                added += 1
                time.sleep(DOWNLOAD_PAUSE)

            # Worth doing even if we ran out of room — it frees some.
            removed = 0
            if config["remove_deleted"]:
                for key in [k for k in known if k not in seen]:
                    self.library.remove_origin(known.pop(key), source_id)
                    removed += 1

            with self._lock:
                record["items"] = known
                record["last_error"] = shortage  # None unless the disk stopped us
                record["last_added"] = added
                record["last_run"] = time.time()
            if added or removed:
                print(f"[sync] {name}: +{added} photo(s), -{removed}")
                if self.storage is not None:
                    self.storage.invalidate()  # the numbers have moved

        except SourceError as exc:
            self._fail(record, name, str(exc))
        except Exception as exc:  # noqa: BLE001 - unexpected, but keep syncing
            self._fail(record, name, f"unexpected error: {exc}")
        finally:
            self._active = None
            self._save()

    def _room_for_more(self) -> bool:
        """Stop importing before the disk is full, dropping the render cache
        first if that's enough to carry on."""
        if self.storage is None:
            return True
        if not self.config.section("storage")["pause_sync_when_low"]:
            return True
        return self.storage.make_room()

    def _fail(self, record: dict, name: str, message: str) -> None:
        print(f"[sync] {name}: {message}")
        with self._lock:
            record["last_error"] = message
            record["last_run"] = time.time()
