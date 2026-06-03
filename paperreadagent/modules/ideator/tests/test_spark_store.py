"""tests for ideator SparkStore"""

import pytest


class TestSparkStore:
    def test_dedup_inserts_when_no_existing(self):
        from paperreadagent.modules.ideator.spark_store import SparkStore

        class MockData:
            def get_existing_sparks(self, limit=200):
                return []
        store = SparkStore.__new__(SparkStore)
        store.data = MockData()
        action, merge_id = store.dedup("test spark", [0.1] * 768)
        assert action == "insert"
        assert merge_id is None

    def test_dedup_merges_when_high_similarity(self):
        from paperreadagent.modules.ideator.spark_store import SparkStore
        from paperreadagent.core.embedding import pack_embedding

        class MockData:
            def get_existing_sparks(self, limit=200):
                return [{
                    "id": 1, "embedding": pack_embedding([0.1] * 768),
                    "content": "existing spark",
                }]
        store = SparkStore.__new__(SparkStore)
        store.data = MockData()
        action, merge_id = store.dedup("very similar", [0.1] * 768)
        assert action == "merge"
        assert merge_id == 1

    def test_feedback_useful_increases_score(self):
        from paperreadagent.modules.ideator.spark_store import SparkStore

        class MockData:
            def get_spark(self, spark_id):
                return {"id": spark_id, "quality_score": 0.5, "source_refs": "[]"}
            def update_spark(self, spark_id, **fields):
                self.last_update = fields

        store = SparkStore.__new__(SparkStore)
        store.data = MockData()
        store.apply_feedback(1, "useful")
        assert store.data.last_update["quality_score"] == 0.7

    def test_feedback_noise_decreases_score(self):
        from paperreadagent.modules.ideator.spark_store import SparkStore

        class MockData:
            def get_spark(self, spark_id):
                return {"id": spark_id, "quality_score": 0.5, "source_refs": "[]"}
            def update_spark(self, spark_id, **fields):
                self.last_update = fields

        store = SparkStore.__new__(SparkStore)
        store.data = MockData()
        store.apply_feedback(1, "duplicate")
        assert store.data.last_update["quality_score"] == 0.2

    def test_update_review_result(self):
        """verify update_review_result calls update_spark with correct fields"""
        from paperreadagent.modules.ideator.spark_store import SparkStore

        class MockData:
            def get_spark(self, spark_id):
                return {"id": spark_id, "quality_score": 0.5, "review_count": 2}
            def update_spark(self, spark_id, **fields):
                self.last_update = fields

        store = SparkStore.__new__(SparkStore)
        store.data = MockData()
        store.update_review_result(1, final_score=0.85, review_status="passed", verdict="PASS")
        assert store.data.last_update["final_score"] == 0.85
        assert store.data.last_update["review_status"] == "passed"
        assert store.data.last_update["review_count"] == 3

    def test_update_review_result_ignores_missing_spark(self):
        """verify update_review_result is no-op for nonexistent spark"""
        from paperreadagent.modules.ideator.spark_store import SparkStore

        call_count = 0

        class MockData:
            def get_spark(self, spark_id):
                return None
            def update_spark(self, spark_id, **fields):
                nonlocal call_count
                call_count += 1

        store = SparkStore.__new__(SparkStore)
        store.data = MockData()
        store.update_review_result(999, final_score=0.5, review_status="passed", verdict="PASS")
        assert call_count == 0
