"""SQLite persistence: idempotent upsert and status-filtered reads.

Dedupe key is ``(source, external_id)``. Re-running the pipeline refreshes
source-derived fields (title, description, ...) but never clobbers workflow
state the user owns (status, applied_at, notes) or enrichment written by the
filter stage (fit_score, matched_keywords). Use ``update_enrichment`` to set
those deliberately.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .schema import Listing, Status

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "jobs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    title            TEXT NOT NULL,
    company          TEXT NOT NULL,
    location         TEXT,
    remote           INTEGER NOT NULL DEFAULT 0,
    url              TEXT NOT NULL,
    description      TEXT NOT NULL,
    posted_at        TEXT,
    fetched_at       TEXT NOT NULL,
    fit_score        REAL NOT NULL DEFAULT 0.0,
    matched_keywords TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'new',
    applied_at       TEXT,
    notes            TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source, external_id)
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection and ensure the schema exists."""
    path = Path(db_path)
    if path.parent and str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        remote=bool(row["remote"]),
        url=row["url"],
        description=row["description"],
        posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        fit_score=row["fit_score"],
        matched_keywords=json.loads(row["matched_keywords"]),
        status=row["status"],
        applied_at=datetime.fromisoformat(row["applied_at"]) if row["applied_at"] else None,
        notes=row["notes"],
    )


def upsert_listing(conn: sqlite3.Connection, listing: Listing) -> bool:
    """Insert a listing or refresh source-derived fields if it already exists.

    Returns True if a new row was inserted, False if an existing row was
    refreshed. Workflow/enrichment fields (status, applied_at, notes,
    fit_score, matched_keywords) on an existing row are preserved.
    """
    cur = conn.execute(
        "SELECT 1 FROM listings WHERE source = ? AND external_id = ?",
        (listing.source, listing.external_id),
    )
    exists = cur.fetchone() is not None

    if exists:
        conn.execute(
            """
            UPDATE listings SET
                title = ?, company = ?, location = ?, remote = ?, url = ?,
                description = ?, posted_at = ?, fetched_at = ?
            WHERE source = ? AND external_id = ?
            """,
            (
                listing.title,
                listing.company,
                listing.location,
                int(listing.remote),
                str(listing.url),
                listing.description,
                _dt(listing.posted_at),
                _dt(listing.fetched_at),
                listing.source,
                listing.external_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO listings (
                source, external_id, title, company, location, remote, url,
                description, posted_at, fetched_at, fit_score,
                matched_keywords, status, applied_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.source,
                listing.external_id,
                listing.title,
                listing.company,
                listing.location,
                int(listing.remote),
                str(listing.url),
                listing.description,
                _dt(listing.posted_at),
                _dt(listing.fetched_at),
                listing.fit_score,
                json.dumps(listing.matched_keywords),
                listing.status,
                _dt(listing.applied_at),
                listing.notes,
            ),
        )
    conn.commit()
    return not exists


def update_enrichment(
    conn: sqlite3.Connection,
    source: str,
    external_id: str,
    *,
    fit_score: float,
    matched_keywords: list[str],
) -> None:
    """Set filter-stage enrichment for an existing listing."""
    conn.execute(
        "UPDATE listings SET fit_score = ?, matched_keywords = ? "
        "WHERE source = ? AND external_id = ?",
        (fit_score, json.dumps(matched_keywords), source, external_id),
    )
    conn.commit()


def set_status(
    conn: sqlite3.Connection,
    source: str,
    external_id: str,
    status: Status,
    *,
    only_if: Status | None = None,
) -> bool:
    """Set a listing's status. If ``only_if`` is given, update solely when
    the current status matches it (used so ranking never overrides a status
    the user already set). Returns True if a row changed.
    """
    query = "UPDATE listings SET status = ? WHERE source = ? AND external_id = ?"
    params: list[object] = [status, source, external_id]
    if only_if is not None:
        query += " AND status = ?"
        params.append(only_if)
    cur = conn.execute(query, params)
    conn.commit()
    return cur.rowcount > 0


def mark(
    conn: sqlite3.Connection,
    source: str,
    external_id: str,
    status: Status,
    *,
    applied_at: datetime | None = None,
    append_note: str | None = None,
) -> bool:
    """Update a listing's workflow state. Returns True if a row changed.

    Sets ``status``; optionally stamps ``applied_at`` and appends a dated
    line to ``notes`` (notes are append-only history, never overwritten).
    """
    row = conn.execute(
        "SELECT notes, applied_at FROM listings WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    if row is None:
        return False
    notes = row["notes"]
    if append_note:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
        line = f"[{stamp}] {append_note}"
        notes = f"{notes}\n{line}".strip() if notes else line
    # Preserve an existing applied_at unless a new one is explicitly given.
    applied_value = _dt(applied_at) if applied_at is not None else row["applied_at"]
    conn.execute(
        "UPDATE listings SET status = ?, applied_at = ?, notes = ? "
        "WHERE source = ? AND external_id = ?",
        (status, applied_value, notes, source, external_id),
    )
    conn.commit()
    return True


def get_listings(
    conn: sqlite3.Connection,
    status: Status | None = None,
    *,
    limit: int | None = None,
) -> list[Listing]:
    """Return listings, optionally filtered by status, best fit first."""
    query = "SELECT * FROM listings"
    params: list[object] = []
    if status is not None:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY fit_score DESC, posted_at DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_listing(r) for r in rows]


def get_listing(
    conn: sqlite3.Connection, source: str, external_id: str
) -> Listing | None:
    """Fetch a single listing by its dedupe key, or None."""
    row = conn.execute(
        "SELECT * FROM listings WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    return _row_to_listing(row) if row else None
