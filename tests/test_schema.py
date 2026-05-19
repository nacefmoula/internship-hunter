from datetime import datetime

import pytest
from pydantic import ValidationError

from hunter.schema import Listing


def _base(**overrides) -> dict:
    data = dict(
        source="remoteok",
        external_id="123",
        title="DevOps Intern",
        company="Acme",
        url="https://example.com/jobs/123",
        description="Kubernetes and Terraform.",
        fetched_at=datetime(2026, 5, 17, 12, 0, 0),
    )
    data.update(overrides)
    return data


def test_minimal_listing_has_sane_defaults():
    listing = Listing(**_base())
    assert listing.remote is False
    assert listing.location is None
    assert listing.fit_score == 0.0
    assert listing.matched_keywords == []
    assert listing.status == "new"
    assert listing.applied_at is None
    assert listing.notes == ""


def test_invalid_url_rejected():
    with pytest.raises(ValidationError):
        Listing(**_base(url="not-a-url"))


def test_invalid_status_rejected():
    with pytest.raises(ValidationError):
        Listing(**_base(status="bogus"))


def test_full_listing_roundtrips_through_dict():
    listing = Listing(
        **_base(
            location="Tunis, Tunisia",
            remote=True,
            posted_at=datetime(2026, 5, 1),
            fit_score=9.0,
            matched_keywords=["kubernetes", "terraform"],
            status="shortlisted",
            notes="strong match",
        )
    )
    clone = Listing(**listing.model_dump())
    assert clone == listing
    assert clone.matched_keywords == ["kubernetes", "terraform"]
