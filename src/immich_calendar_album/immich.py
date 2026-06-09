"""Immich REST API client.

Covers the operations needed by this service:
  - List / create albums
  - List shared links per album, create shared links with upload permission
  - Fetch unassigned assets (paginated)
  - Add assets to an album

Authentication is via the ``x-api-key`` header.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterator, TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

if TYPE_CHECKING:
    from immich_calendar_album.config import Config

log = logging.getLogger(__name__)

_PAGE_SIZE = 1000  # assets per page


@dataclass
class ImmichAlbum:
    id: str
    name: str


@dataclass
class ImmichAsset:
    id: str
    local_date: date  # capture date in configured TZ


@dataclass
class ImmichSharedLink:
    id: str
    key: str
    album_id: str | None


class ImmichClient:
    """Thin wrapper around the Immich REST API."""

    def __init__(self, cfg: "Config") -> None:
        self._base = cfg.immich_base_url.rstrip("/")
        self._headers = {
            "x-api-key": cfg.immich_api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._tz = ZoneInfo(cfg.tz)
        self._http = httpx.Client(
            base_url=self._base,
            headers=self._headers,
            follow_redirects=True,
            timeout=30,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ImmichClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Albums
    # ------------------------------------------------------------------

    def list_albums(self) -> list[ImmichAlbum]:
        resp = self._http.get("/api/albums")
        resp.raise_for_status()
        return [
            ImmichAlbum(id=a["id"], name=a["albumName"])
            for a in resp.json()
        ]

    def create_album(self, name: str) -> ImmichAlbum:
        resp = self._http.post("/api/albums", json={"albumName": name})
        resp.raise_for_status()
        data = resp.json()
        log.info("Created album %r (id=%s)", name, data["id"])
        return ImmichAlbum(id=data["id"], name=data["albumName"])

    def ensure_album(
        self,
        name: str,
        existing: dict[str, ImmichAlbum],
    ) -> ImmichAlbum:
        """Return an existing album by name or create a new one."""
        if name in existing:
            return existing[name]
        album = self.create_album(name)
        existing[name] = album
        return album

    # ------------------------------------------------------------------
    # Shared links
    # ------------------------------------------------------------------

    def list_shared_links(self) -> list[ImmichSharedLink]:
        resp = self._http.get("/api/shared-links")
        resp.raise_for_status()
        links = []
        for item in resp.json():
            album_id: str | None = None
            if isinstance(item.get("album"), dict):
                album_id = item["album"].get("id")
            links.append(
                ImmichSharedLink(
                    id=item["id"],
                    key=item["key"],
                    album_id=album_id,
                )
            )
        return links

    def get_or_create_share_link(
        self,
        album: ImmichAlbum,
        existing_links: list[ImmichSharedLink],
    ) -> str:
        """Return the public share URL for *album*, creating one if needed.

        The link allows both viewing and uploading.
        """
        for link in existing_links:
            if link.album_id == album.id:
                log.debug(
                    "Reusing existing shared link for album %r", album.name
                )
                return self._share_url(link.key)

        resp = self._http.post(
            "/api/shared-links",
            json={
                "type": "ALBUM",
                "albumId": album.id,
                "allowUpload": True,
                "allowDownload": True,
                "showMetadata": True,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        key = data["key"]
        log.info(
            "Created shared link for album %r (key=%s)", album.name, key
        )
        return self._share_url(key)

    def _share_url(self, key: str) -> str:
        return f"{self._base}/share/{key}"

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    def iter_unassigned_assets(self) -> Iterator[ImmichAsset]:
        """Yield all assets that are not part of any album, page by page."""
        page = 1
        while True:
            resp = self._http.get(
                "/api/assets",
                params={
                    "withoutAlbum": "true",
                    "page": str(page),
                    "size": str(_PAGE_SIZE),
                },
            )
            resp.raise_for_status()
            items = resp.json()
            if not items:
                break

            for item in items:
                asset = self._parse_asset(item)
                if asset is not None:
                    yield asset

            if len(items) < _PAGE_SIZE:
                break
            page += 1

    def add_assets_to_album(
        self,
        album_id: str,
        asset_ids: list[str],
    ) -> None:
        if not asset_ids:
            return
        resp = self._http.put(
            f"/api/albums/{album_id}/assets",
            json={"ids": asset_ids},
        )
        resp.raise_for_status()
        results = resp.json()
        failed = [r for r in results if not r.get("success")]
        if failed:
            log.warning(
                "Some assets could not be added to album %s: %s",
                album_id,
                [f.get("id") for f in failed],
            )
        log.info(
            "Added %d asset(s) to album %s (%d failed)",
            len(asset_ids) - len(failed),
            album_id,
            len(failed),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_asset(self, item: dict[str, Any]) -> ImmichAsset | None:
        """Extract capture date from an asset JSON object."""
        # Prefer exif localDateTime; fall back to fileCreatedAt / createdAt
        for field in ("localDateTime", "fileCreatedAt", "createdAt"):
            raw = item.get(field)
            if raw:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    local_date = dt.astimezone(self._tz).date()
                    return ImmichAsset(id=item["id"], local_date=local_date)
                except Exception:
                    continue
        log.debug("Asset %s has no parseable date – skipping", item.get("id"))
        return None
