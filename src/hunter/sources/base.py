"""Source abstraction shared by every job board adapter.

Design rules baked in here so individual sources can't forget them:

* **Polite**: every outbound request carries a descriptive User-Agent.
* **Rate-limited**: at most one request per ``min_interval`` seconds per
  source instance (default 2s), enforced by an async lock.
* **Fail soft**: ``fetch_safe`` wraps ``fetch`` so one broken source logs
  and yields ``[]`` instead of crashing the pipeline.
* **Testable offline**: HTTP and parsing are separate. ``parse`` takes raw
  bytes/objects and never touches the network, so fixtures drive tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

import httpx
from selectolax.parser import HTMLParser

from ..schema import Listing


def html_to_text(html: str | None) -> str:
    """Collapse an HTML description into clean whitespace-normalized text."""
    if not html:
        return ""
    text = HTMLParser(html).text(separator=" ", strip=True)
    return " ".join(text.split())

logger = logging.getLogger("hunter.sources")

DEFAULT_USER_AGENT = (
    "internship-hunter/0.1 (personal internship search bot; "
    "respects robots.txt; contact via repo owner)"
)

# Markers of a bot-wall (Cloudflare JS challenge, etc.) seen in a body.
_BLOCK_MARKERS = (
    "just a moment...",
    "challenge-platform",
    "cf-chl",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
)


class SourceBlocked(Exception):
    """Raised when a source actively blocks automated access.

    Carries the URL the user should open in a real browser instead.
    """

    def __init__(self, source: str, manual_url: str, detail: str = "") -> None:
        self.source = source
        self.manual_url = manual_url
        super().__init__(
            f"{source} blocked"
            + (f" ({detail})" if detail else "")
            + f" — check manually: {manual_url}"
        )


def looks_blocked(text: str) -> bool:
    """Heuristic: does this body look like an anti-bot challenge page?"""
    head = text[:4000].lower()
    return any(m in head for m in _BLOCK_MARKERS)


_FR_MONTHS = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}


def parse_fr_date(text: str | None):
    """Parse Tunisian board date formats -> aware UTC datetime, or None.

    Handles ``"11 mai 2026"`` and ``"13.04.2026"`` / ``"13/04/2026"``.
    """
    import re
    from datetime import UTC, datetime

    if not text:
        return None
    s = text.strip().lower()
    m = re.search(r"(\d{1,2})\s+([a-zàâäéèêëîïôöûüç]+)\s+(\d{4})", s)
    if m and m.group(2) in _FR_MONTHS:
        d, mon, y = int(m.group(1)), _FR_MONTHS[m.group(2)], int(m.group(3))
        try:
            return datetime(y, mon, d, tzinfo=UTC)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:
        d, mon, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mon, d, tzinfo=UTC)
        except ValueError:
            return None
    return None


class RateLimiter:
    """Minimum-interval async throttle, shared per source instance."""

    def __init__(self, min_interval: float = 2.0) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                await asyncio.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class Source(ABC):
    """Abstract job source. Subclasses set ``name`` and implement ``fetch``."""

    name: str = ""
    #: Primary language of this board's postings ("en" or "fr"). Drives which
    #: CV + cover-letter language `hunter draft` uses for the listing.
    language: str = "en"
    #: Seconds between requests; >= 2.0 keeps us polite per project rules.
    min_interval: float = 2.0
    #: Default request timeout (seconds).
    timeout: float = 20.0
    #: Human-facing URL to open if the source blocks automation.
    manual_url: str | None = None

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__} must set a class-level `name`")
        self._external_client = client is not None
        self._client = client
        self._limiter = RateLimiter(self.min_interval)
        #: Set True by fetch_safe when the source actively blocked us.
        self.blocked = False

    # --- HTTP -----------------------------------------------------------
    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": DEFAULT_USER_AGENT},
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        """Rate-limited GET with the polite User-Agent."""
        client = self._ensure_client()
        await self._limiter.wait()
        logger.debug("%s GET %s", self.name, url)
        resp = await client.get(url, **kwargs)
        resp.raise_for_status()
        return resp

    async def get_or_block(self, url: str, **kwargs) -> httpx.Response:
        """GET that turns anti-bot walls into a clean SourceBlocked.

        Treats HTTP 403/429/503 and challenge-page bodies as blocks.
        """
        try:
            resp = await self._get(url, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403, 429, 503):
                raise SourceBlocked(
                    self.name,
                    self.manual_url or url,
                    f"HTTP {exc.response.status_code}",
                ) from exc
            raise
        if looks_blocked(resp.text):
            raise SourceBlocked(self.name, self.manual_url or url, "challenge page")
        return resp

    async def robots_allows(self, url: str) -> bool:
        """Check the site's robots.txt for our User-Agent (fail-open)."""
        from urllib.parse import urlsplit
        from urllib.robotparser import RobotFileParser

        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            resp = await self._get(robots_url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: robots.txt fetch failed (%s); fail-open", self.name, exc)
            return True
        if looks_blocked(resp.text):
            # Even robots.txt is behind a challenge -> the site blocks bots.
            raise SourceBlocked(self.name, self.manual_url or url, "robots.txt challenged")
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp.can_fetch(DEFAULT_USER_AGENT, url)

    async def aclose(self) -> None:
        if self._client is not None and not self._external_client:
            await self._client.aclose()
            self._client = None

    # --- contract -------------------------------------------------------
    @abstractmethod
    async def fetch(self) -> list[Listing]:
        """Fetch and normalize all listings from this source."""

    async def fetch_safe(self) -> list[Listing]:
        """Run ``fetch`` but never raise: log and return ``[]`` on failure."""
        try:
            return await self.fetch()
        except SourceBlocked as exc:
            self.blocked = True
            logger.warning("%s", exc)
            return []
        except Exception as exc:  # noqa: BLE001 - fail soft is intentional
            logger.warning("source %s failed: %s", self.name, exc)
            return []
        finally:
            await self.aclose()
