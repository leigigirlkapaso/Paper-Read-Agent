"""
core/tests/test_llm.py
Tests for CoreLLM — configuration parsing, prompt loading, and error handling.
Does NOT make real API calls.
"""

import json
import tempfile
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def sample_llm_config():
    return {
        "api_key": "sk-test-key-12345",
        "api_base_url": "https://api.example.com/v1",
        "model_name": "gpt-4-test",
        "temperature": 0.5,
        "embedding_model": "test-embed-model",
        "embedding_provider": "local",
    }


@pytest.fixture
def minimal_llm_config():
    return {
        "api_key": "sk-minimal",
        "api_base_url": "https://api.minimal.com",
        "model_name": "gpt-mini",
    }


@pytest.fixture
def temp_prompt_dir():
    """Create a temporary paperreadagent/modules/<module>/prompts directory tree
    so load_prompt can resolve Path('paperreadagent/modules')/<module>/prompts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir) / "paperreadagent" / "modules"
        prompts_dir = base / "testmod" / "prompts"
        prompts_dir.mkdir(parents=True)
        yield tmpdir


# ── Mock helpers ──────────────────────────────────────────────────────


@contextmanager
def _mock_openai():
    """Context manager that patches openai.OpenAI and openai.AsyncOpenAI
    so CoreLLM construction does not make real network connections."""
    mock_sync = MagicMock()
    mock_async = MagicMock()
    with patch("openai.OpenAI", mock_sync), patch("openai.AsyncOpenAI", mock_async):
        yield mock_sync, mock_async


# ── Tests: from_config ────────────────────────────────────────────────


class TestCoreLLMFromConfig:
    """Test config parsing without touching the network."""

    def test_from_config_parses_all_fields(self, sample_llm_config):
        """All config fields are correctly extracted."""
        from core.llm import CoreLLM

        with _mock_openai() as (mock_sync, mock_async):
            llm = CoreLLM.from_config(sample_llm_config)

        assert llm.api_key == "sk-test-key-12345"
        assert llm.api_base_url == "https://api.example.com/v1"
        assert llm.model_name == "gpt-4-test"
        assert llm.temperature == 0.5
        assert llm.embedding_model == "test-embed-model"
        assert llm.embedding_provider == "local"

    def test_from_config_uses_defaults(self, minimal_llm_config):
        """Missing optional fields fall back to defaults."""
        from core.llm import CoreLLM

        with _mock_openai() as (mock_sync, mock_async):
            llm = CoreLLM.from_config(minimal_llm_config)

        assert llm.temperature == 0.3
        assert llm.embedding_model == "BAAI/bge-m3"
        assert llm.embedding_provider == "local"

    def test_from_config_passes_db_parameter(self, minimal_llm_config):
        """Optional db parameter is forwarded to constructor."""
        from core.llm import CoreLLM
        from core.database import CoreDatabase

        db = CoreDatabase(":memory:")
        db.initialize()

        with _mock_openai() as (mock_sync, mock_async):
            llm = CoreLLM.from_config(minimal_llm_config, db=db)

        assert llm._db is db

        db.close()

    def test_api_key_set_correctly(self, minimal_llm_config):
        """API key is stored on the instance."""
        from core.llm import CoreLLM

        with _mock_openai() as (mock_sync, mock_async):
            llm = CoreLLM.from_config(minimal_llm_config)

        assert llm.api_key == "sk-minimal"


# ── Tests: load_prompt ────────────────────────────────────────────────


class TestCoreLLMLoadPrompt:
    """Test Jinja2 prompt loading (no API calls)."""

    def test_load_prompt_renders_template_variables(self, temp_prompt_dir):
        """Jinja2 template variables are rendered correctly."""
        from core.llm import CoreLLM

        # Write a template file in the expected directory structure.
        # Note: temp_prompt_dir now contains paperreadagent/modules/testmod/prompts/
        prompts_dir = Path(temp_prompt_dir) / "paperreadagent" / "modules" / "testmod" / "prompts"
        template_path = prompts_dir / "greet.jinja2"
        # Avoid 'name' as template variable — it collides with load_prompt's 'name' param.
        template_path.write_text("Hello, {{ user_name }}!", encoding="utf-8")

        with _mock_openai() as (mock_sync, mock_async):
            llm = CoreLLM(
                api_key="sk-test",
                api_base_url="https://api.test.com",
                model_name="test",
            )

        original_cwd = os.getcwd()
        try:
            # Change to temp dir so Path("paperreadagent/modules") resolves.
            os.chdir(temp_prompt_dir)
            result = llm.load_prompt("testmod", "greet", user_name="World")
        finally:
            os.chdir(original_cwd)

        assert result == "Hello, World!"

    def test_load_prompt_fallback_treats_name_as_template(self):
        """When no file exists, 'name' is treated as a Jinja2 template string."""
        from core.llm import CoreLLM

        with _mock_openai() as (mock_sync, mock_async):
            llm = CoreLLM(
                api_key="sk-test",
                api_base_url="https://api.test.com",
                model_name="test",
            )

        # Use a simple inline template as the 'name' parameter.
        # This exercises the fallback branch (line 365-367 in llm.py).
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                # No paperreadagent/modules/... path exists → fallback
                result = llm.load_prompt("nonexistent", "Inline: {{ value }}", value=42)
            finally:
                os.chdir(original_cwd)

        assert "Inline: 42" in result

    def test_load_prompt_with_multiple_variables(self, temp_prompt_dir):
        """Template with multiple variables is rendered."""
        from core.llm import CoreLLM

        prompts_dir = Path(temp_prompt_dir) / "paperreadagent" / "modules" / "testmod" / "prompts"
        template_path = prompts_dir / "summary.jinja2"
        template_path.write_text(
            "Module: {{ module_name }}\nCount: {{ count }}\nFlag: {{ flag }}",
            encoding="utf-8",
        )

        with _mock_openai() as (mock_sync, mock_async):
            llm = CoreLLM(
                api_key="sk-test",
                api_base_url="https://api.test.com",
                model_name="test",
            )

        original_cwd = os.getcwd()
        try:
            os.chdir(temp_prompt_dir)
            result = llm.load_prompt(
                "testmod", "summary", module_name="thinker", count=10, flag=True,
            )
        finally:
            os.chdir(original_cwd)

        assert "Module: thinker" in result
        assert "Count: 10" in result
        assert "Flag: True" in result


# ── Tests: Error handling ─────────────────────────────────────────────


class TestCoreLLMErrorHandling:
    """Test behavior when config is invalid or incomplete."""

    def test_empty_api_key_does_not_crash_constructor(self):
        """Empty API key should not break construction (OpenAI client may be invalid)."""
        from core.llm import CoreLLM

        with _mock_openai() as (mock_sync, mock_async):
            llm = CoreLLM(
                api_key="",
                api_base_url="https://api.test.com",
                model_name="test",
            )

        assert llm.api_key == ""

    def test_extract_json_list_exists(self):
        """Smoke test: the static helper method callable without error."""
        from core.llm import CoreLLM

        # extract_json_list should handle empty/malformed input gracefully.
        result = CoreLLM.extract_json_list("[]")
        assert isinstance(result, list)

        result = CoreLLM.extract_json_list('["a", "b"]')
        assert isinstance(result, list)

    def test_chat_returns_empty_on_no_choices(self):
        """chat() returns empty string/dict when API returns no choices."""
        from core.llm import CoreLLM

        with _mock_openai() as (mock_sync, mock_async):
            llm = CoreLLM(
                api_key="sk-test",
                api_base_url="https://api.test.com",
                model_name="test",
            )

        # Mock the sync client response with no choices
        mock_resp = MagicMock()
        mock_resp.choices = []
        llm._sync_client.chat.completions.create.return_value = mock_resp

        content, usage = llm.chat("Hello", system_prompt="You are helpful.")
        assert content == ""
        assert usage == {}

    def test_chat_with_tools_empty_choices(self):
        """chat_with_tools returns error dict when API returns no choices."""
        from core.llm import CoreLLM

        with _mock_openai() as (mock_sync, mock_async):
            llm = CoreLLM(
                api_key="sk-test",
                api_base_url="https://api.test.com",
                model_name="test",
            )

        mock_resp = MagicMock()
        mock_resp.choices = []
        llm._sync_client.chat.completions.create.return_value = mock_resp

        result = llm.chat_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
        )
        assert result["content"] == ""
        assert result["finish_reason"] == "error"


# ── embed_batch ──────────────────────────────────────────────

class TestEmbedBatch:
    def _llm(self, sample_llm_config):
        from core.llm import CoreLLM
        return CoreLLM.from_config(sample_llm_config)

    @pytest.mark.asyncio
    async def test_returns_list_per_text(self, sample_llm_config):
        from unittest.mock import AsyncMock
        llm = self._llm(sample_llm_config)
        llm.embed = AsyncMock(side_effect=[[0.1] * 1024, [0.2] * 1024, [0.3] * 1024])
        out = await llm.embed_batch(["a", "b", "c"], concurrency=2)
        assert len(out) == 3
        assert out[0][0] == 0.1 and out[2][0] == 0.3

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self, sample_llm_config):
        out = await self._llm(sample_llm_config).embed_batch([])
        assert out == []

    @pytest.mark.asyncio
    async def test_partial_failure_returns_empty_for_that_slot(self, sample_llm_config):
        from unittest.mock import AsyncMock
        llm = self._llm(sample_llm_config)
        async def side(t, **kw):
            if t == "boom":
                raise RuntimeError("local model down")
            return [0.5] * 1024
        llm.embed = AsyncMock(side_effect=side)
        out = await llm.embed_batch(["ok", "boom", "ok2"])
        assert len(out) == 3
        assert out[0] and out[2]
        assert out[1] == []  # failure slot

    @pytest.mark.asyncio
    async def test_concurrency_limit_respected(self, sample_llm_config):
        import asyncio
        from unittest.mock import AsyncMock
        llm = self._llm(sample_llm_config)
        in_flight = {"n": 0, "peak": 0}
        async def slow(t, **kw):
            in_flight["n"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["n"])
            await asyncio.sleep(0.01)
            in_flight["n"] -= 1
            return [1.0]
        llm.embed = AsyncMock(side_effect=slow)
        await llm.embed_batch(["x"] * 10, concurrency=3)
        assert in_flight["peak"] == 3

    @pytest.mark.asyncio
    async def test_concurrency_zero_does_not_deadlock(self, sample_llm_config):
        import asyncio
        from unittest.mock import AsyncMock
        llm = self._llm(sample_llm_config)
        llm.embed = AsyncMock(return_value=[0.7])
        # concurrency=0 must be guarded to >=1 internally; else asyncio.Semaphore(0) deadlocks.
        out = await asyncio.wait_for(llm.embed_batch(["a", "b"], concurrency=0), timeout=1.0)
        assert out == [[0.7], [0.7]]
