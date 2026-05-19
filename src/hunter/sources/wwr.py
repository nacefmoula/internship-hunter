"""We Work Remotely adapter (DevOps category RSS).

Feed: https://weworkremotely.com/categories/remote-devops-jobs.rss

WWR item titles follow ``"Company: Position"``. The feed exposes custom
``region`` and ``type`` elements (feedparser surfaces these as lowercase
entry keys). All WWR listings are remote.

Note: WWR sits behind Cloudflare and may soft-block non-browser clients
(observed: HTTP 301 with an empty Location). ``fetch`` is fail-soft via the
base class, so a block degrades to "0 fetched" rather than crashing; in
that case check the feed manually in a browser.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import mktime

import feedparser
import httpx

from ..schema import Listing
from .base import Source, SourceBlocked, html_to_text

FEED_URL = "https://weworkremotely.com/categories/remote-devops-jobs.rss"


def _split_company_title(raw_title: str) -> tuple[str, str]:
    if ":" in raw_title:
        company, _, title = raw_title.partition(":")
        return company.strip() or "Unknown", title.strip() or raw_title.strip()
    return "Unknown", raw_title.strip()


def parse(raw: bytes | str) -> list[Listing]:
    """Parse a raw WWR RSS feed into Listings (no network)."""
    feed = feedparser.parse(raw)
    fetched_at = datetime.now(UTC)
    listings: list[Listing] = []
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue
        company, title = _split_company_title(entry.get("title", ""))
        posted_at: datetime | None = None
        if entry.get("published_parsed"):
            posted_at = datetime.fromtimestamp(
                mktime(entry.published_parsed), tz=UTC
            )
        listings.append(
            Listing(
                source="wwr",
                external_id=entry.get("id") or link,
                title=title,
                company=company,
                location=entry.get("region") or "Remote",
                remote=True,
                url=link,
                description=html_to_text(entry.get("summary")),
                posted_at=posted_at,
                fetched_at=fetched_at,
            )
        )
    return listings


class WeWorkRemotely(Source):
    name = "wwr"
    manual_url = "https://weworkremotely.com/categories/remote-devops-jobs"

    async def fetch(self) -> list[Listing]:
        try:
            resp = await self.get_or_block(FEED_URL)
        except httpx.HTTPError as exc:
            # Cloudflare returns a 301 with no Location; httpx can't follow
            # it and raises. Treat that as a hard block, not a crash.
            raise SourceBlocked(
                self.name, self.manual_url, f"{type(exc).__name__} (Cloudflare)"
            ) from exc
        if resp.status_code in (301, 302, 303, 307, 308) or not resp.content:
            raise SourceBlocked(self.name, self.manual_url, "empty/redirect (Cloudflare)")
        listings = parse(resp.content)
        if not listings and b"<item" not in resp.content:
            raise SourceBlocked(self.name, self.manual_url, "no feed items")
        return listings
