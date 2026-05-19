"""Keejob.com scraper (Tunisia).

robots.txt allows ``/offres-emploi/`` (only /api/, /recruiter/* etc. are
disallowed). We query a few role keywords, rate-limited and with a polite
User-Agent. Jobs are on-site in Tunisia unless the text says télétravail.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

from selectolax.parser import HTMLParser

from ..schema import Listing
from .base import Source, SourceBlocked, html_to_text, logger, parse_fr_date

BASE = "https://www.keejob.com"
SEARCH = BASE + "/offres-emploi/?keywords={kw}"
QUERIES = (
    "devops",
    "cloud",
    "sre",
    "stage devops",
    "stage cloud",
    "stagiaire devops",
)

_JOB_HREF = re.compile(r"^/offres-emploi/(\d+)/")
_TN_REGIONS = (
    "tunis",
    "ariana",
    "ben arous",
    "manouba",
    "nabeul",
    "sousse",
    "sfax",
    "monastir",
    "bizerte",
    "gabes",
    "kairouan",
    "tunisie",
)


def _abs(href: str) -> str:
    return href if href.startswith("http") else BASE + href


def parse(html: bytes | str) -> list[Listing]:
    """Parse one Keejob results page into Listings (no network)."""
    from datetime import UTC, datetime

    tree = HTMLParser(html if isinstance(html, str) else html.decode("utf-8", "replace"))
    fetched_at = datetime.now(UTC)
    out: list[Listing] = []
    for art in tree.css("article"):
        title_a = None
        for a in art.css("a"):
            href = a.attributes.get("href") or ""
            if _JOB_HREF.match(href):
                title_a = a
                break
        if title_a is None:
            continue
        href = title_a.attributes["href"]
        ext_id = _JOB_HREF.match(href).group(1)
        title = " ".join(title_a.text().split())

        company = "Unknown"
        for a in art.css("a"):
            h = a.attributes.get("href") or ""
            if "/offres-emploi/companies/" in h and a.text().strip():
                company = " ".join(a.text().split())
                break

        desc_node = art.css_first("div.mb-3 p") or art.css_first("p")
        description = html_to_text(desc_node.html) if desc_node else ""

        art_text = " ".join(art.text().split())
        location = next(
            (r.title() for r in _TN_REGIONS if r in art_text.lower()),
            "Tunisie",
        )
        remote = "télétravail" in art_text.lower() or "teletravail" in art_text.lower()
        posted_at = parse_fr_date(art_text)

        out.append(
            Listing(
                source="keejob",
                external_id=ext_id,
                title=title,
                company=company,
                location=location,
                remote=remote,
                url=_abs(href),
                description=description or title,
                posted_at=posted_at,
                fetched_at=fetched_at,
            )
        )
    return out


class Keejob(Source):
    name = "keejob"
    language = "fr"
    manual_url = BASE + "/offres-emploi/?keywords=devops"

    async def fetch(self) -> list[Listing]:
        probe = SEARCH.format(kw=quote_plus(QUERIES[0]))
        if not await self.robots_allows(probe):
            raise SourceBlocked(self.name, self.manual_url, "robots.txt disallows")
        seen: set[str] = set()
        listings: list[Listing] = []
        for kw in QUERIES:
            try:
                resp = await self.get_or_block(SEARCH.format(kw=quote_plus(kw)))
            except SourceBlocked:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad query is not fatal
                logger.warning("keejob query %r failed: %s", kw, exc)
                continue
            for listing in parse(resp.text):
                if listing.external_id in seen:
                    continue
                seen.add(listing.external_id)
                listings.append(listing)
        return listings
