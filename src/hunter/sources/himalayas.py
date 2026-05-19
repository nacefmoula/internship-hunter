"""Himalayas adapter (https://himalayas.app/jobs/api).

Public JSON API. Returns ``{"jobs": [...], ...}``. Himalayas is a
remote-only board, so every listing is remote; ``locationRestrictions``
holds the allowed candidate regions. ``pubDate`` is a unix epoch int.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ..schema import Listing
from .base import Source, html_to_text

API_URL = "https://himalayas.app/jobs/api?limit=100"


def _epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def parse(raw: bytes | str | dict) -> list[Listing]:
    """Parse a raw Himalayas API payload into Listings (no network)."""
    data = raw if isinstance(raw, dict) else json.loads(raw)
    fetched_at = datetime.now(UTC)
    listings: list[Listing] = []
    for job in data.get("jobs", []):
        guid = job.get("guid") or job.get("applicationLink")
        if not guid:
            continue
        regions = job.get("locationRestrictions") or []
        location = ", ".join(regions) if regions else "Worldwide"
        listings.append(
            Listing(
                source="himalayas",
                external_id=str(guid),
                title=job["title"],
                company=(job.get("companyName") or "").strip() or "Unknown",
                location=location,
                remote=True,
                url=job.get("applicationLink") or guid,
                description=html_to_text(job.get("description") or job.get("excerpt")),
                posted_at=_epoch(job.get("pubDate")),
                fetched_at=fetched_at,
            )
        )
    return listings


class Himalayas(Source):
    name = "himalayas"

    async def fetch(self) -> list[Listing]:
        resp = await self._get(API_URL)
        return parse(resp.content)
