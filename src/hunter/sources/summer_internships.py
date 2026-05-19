"""Summer 2026 internships aggregator (community GitHub dataset).

vanshb03/Summer2026-Internships publishes a structured ``listings.json`` on
raw.githubusercontent.com — a large, curated feed of real tech internships
(SWE/Data/Infra/Cloud at named companies). It is plain JSON, no anti-bot
wall, so it is fetched API-style like the Remotive source (no robots probe).

Only **active, visible, Summer** postings are kept. Most entries are US
on-site and will be hard-filtered out as "on-site abroad" — the payoff is
the remote (and occasionally Tunisia-eligible) DevOps/Cloud subset, which
the rest of these boards essentially never carry.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ..schema import Listing
from .base import Source

LISTINGS_URL = (
    "https://raw.githubusercontent.com/vanshb03/"
    "Summer2026-Internships/dev/.github/scripts/listings.json"
)


def _is_remote(locations: list[str]) -> bool:
    return any("remote" in (loc or "").lower() for loc in locations)


def parse(raw: bytes | str | list) -> list[Listing]:
    """Parse the raw listings.json into Listings (no network)."""
    data = raw if isinstance(raw, list) else json.loads(raw)
    fetched_at = datetime.now(UTC)
    out: list[Listing] = []
    for job in data:
        if not (job.get("active") and job.get("is_visible")):
            continue
        if (job.get("season") or "").lower() != "summer":
            continue
        ext_id = job.get("id") or job.get("url")
        if not ext_id or not job.get("url"):
            continue

        locations = job.get("locations") or []
        loc_str = "; ".join(locations) if locations else None
        posted_at: datetime | None = None
        if job.get("date_posted"):
            try:
                posted_at = datetime.fromtimestamp(int(job["date_posted"]), UTC)
            except (ValueError, OSError, TypeError):
                posted_at = None

        title = (job.get("title") or "").strip() or "Internship"
        # Guarantee an internship term so the filter's internship gate passes
        # even if a title omits the word.
        description = (
            f"{title} at {job.get('company_name') or 'Unknown'}. "
            f"Summer 2026 internship. Locations: {loc_str or 'n/a'}."
        )

        out.append(
            Listing(
                source="summer_internships",
                external_id=str(ext_id),
                title=title,
                company=(job.get("company_name") or "").strip() or "Unknown",
                location=loc_str,
                remote=_is_remote(locations),
                url=job["url"],
                description=description,
                posted_at=posted_at,
                fetched_at=fetched_at,
            )
        )
    return out


class SummerInternships(Source):
    name = "summer_internships"
    manual_url = "https://github.com/vanshb03/Summer2026-Internships"

    async def fetch(self) -> list[Listing]:
        resp = await self._get(LISTINGS_URL)
        return parse(resp.content)
