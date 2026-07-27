"""An SMB/CIFS share, spoken directly rather than mounted.

Uses smbprotocol's `smbclient`, which is pure Python — so no root, no fstab
entry, and nothing to go wrong at boot if the NAS happens to be off.
"""

from __future__ import annotations

import ntpath

from . import PhotoSource, RemoteItem, SourceError, is_supported_image

MAX_DEPTH = 6  # deep enough for year/event trees, shallow enough to stay quick


def _smbclient():
    try:
        import smbclient
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise SourceError(
            "SMB support needs the smbprotocol package — pip install smbprotocol"
        ) from exc
    return smbclient


class SmbSource(PhotoSource):
    type_name = "smb"

    def _port(self) -> int:
        try:
            return int(self.config.get("port") or 445)
        except (TypeError, ValueError):
            return 445

    def _connect(self):
        server = (self.config.get("server") or "").strip()
        share = (self.config.get("share") or "").strip().strip("/\\")
        if not server or not share:
            raise SourceError("Both a server and a share name are required")

        smbclient = _smbclient()
        username = (self.config.get("username") or "").strip() or None
        domain = (self.config.get("domain") or "").strip()
        if username and domain:
            username = f"{domain}\\{username}"

        try:
            smbclient.register_session(
                server,
                username=username,
                password=self.config.get("password") or None,
                port=self._port(),
                connection_timeout=15,
            )
        except Exception as exc:  # smbprotocol raises a broad family
            raise SourceError(f"Could not connect to {server}: {exc}") from exc

        subpath = (self.config.get("path") or "").strip().strip("/\\").replace("/", "\\")
        root = ntpath.join(f"\\\\{server}\\{share}", subpath) if subpath else f"\\\\{server}\\{share}"
        return smbclient, root

    def list_items(self) -> list[RemoteItem]:
        smbclient, root = self._connect()
        # The UNC path carries no port, so every call has to be told again or
        # it silently reconnects on the default 445.
        port = self._port()
        items: list[RemoteItem] = []

        def walk(path: str, depth: int) -> None:
            if depth > MAX_DEPTH:
                return
            try:
                entries = list(smbclient.scandir(path, port=port))
            except Exception as exc:
                if depth == 0:
                    raise SourceError(f"Could not list {path}: {exc}") from exc
                return  # an unreadable subfolder shouldn't sink the whole sync

            for entry in entries:
                if entry.name.startswith("."):
                    continue
                full = ntpath.join(path, entry.name)
                if entry.is_dir():
                    walk(full, depth + 1)
                elif is_supported_image(entry.name):
                    try:
                        stat = entry.stat()
                        size, mtime = stat.st_size, int(stat.st_mtime)
                    except Exception:
                        size, mtime = 0, 0
                    items.append(RemoteItem(
                        key=f"{full}|{mtime}|{size}",
                        filename=entry.name,
                        size=size,
                        extra={"path": full},
                    ))

        walk(root, 0)
        items.sort(key=lambda item: item.extra["path"])
        return items

    def fetch(self, item: RemoteItem) -> bytes:
        smbclient = _smbclient()
        try:
            with smbclient.open_file(item.extra["path"], mode="rb", port=self._port()) as handle:
                return handle.read()
        except Exception as exc:
            raise SourceError(f"Could not read {item.filename}: {exc}") from exc
