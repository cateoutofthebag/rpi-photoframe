"""A PhotoPrism album.

Authenticates with either an app password (sent straight as a bearer token) or
a username and password, then pulls the album's photos through the thumbnailer
rather than downloading originals — PhotoPrism will happily hand over a
screen-sized, correctly-oriented JPEG, which is exactly what the frame wants
and a fraction of the bytes.

API shapes differ a little between PhotoPrism releases, so token parsing is
deliberately forgiving.
"""

from __future__ import annotations

from typing import Any

import requests

from . import PhotoSource, RemoteItem, SourceError

PAGE_SIZE = 200
TIMEOUT = 30


class PhotoPrismSource(PhotoSource):
    type_name = "photoprism"

    def __init__(self, config: dict):
        super().__init__(config)
        self._session = requests.Session()
        self._session.verify = bool(config.get("verify_tls", True))
        self._headers: dict[str, str] = {}
        self._preview_token = ""
        self._download_token = ""

    # ----------------------------------------------------------------- auth

    def _base(self) -> str:
        url = (self.config.get("url") or "").strip().rstrip("/")
        if not url:
            raise SourceError("No PhotoPrism URL set")
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return url

    def _login(self) -> None:
        if self._headers and self._preview_token:
            return
        base = self._base()

        token = (self.config.get("token") or "").strip()
        if token:
            # App passwords work as bearer tokens without creating a session.
            self._headers = {"Authorization": f"Bearer {token}"}
        else:
            username = (self.config.get("username") or "").strip()
            password = self.config.get("password") or ""
            if not username or not password:
                raise SourceError("Set either an app password, or a username and password")
            try:
                response = self._session.post(
                    f"{base}/api/v1/session",
                    json={"username": username, "password": password},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                raise SourceError(f"Could not reach {base}: {exc}") from exc
            if response.status_code in (401, 403):
                raise SourceError("PhotoPrism rejected those credentials")
            self._raise_for_status(response, "sign in")

            data = self._json(response)
            access = data.get("access_token") or data.get("id") or data.get("session_id")
            if not access:
                raise SourceError("PhotoPrism returned no session token")
            self._headers = {"Authorization": f"Bearer {access}", "X-Auth-Token": str(access)}
            self._read_tokens(data)

        if not self._preview_token:
            self._read_tokens(self._get_json(f"{base}/api/v1/config"))
        if not self._preview_token:
            raise SourceError("Could not obtain a preview token from PhotoPrism")

    def _read_tokens(self, data: dict[str, Any]) -> None:
        """Tokens sit at the top level or under 'config', depending on version."""
        for holder in (data, data.get("config") or {}):
            if not isinstance(holder, dict):
                continue
            for key in ("previewToken", "preview_token", "PreviewToken"):
                if holder.get(key):
                    self._preview_token = str(holder[key])
                    break
            for key in ("downloadToken", "download_token", "DownloadToken"):
                if holder.get(key):
                    self._download_token = str(holder[key])
                    break

    # ------------------------------------------------------------- requests

    def _raise_for_status(self, response: requests.Response, what: str) -> None:
        if response.status_code >= 400:
            raise SourceError(f"PhotoPrism returned HTTP {response.status_code} trying to {what}")

    def _json(self, response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError("PhotoPrism returned a response that wasn't JSON") from exc

    def _get_json(self, url: str, params: dict | None = None) -> Any:
        try:
            response = self._session.get(url, params=params, headers=self._headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SourceError(f"Request to PhotoPrism failed: {exc}") from exc
        self._raise_for_status(response, "read data")
        return self._json(response)

    # -------------------------------------------------------------- listing

    def _album_uid(self) -> str:
        wanted = (self.config.get("album") or "").strip()
        if not wanted:
            raise SourceError("No album set")

        base = self._base()
        albums = self._get_json(
            f"{base}/api/v1/albums", {"count": 1000, "offset": 0, "type": "album"}
        )
        if not isinstance(albums, list):
            raise SourceError("Could not list albums")

        for album in albums:
            if str(album.get("UID", "")) == wanted:
                return wanted
        for album in albums:  # fall back to matching the title
            if str(album.get("Title", "")).strip().lower() == wanted.lower():
                return str(album["UID"])

        names = ", ".join(sorted(str(a.get("Title", "?")) for a in albums)[:10]) or "none"
        raise SourceError(f"No album called '{wanted}'. Available: {names}")

    def list_items(self) -> list[RemoteItem]:
        self._login()
        base = self._base()
        album_uid = self._album_uid()
        size = self.config.get("size") or "fit_1920"
        limit = int(self.config.get("limit") or 500)

        items: list[RemoteItem] = []
        offset = 0
        while len(items) < limit:
            page = self._get_json(f"{base}/api/v1/photos", {
                "count": min(PAGE_SIZE, limit - len(items)),
                "offset": offset,
                "album": album_uid,
                "order": "added",
            })
            if not isinstance(page, list) or not page:
                break

            for photo in page:
                if str(photo.get("Type", "image")).lower() == "video":
                    continue
                file_hash = photo.get("Hash") or _first_file_hash(photo)
                if not file_hash:
                    continue
                uid = str(photo.get("UID", file_hash))
                name = str(photo.get("FileName") or photo.get("Title") or uid).split("/")[-1]
                items.append(RemoteItem(
                    key=f"{uid}|{file_hash}|{size}",
                    filename=name if "." in name else f"{name}.jpg",
                    extra={"hash": str(file_hash)},
                ))

            offset += len(page)
            if len(page) < PAGE_SIZE:
                break
        return items

    def fetch(self, item: RemoteItem) -> bytes:
        self._login()
        base = self._base()
        size = self.config.get("size") or "fit_1920"
        file_hash = item.extra["hash"]

        if size == "original":
            if not self._download_token:
                raise SourceError("PhotoPrism gave no download token; pick a size other than original")
            url = f"{base}/api/v1/dl/{file_hash}"
            params = {"t": self._download_token}
        else:
            url = f"{base}/api/v1/t/{file_hash}/{self._preview_token}/{size}"
            params = None

        try:
            response = self._session.get(url, params=params, headers=self._headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SourceError(f"Could not download {item.filename}: {exc}") from exc
        self._raise_for_status(response, f"download {item.filename}")
        return response.content

    def check(self) -> str:
        self._login()
        items = self.list_items()
        size = self.config.get("size")
        if not items:
            return "Connected, but that album has no photos"
        return f"Connected — {len(items)} photo(s) at {size}, e.g. {items[0].filename}"


def _first_file_hash(photo: dict) -> str | None:
    for entry in photo.get("Files") or []:
        if isinstance(entry, dict) and entry.get("Hash"):
            return str(entry["Hash"])
    return None
