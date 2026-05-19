"""TanitJobs.com scraper (Tunisia).

TanitJobs sits behind a Cloudflare JS challenge that fires even on
``/robots.txt`` (HTTP 200 with a "Just a moment..." interstitial), so an
HTTP client cannot read it. ``fetch`` therefore detects the wall and
raises ``SourceBlocked`` so the CLI can tell you to open it manually.

The DOM parser is implemented against TanitJobs' standard listing markup
so that if the challenge is ever lifted (or run behind a browser) it works
without further changes.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from ..schema import Listing
from .base import Source, html_to_text, parse_fr_date

BASE = "https://www.tanitjobs.com"
SEARCH = BASE + "/jobs/?q=devops"

_ID_RE = re.compile(r"/job/(\d+)|(\d+)(?:[/?#]|$)")


def _abs(href: str) -> str:
    return href if href.startswith("http") else BASE + href.lstrip("/")


def parse(html: bytes | str) -> list[Listing]:
    """Parse a TanitJobs listing page (best-effort; no network)."""
    from datetime import UTC, datetime

    tree = HTMLParser(html if isinstance(html, str) else html.decode("utf-8", "replace"))
    fetched_at = datetime.now(UTC)
    out: list[Listing] = []
    for node in tree.css("div.list-group-item, article.job, div.job-item"):
        link = node.css_first('a[href*="/job/"]') or node.css_first("h2 a, h3 a")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        if not href:
            continue
        url = _abs(href)
        m = _ID_RE.search(url)
        ext_id = (m.group(1) or m.group(2)) if m else url
        comp = node.css_first(".company, .media-heading a, .job-company")
        loc = node.css_first(".location, .job-location")
        out.append(
            Listing(
                source="tanitjobs",
                external_id=str(ext_id),
                title=" ".join(link.text().split()),
                company=" ".join(comp.text().split()) if comp else "Unknown",
                location=(" ".join(loc.text().split()) if loc else "Tunisie"),
                remote=False,
                url=url,
                description=html_to_text(node.html) or " ".join(link.text().split()),
                posted_at=parse_fr_date(" ".join(node.text().split())),
                fetched_at=fetched_at,
            )
        )
    return out


class TanitJobs(Source):
    name = "tanitjobs"
    language = "fr"
    manual_url = SEARCH

    async def fetch(self) -> list[Listing]:
        # robots_allows raises SourceBlocked when robots.txt is challenged,
        # which is the current TanitJobs behaviour.
        if not await self.robots_allows(SEARCH):
            from .base import SourceBlocked

            raise SourceBlocked(self.name, self.manual_url, "robots.txt disallows")
        resp = await self.get_or_block(SEARCH)
        return parse(resp.text)
