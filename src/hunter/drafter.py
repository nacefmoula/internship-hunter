"""Cover-letter + application-notes drafting.

Two providers are supported, selected by ``HUNTER_PROVIDER``:

* ``anthropic`` (default when ``ANTHROPIC_API_KEY`` is set) — each draft makes
  two calls sharing a cached ``system`` prefix (instructions + profile + CV;
  ``cache_control: ephemeral`` on the last block), so the second call and any
  re-draft within the 5-minute TTL read the CV/profile from cache.
* ``groq`` (default when only ``GROQ_API_KEY`` is set) — free, via Groq's
  OpenAI-compatible endpoint over httpx. No prompt caching (and the free tier
  isn't token-billed anyway).

Model defaults per provider (``claude-sonnet-4-6`` / ``llama-3.3-70b-versatile``);
override with ``HUNTER_MODEL`` or ``--model``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from pypdf import PdfReader

from .schema import Listing
from .sources import SOURCE_LANG

DEFAULT_MODEL = "claude-sonnet-4-6"
GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"
GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
OUTPUTS_DIR = Path(__file__).resolve().parents[2] / "outputs"


def _resolve_provider() -> str:
    """anthropic | groq. Explicit env wins; else infer from which key is set."""
    explicit = (os.environ.get("HUNTER_PROVIDER") or "").strip().lower()
    if explicit in ("anthropic", "groq"):
        return explicit
    if os.environ.get("GROQ_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        return "groq"
    return "anthropic"

#: Appended to each task so the model writes in the listing's language.
_LANG_DIRECTIVE = {
    "en": "Write everything in English.",
    "fr": "Rédige toute la réponse en français.",
}


def resolve_language(listing: Listing, override: str | None = None) -> str:
    """Pick the draft language: explicit override, else the source's, else en."""
    if override:
        return override
    return SOURCE_LANG.get(listing.source, "en")


def resolve_cv_path(cv_path_cfg, lang: str):
    """Resolve the CV path for ``lang``.

    Accepts a plain string (used for every language) or a
    ``{"en": ..., "fr": ...}`` mapping. Falls back to the other language's
    CV if the requested one is missing.
    """
    if isinstance(cv_path_cfg, dict):
        return (
            cv_path_cfg.get(lang)
            or cv_path_cfg.get("en")
            or next(iter(cv_path_cfg.values()), None)
        )
    return cv_path_cfg


class DrafterError(RuntimeError):
    """Raised for user-actionable drafting failures (missing key/CV/etc.)."""


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return s[:60] or fallback


def extract_cv_text(cv_path: Path | str) -> str:
    """Extract plain text from the CV PDF."""
    path = Path(cv_path)
    if not path.exists():
        raise DrafterError(
            f"CV not found at {path}. Set cv_path in profile.yaml and drop your CV there."
        )
    try:
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise DrafterError(f"could not read CV PDF ({path}): {exc}") from exc
    text = text.strip()
    if not text:
        raise DrafterError(
            f"CV at {path} produced no extractable text (scanned image PDF?)."
        )
    return text


@dataclass
class DraftResult:
    cover_letter_path: Path
    notes_path: Path
    cover_letter: str
    notes: str
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


_SYSTEM_INSTRUCTIONS = """\
You help a 4th-year Cloud & DevOps engineering student at Esprit (Tunisia) \
apply to a specific internship/stage. Rules:

1. GROUND TRUTH: use ONLY the candidate's real CV and profile below. Never \
invent experience, employers, tools, or credentials.
2. ANALYZE THE JOB FIRST: from the job description, extract the explicit \
required tools/skills/responsibilities. Then map the candidate's REAL, \
overlapping tools and projects to them — name the tool and the concrete \
project or experience where it was used (e.g. "GitLab CI in <project>"). \
Do not claim skills the job lists that the candidate lacks.
3. RELEVANCE: lead with the evidence most relevant to THIS role. For a \
DevOps/Cloud/SRE role, foreground infrastructure/CI-CD/Kubernetes work; \
mention unrelated full-stack/dev work only briefly if at all.
4. NO FILLER: ban generic clichés ("I am confident my skills will be an \
asset", "I am passionate about technology", "team player"). Every sentence \
must carry specific, checkable information.
5. POSTING TYPE: if the description is a staffing-agency / talent-pool / \
"collecting CVs to pass to clients" ad rather than a direct hire, adjust \
the framing accordingly (interest in being put forward) instead of \
addressing "your team / your projects" as if it were the employer.
6. TONE: confident but humble student. Output GitHub-flavored Markdown \
only, no preamble or surrounding commentary."""


def _system_blocks(profile: dict, cv_text: str) -> list[dict]:
    """Stable, cacheable prefix: instructions + profile + CV.

    cache_control on the LAST block caches the whole system prefix.
    """
    profile_yaml = yaml.safe_dump(profile, sort_keys=True, allow_unicode=True)
    return [
        {"type": "text", "text": _SYSTEM_INSTRUCTIONS},
        {"type": "text", "text": f"# Candidate profile (profile.yaml)\n{profile_yaml}"},
        {
            "type": "text",
            "text": f"# Candidate CV (extracted text)\n{cv_text}",
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _listing_context(listing: Listing) -> str:
    return (
        f"Company: {listing.company}\n"
        f"Role: {listing.title}\n"
        f"Location: {listing.location or 'n/a'} "
        f"(remote: {listing.remote})\n"
        f"Source: {listing.source}\nURL: {listing.url}\n\n"
        f"Job description:\n{listing.description}"
    )


_COVER_LETTER_TASK = """\
Write a tailored one-page cover letter (~250-350 words) in Markdown for the \
internship below.

- Opening: name the exact role and company, with a specific hook tied to \
what THIS job actually does (not a generic intro).
- Body: pick 2-3 requirements/tools the job explicitly asks for and, for \
each, map it to a specific tool+project/experience from the candidate's \
real CV. Name the tools. Prioritise DevOps/Cloud evidence for DevOps/Cloud \
roles; do not lean on unrelated full-stack work.
- Do not restate the CV generically and do not use filler clichés.
- Close: one concise, concrete call to action.
- Use the real name and email from the profile — no placeholders like \
[Your Name]. Sign with the name from the profile.

Job follows:

"""

_NOTES_TASK = """\
Produce an `application_notes.md` in Markdown with exactly these sections:

## 3 talking points
Three bullets, each naming a specific tool/project from the CV and the \
job requirement it addresses. No vague "I have skills in X" phrasing.

## Likely interview questions
Five questions an interviewer for THIS specific role would ask (derived \
from the job's stated requirements). For each, give a one-line answer \
angle that references a concrete tool or project from the candidate's CV \
— not "by highlighting my experience".

## Research the company
Concrete, specific things to look up about this company/team before \
applying (and, if it is a staffing/talent-pool ad, say so and note what \
that means for the application).

Job follows:

"""


def _user_content(task: str, listing: Listing, lang: str) -> str:
    directive = _LANG_DIRECTIVE.get(lang, _LANG_DIRECTIVE["en"])
    return f"{directive}\n\n{task}{_listing_context(listing)}"


def _system_text(profile: dict, cv_text: str) -> str:
    """Flatten the cacheable system blocks into one plain string (Groq)."""
    return "\n\n".join(b["text"] for b in _system_blocks(profile, cv_text))


def _generate_anthropic(
    client, model: str, system: list[dict], task: str, listing: Listing, lang: str
):
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": _user_content(task, listing, lang)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    usage = resp.usage
    return (
        text,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
        getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def _generate_groq(
    model: str, system_text: str, task: str, listing: Listing, lang: str
):
    """One chat completion via Groq's OpenAI-compatible endpoint.

    Groq has no prompt caching, so cache token counts are always 0 (and the
    free tier isn't billed by token anyway).
    """
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise DrafterError(
            "GROQ_API_KEY is not set. Get a free key at https://console.groq.com/keys "
            "then: export GROQ_API_KEY=..."
        )
    try:
        resp = httpx.post(
            GROQ_BASE_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "max_tokens": 2000,
                "messages": [
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": _user_content(task, listing, lang)},
                ],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        hint = {
            401: "bad/expired GROQ_API_KEY",
            429: "Groq rate limit hit — wait a minute and retry",
        }.get(code, exc.response.text[:200])
        raise DrafterError(f"Groq API error (HTTP {code}): {hint}") from exc
    except httpx.HTTPError as exc:
        raise DrafterError(f"Groq request failed: {exc}") from exc
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return text, 0, 0


def draft_listing(
    listing: Listing,
    profile: dict,
    *,
    cv_path: Path | str | None = None,
    model: str | None = None,
    lang: str | None = None,
    outputs_dir: Path = OUTPUTS_DIR,
) -> DraftResult:
    """Generate and save the cover letter + application notes for a listing.

    Provider is chosen by ``HUNTER_PROVIDER`` (anthropic | groq), else
    inferred from whichever API key is set. Language (and therefore which CV)
    defaults to the listing's source — French for keejob/emploitunisie/
    tanitjobs, English otherwise. Pass ``lang`` ("en"/"fr") to override.
    """
    provider = _resolve_provider()
    draft_lang = resolve_language(listing, lang)
    cv_path = cv_path or resolve_cv_path(
        profile.get("cv_path", "./cv.pdf"), draft_lang
    )

    # Validate provider config before touching the CV/network so a missing
    # key fails fast with a clear, actionable message.
    anthropic = None
    if provider == "groq":
        if not os.environ.get("GROQ_API_KEY"):
            raise DrafterError(
                "GROQ_API_KEY is not set. Get a free key at "
                "https://console.groq.com/keys then: export GROQ_API_KEY=..."
            )
        model = model or os.environ.get("HUNTER_MODEL") or GROQ_DEFAULT_MODEL
    else:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise DrafterError(
                "anthropic SDK not installed (pip install -e .)"
            ) from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise DrafterError(
                "ANTHROPIC_API_KEY is not set. Set it, or use Groq for free: "
                "export GROQ_API_KEY=... (get one at https://console.groq.com/keys)."
            )
        model = model or os.environ.get("HUNTER_MODEL") or DEFAULT_MODEL

    cv_text = extract_cv_text(cv_path)

    if provider == "groq":
        system_text = _system_text(profile, cv_text)
        cover, r1, w1 = _generate_groq(
            model, system_text, _COVER_LETTER_TASK, listing, draft_lang
        )
        notes, r2, w2 = _generate_groq(
            model, system_text, _NOTES_TASK, listing, draft_lang
        )
    else:
        system = _system_blocks(profile, cv_text)
        client = anthropic.Anthropic()
        try:
            cover, r1, w1 = _generate_anthropic(
                client, model, system, _COVER_LETTER_TASK, listing, draft_lang
            )
            notes, r2, w2 = _generate_anthropic(
                client, model, system, _NOTES_TASK, listing, draft_lang
            )
        except anthropic.APIError as exc:
            raise DrafterError(f"Anthropic API error: {exc}") from exc

    folder_name = (
        f"{_slug(listing.company, listing.source)}_{_slug(listing.title, 'role')}"
    )
    folder = outputs_dir / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    cover_path = folder / "cover_letter.md"
    notes_path = folder / "application_notes.md"
    cover_path.write_text(cover + "\n", encoding="utf-8")
    notes_path.write_text(notes + "\n", encoding="utf-8")

    return DraftResult(
        cover_letter_path=cover_path,
        notes_path=notes_path,
        cover_letter=cover,
        notes=notes,
        cache_read_tokens=r1 + r2,
        cache_write_tokens=w1 + w2,
    )
