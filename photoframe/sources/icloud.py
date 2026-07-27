"""A public iCloud shared album.

Apple's web-shared albums are backed by an undocumented but long-stable JSON
service, the same one the icloud.com share page uses. It needs no account and
no credentials — only the album's public link, with "Public Website" enabled
in the Photos share options.

This is unofficial, so treat breakage as possible rather than surprising.
Private libraries are out of reach: those need a full authenticated session
with two-factor, which no unattended picture frame should be holding.
"""

from __future__ import annotations

import re
from typing import Any

import requests

from . import PhotoSource, RemoteItem, SourceError

# Any partition works as an entry point; Apple redirects us to the right one.
INITIAL_HOST = "p123-sharedstreams.icloud.com"
ASSET_BATCH = 25
TIMEOUT = 30


def extract_token(url: str) -> str:
    """Pull the album token out of a share link (or accept a bare token)."""
    value = (url or "").strip()
    if not value:
        raise SourceError("No iCloud shared album link set")
    if "#" in value:
        value = value.split("#", 1)[1]
    value = value.strip().strip("/").split("?")[0]
    if not re.fullmatch(r"[A-Za-z0-9._-]{10,60}", value):
        raise SourceError("That doesn't look like an iCloud shared album link")
    return value


class ICloudSharedAlbumSource(PhotoSource):
    type_name = "icloud"

    def __init__(self, config: dict):
        super().__init__(config)
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "text/plain", "Origin": "https://www.icloud.com"})
        self._host = INITIAL_HOST

    def _post(self, endpoint: str, payload: dict, allow_redirect: bool = True) -> dict[str, Any]:
        token = extract_token(self.config.get("url", ""))
        url = f"https://{self._host}/{token}/sharedstreams/{endpoint}"
        try:
            response = self._session.post(url, json=payload, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SourceError(f"Could not reach iCloud: {exc}") from exc

        # 330 means "wrong partition" — the body names the right host.
        if response.status_code == 330 and allow_redirect:
            try:
                host = response.json().get("X-Apple-MMe-Host")
            except ValueError:
                host = None
            if not host:
                raise SourceError("iCloud asked us to redirect but gave no host")
            self._host = str(host)
            return self._post(endpoint, payload, allow_redirect=False)

        if response.status_code == 401:
            raise SourceError("iCloud refused the album — is the public link still enabled?")
        if response.status_code >= 400:
            raise SourceError(f"iCloud returned HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise SourceError("iCloud returned a response that wasn't JSON") from exc

    def list_items(self) -> list[RemoteItem]:
        stream = self._post("webstream", {"streamCtag": None})
        photos = stream.get("photos")
        if not isinstance(photos, list):
            raise SourceError("iCloud returned no photo list for that album")

        limit = int(self.config.get("limit") or 500)
        wanted: list[tuple[str, str, dict]] = []  # (guid, checksum, derivative)
        for photo in photos[:limit]:
            if str(photo.get("mediaAssetType", "")).lower() == "video":
                continue
            guid = photo.get("photoGuid")
            best = _best_derivative(photo.get("derivatives") or {})
            if guid and best and best.get("checksum"):
                wanted.append((str(guid), str(best["checksum"]), best))
        if not wanted:
            return []

        # Asset URLs are handed out separately, and expire, so resolve them
        # now and download promptly.
        urls: dict[str, str] = {}
        for start in range(0, len(wanted), ASSET_BATCH):
            batch = wanted[start:start + ASSET_BATCH]
            response = self._post("webasseturls", {"photoGuids": [guid for guid, _, _ in batch]})
            for checksum, location in (response.get("items") or {}).items():
                if isinstance(location, dict) and location.get("url_location"):
                    urls[checksum] = f"https://{location['url_location']}{location.get('url_path', '')}"

        items = []
        for guid, checksum, derivative in wanted:
            url = urls.get(checksum)
            if not url:
                continue  # Apple didn't offer this one; it'll retry next sync
            items.append(RemoteItem(
                key=f"{guid}|{checksum}",
                filename=f"{guid[:12]}.jpg",
                size=_as_int(derivative.get("fileSize")),
                extra={"url": url},
            ))
        return items

    def fetch(self, item: RemoteItem) -> bytes:
        try:
            response = requests.get(item.extra["url"], timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SourceError(f"Could not download from iCloud: {exc}") from exc
        if response.status_code >= 400:
            raise SourceError(
                f"iCloud download failed with HTTP {response.status_code}"
                " (asset links expire — the next sync will refresh them)"
            )
        return response.content

    def check(self) -> str:
        items = self.list_items()
        if not items:
            return "Connected, but the album has no photos we can use"
        return f"Connected — {len(items)} photo(s) in the shared album"


def _best_derivative(derivatives: dict) -> dict | None:
    """Pick the largest available rendition of a photo."""
    best, best_size = None, -1
    for key, derivative in derivatives.items():
        if not isinstance(derivative, dict) or str(key).lower() == "posterframe":
            continue
        size = _as_int(derivative.get("fileSize")) or _as_int(derivative.get("width")) or 0
        if size > best_size:
            best, best_size = derivative, size
    return best


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
