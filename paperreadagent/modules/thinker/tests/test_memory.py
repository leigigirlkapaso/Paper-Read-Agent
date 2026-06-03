"""tests for thinker MemoryPipeline"""

import pytest


class TestMemoryPipeline:
    def test_inject_empty_returns_empty(self):
        from paperreadagent.modules.thinker.memory import MemoryPipeline
        mp = MemoryPipeline.__new__(MemoryPipeline)
        result = mp.inject([])
        assert result == ""

    def test_inject_formats_memories(self):
        from paperreadagent.modules.thinker.memory import MemoryPipeline
        mp = MemoryPipeline.__new__(MemoryPipeline)
        ranked = [
            {"content": "用户讨论过 NLP", "type": "insight", "relevance": "high", "reason": "相关"},
            {"content": "用户承诺读论文", "type": "resolution", "relevance": "high", "reason": ""},
            {"content": "研究领域: NLP", "type": "profile", "relevance": "medium", "reason": ""},
        ]
        result = mp.inject(ranked)
        assert "相关记忆" in result
        assert "[相关]" in result
        assert "[未完成]" in result
        assert "[画像]" in result
        assert "NLP" in result

    def test_rerank_skip_when_few_candidates(self):
        """<=6 条候选时跳过 LLM 重排。"""
        from paperreadagent.modules.thinker.memory import MemoryPipeline
        mp = MemoryPipeline.__new__(MemoryPipeline)

        async def _run():
            candidates = [{"id": str(i), "content": f"记忆{i}"} for i in range(5)]
            result = await mp.rerank("hello", candidates)
            assert len(result) == 5
            for r in result:
                assert "relevance" in r

        import asyncio
        asyncio.run(_run())
