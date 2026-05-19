"""Scoring and hard filters.

Scoring (per the project spec):

* ``+3`` for each ``keywords_strong`` term found in the **title**
* ``+1`` for each ``keywords_strong`` term found in the **description**
* ``+1`` for each ``keywords_nice`` term found anywhere

Hard reject (listing is dropped, not just low-scored):

* an ``exclude_terms`` term appears in the title **and** no
  ``internship_terms`` term is present anywhere, or
* the role is on-site abroad (not remote, not Tunisia) when
  ``onsite_abroad`` is false, or
* the posting is older than ``max_age_days``.

Matching is case-insensitive substring matching, so multi-word keywords
like ``"ci/cd"`` or ``"github actions"`` work as written in profile.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml

from .schema import Listing
from .sources import SOURCE_LANG

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / "profile.yaml"

_TUNISIA_HINTS = ("tunis", "tunisia", "tunisie", "tunisien")

#: Internship terms that are only meaningful in French postings. The bare
#: English word "stage" ("early-stage startup", "clinical-stage") is noise,
#: so these count only on French sources (keejob/emploitunisie/tanitjobs).
_FR_ONLY_INTERNSHIP_TERMS = {"stage", "stagiaire"}


def load_profile(path: Path | str = DEFAULT_PROFILE_PATH) -> dict:
    """Load profile.yaml into a dict."""
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass
class ScoreResult:
    fit_score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    rejected: bool = False
    reject_reason: str | None = None


@lru_cache(maxsize=512)
def _term_re(term: str) -> re.Pattern[str]:
    """Word-boundary, case-insensitive matcher for a phrase term.

    Used for internship_terms/exclude_terms so 'intern' does not match
    'internal'/'international' and 'lead' does not match 'leading'.
    """
    return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)


def _has_term(text: str, terms: list[str]) -> str | None:
    """Return the first term that appears as a whole word/phrase, or None."""
    for term in terms:
        if _term_re(term).search(text):
            return term
    return None


def _hard_filters(profile: dict) -> dict:
    return profile.get("hard_filters", {}) or {}


def _location_rules(profile: dict) -> dict:
    rules: dict = {}
    for entry in _hard_filters(profile).get("location_rules", []) or []:
        if isinstance(entry, dict):
            rules.update(entry)
    return rules


def _is_tunisia(location: str | None) -> bool:
    if not location:
        return False
    loc = location.lower()
    return any(h in loc for h in _TUNISIA_HINTS)


def _age_days(posted_at: datetime | None, now: datetime) -> float | None:
    if posted_at is None:
        return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    return (now - posted_at).total_seconds() / 86400.0


def score_listing(
    listing: Listing,
    profile: dict,
    *,
    now: datetime | None = None,
) -> ScoreResult:
    """Score one listing and decide whether it is hard-rejected."""
    now = now or datetime.now(UTC)
    title = listing.title.lower()
    desc = listing.description.lower()
    hard = _hard_filters(profile)

    strong = [k.lower() for k in profile.get("keywords_strong", [])]
    nice = [k.lower() for k in profile.get("keywords_nice", [])]
    internship_terms = [str(t) for t in hard.get("internship_terms", [])]
    exclude_terms = [str(t) for t in hard.get("exclude_terms", [])]

    # French-only terms ("stage"/"stagiaire") count solely on French sources;
    # on English boards the bare word "stage" is noise.
    if SOURCE_LANG.get(listing.source, "en") != "fr":
        internship_terms = [
            t for t in internship_terms
            if t.lower() not in _FR_ONLY_INTERNSHIP_TERMS
        ]

    result = ScoreResult()
    matched: list[str] = []
    score = 0.0

    for kw in strong:
        if kw in title:
            score += 3
            matched.append(kw)
        if kw in desc:
            score += 1
            if kw not in matched:
                matched.append(kw)
    for kw in nice:
        if kw in title or kw in desc:
            score += 1
            matched.append(kw)

    result.fit_score = score
    result.matched_keywords = matched

    # --- hard rejects ---------------------------------------------------
    has_internship_term = _has_term(
        f"{listing.title}\n{listing.description}", internship_terms
    )
    excluded_in_title = _has_term(listing.title, exclude_terms)
    if excluded_in_title and not has_internship_term:
        result.rejected = True
        result.reject_reason = (
            f"senior-level ('{excluded_in_title}' in title, no internship term)"
        )
        return result

    # Internship-only: drop anything that isn't explicitly an internship/stage.
    # Defaults on (the whole project targets internships); disable by setting
    # hard_filters.require_internship: false in profile.yaml.
    require_internship = hard.get("require_internship", True)
    if require_internship and not has_internship_term:
        result.rejected = True
        result.reject_reason = "not an internship/stage (no internship term)"
        return result

    loc_rules = _location_rules(profile)
    onsite_abroad_ok = loc_rules.get("onsite_abroad", False)
    tunisia_ok = loc_rules.get("tunisia_ok", True)
    remote_ok = loc_rules.get("remote_ok", True)
    if not onsite_abroad_ok:
        is_remote = listing.remote and remote_ok
        is_tunisia = _is_tunisia(listing.location) and tunisia_ok
        if not is_remote and not is_tunisia:
            result.rejected = True
            result.reject_reason = "on-site abroad (not remote, not Tunisia)"
            return result

    max_age = hard.get("max_age_days")
    if max_age is not None:
        age = _age_days(listing.posted_at, now)
        if age is not None and age > max_age:
            result.rejected = True
            result.reject_reason = f"stale ({age:.0f}d > {max_age}d)"
            return result

    return result


def rank_listings(
    listings: list[Listing],
    profile: dict,
    *,
    now: datetime | None = None,
) -> tuple[list[tuple[Listing, ScoreResult]], list[tuple[Listing, ScoreResult]]]:
    """Score every listing.

    Returns ``(kept, rejected)`` where ``kept`` is sorted by fit_score
    descending (then most-recent first).
    """
    kept: list[tuple[Listing, ScoreResult]] = []
    rejected: list[tuple[Listing, ScoreResult]] = []
    for listing in listings:
        res = score_listing(listing, profile, now=now)
        (rejected if res.rejected else kept).append((listing, res))

    def sort_key(pair: tuple[Listing, ScoreResult]):
        listing, res = pair
        posted = listing.posted_at or datetime.min.replace(tzinfo=UTC)
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=UTC)
        return (-res.fit_score, -posted.timestamp())

    kept.sort(key=sort_key)
    return kept, rejected
