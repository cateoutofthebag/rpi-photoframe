"""The photo library: ingest, screen-sized render cache, thumbnails, index.

Decoding a full-resolution phone photo on a Zero 2 W costs seconds and a large
slice of the 512 MB of RAM, so nothing in the display path ever touches an
original. Uploads are downscaled once, and each photo is composed to an exact
screen-sized JPEG the first time it is shown; after that the display thread
only ever decodes an image that is already the right size.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

try:  # HEIC/HEIF (iPhone photos) is optional — the rest works without it.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - depends on the install
    HEIF_SUPPORTED = False

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
if HEIF_SUPPORTED:
    SUPPORTED_EXTENSIONS |= {".heic", ".heif"}

THUMB_MAX = 480
ORIGINAL_MAX = 3200  # plenty for re-rendering later, a fraction of the disk cost


class LibraryError(Exception):
    """Raised for uploads we can't accept."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_stem(filename: str) -> str:
    """A display-safe version of the uploaded filename (not a path)."""
    name = Path(str(filename or "photo")).name
    cleaned = "".join(c for c in name if c.isalnum() or c in " ._-()").strip()
    return (cleaned or "photo")[:120]


class PhotoLibrary:
    """Stores photos under `root` and keeps a JSON index of them."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.originals = self.root / "originals"
        self.cache = self.root / "cache"
        self.thumbs = self.root / "thumbs"
        self.index_path = self.root / "index.json"
        for directory in (self.originals, self.cache, self.thumbs):
            directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._index: dict[str, dict[str, Any]] = {}
        self._version = 0
        self._load_index()

    # ---------------------------------------------------------------- index

    def _load_index(self) -> None:
        try:
            with self.index_path.open(encoding="utf-8") as fh:
                stored = json.load(fh)
            photos = stored.get("photos", {}) if isinstance(stored, dict) else {}
        except FileNotFoundError:
            photos = {}
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[library] index unreadable ({exc}); rebuilding from disk")
            photos = {}
        # Drop entries whose file has gone missing (e.g. hand-deleted).
        self._index = {
            pid: entry
            for pid, entry in photos.items()
            if isinstance(entry, dict) and (self.originals / entry.get("stored", "")).exists()
        }
        if len(self._index) != len(photos):
            self._save_index()

    def _save_index(self) -> None:
        payload = json.dumps({"photos": self._index}, indent=2, sort_keys=True)
        tmp = self.index_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self.index_path)

    @property
    def version(self) -> int:
        """Bumped whenever photos are added or removed."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._index)

    def list_photos(self) -> list[dict[str, Any]]:
        """All photos, oldest upload first (the sequential slideshow order)."""
        with self._lock:
            photos = [dict(entry, id=pid) for pid, entry in self._index.items()]
        photos.sort(key=lambda p: (p.get("added", ""), p["id"]))
        return photos

    def ids(self) -> list[str]:
        return [photo["id"] for photo in self.list_photos()]

    def get(self, photo_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._index.get(photo_id)
            return dict(entry, id=photo_id) if entry else None

    # --------------------------------------------------------------- ingest

    def add(self, data: bytes, filename: str, origin: str = "upload") -> dict[str, Any]:
        """Ingest one photo. Re-adding a known photo just records the new origin.

        `origin` is "upload" or a source id, and a photo can have several: the
        same picture may arrive from a manual upload and an SMB share. It's
        only deleted once every origin has let go of it.
        """
        if not data:
            raise LibraryError("empty file")

        suffix = Path(_safe_stem(filename)).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            hint = " (install pillow-heif for HEIC)" if suffix in {".heic", ".heif"} else ""
            raise LibraryError(f"unsupported file type '{suffix or filename}'{hint}")

        photo_id = hashlib.sha256(data).hexdigest()[:16]
        if self.get(photo_id):
            return self.add_origin(photo_id, origin)

        try:
            with Image.open(io.BytesIO(data)) as img:
                img = ImageOps.exif_transpose(img)  # honour the camera's rotation
                img = img.convert("RGB")
                img.thumbnail((ORIGINAL_MAX, ORIGINAL_MAX), Image.LANCZOS)
                stored_name = f"{photo_id}.jpg"
                img.save(self.originals / stored_name, "JPEG", quality=92, optimize=True)
                width, height = img.size

                thumb = img.copy()
                thumb.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
                thumb.save(self.thumbs / f"{photo_id}.jpg", "JPEG", quality=80, optimize=True)
        except LibraryError:
            raise
        except Exception as exc:  # Pillow raises a wide variety for bad files
            raise LibraryError(f"could not read image: {exc}") from exc

        entry = {
            "filename": _safe_stem(filename),
            "stored": stored_name,
            "added": _now_iso(),
            "width": width,
            "height": height,
            "bytes": len(data),
            "origins": [origin],
        }
        with self._lock:
            self._index[photo_id] = entry
            self._version += 1
            self._save_index()
        return dict(entry, id=photo_id)

    def add_origin(self, photo_id: str, origin: str) -> dict[str, Any]:
        """Record that `origin` also supplies an already-known photo."""
        with self._lock:
            entry = self._index[photo_id]
            origins = entry.setdefault("origins", ["upload"])
            if origin not in origins:
                origins.append(origin)
                self._save_index()
            return dict(entry, id=photo_id)

    def remove_origin(self, photo_id: str, origin: str) -> bool:
        """Drop one origin. Deletes the photo if that was the last one.

        Returns True if the photo itself was deleted.
        """
        with self._lock:
            entry = self._index.get(photo_id)
            if entry is None:
                return False
            origins = [o for o in entry.get("origins", ["upload"]) if o != origin]
            if origins:
                entry["origins"] = origins
                self._save_index()
                return False
        return self.delete(photo_id)

    def ids_for_origin(self, origin: str) -> list[str]:
        with self._lock:
            return [pid for pid, entry in self._index.items()
                    if origin in entry.get("origins", ["upload"])]

    def delete(self, photo_id: str) -> bool:
        with self._lock:
            entry = self._index.pop(photo_id, None)
            if entry is None:
                return False
            self._version += 1
            self._save_index()
        (self.originals / entry.get("stored", "")).unlink(missing_ok=True)
        (self.thumbs / f"{photo_id}.jpg").unlink(missing_ok=True)
        for cached in self.cache.glob(f"{photo_id}_*.jpg"):
            cached.unlink(missing_ok=True)
        return True

    def thumb_path(self, photo_id: str) -> Path | None:
        path = self.thumbs / f"{photo_id}.jpg"
        return path if path.exists() else None

    # -------------------------------------------------------- render cache

    def _cache_path(self, photo_id: str, size: tuple[int, int], fit: str) -> Path:
        return self.cache / f"{photo_id}_{size[0]}x{size[1]}_{fit}.jpg"

    def render(self, photo_id: str, size: tuple[int, int], fit: str) -> Image.Image | None:
        """Return the photo composed to exactly `size`, generating it if needed.

        Called from the prefetch thread, never from the render loop.
        """
        entry = self.get(photo_id)
        if entry is None:
            return None

        cached = self._cache_path(photo_id, size, fit)
        if cached.exists():
            try:
                with Image.open(cached) as img:
                    return img.convert("RGB")
            except Exception:
                cached.unlink(missing_ok=True)  # regenerate below

        source = self.originals / entry["stored"]
        try:
            with Image.open(source) as img:
                composed = _compose(img.convert("RGB"), size, fit)
        except Exception as exc:
            print(f"[library] cannot render {photo_id}: {exc}")
            return None

        try:
            composed.save(cached, "JPEG", quality=88, optimize=False)
        except OSError as exc:  # a full SD card shouldn't stop the slideshow
            print(f"[library] cache write failed: {exc}")
        return composed

    def prune_cache(self, keep_size: tuple[int, int], keep_fit: str) -> int:
        """Delete cached renders for other screen sizes / fit modes."""
        suffix = f"_{keep_size[0]}x{keep_size[1]}_{keep_fit}.jpg"
        removed = 0
        for path in self.cache.glob("*.jpg"):
            if not path.name.endswith(suffix):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def uncached(self, size: tuple[int, int], fit: str) -> Iterator[str]:
        """Photo ids with no render for this size/fit yet (for cache warming)."""
        for photo_id in self.ids():
            if not self._cache_path(photo_id, size, fit).exists():
                yield photo_id


def _compose(img: Image.Image, size: tuple[int, int], fit: str) -> Image.Image:
    """Fit `img` onto a `size` canvas using the chosen strategy."""
    target_w, target_h = size
    if fit == "cover":
        return ImageOps.fit(img, size, Image.LANCZOS, centering=(0.5, 0.5))

    scaled = img.copy()
    scaled.thumbnail(size, Image.LANCZOS)
    offset = ((target_w - scaled.width) // 2, (target_h - scaled.height) // 2)

    if fit == "blur" and (scaled.width < target_w or scaled.height < target_h):
        # Fill the letterbox bars with a blurred, dimmed copy of the photo.
        # Blurring a tiny image and scaling it up is far cheaper than a large
        # Gaussian, and looks the same once it's this soft.
        small = ImageOps.fit(img, (64, 36), Image.BILINEAR, centering=(0.5, 0.5))
        background = small.filter(ImageFilter.GaussianBlur(4)).resize(size, Image.BICUBIC)
        background = ImageEnhance.Brightness(background).enhance(0.45)
    else:
        background = Image.new("RGB", size, (0, 0, 0))

    background.paste(scaled, offset)
    return background
