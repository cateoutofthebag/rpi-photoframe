"""A watched directory on the Pi.

Useful on its own (a USB stick, an NFS or CIFS mount done by the OS) and the
practical answer for services with no usable API — point rclone, Syncthing or
a Google Takeout export at a folder and the frame picks it up.
"""

from __future__ import annotations

from pathlib import Path

from . import PhotoSource, RemoteItem, SourceError, is_supported_image


class FolderSource(PhotoSource):
    type_name = "folder"

    def _root(self) -> Path:
        raw = (self.config.get("path") or "").strip()
        if not raw:
            raise SourceError("No folder path set")
        root = Path(raw).expanduser()
        if not root.exists():
            raise SourceError(f"{root} does not exist")
        if not root.is_dir():
            raise SourceError(f"{root} is not a directory")
        return root

    def list_items(self) -> list[RemoteItem]:
        root = self._root()
        items: list[RemoteItem] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not is_supported_image(path.name):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            # Size and mtime are in the key, so an edited photo re-syncs and
            # the stale copy is cleaned up as a disappeared item.
            items.append(RemoteItem(
                key=f"{relative}|{int(stat.st_mtime)}|{stat.st_size}",
                filename=path.name,
                size=stat.st_size,
                extra={"path": str(path)},
            ))
        return items

    def fetch(self, item: RemoteItem) -> bytes:
        try:
            return Path(item.extra["path"]).read_bytes()
        except OSError as exc:
            raise SourceError(f"Could not read {item.filename}: {exc}") from exc
