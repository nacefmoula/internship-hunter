import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from hunter.schema import Listing
from hunter.storage import connect, get_listing, upsert_listing
from hunter.tracker import TrackerError, export_csv, followups, mark

NOW = datetime(2026, 5, 17, tzinfo=UTC)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "jobs.db")
    yield c
    c.close()


def seed(conn, external_id="1", **kw) -> Listing:
    data = dict(
        source="remoteok",
        external_id=external_id,
        title="DevOps Intern",
        company="Acme",
        location="Remote",
        remote=True,
        url=f"https://example.com/{external_id}",
        description="k8s",
        posted_at=NOW - timedelta(days=2),
        fetched_at=NOW,
    )
    data.update(kw)
    listing = Listing(**data)
    upsert_listing(conn, listing)
    return listing


def test_mark_applied_stamps_applied_at(conn):
    seed(conn)
    mark(conn, "remoteok", "1", "applied", now=NOW)
    got = get_listing(conn, "remoteok", "1")
    assert got.status == "applied"
    assert got.applied_at == NOW


def test_mark_rejects_invalid_status(conn):
    seed(conn)
    with pytest.raises(TrackerError, match="invalid status"):
        mark(conn, "remoteok", "1", "ghosted")


def test_mark_unknown_listing_raises(conn):
    with pytest.raises(TrackerError, match="no listing"):
        mark(conn, "nope", "999", "applied")


def test_mark_non_applied_preserves_applied_at(conn):
    seed(conn)
    mark(conn, "remoteok", "1", "applied", now=NOW)
    mark(conn, "remoteok", "1", "interview", now=NOW + timedelta(days=3))
    got = get_listing(conn, "remoteok", "1")
    assert got.status == "interview"
    assert got.applied_at == NOW  # not cleared


def test_mark_appends_dated_note_history(conn):
    seed(conn)
    mark(conn, "remoteok", "1", "applied", note="sent via site", now=NOW)
    mark(conn, "remoteok", "1", "interview", note="call booked", now=NOW)
    notes = get_listing(conn, "remoteok", "1").notes
    assert "sent via site" in notes and "call booked" in notes
    assert notes.count("\n") == 1  # two appended lines


def test_followups_lists_only_stale_applied(conn):
    seed(conn, "old")
    seed(conn, "fresh")
    seed(conn, "interviewing")
    mark(conn, "remoteok", "old", "applied", now=NOW - timedelta(days=10))
    mark(conn, "remoteok", "fresh", "applied", now=NOW - timedelta(days=2))
    mark(conn, "remoteok", "interviewing", "applied", now=NOW - timedelta(days=30))
    mark(conn, "remoteok", "interviewing", "interview", now=NOW)

    pending = followups(conn, days=7, now=NOW)
    ids = [x.external_id for x in pending]
    assert ids == ["old"]  # fresh too recent, interviewing got a response


def test_followups_window_is_configurable(conn):
    seed(conn, "a")
    mark(conn, "remoteok", "a", "applied", now=NOW - timedelta(days=5))
    assert followups(conn, days=7, now=NOW) == []
    assert [x.external_id for x in followups(conn, days=3, now=NOW)] == ["a"]


def test_export_csv_roundtrips(conn):
    seed(conn, "1", company="Acme")
    mark(conn, "remoteok", "1", "applied", note="line one", now=NOW)
    text = export_csv(conn)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 1
    r = rows[0]
    assert r["source"] == "remoteok"
    assert r["status"] == "applied"
    assert r["company"] == "Acme"
    assert "line one" in r["notes"]
    assert "\n" not in r["notes"]  # newlines flattened for the cell


def test_export_csv_status_filter(conn):
    seed(conn, "1")
    seed(conn, "2")
    mark(conn, "remoteok", "2", "applied", now=NOW)
    rows = list(csv.DictReader(io.StringIO(export_csv(conn, status="applied"))))
    assert [r["external_id"] for r in rows] == ["2"]
