"""Emploitunisie.com scraper (Tunisia).

robots.txt: ``User-agent: *`` is allowed ``/`` (only named AI bots are
disallowed; our crawler UA is permitted). Job cards are
``div.card.card-job`` with a ``data-href`` to the full posting.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from selectolax.parser import HTMLParser

from ..schema import Listing
from .base import Source, SourceBlocked, html_to_text, logger, parse_fr_date

BASE = "https://www.emploitunisie.com"
SEARCH = BASE + "/recherche-jobs-tunisie/{kw}"
QUERIES = (
    "devops",
    "cloud",
    "sre",
    "stage devops",
    "stage cloud",
    "stagiaire devops",
)

_ID_RE = re.compile(r"(\d+)(?:[/?#]|$)")


def _abs(href: str) -> str:
    return href if href.startswith("http") else BASE + href


def parse(html: bytes | str) -> list[Listing]:
    """Parse one Emploitunisie results page into Listings (no network)."""
    from datetime import UTC, datetime

    tree = HTMLParser(html if isinstance(html, str) else html.decode("utf-8", "replace"))
    fetched_at = datetime.now(UTC)
    out: list[Listing] = []
    for card in tree.css("div.card.card-job"):
        data_href = card.attributes.get("data-href") or ""
        title_a = card.css_first("div.card-job-detail h3 a")
        if title_a is None:
            continue
        href = data_href or title_a.attributes.get("href") or ""
        if not href:
            continue
        url = _abs(href)
        m = _ID_RE.search(url)
        ext_id = m.group(1) if m else url

        title = " ".join(title_a.text().split())
        comp_node = card.css_first("a.card-job-company")
        company = " ".join(comp_node.text().split()) if comp_node else "Unknown"
        desc_node = card.css_first("div.card-job-description")
        description = html_to_text(desc_node.html) if desc_node else ""

        meta = card.css_first("ul")
        meta_text = " ".join(meta.text().split()) if meta else ""
        location = "Tunisie"
        rm = re.search(r"R[ée]gion de\s*:\s*([^|]+?)(?:Niveau|$)", meta_text, re.I)
        if rm:
            location = rm.group(1).strip(" -") or "Tunisie"

        time_node = card.css_first("time")
        posted_at = parse_fr_date(time_node.text() if time_node else meta_text)
        remote = "télétravail" in (meta_text + description).lower()

        out.append(
            Listing(
                source="emploitunisie",
                external_id=str(ext_id),
                title=title,
                company=company or "Unknown",
                location=location,
                remote=remote,
                url=url,
                description=description or title,
                posted_at=posted_at,
                fetched_at=fetched_at,
            )
        )
    return out


class EmploiTunisie(Source):
    name = "emploitunisie"
    language = "fr"
    manual_url = BASE + "/recherche-jobs-tunisie/devops"

    async def fetch(self) -> list[Listing]:
        probe = SEARCH.format(kw=quote(QUERIES[0]))
        if not await self.robots_allows(probe):
            raise SourceBlocked(self.name, self.manual_url, "robots.txt disallows")
        seen: set[str] = set()
        listings: list[Listing] = []
        for kw in QUERIES:
            try:
                resp = await self.get_or_block(SEARCH.format(kw=quote(kw)))
            except SourceBlocked:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad query is not fatal
                logger.warning("emploitunisie query %r failed: %s", kw, exc)
                continue
            for listing in parse(resp.text):
                if listing.external_id in seen:
                    continue
                seen.add(listing.external_id)
                listings.append(listing)
        return listings
