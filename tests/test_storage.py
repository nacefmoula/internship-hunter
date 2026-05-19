from datetime import datetime

import pytest

from hunter.schema import Listing
from hunter.storage import (
    connect,
    get_listing,
    get_listings,
    update_enrichment,
    upsert_listing,
)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "jobs.db")
    yield c
    c.close()


def make_listing(external_id="1", **overrides) -> Listing:
    data = dict(
        source="remoteok",
        external_id=external_id,
        title="DevOps Intern",
        company="Acme",
        location="Remote",
        remote=True,
        url="https://example.com/jobs/" + external_id,
        description="Kubernetes, Terraform, CI/CD.",
        posted_at=datetime(2026, 5, 1),
        fetched_at=datetime(2026, 5, 17, 12, 0, 0),
    )
    data.update(overrides)
    return Listing(**data)


def test_insert_then_read_back(conn):
    inserted = upsert_listing(conn, make_listing())
    assert inserted is True

    got = get_listing(conn, "remoteok", "1")
    assert got is not None
    assert got.title == "DevOps Intern"
    assert got.remote is True
    assert got.posted_at == datetime(2026, 5, 1)


def test_upsert_is_idempotent_no_duplicate_rows(conn):
    upsert_listing(conn, make_listing())
    inserted_again = upsert_listing(conn, make_listing(title="DevOps Intern (updated)"))
    assert inserted_again is False

    rows = get_listings(conn)
    assert len(rows) == 1
    assert rows[0].title == "DevOps Intern (updated)"


def test_reupsert_preserves_workflow_and_enrichment(conn):
    upsert_listing(conn, make_listing())
    update_enrichment(
        conn, "remoteok", "1", fit_score=7.5, matched_keywords=["kubernetes"]
    )
    conn.execute(
        "UPDATE listings SET status='applied', notes='sent', applied_at=? "
        "WHERE source='remoteok' AND external_id='1'",
        (datetime(2026, 5, 10).isoformat(),),
    )
    conn.commit()

    # Re-fetch from source overwrites description but must keep user state.
    upsert_listing(conn, make_listing(description="New description from refetch."))

    got = get_listing(conn, "remoteok", "1")
    assert got.description == "New description from refetch."
    assert got.status == "applied"
    assert got.notes == "sent"
    assert got.applied_at == datetime(2026, 5, 10)
    assert got.fit_score == 7.5
    assert got.matched_keywords == ["kubernetes"]


def test_get_listings_filters_by_status(conn):
    upsert_listing(conn, make_listing("1"))
    upsert_listing(conn, make_listing("2"))
    conn.execute("UPDATE listings SET status='applied' WHERE external_id='2'")
    conn.commit()

    new_only = get_listings(conn, status="new")
    assert [x.external_id for x in new_only] == ["1"]
    applied_only = get_listings(conn, status="applied")
    assert [x.external_id for x in applied_only] == ["2"]


def test_get_listings_orders_by_fit_score_desc(conn):
    upsert_listing(conn, make_listing("low"))
    upsert_listing(conn, make_listing("high"))
    update_enrichment(conn, "remoteok", "low", fit_score=1.0, matched_keywords=[])
    update_enrichment(conn, "remoteok", "high", fit_score=9.0, matched_keywords=[])

    ordered = get_listings(conn)
    assert [x.external_id for x in ordered] == ["high", "low"]


def test_get_listings_respects_limit(conn):
    for i in range(5):
        upsert_listing(conn, make_listing(str(i)))
    assert len(get_listings(conn, limit=3)) == 3


def test_distinct_sources_same_external_id_coexist(conn):
    upsert_listing(conn, make_listing("1", source="remoteok"))
    upsert_listing(conn, make_listing("1", source="remotive"))
    assert len(get_listings(conn)) == 2
