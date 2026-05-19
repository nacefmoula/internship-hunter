import json
from pathlib import Path

import pytest

from hunter.schema import Listing
from hunter.sources import REGISTRY
from hunter.sources.base import Source, SourceBlocked, html_to_text, looks_blocked, parse_fr_date
from hunter.sources.emploitunisie import parse as parse_et
from hunter.sources.himalayas import parse as parse_himalayas
from hunter.sources.hn_hiring import parse_comments, pick_latest_hiring_story
from hunter.sources.keejob import Keejob
from hunter.sources.keejob import parse as parse_keejob
from hunter.sources.remoteok import parse as parse_remoteok
from hunter.sources.remotive import parse as parse_remotive
from hunter.sources.summer_internships import parse as parse_summer
from hunter.sources.wwr import parse as parse_wwr

FIXTURES = Path(__file__).parent / "fixtures"


def test_html_to_text_strips_and_normalizes():
    out = html_to_text("<p>Hello <b>world</b></p>\n<p>  CI/CD   here </p>")
    assert out == "Hello world CI/CD here"
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_registry_names_match_classes():
    for name, cls in REGISTRY.items():
        assert cls.name == name
        assert issubclass(cls, Source)


# --- RemoteOK -----------------------------------------------------------
def test_remoteok_parses_fixture():
    raw = (FIXTURES / "remoteok.json").read_bytes()
    listings = parse_remoteok(raw)
    assert listings, "expected at least one listing"
    assert all(isinstance(x, Listing) for x in listings)
    assert all(x.source == "remoteok" for x in listings)
    assert all(x.remote is True for x in listings)
    assert all(x.external_id for x in listings)
    assert all("<" not in x.description for x in listings)  # HTML stripped


def test_remoteok_skips_legal_notice_element():
    raw = json.loads((FIXTURES / "remoteok.json").read_text())
    assert "legal" in raw[0]  # first element is the notice
    listings = parse_remoteok(raw)
    # No listing should carry the metadata element's shape.
    assert len(listings) == sum(
        1 for it in raw if isinstance(it, dict) and "position" in it
    )


def test_remoteok_handles_empty_and_garbage():
    assert parse_remoteok([]) == []
    assert parse_remoteok([{"legal": "x"}, {"nope": 1}]) == []


# --- Remotive -----------------------------------------------------------
def test_remotive_parses_fixture():
    raw = (FIXTURES / "remotive.json").read_bytes()
    listings = parse_remotive(raw)
    assert listings
    assert all(x.source == "remotive" for x in listings)
    assert all(x.remote is True for x in listings)
    sample = listings[0]
    assert sample.title and sample.company and str(sample.url).startswith("http")
    assert "<" not in sample.description


def test_remotive_handles_missing_jobs_key():
    assert parse_remotive({"job-count": 0}) == []
    assert parse_remotive('{"jobs": []}') == []


def test_summer_internships_parses_and_filters():
    raw = (FIXTURES / "summer2026.json").read_bytes()
    listings = parse_summer(raw)
    # 4 entries: keep only active+visible+Summer (drops the closed + the Fall).
    ids = {x.external_id for x in listings}
    assert ids == {
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    }
    assert all(x.source == "summer_internships" for x in listings)
    devops = next(x for x in listings if x.company == "CloudCorp")
    assert devops.remote is True  # "Remote in USA"
    assert "internship" in devops.description.lower()
    tesla = next(x for x in listings if x.company == "Tesla")
    assert tesla.remote is False  # "Palo Alto, CA"
    assert tesla.posted_at is not None


def test_summer_internships_handles_empty_and_garbage():
    assert parse_summer("[]") == []
    assert parse_summer([]) == []


# --- WWR ----------------------------------------------------------------
def test_wwr_parses_company_and_title():
    raw = (FIXTURES / "wwr.rss").read_bytes()
    listings = parse_wwr(raw)
    assert len(listings) == 3
    by_company = {x.company: x for x in listings}
    assert by_company["Acme Cloud"].title == "Junior DevOps Engineer (Internship)"
    assert by_company["Acme Cloud"].location == "Anywhere in the World"
    assert all(x.remote is True for x in listings)
    assert all(x.source == "wwr" for x in listings)
    assert all("<" not in x.description for x in listings)


def test_wwr_title_without_colon_keeps_full_title():
    listings = parse_wwr((FIXTURES / "wwr.rss").read_bytes())
    initech = next(x for x in listings if "initech" in str(x.url))
    assert initech.company == "Unknown"
    assert initech.title == "Platform Engineer at Initech"


def test_wwr_handles_empty_feed():
    assert parse_wwr(b"<rss><channel></channel></rss>") == []


# --- Himalayas ----------------------------------------------------------
def test_himalayas_parses_fixture():
    raw = (FIXTURES / "himalayas.json").read_bytes()
    listings = parse_himalayas(raw)
    assert listings
    sample = listings[0]
    assert sample.source == "himalayas"
    assert sample.remote is True
    assert sample.location  # locationRestrictions or "Worldwide"
    assert sample.posted_at is not None  # epoch parsed
    assert "<" not in sample.description


def test_himalayas_handles_missing_jobs():
    assert parse_himalayas({"jobs": []}) == []
    assert parse_himalayas('{"totalCount": 0}') == []


# --- HN "Who is hiring?" ------------------------------------------------
def test_hn_picks_most_recent_who_is_hiring_thread():
    raw = (FIXTURES / "hn_search.json").read_bytes()
    story = pick_latest_hiring_story(raw)
    assert story is not None
    assert "who is hiring" in story["title"].lower()
    # Newest in the fixture is May 2026.
    assert "May 2026" in story["title"]


def test_hn_pick_returns_none_when_no_match():
    assert pick_latest_hiring_story({"hits": []}) is None
    assert (
        pick_latest_hiring_story(
            {"hits": [{"author": "x", "title": "Who is hiring? (May 2026)"}]}
        )
        is None
    )


def test_hn_comments_filtered_to_relevant_and_normalized():
    raw = (FIXTURES / "hn_comments.json").read_bytes()
    listings = parse_comments(raw)
    assert listings, "expected some DevOps/cloud/SRE comments"
    assert all(x.source == "hn_hiring" for x in listings)
    assert all(
        str(x.url).startswith("https://news.ycombinator.com/item?id=")
        for x in listings
    )
    # Relevance filter must drop at least some of the 555 comments.
    import json as _json

    total = len(_json.loads(raw)["hits"])
    assert len(listings) < total
    # HTML tags stripped (a bare '<' can legitimately remain as text,
    # e.g. "latency < 5ms", so check for tag-shaped fragments instead).
    assert all(
        "</" not in x.description and "<a " not in x.description
        for x in listings
    )


def test_hn_company_title_parsing():
    payload = {
        "hits": [
            {
                "objectID": "999",
                "created_at": "2026-05-10T12:00:00Z",
                "comment_text": "BigCorp | Platform Engineer | Remote | "
                "kubernetes, terraform | apply@bigcorp.com",
            },
            {
                "objectID": "1000",
                "created_at": "2026-05-10T12:00:00Z",
                "comment_text": "We sell artisanal cheese, no tech here.",
            },
        ]
    }
    out = parse_comments(payload)
    assert len(out) == 1
    assert out[0].company == "BigCorp"
    assert out[0].title == "Platform Engineer"
    assert out[0].remote is True


# --- Tunisia: Keejob ----------------------------------------------------
def test_keejob_parses_fixture():
    listings = parse_keejob((FIXTURES / "keejob.html").read_bytes())
    assert listings, "expected at least one keejob job"
    j = listings[0]
    assert j.source == "keejob"
    assert j.external_id.isdigit()
    assert str(j.url).startswith("https://www.keejob.com/offres-emploi/")
    assert j.company != "Unknown"
    assert j.location  # defaults to Tunisie if unparsed
    assert "<" not in j.description


def test_keejob_dedupes_within_page():
    raw = (FIXTURES / "keejob.html").read_bytes()
    listings = parse_keejob(raw)
    ids = [x.external_id for x in listings]
    assert len(ids) == len(set(ids))


# --- Tunisia: Emploitunisie --------------------------------------------
def test_emploitunisie_parses_fixture():
    listings = parse_et((FIXTURES / "emploitunisie.html").read_bytes())
    assert listings
    j = listings[0]
    assert j.source == "emploitunisie"
    assert str(j.url).startswith("https://www.emploitunisie.com/offre-emploi")
    assert j.company and j.company != "Unknown"
    assert j.location
    assert "<" not in j.description


# --- block detection / manual-fallback ----------------------------------
def test_looks_blocked_detects_cloudflare_interstitial():
    assert looks_blocked("<html><title>Just a moment...</title>")
    assert looks_blocked("please Enable JavaScript and cookies to continue")
    assert not looks_blocked("<rss><item><title>real job</title></item></rss>")


def test_source_blocked_carries_manual_url():
    exc = SourceBlocked("tanitjobs", "https://x/jobs", "challenge")
    assert exc.manual_url == "https://x/jobs"
    assert "check manually" in str(exc)


@pytest.mark.asyncio
async def test_fetch_safe_marks_blocked_and_returns_empty(monkeypatch):
    src = Keejob()

    async def blocked():
        raise SourceBlocked(src.name, src.manual_url, "robots.txt challenged")

    monkeypatch.setattr(src, "fetch", blocked)
    assert await src.fetch_safe() == []
    assert src.blocked is True


# --- French date parsing -----------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("11 mai 2026", (2026, 5, 11)),
        ("Publié le 1 décembre 2025", (2025, 12, 1)),
        ("13.04.2026", (2026, 4, 13)),
        ("13/04/2026", (2026, 4, 13)),
    ],
)
def test_parse_fr_date(text, expected):
    d = parse_fr_date(text)
    assert (d.year, d.month, d.day) == expected


def test_parse_fr_date_none_on_garbage():
    assert parse_fr_date("") is None
    assert parse_fr_date("recently") is None


# --- fetch wiring (offline via monkeypatched parse path) ----------------
@pytest.mark.asyncio
async def test_fetch_safe_swallows_errors(monkeypatch):
    from hunter.sources.remoteok import RemoteOK

    src = RemoteOK()

    async def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(src, "fetch", boom)
    assert await src.fetch_safe() == []
