"""Tests for paperreadagent.utils.abstract_resolver."""
import json
from unittest.mock import MagicMock
import pytest

from utils.abstract_resolver import (
    resolve_abstract,
    _reconstruct_from_inverted,
)


class TestReconstructFromInverted:
    def test_reconstruct_basic(self):
        # OpenAlex inverted-index format: {word: [positions]}
        inv = {"hello": [0], "world": [1], "foo": [2, 4], "bar": [3]}
        result = _reconstruct_from_inverted(inv)
        assert result == "hello world foo bar foo"

    def test_reconstruct_empty(self):
        assert _reconstruct_from_inverted({}) == ""

    def test_reconstruct_single_word(self):
        assert _reconstruct_from_inverted({"alone": [0]}) == "alone"


class TestResolveAbstract:
    @pytest.mark.asyncio
    async def test_empty_doi_returns_empty(self):
        session = MagicMock()
        result = await resolve_abstract("", session=session)
        assert result == ""

    @pytest.mark.asyncio
    async def test_openalex_hit_short_circuits(self, monkeypatch):
        """Tier 1 (OpenAlex) hit: don't call S2 or CORE."""
        from utils import abstract_resolver as ar

        async def fake_fetch(session, url, **kwargs):
            if "openalex.org" in url:
                body = json.dumps({"abstract_inverted_index": {"hello": [0], "world": [1]}}).encode()
                return body, 200
            raise AssertionError(f"Should not call {url}")

        monkeypatch.setattr(ar, "limited_fetch", fake_fetch)
        result = await resolve_abstract("10.1145/foo", session=MagicMock(), core_api_key="key")
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_s2_fallback_when_openalex_missing(self, monkeypatch):
        """OpenAlex returns 200 but no inverted-index -> fall through to S2."""
        from utils import abstract_resolver as ar

        async def fake_fetch(session, url, **kwargs):
            if "openalex.org" in url:
                return json.dumps({"abstract_inverted_index": None}).encode(), 200
            if "semanticscholar.org" in url:
                return json.dumps({"abstract": "S2 abstract here"}).encode(), 200
            raise AssertionError(f"Unexpected URL: {url}")

        monkeypatch.setattr(ar, "limited_fetch", fake_fetch)
        result = await resolve_abstract("10.1145/foo", session=MagicMock())
        assert result == "S2 abstract here"

    @pytest.mark.asyncio
    async def test_core_fallback_with_key(self, monkeypatch):
        """OpenAlex empty + S2 empty + CORE has it."""
        from utils import abstract_resolver as ar

        async def fake_fetch(session, url, **kwargs):
            if "openalex.org" in url:
                return None, 0
            if "semanticscholar.org" in url:
                return json.dumps({"abstract": None}).encode(), 200
            if "core.ac.uk" in url:
                return json.dumps({"results": [{"abstract": "CORE abstract"}]}).encode(), 200
            raise AssertionError(f"Unexpected: {url}")

        monkeypatch.setattr(ar, "limited_fetch", fake_fetch)
        result = await resolve_abstract("10.1145/foo", session=MagicMock(),
                                        core_api_key="my-key")
        assert result == "CORE abstract"

    @pytest.mark.asyncio
    async def test_all_layers_fail_returns_empty(self, monkeypatch):
        from utils import abstract_resolver as ar

        async def fake_fetch(session, url, **kwargs):
            return None, 0

        monkeypatch.setattr(ar, "limited_fetch", fake_fetch)
        result = await resolve_abstract("10.1145/foo", session=MagicMock(),
                                        core_api_key="my-key")
        assert result == ""

    @pytest.mark.asyncio
    async def test_no_core_key_skips_core(self, monkeypatch):
        """When core_api_key is empty, CORE tier is skipped."""
        from utils import abstract_resolver as ar
        called_urls = []

        async def fake_fetch(session, url, **kwargs):
            called_urls.append(url)
            return None, 0

        monkeypatch.setattr(ar, "limited_fetch", fake_fetch)
        result = await resolve_abstract("10.1145/foo", session=MagicMock(),
                                        core_api_key="")
        assert result == ""
        assert not any("core.ac.uk" in u for u in called_urls)

    @pytest.mark.asyncio
    async def test_skip_sources_param(self, monkeypatch):
        """skip_sources={'openalex'} should skip OpenAlex tier."""
        from utils import abstract_resolver as ar
        called_urls = []

        async def fake_fetch(session, url, **kwargs):
            called_urls.append(url)
            if "semanticscholar.org" in url:
                return json.dumps({"abstract": "S2 only"}).encode(), 200
            return None, 0

        monkeypatch.setattr(ar, "limited_fetch", fake_fetch)
        result = await resolve_abstract("10.1145/foo", session=MagicMock(),
                                        skip_sources={"openalex"})
        assert result == "S2 only"
        assert not any("openalex.org" in u for u in called_urls)


class TestCoreKeyExpiry:
    @pytest.mark.asyncio
    async def test_expired_core_key_degrades_gracefully(self, monkeypatch):
        """CORE returns 401 (expired key) -> resolver returns '' without crashing.

        OpenAlex + S2 both miss; CORE auth-fails. Pipeline must keep running.
        """
        from utils import abstract_resolver as ar
        monkeypatch.setattr(ar, "_CORE_KEY_WARNED", False)

        async def fake_fetch(session, url, **kwargs):
            if "core.ac.uk" in url:
                return None, 401   # expired/invalid key -> auth failure
            return None, 0          # OpenAlex + S2 miss

        monkeypatch.setattr(ar, "limited_fetch", fake_fetch)
        # Must not raise; returns empty string (graceful degradation)
        result = await resolve_abstract("10.1145/foo", session=MagicMock(),
                                        core_api_key="expired-key")
        assert result == ""

    @pytest.mark.asyncio
    async def test_expired_core_key_warns_once(self, monkeypatch, caplog):
        """401/403 from CORE emits exactly one WARNING per process."""
        import logging
        from utils import abstract_resolver as ar
        monkeypatch.setattr(ar, "_CORE_KEY_WARNED", False)

        async def fake_fetch(session, url, **kwargs):
            if "core.ac.uk" in url:
                return None, 403
            return None, 0

        monkeypatch.setattr(ar, "limited_fetch", fake_fetch)

        with caplog.at_level(logging.WARNING, logger="utils.abstract_resolver"):
            # Resolve 3 papers — should warn only ONCE total
            for _ in range(3):
                await resolve_abstract("10.1145/x", session=MagicMock(),
                                       core_api_key="expired-key")

        expiry_warnings = [r for r in caplog.records
                           if "CORE API key" in r.message and "过期" in r.message]
        assert len(expiry_warnings) == 1, f"expected 1 warning, got {len(expiry_warnings)}"

    @pytest.mark.asyncio
    async def test_valid_core_key_no_warning(self, monkeypatch, caplog):
        """A working CORE key (200 with abstract) emits no expiry warning."""
        import logging
        from utils import abstract_resolver as ar
        monkeypatch.setattr(ar, "_CORE_KEY_WARNED", False)

        async def fake_fetch(session, url, **kwargs):
            if "core.ac.uk" in url:
                return json.dumps({"results": [{"abstract": "good"}]}).encode(), 200
            return None, 0

        monkeypatch.setattr(ar, "limited_fetch", fake_fetch)
        with caplog.at_level(logging.WARNING, logger="utils.abstract_resolver"):
            result = await resolve_abstract("10.1145/x", session=MagicMock(),
                                            core_api_key="valid-key")
        assert result == "good"
        assert not any("CORE API key" in r.message for r in caplog.records)
