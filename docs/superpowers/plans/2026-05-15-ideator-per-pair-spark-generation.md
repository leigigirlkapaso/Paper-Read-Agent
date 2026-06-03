# S2 逐组火花生成实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 S2 火花生成从批量跨对合成改为贪心共享源分组 + 逐组并行生成，每个火花精确绑定来源。

**Architecture:** C1 贪心分组策略：统计每个 source 的出现次数，按频次降序建组（每组 ≤5 对），孤对自成一组。每组独立 LLM 调用产出 0-4 个火花，并行限流 Semaphore(5)。

**Tech Stack:** Python asyncio, Jinja2 prompt templates, pytest + unittest.mock

---

### Task 1: 分组逻辑 — 测试先行

**Files:**
- Modify: `paperreadagent/modules/ideator/tests/test_pipeline.py` (追加)
- Modify: `paperreadagent/modules/ideator/pipeline.py` (新增 `_group_by_shared_source` 方法)

- [ ] **Step 1: 写分组测试**

在 `test_pipeline.py` 文件末尾追加：

```python
class TestGroupBySharedSource:
    """Tests for _group_by_shared_source — C1 greedy grouping."""

    @staticmethod
    def _make_link(a_type, a_id, a_content, b_type, b_id, b_content, score=0.5):
        return {
            "source_a": {"type": a_type, "id": a_id, "content": a_content},
            "source_b": {"type": b_type, "id": b_id, "content": b_content},
            "recall_path": "similarity",
            "relevance_score": score,
            "reasoning": "test reason",
        }

    def test_happy_path_shared_source_groups(self):
        """A×B, A×C, B×E → two groups: {A×B,A×C} around A, {B×E} around B."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        links = [
            self._make_link("core_note", 1, "A", "core_note", 2, "B"),
            self._make_link("core_note", 1, "A", "core_note", 3, "C"),
            self._make_link("core_note", 2, "B", "paper", 5, "E"),
        ]
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source(links)
        assert len(groups) == 2
        # First group should have 2 pairs (A is most frequent)
        assert len(groups[0]) == 2
        # Second group should have 1 pair
        assert len(groups[1]) == 1

    def test_single_pair(self):
        """One link → one group with one element."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        links = [self._make_link("core_note", 1, "A", "core_note", 2, "B")]
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source(links)
        assert len(groups) == 1
        assert len(groups[0]) == 1

    def test_empty_links(self):
        """Empty input → empty list."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source([])
        assert groups == []

    def test_orphan_pairs_each_own_group(self):
        """A×B, C×D (no shared source) → two groups of 1."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        links = [
            self._make_link("core_note", 1, "A", "core_note", 2, "B"),
            self._make_link("core_note", 3, "C", "core_note", 4, "D"),
        ]
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source(links)
        assert len(groups) == 2
        assert all(len(g) == 1 for g in groups)

    def test_max_per_group_truncation(self):
        """6 pairs sharing source A → group capped at 5, 6th becomes orphan."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        links = [
            self._make_link("core_note", 1, "A", "core_note", i, f"B{i}")
            for i in range(2, 8)  # A×B2 through A×B7 = 6 pairs
        ]
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source(links, max_per_group=5)
        assert len(groups) == 2
        assert len(groups[0]) == 5  # capped
        assert len(groups[1]) == 1  # overflow orphan

    def test_source_count_with_mixed_types(self):
        """Sources with same id but different type are NOT merged."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        links = [
            self._make_link("paper", 1, "A", "core_note", 1, "B"),
            self._make_link("core_note", 1, "A2", "core_note", 2, "C"),
        ]
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source(links)
        # paper:1 is different from core_note:1 → no shared source → 2 orphan groups
        assert len(groups) == 2
        assert all(len(g) == 1 for g in groups)
```

- [ ] **Step 2: 运行测试验证失败**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_pipeline.py::TestGroupBySharedSource -v
```

预期：全部 6 个测试 FAIL，`AttributeError: 'IdeatorPipeline' object has no attribute '_group_by_shared_source'`

- [ ] **Step 3: 实现 `_group_by_shared_source`**

在 `pipeline.py` 的 `IdeatorPipeline` 类中，`_generate_sparks` 方法之前添加：

```python
    def _group_by_shared_source(self, links: list[dict], max_per_group: int = 5) -> list[list[dict]]:
        """Greedy C1 grouping: cluster links by shared source, max N per group.

        Links sharing the same source (by type+id) are grouped together.
        Sources are prioritized by occurrence frequency (most frequent first).
        Orphan links (no shared source) each form their own group.
        """
        if not links:
            return []

        # 1. Count source occurrences
        source_count: dict[str, int] = {}
        for l in links:
            for src in (l["source_a"], l["source_b"]):
                key = f"{src['type']}:{src['id']}"
                source_count[key] = source_count.get(key, 0) + 1

        # 2. Sort sources by frequency descending
        sorted_sources = sorted(source_count.keys(), key=lambda k: source_count[k], reverse=True)

        # 3. Greedy grouping
        assigned: set[int] = set()
        groups: list[list[dict]] = []

        for src_key in sorted_sources:
            group: list[dict] = []
            for i, l in enumerate(links):
                if i in assigned:
                    continue
                if len(group) >= max_per_group:
                    break
                a_key = f"{l['source_a']['type']}:{l['source_a']['id']}"
                b_key = f"{l['source_b']['type']}:{l['source_b']['id']}"
                if a_key == src_key or b_key == src_key:
                    group.append(l)
                    assigned.add(i)
            if group:
                groups.append(group)

        # 4. Unassigned links each become their own group
        for i, l in enumerate(links):
            if i not in assigned:
                groups.append([l])

        return groups
```

- [ ] **Step 4: 运行测试验证通过**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_pipeline.py::TestGroupBySharedSource -v
```

预期：全部 6 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add paperreadagent/modules/ideator/pipeline.py paperreadagent/modules/ideator/tests/test_pipeline.py
git commit -m "feat(ideator): add _group_by_shared_source C1 greedy grouping for S2 spark generation"
```

---

### Task 2: Prompt 模板改写

**Files:**
- Modify: `paperreadagent/modules/ideator/prompts/spark_generate.jinja2`

- [ ] **Step 1: 改写真身**

将 `prompts/spark_generate.jinja2` 替换为：

```jinja2
你是一个研究火花生成器。从给定的信息关联中发现值得探索的研究方向。

## 关联信息
{% for l in links %}
关联 {{ loop.index }}：
  来源 A：{{ l.a }}
  来源 B：{{ l.b }}
  关联类型：{{ l.link_type }}
  关联质量：{{ l.quality_score }}
  关联理由：{{ l.reason }}
{% endfor %}

## 要求
{% if links|length == 1 %}
1. 深度审视这一对关联，如果确实有研究价值，生成 1-2 个火花（一句话假说/研究方向）
2. 如果关联太弱、信息不足、或没有研究价值，返回空数组 []
3. 火花必须具体，明确指出可以研究什么、怎么研究
{% else %}
1. 在以上关联中发现值得研究的模式或方向，生成 1-4 个火花（一句话假说/研究方向）
2. 火花可以从多个关联中综合得出，但来源必须限定在以上列出的关联中
3. 不要强行关联——如果没有足够有价值的发现，返回空数组 []
{% endif %}
4. 火花之间尽量不要重复
5. 返回纯 JSON 数组（不要 markdown 代码块）：
   [{"content": "火花正文", "quality_score": 0.0-1.0}]
```

- [ ] **Step 2: 提交**

```bash
git add paperreadagent/modules/ideator/prompts/spark_generate.jinja2
git commit -m "feat(ideator): rewrite spark_generate prompt for per-group generation"
```

---

### Task 3: `_generate_sparks` 重写

**Files:**
- Modify: `paperreadagent/modules/ideator/pipeline.py:331-369`
- Modify: `paperreadagent/modules/ideator/tests/test_pipeline.py` (追加)

- [ ] **Step 1: 写生成测试**

在 `test_pipeline.py` 文件末尾追加：

```python
class TestGenerateSparksPerGroup:
    """Tests for the rewritten _generate_sparks with per-group generation."""

    @staticmethod
    def _make_link(a_type, a_id, a_content, b_type, b_id, b_content, score=0.5):
        return {
            "source_a": {"type": a_type, "id": a_id, "content": a_content},
            "source_b": {"type": b_type, "id": b_id, "content": b_content},
            "recall_path": "similarity",
            "relevance_score": score,
            "reasoning": "test reason",
        }

    @pytest.mark.asyncio
    async def test_generates_sparks_from_groups(self):
        """Two groups → parallel LLM calls → sparks from both groups."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        import json

        links = [
            self._make_link("core_note", 1, "Note A about transformers", "core_note", 2, "Note B about RLHF"),
            self._make_link("core_note", 1, "Note A about transformers", "core_note", 3, "Note C about attention"),
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="generate sparks from these links")
        core.llm.achat = AsyncMock()
        core.llm.achat.return_value = (
            json.dumps([
                {"content": "Spark 1: investigate RLHF+transformers", "quality_score": 0.8},
                {"content": "Spark 2: attention mechanism comparison", "quality_score": 0.7},
            ]),
            MagicMock(),  # usage
        )

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links)
        assert len(sparks) >= 1
        # Each spark must have source_refs injected
        for s in sparks:
            assert "source_refs" in s
            assert isinstance(s["source_refs"], list)
            assert len(s["source_refs"]) > 0
            assert "source_type" in s

    @pytest.mark.asyncio
    async def test_weak_pair_returns_empty(self):
        """LLM returns [] for a weak pair → that group contributes nothing."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        import json

        links = [
            self._make_link("core_note", 1, "Weak A", "core_note", 2, "Weak B", score=0.1),
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="prompt")
        core.llm.achat = AsyncMock()
        core.llm.achat.return_value = ("[]", MagicMock())

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links)
        assert sparks == []

    @pytest.mark.asyncio
    async def test_parallel_failure_isolated(self):
        """One group's LLM call fails → other groups still produce sparks."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        import json

        links = [
            self._make_link("core_note", 1, "Good A", "core_note", 2, "Good B", score=0.8),
            self._make_link("core_note", 3, "Bad C", "core_note", 4, "Bad D", score=0.3),
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="prompt")

        # First call succeeds, second call fails
        call_count = [0]

        async def mock_achat(user_prompt, module, purpose):
            call_count[0] += 1
            if call_count[0] == 1:
                return (json.dumps([{"content": "Good spark", "quality_score": 0.8}]), MagicMock())
            else:
                raise Exception("LLM timeout")

        core.llm.achat = mock_achat

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links)
        # Should still have the successful spark despite one group failing
        assert len(sparks) >= 1

    @pytest.mark.asyncio
    async def test_source_refs_extracted_from_entire_group(self):
        """All unique sources in a group appear in each spark's source_refs."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        import json

        links = [
            self._make_link("core_note", 1, "A", "core_note", 2, "B"),
            self._make_link("core_note", 1, "A", "paper", 10, "D"),
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="prompt")
        core.llm.achat = AsyncMock()
        core.llm.achat.return_value = (
            json.dumps([{"content": "Cross-source spark", "quality_score": 0.9}]),
            MagicMock(),
        )

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links)
        assert len(sparks) == 1
        refs = sparks[0]["source_refs"]
        # Should have 3 unique sources: core_note:1, core_note:2, paper:10
        assert len(refs) == 3

    @pytest.mark.asyncio
    async def test_json_parse_failure_returns_empty(self):
        """LLM returns invalid JSON → group returns [], rest unaffected."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        import json

        links = [
            self._make_link("core_note", 1, "A", "core_note", 2, "B", score=0.7),
            self._make_link("core_note", 3, "C", "core_note", 4, "D", score=0.6),
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="prompt")

        call_count = [0]

        async def mock_achat(user_prompt, module, purpose):
            call_count[0] += 1
            if call_count[0] == 1:
                return ("not valid json {{", MagicMock())
            else:
                return (json.dumps([{"content": "Valid spark", "quality_score": 0.8}]), MagicMock())

        core.llm.achat = mock_achat

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links)
        # Only the second (valid) group should produce
        assert len(sparks) == 1

    @pytest.mark.asyncio
    async def test_respects_spark_pair_limit_from_params(self):
        """spark_pair_limit from params controls how many top links are used."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        import json

        links = [
            self._make_link("core_note", i, f"N{i}", "core_note", i + 10, f"M{i}", score=0.9 - i * 0.05)
            for i in range(1, 8)  # 7 links total
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="prompt")
        core.llm.achat = AsyncMock()
        core.llm.achat.return_value = (json.dumps([{"content": "spark", "quality_score": 0.5}]), MagicMock())

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        # Only top 3 links should be used
        sparks = await pipeline._generate_sparks(links, params={"spark_pair_limit": 3})
        # 3 orphan links → 3 groups → 3 calls → each returns 1 spark
        assert len(sparks) == 3
```

- [ ] **Step 2: 运行测试验证失败**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_pipeline.py::TestGenerateSparksPerGroup -v
```

预期：测试失败，因为 `_generate_sparks` 仍是旧批量实现。

- [ ] **Step 3: 重写 `_generate_sparks`**

将 `pipeline.py` 中 `_generate_sparks` 方法（331-369 行）替换为：

```python
    async def _generate_sparks(
        self, scored_links: list[dict], params: dict | None = None,
    ) -> list[dict]:
        """从高分关联对中按组生成火花候选。

        使用贪心共享源分组 (C1)，每组独立 LLM 调用，并行限流 Semaphore(5)。
        每组产出 0-4 个火花，每个火花精确绑定本组涉及的来源。
        """
        if not scored_links:
            return []

        max_pairs = params.get("spark_pair_limit", 10) if params else 10
        top_links = sorted(
            scored_links, key=lambda x: x.get("relevance_score", 0), reverse=True,
        )[:max_pairs]

        groups = self._group_by_shared_source(top_links)

        sem = asyncio.Semaphore(5)

        async def _generate_group(group: list[dict]) -> list[dict]:
            async with sem:
                links_data = [
                    {"a": l["source_a"]["content"][:200],
                     "b": l["source_b"]["content"][:200],
                     "reason": l.get("reasoning", ""),
                     "link_type": l.get("recall_path", ""),
                     "quality_score": l.get("relevance_score", 0)}
                    for l in group
                ]

                prompt = self.core.llm.load_prompt(
                    "ideator", "spark_generate", links=links_data,
                )

                try:
                    raw, _ = await self.core.llm.achat(
                        user_prompt=prompt, module="ideator", purpose="spark_generate",
                    )
                    sparks = json.loads(raw)
                    if not isinstance(sparks, list):
                        return []

                    # Inject precise source_refs from the group
                    refs = []
                    seen = set()
                    for l in group:
                        for src in (l["source_a"], l["source_b"]):
                            key = f"{src['type']}:{src['id']}"
                            if key not in seen:
                                seen.add(key)
                                refs.append({"type": src["type"], "id": src["id"]})

                    for s in sparks:
                        s["source_refs"] = refs
                        s["source_type"] = group[0].get("recall_path", "cross_layer")

                    return sparks
                except Exception:
                    logger.debug("[IdeatorPipeline] 组火花生成失败", exc_info=True)
                    return []

        results = await asyncio.gather(*[_generate_group(g) for g in groups])

        all_sparks: list[dict] = []
        for r in results:
            all_sparks.extend(r)

        return all_sparks
```

- [ ] **Step 4: 运行全部新测试**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_pipeline.py::TestGenerateSparksPerGroup -v
```

预期：全部 6 个测试 PASS。

- [ ] **Step 5: 运行全部 pipeline 测试确保无回归**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_pipeline.py -v
```

预期：所有测试 PASS（旧测试 + 新测试，共 ~21 个）。

- [ ] **Step 6: 提交**

```bash
git add paperreadagent/modules/ideator/pipeline.py paperreadagent/modules/ideator/tests/test_pipeline.py
git commit -m "feat(ideator): rewrite S2 _generate_sparks with per-group C1 generation"
```

---

### Task 4: Effort 参数迁移

**Files:**
- Modify: `paperreadagent/modules/ideator/effort.py:45,55,68,82`
- Modify: `paperreadagent/modules/ideator/tests/test_integration.py:73-76,516-523`

- [ ] **Step 1: 更新 `EFFORT_PARAMS`**

将 `effort.py` 中四个 effort level 的 `spark_count` 替换为 `spark_pair_limit`：

```python
# lite (line 45): "spark_count": (2, 3),  →  "spark_pair_limit": 5,
# balanced (line 55): "spark_count": (3, 5),  →  "spark_pair_limit": 10,
# max (line 68): "spark_count": (5, 7),  →  "spark_pair_limit": 15,
# beast (line 82): "spark_count": (7, 10),  →  "spark_pair_limit": 20,
```

具体编辑如下：

```python
# effort.py line 45
"spark_pair_limit": 5,

# effort.py line 55
"spark_pair_limit": 10,

# effort.py line 68
"spark_pair_limit": 15,

# effort.py line 82
"spark_pair_limit": 20,
```

- [ ] **Step 2: 更新集成测试**

将 `test_integration.py` 中的 `test_lite_produces_fewer_sparks` 和 `test_spark_count_increasing` 替换为：

```python
    def test_lite_produces_fewer_sparks(self):
        """Lite effort spark_pair_limit is lower than max/beast."""
        lite_limit = EFFORT_PARAMS["lite"]["spark_pair_limit"]
        beast_limit = EFFORT_PARAMS["beast"]["spark_pair_limit"]
        assert lite_limit < beast_limit, (
            f"Lite pair limit {lite_limit} should be less than beast {beast_limit}"
        )


# ... and in TestEffortMonotonic class:

    def test_spark_pair_limit_increasing(self):
        """Higher effort levels use more pairs for spark generation."""
        prev_limit = 0
        for level in self.ALL_LEVELS:
            limit = EFFORT_PARAMS[level]["spark_pair_limit"]
            assert limit >= prev_limit, (
                f"{level} spark_pair_limit {limit} < prev {prev_limit}"
            )
            prev_limit = limit
```

- [ ] **Step 3: 运行集成测试**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_integration.py -v -k "spark"
```

预期：2 个测试 PASS（`test_lite_produces_fewer_sparks` + `test_spark_pair_limit_increasing`）。

- [ ] **Step 4: 提交**

```bash
git add paperreadagent/modules/ideator/effort.py paperreadagent/modules/ideator/tests/test_integration.py
git commit -m "feat(ideator): migrate effort params from spark_count tuple to spark_pair_limit"
```

---

### Task 5: 全量测试验证

- [ ] **Step 1: 运行 ideator 全部测试**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v
```

预期：所有测试 PASS，无回归。

- [ ] **Step 2: 运行全部项目测试**

```bash
uv run python -m pytest paperreadagent/tests/ -v
```

预期：所有测试 PASS。
