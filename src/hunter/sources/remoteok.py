"""RemoteOK adapter (https://remoteok.com/api).

The API returns a JSON array whose first element is a legal/metadata notice;
every following element is a job. All RemoteOK listings are remote.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ..schema import Listing
from .base import Source, html_to_text

API_URL = "https://remoteok.com/api"


def parse(raw: bytes | str | list) -> list[Listing]:
    """Parse a raw RemoteOK API payload into Listings (no network)."""
    data = raw if isinstance(raw, list) else json.loads(raw)
    fetched_at = datetime.now(UTC)
    listings: list[Listing] = []
    for item in data:
        # Skip the leading {"legal": ..., "last_updated": ...} notice and
        # any element that isn't a real job posting.
        if not isinstance(item, dict) or "id" not in item or "position" not in item:
            continue
        posted_at: datetime | None = None
        if item.get("date"):
            try:
                posted_at = datetime.fromisoformat(item["date"])
            except ValueError:
                posted_at = None
        listings.append(
            Listing(
                source="remoteok",
                external_id=str(item["id"]),
                title=item["position"],
                company=(item.get("company") or "").strip() or "Unknown",
                location=item.get("location") or None,
                remote=True,
                url=item["url"],
                description=html_to_text(item.get("description")),
                posted_at=posted_at,
                fetched_at=fetched_at,
            )
        )
    return listings


class RemoteOK(Source):
    name = "remoteok"

    async def fetch(self) -> list[Listing]:
        resp = await self._get(API_URL)
        return parse(resp.content)
