"""Application tracking: status changes, follow-up detection, CSV export.

A listing still in ``applied`` after the follow-up window with no later
transition (interview/offer/rejected) is a candidate for a nudge.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import get_args

from .schema import Listing, Status
from .storage import get_listings
from .storage import mark as _storage_mark

VALID_STATUSES: tuple[str, ...] = get_args(Status)


class TrackerError(ValueError):
    """Raised for user-actionable tracking errors (bad status/id)."""


def mark(
    conn: sqlite3.Connection,
    source: str,
    external_id: str,
    status: str,
    *,
    note: str | None = None,
    now: datetime | None = None,
) -> None:
    """Set a listing's status. Stamps applied_at when moving to 'applied'."""
    if status not in VALID_STATUSES:
        raise TrackerError(
            f"invalid status '{status}'. Valid: {', '.join(VALID_STATUSES)}"
        )
    applied_at = (now or datetime.now(UTC)) if status == "applied" else None
    if not _storage_mark(
        conn,
        source,
        external_id,
        status,  # type: ignore[arg-type]
        applied_at=applied_at,
        append_note=note,
    ):
        raise TrackerError(f"no listing {source}:{external_id}")


def followups(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> list[Listing]:
    """Applied listings with no response older than ``days``."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    out: list[Listing] = []
    for listing in get_listings(conn, status="applied"):
        applied = listing.applied_at
        if applied is None:
            continue
        if applied.tzinfo is None:
            applied = applied.replace(tzinfo=UTC)
        if applied <= cutoff:
            out.append(listing)
    out.sort(key=lambda x: x.applied_at or now)
    return out


_EXPORT_COLUMNS = [
    "source",
    "external_id",
    "status",
    "fit_score",
    "company",
    "title",
    "location",
    "remote",
    "url",
    "posted_at",
    "applied_at",
    "matched_keywords",
    "notes",
]


def export_csv(
    conn: sqlite3.Connection,
    *,
    status: Status | None = None,
) -> str:
    """Return all listings (optionally filtered) as CSV text for a Sheet."""
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for x in get_listings(conn, status=status):
        writer.writerow(
            {
                "source": x.source,
                "external_id": x.external_id,
                "status": x.status,
                "fit_score": f"{x.fit_score:.0f}",
                "company": x.company,
                "title": x.title,
                "location": x.location or "",
                "remote": x.remote,
                "url": str(x.url),
                "posted_at": x.posted_at.isoformat() if x.posted_at else "",
                "applied_at": x.applied_at.isoformat() if x.applied_at else "",
                "matched_keywords": ", ".join(x.matched_keywords),
                "notes": (x.notes or "").replace("\n", " | "),
            }
        )
    return buf.getvalue()
