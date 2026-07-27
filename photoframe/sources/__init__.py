"""Network photo sources.

Each source knows how to list what's available somewhere else and fetch one
item's bytes. Everything past that — decoding, resizing, dedupe, deletion —
is the library's job, so adding a new source means implementing two methods.

Optional dependencies are imported inside the source modules, so a missing
`smbprotocol` only breaks SMB rather than the whole frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SourceError(Exception):
    """Anything that stops a source working, phrased for a human to read."""


@dataclass
class RemoteItem:
    """One photo available from a source."""

    key: str  # stable within the source; changes if the photo changes
    filename: str
    size: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class PhotoSource:
    """Base class. Subclasses implement list_items() and fetch()."""

    type_name = ""

    def __init__(self, config: dict):
        self.config = config
        self.id: str = config["id"]
        self.name: str = config.get("name") or config["type"]

    def list_items(self) -> list[RemoteItem]:
        raise NotImplementedError

    def fetch(self, item: RemoteItem) -> bytes:
        raise NotImplementedError

    def check(self) -> str:
        """Used by the Test button; returns a line to show the user."""
        items = self.list_items()
        if not items:
            return "Connected, but found no photos"
        return f"Connected — {len(items)} photo(s) available, e.g. {items[0].filename}"


def build_source(config: dict) -> PhotoSource:
    """Construct the source for a config entry. Imports are deferred so that a
    missing optional dependency only affects the source that needs it."""
    kind = config.get("type")

    if kind == "folder":
        from .folder import FolderSource

        return FolderSource(config)
    if kind == "smb":
        from .smb import SmbSource

        return SmbSource(config)
    if kind == "photoprism":
        from .photoprism import PhotoPrismSource

        return PhotoPrismSource(config)
    if kind == "icloud":
        from .icloud import ICloudSharedAlbumSource

        return ICloudSharedAlbumSource(config)

    raise SourceError(f"Unknown source type '{kind}'")


def is_supported_image(filename: str) -> bool:
    from ..library import SUPPORTED_EXTENSIONS
    from pathlib import Path

    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS
