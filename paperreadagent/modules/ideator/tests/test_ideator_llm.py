"""tests for IdeatorLLM.chat_stream pass-through to CoreLLM.chat_stream."""
import pytest
from unittest.mock import MagicMock

from paperreadagent.modules.ideator.ideator_llm import IdeatorLLM


class _FakeCoreLLM:
    """Minimal CoreLLM stub with a configurable chat_stream generator."""

    def __init__(self, deltas=None, raise_after=None):
        self._deltas = deltas or []
        self._raise_after = raise_after  # raise after yielding N deltas

    async def chat_stream(self, messages, *, module, purpose,
                          temperature=None, max_tokens=8192,
                          stream_timeout=300.0):
        for i, d in enumerate(self._deltas):
            if self._raise_after is not None and i >= self._raise_after:
                raise RuntimeError("simulated stream failure")
            yield d


@pytest.mark.asyncio
async def test_chat_stream_yields_deltas_in_order():
    core = _FakeCoreLLM(deltas=["a", "bc", "d"])
    llm = IdeatorLLM(core_llm=core)
    received = []
    async for delta in llm.chat_stream(
        model_role="reviewer_1",
        messages=[{"role": "user", "content": "hi"}],
    ):
        received.append(delta)
    assert received == ["a", "bc", "d"]


@pytest.mark.asyncio
async def test_chat_stream_no_retry_on_exception():
    """Mid-stream errors propagate immediately; no retry logic."""
    core = _FakeCoreLLM(deltas=["a", "b", "c"], raise_after=1)
    llm = IdeatorLLM(core_llm=core)
    received = []
    with pytest.raises(RuntimeError, match="simulated stream failure"):
        async for delta in llm.chat_stream(
            model_role="reviewer_1",
            messages=[{"role": "user", "content": "hi"}],
        ):
            received.append(delta)
    assert received == ["a"]  # got the first one, then exception
