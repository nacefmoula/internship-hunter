import sys
import types
from datetime import UTC, datetime

import pytest

from hunter import drafter
from hunter.drafter import (
    DrafterError,
    _listing_context,
    _slug,
    _system_blocks,
    draft_listing,
    extract_cv_text,
    resolve_cv_path,
    resolve_language,
)
from hunter.schema import Listing


def make_listing(**kw) -> Listing:
    data = dict(
        source="remoteok",
        external_id="42",
        title="DevOps Intern",
        company="Acme Cloud",
        location="Remote",
        remote=True,
        url="https://example.com/jobs/42",
        description="Kubernetes, Terraform, CI/CD on AWS.",
        posted_at=datetime(2026, 5, 1, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    data.update(kw)
    return Listing(**data)


def test_slug_sanitizes_and_falls_back():
    assert _slug("Acme Cloud, Inc.", "x") == "acme-cloud-inc"
    assert _slug("", "fallback") == "fallback"
    assert _slug("!!!", "fb") == "fb"


def test_system_blocks_cache_only_on_last_block():
    blocks = _system_blocks({"name": "Yosr"}, "CV TEXT HERE")
    assert len(blocks) == 3
    assert "cache_control" not in blocks[0]
    assert "cache_control" not in blocks[1]
    assert blocks[2]["cache_control"] == {"type": "ephemeral"}
    assert "CV TEXT HERE" in blocks[2]["text"]
    assert "Yosr" in blocks[1]["text"]


def test_listing_context_has_key_fields():
    ctx = _listing_context(make_listing())
    assert "Acme Cloud" in ctx
    assert "DevOps Intern" in ctx
    assert "Kubernetes" in ctx


def test_extract_cv_text_missing_file_raises():
    with pytest.raises(DrafterError, match="CV not found"):
        extract_cv_text("/nonexistent/cv.pdf")


def test_draft_listing_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("HUNTER_PROVIDER", raising=False)
    with pytest.raises(DrafterError, match="ANTHROPIC_API_KEY"):
        draft_listing(make_listing(), {"name": "Y"})


def test_groq_provider_used_when_selected(monkeypatch, tmp_path):
    monkeypatch.setenv("HUNTER_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(drafter, "extract_cv_text", lambda p: "CV CONTENT")

    posted: list[dict] = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "# DRAFT\nbody"}}]}

    def fake_post(url, **kw):
        posted.append({"url": url, **kw})
        return FakeResp()

    monkeypatch.setattr(drafter.httpx, "post", fake_post)

    res = draft_listing(
        make_listing(source="keejob", external_id="7"),
        {"name": "Nacef", "cv_path": {"en": "./en.pdf", "fr": "./fr.pdf"}},
        outputs_dir=tmp_path,
    )

    assert res.cover_letter_path.read_text().startswith("# DRAFT")
    assert res.cache_read_tokens == 0  # Groq has no caching
    assert len(posted) == 2
    for p in posted:
        assert p["url"] == drafter.GROQ_BASE_URL
        assert p["headers"]["Authorization"] == "Bearer gsk-test"
        body = p["json"]
        assert body["messages"][0]["role"] == "system"
        # French source -> French directive in the user turn
        assert "en français" in body["messages"][1]["content"]


def test_groq_missing_key_raises(monkeypatch):
    monkeypatch.setenv("HUNTER_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(DrafterError, match="GROQ_API_KEY"):
        draft_listing(make_listing(), {"name": "Y"})


def test_resolve_language_by_source_with_override():
    assert resolve_language(make_listing(source="remoteok")) == "en"
    assert resolve_language(make_listing(source="keejob")) == "fr"
    assert resolve_language(make_listing(source="emploitunisie")) == "fr"
    assert resolve_language(make_listing(source="tanitjobs")) == "fr"
    assert resolve_language(make_listing(source="unknown")) == "en"
    # explicit override wins
    assert resolve_language(make_listing(source="keejob"), "en") == "en"


def test_resolve_cv_path_dict_and_string_and_fallback():
    cfg = {"en": "./en.pdf", "fr": "./fr.pdf"}
    assert resolve_cv_path(cfg, "fr") == "./fr.pdf"
    assert resolve_cv_path(cfg, "en") == "./en.pdf"
    # missing language falls back to en
    assert resolve_cv_path({"en": "./en.pdf"}, "fr") == "./en.pdf"
    # plain string used for any language (back-compat)
    assert resolve_cv_path("./cv.pdf", "fr") == "./cv.pdf"


def _install_fake_anthropic(monkeypatch, capture):
    """Stub the anthropic module so no network/key is needed."""

    class FakeUsage:
        cache_read_input_tokens = 11
        cache_creation_input_tokens = 22

    class FakeBlock:
        type = "text"

        def __init__(self, text):
            self.text = text

    class FakeResp:
        def __init__(self, text):
            self.content = [FakeBlock(text)]
            self.usage = FakeUsage()

    class FakeMessages:
        def create(self, **kw):
            capture.append(kw)
            # Distinguish the two calls by task text.
            user = kw["messages"][0]["content"]
            kind = "COVER" if "cover letter" in user else "NOTES"
            return FakeResp(f"# {kind}\nbody")

    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = FakeClient
    mod.APIError = type("APIError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "anthropic", mod)


def test_draft_listing_writes_files_and_reports_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(drafter, "extract_cv_text", lambda p: "CV CONTENT")
    calls: list[dict] = []
    _install_fake_anthropic(monkeypatch, calls)

    res = draft_listing(
        make_listing(),
        {"name": "Yosr", "cv_path": "./cv.pdf"},
        outputs_dir=tmp_path,
    )

    assert res.cover_letter_path.exists()
    assert res.notes_path.exists()
    assert res.cover_letter_path.read_text().startswith("# COVER")
    assert res.notes_path.read_text().startswith("# NOTES")
    assert res.cache_read_tokens == 22  # 11 per call x 2
    assert res.cache_write_tokens == 44

    # Cached prefix invariant: every call sends the cache breakpoint on the
    # last system block, and the volatile listing goes in the user message.
    for kw in calls:
        assert kw["system"][-1]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in kw["system"][0]
        assert "Acme Cloud" in kw["messages"][0]["content"]
    assert len(calls) == 2

    folder = res.cover_letter_path.parent
    assert folder.name == "acme-cloud_devops-intern"


def test_french_source_uses_fr_cv_and_french_directive(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen_paths: list = []
    monkeypatch.setattr(
        drafter, "extract_cv_text", lambda p: seen_paths.append(p) or "CV CONTENT"
    )
    calls: list[dict] = []
    _install_fake_anthropic(monkeypatch, calls)

    draft_listing(
        make_listing(source="keejob", external_id="9"),
        {"name": "Nacef", "cv_path": {"en": "./en.pdf", "fr": "./fr.pdf"}},
        outputs_dir=tmp_path,
    )

    assert seen_paths == ["./fr.pdf"]
    for kw in calls:
        assert "en français" in kw["messages"][0]["content"]


def test_lang_override_forces_english_on_french_source(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen_paths: list = []
    monkeypatch.setattr(
        drafter, "extract_cv_text", lambda p: seen_paths.append(p) or "CV CONTENT"
    )
    calls: list[dict] = []
    _install_fake_anthropic(monkeypatch, calls)

    draft_listing(
        make_listing(source="keejob", external_id="9"),
        {"name": "Nacef", "cv_path": {"en": "./en.pdf", "fr": "./fr.pdf"}},
        lang="en",
        outputs_dir=tmp_path,
    )

    assert seen_paths == ["./en.pdf"]
    for kw in calls:
        assert "in English" in kw["messages"][0]["content"]
