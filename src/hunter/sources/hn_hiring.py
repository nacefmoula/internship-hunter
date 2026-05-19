"""Hacker News "Ask HN: Who is hiring?" adapter.

Two polite requests via the Algolia HN API (no Firebase fan-out over
hundreds of comments):

1. ``search_by_date`` for ``author_whoishiring`` stories -> pick the most
   recent "Who is hiring?" thread.
2. one ``search`` call for all top-level comments of that story.

Only comments mentioning DevOps/Cloud/SRE-relevant terms are kept; the
monthly thread carries every kind of job.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from ..schema import Listing
from .base import Source, html_to_text

SEARCH_URL = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?tags=story,author_whoishiring&query=hiring&hitsPerPage=12"
)
COMMENTS_URL = (
    "https://hn.algolia.com/api/v1/search"
    "?tags=comment,story_{story_id}&hitsPerPage=1000"
)

# Distinctive terms; word-boundary matched to avoid e.g. 'sre' in 'pressure'.
RELEVANCE_TERMS = [
    "devops",
    "sre",
    "site reliability",
    "kubernetes",
    "k8s",
    "terraform",
    "ansible",
    "docker",
    "cloud",
    "aws",
    "gcp",
    "azure",
    "ci/cd",
    "platform engineer",
    "infrastructure",
    "observability",
    "prometheus",
    "grafana",
]
_RELEVANCE_RE = re.compile(
    "|".join(rf"\b{re.escape(t)}\b" for t in RELEVANCE_TERMS), re.IGNORECASE
)
_HIRING_RE = re.compile(r"who\s+is\s+hiring", re.IGNORECASE)
_NOT_HIRING_RE = re.compile(r"freelanc|wants?\s+to\s+be\s+hired", re.IGNORECASE)


def pick_latest_hiring_story(raw: bytes | str | dict) -> dict | None:
    """From a search payload, return the newest 'Who is hiring?' story."""
    data = raw if isinstance(raw, dict) else json.loads(raw)
    candidates = [
        h
        for h in data.get("hits", [])
        if h.get("author") == "whoishiring"
        and _HIRING_RE.search(h.get("title", ""))
        and not _NOT_HIRING_RE.search(h.get("title", ""))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda h: h.get("created_at_i", 0))


def _parse_company_title(text: str) -> tuple[str, str | None]:
    """HN posts conventionally lead with 'Company | Role | Location | ...'."""
    first = text.split("\n", 1)[0]
    parts = [p.strip() for p in first.split("|") if p.strip()]
    if not parts:
        return "Unknown", None
    company = parts[0][:80] or "Unknown"
    title = parts[1][:120] if len(parts) > 1 else None
    return company, title


def parse_comments(raw: bytes | str | dict) -> list[Listing]:
    """Parse a story's comments payload into relevant Listings (no network)."""
    data = raw if isinstance(raw, dict) else json.loads(raw)
    fetched_at = datetime.now(UTC)
    listings: list[Listing] = []
    for hit in data.get("hits", []):
        body = html_to_text(hit.get("comment_text"))
        if not body or not _RELEVANCE_RE.search(body):
            continue
        obj_id = str(hit.get("objectID"))
        company, parsed_title = _parse_company_title(body)
        title = parsed_title or f"{company} (HN: who is hiring)"
        posted_at: datetime | None = None
        if hit.get("created_at"):
            try:
                posted_at = datetime.fromisoformat(
                    hit["created_at"].replace("Z", "+00:00")
                )
            except ValueError:
                posted_at = None
        listings.append(
            Listing(
                source="hn_hiring",
                external_id=obj_id,
                title=title,
                company=company,
                location=None,
                remote="remote" in body.lower(),
                url=f"https://news.ycombinator.com/item?id={obj_id}",
                description=body,
                posted_at=posted_at,
                fetched_at=fetched_at,
            )
        )
    return listings


class HNHiring(Source):
    name = "hn_hiring"

    async def fetch(self) -> list[Listing]:
        search_resp = await self._get(SEARCH_URL)
        story = pick_latest_hiring_story(search_resp.content)
        if story is None:
            return []
        story_id = story.get("objectID")
        comments_resp = await self._get(
            COMMENTS_URL.format(story_id=story_id)
        )
        return parse_comments(comments_resp.content)
