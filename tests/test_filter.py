from datetime import UTC, datetime, timedelta

from hunter.filter import load_profile, rank_listings, score_listing
from hunter.schema import Listing

NOW = datetime(2026, 5, 17, tzinfo=UTC)

PROFILE = {
    "keywords_strong": ["kubernetes", "terraform", "ci/cd"],
    "keywords_nice": ["python", "sre"],
    "hard_filters": {
        "internship_terms": ["intern", "internship", "stage", "trainee"],
        "exclude_terms": ["senior", "lead", "10+ years"],
        "location_rules": [
            {"tunisia_ok": True},
            {"remote_ok": True},
            {"onsite_abroad": False},
        ],
        "max_age_days": 30,
    },
}


def L(**kw) -> Listing:
    data = dict(
        source="t",
        external_id="1",
        title="DevOps Intern",
        company="Acme",
        location=None,
        remote=True,
        url="https://example.com/1",
        description="",
        posted_at=NOW - timedelta(days=1),
        fetched_at=NOW,
    )
    data.update(kw)
    return Listing(**data)


def test_strong_keyword_in_title_scores_3():
    r = score_listing(L(title="Kubernetes Intern"), PROFILE, now=NOW)
    assert r.fit_score == 3
    assert "kubernetes" in r.matched_keywords
    assert not r.rejected


def test_strong_keyword_in_description_scores_1():
    r = score_listing(
        L(title="Intern", description="we use terraform"), PROFILE, now=NOW
    )
    assert r.fit_score == 1


def test_strong_in_title_and_description_stacks_to_4():
    r = score_listing(
        L(title="Terraform Intern", description="terraform daily"),
        PROFILE,
        now=NOW,
    )
    assert r.fit_score == 4
    assert r.matched_keywords == ["terraform"]


def test_nice_keyword_scores_1():
    r = score_listing(
        L(title="Intern", description="python and sre"), PROFILE, now=NOW
    )
    assert r.fit_score == 2  # python + sre


def test_multiword_keyword_matches():
    r = score_listing(L(title="CI/CD Intern"), PROFILE, now=NOW)
    assert r.fit_score == 3


def test_exclude_term_in_title_without_internship_term_is_rejected():
    r = score_listing(
        L(title="Senior Kubernetes Engineer", description="kubernetes"),
        PROFILE,
        now=NOW,
    )
    assert r.rejected
    assert "senior" in (r.reject_reason or "")


def test_exclude_term_allowed_when_internship_term_present():
    r = score_listing(
        L(title="Senior? no — DevOps Intern", description="kubernetes"),
        PROFILE,
        now=NOW,
    )
    assert not r.rejected


def test_internship_term_uses_word_boundary_not_substring():
    # "internal" / "international" must NOT satisfy the internship test,
    # so a Senior title with such words still gets rejected.
    r = score_listing(
        L(
            title="Senior Platform Engineer",
            description="work on internal international internet systems",
        ),
        PROFILE,
        now=NOW,
    )
    assert r.rejected
    assert "senior" in (r.reject_reason or "")


def test_exclude_term_word_boundary_not_substring():
    # "leadership" should not trip the "lead" exclude term.
    r = score_listing(
        L(
            title="DevOps Intern with leadership skills",
            description="kubernetes",
        ),
        PROFILE,
        now=NOW,
    )
    assert not r.rejected


def test_onsite_abroad_rejected():
    r = score_listing(
        L(title="DevOps Intern", remote=False, location="Berlin, Germany"),
        PROFILE,
        now=NOW,
    )
    assert r.rejected
    assert "on-site abroad" in (r.reject_reason or "")


def test_onsite_tunisia_accepted():
    # French "stage" only counts on French sources (keejob/emploitunisie/...).
    r = score_listing(
        L(
            source="keejob",
            title="Stage DevOps",
            remote=False,
            location="Tunis, Tunisia",
        ),
        PROFILE,
        now=NOW,
    )
    assert not r.rejected


def test_remote_accepted():
    r = score_listing(
        L(title="DevOps Intern", remote=True, location="Anywhere"),
        PROFILE,
        now=NOW,
    )
    assert not r.rejected


def test_stale_posting_rejected():
    r = score_listing(
        L(title="DevOps Intern", posted_at=NOW - timedelta(days=45)),
        PROFILE,
        now=NOW,
    )
    assert r.rejected
    assert "stale" in (r.reject_reason or "")


def test_missing_posted_at_not_rejected_for_age():
    r = score_listing(L(title="DevOps Intern", posted_at=None), PROFILE, now=NOW)
    assert not r.rejected


def test_rank_sorts_kept_by_score_desc_and_splits_rejected():
    listings = [
        L(external_id="low", title="Cloud Intern", description="kubernetes"),
        L(external_id="high", title="Kubernetes Terraform Intern"),
        L(external_id="bad", title="Senior Engineer", description="x"),
    ]
    kept, rejected = rank_listings(listings, PROFILE, now=NOW)
    assert [x[0].external_id for x in kept] == ["high", "low"]
    assert [x[0].external_id for x in rejected] == ["bad"]


def test_non_internship_rejected_even_without_exclude_term():
    # A plain full-time role (no senior term, no internship term) must be
    # dropped — this is the internship-only requirement.
    r = score_listing(
        L(title="DevOps Engineer (M/F)", description="kubernetes terraform"),
        PROFILE,
        now=NOW,
    )
    assert r.rejected
    assert "not an internship" in (r.reject_reason or "")


def test_require_internship_false_keeps_regular_jobs():
    profile = {**PROFILE, "hard_filters": {**PROFILE["hard_filters"]}}
    profile["hard_filters"]["require_internship"] = False
    r = score_listing(
        L(title="DevOps Engineer", description="kubernetes"), profile, now=NOW
    )
    assert not r.rejected


def test_english_stage_word_does_not_count_as_internship():
    # "early-stage startup" on an English board must NOT qualify.
    r = score_listing(
        L(
            source="remoteok",
            title="Account Executive",
            description="join our early-stage startup at this stage",
        ),
        PROFILE,
        now=NOW,
    )
    assert r.rejected
    assert "not an internship" in (r.reject_reason or "")


def test_french_stage_counts_on_french_source():
    r = score_listing(
        L(source="keejob", title="Offre de stage DevOps", location="Tunis"),
        PROFILE,
        now=NOW,
    )
    assert not r.rejected


def test_real_profile_yaml_loads():
    p = load_profile()
    assert "keywords_strong" in p and "hard_filters" in p
