"""Remotive adapter (https://remotive.com/api/remote-jobs?category=devops).

Returns a JSON object with metadata keys plus a ``jobs`` list. All Remotive
listings are remote; location is the candidate-required region.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ..schema import Listing
from .base import Source, html_to_text

API_URL = "https://remotive.com/api/remote-jobs?category=devops"


def parse(raw: bytes | str | dict) -> list[Listing]:
    """Parse a raw Remotive API payload into Listings (no network)."""
    data = raw if isinstance(raw, dict) else json.loads(raw)
    fetched_at = datetime.now(UTC)
    listings: list[Listing] = []
    for job in data.get("jobs", []):
        posted_at: datetime | None = None
        if job.get("publication_date"):
            try:
                posted_at = datetime.fromisoformat(job["publication_date"])
            except ValueError:
                posted_at = None
        listings.append(
            Listing(
                source="remotive",
                external_id=str(job["id"]),
                title=job["title"],
                company=(job.get("company_name") or "").strip() or "Unknown",
                location=job.get("candidate_required_location") or None,
                remote=True,
                url=job["url"],
                description=html_to_text(job.get("description")),
                posted_at=posted_at,
                fetched_at=fetched_at,
            )
        )
    return listings


class Remotive(Source):
    name = "remotive"

    async def fetch(self) -> list[Listing]:
        resp = await self._get(API_URL)
        return parse(resp.content)
