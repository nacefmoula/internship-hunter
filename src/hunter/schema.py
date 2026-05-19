"""Normalized job listing schema shared across all sources."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, HttpUrl

Status = Literal[
    "new",
    "shortlisted",
    "drafted",
    "applied",
    "rejected",
    "interview",
    "offer",
    "skipped",
]


class Listing(BaseModel):
    source: str
    external_id: str
    title: str
    company: str
    location: str | None = None
    remote: bool = False
    url: HttpUrl
    description: str
    posted_at: datetime | None = None
    fetched_at: datetime
    # enrichment
    fit_score: float = 0.0
    matched_keywords: list[str] = []
    status: Status = "new"
    applied_at: datetime | None = None
    notes: str = ""
