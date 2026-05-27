"""tests for SparkAuditor"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from modules.ideator.auditor import SparkAuditor, AuditResult


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.chat = AsyncMock()
    llm.model_for = MagicMock(return_value="qwen3.6-plus")
    return llm


@pytest.fixture
def auditor(mock_llm):
    return SparkAuditor(llm=mock_llm)


@pytest.mark.asyncio
async def test_audit_returns_supported(auditor, mock_llm):
    mock_llm.chat.return_value = (
        '{"verdict":"SUPPORTED","claims_check":[{"claim":"x","evidence_in_source":"y","supported":true}],"reasoning":"good"}'
    )
    result = await auditor.audit(spark_content="spark text",
                                  source_refs=[{"type":"paper","text":"source content"}])
    assert result.verdict == "SUPPORTED"


@pytest.mark.asyncio
async def test_audit_returns_unsupported(auditor, mock_llm):
    mock_llm.chat.return_value = (
        '{"verdict":"UNSUPPORTED","claims_check":[],"reasoning":"no evidence"}'
    )
    result = await auditor.audit(spark_content="spark text",
                                  source_refs=[{"type":"paper","text":"unrelated"}])
    assert result.verdict == "UNSUPPORTED"


def test_score_delta_values():
    assert SparkAuditor.score_delta("SUPPORTED") == 0.1
    assert SparkAuditor.score_delta("STRETCHED") == 0.0
    assert SparkAuditor.score_delta("UNSUPPORTED") == -0.3
    assert SparkAuditor.score_delta("unknown") == 0.0
