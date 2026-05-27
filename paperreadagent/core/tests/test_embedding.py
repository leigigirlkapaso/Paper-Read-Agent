"""
core/tests/test_embedding.py
测试 embedding 工具函数：余弦相似度、序列化/反序列化。
"""

import math

from core.embedding import cosine_similarity, pack_embedding, unpack_embedding


def test_cosine_similarity_identical_vectors():
    a = [1.0, 2.0, 3.0]
    assert math.isclose(cosine_similarity(a, a), 1.0, rel_tol=1e-9)


def test_cosine_similarity_orthogonal():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert math.isclose(cosine_similarity(a, b), 0.0, abs_tol=1e-9)


def test_cosine_similarity_opposite():
    a = [1.0, 2.0, 3.0]
    b = [-1.0, -2.0, -3.0]
    assert math.isclose(cosine_similarity(a, b), -1.0, rel_tol=1e-9)


def test_cosine_similarity_empty_vector():
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([1.0], []) == 0.0


def test_cosine_similarity_mismatched_lengths():
    assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0


def test_cosine_similarity_zero_norm():
    a = [0.0, 0.0, 0.0]
    b = [1.0, 2.0, 3.0]
    assert cosine_similarity(a, b) == 0.0


def test_pack_unpack_roundtrip():
    vec = [0.1, 0.2, 0.3, -0.4, 0.5]
    packed = pack_embedding(vec)
    unpacked = unpack_embedding(packed)
    assert len(unpacked) == len(vec)
    for a, b in zip(vec, unpacked):
        assert math.isclose(a, b, rel_tol=1e-9)


def test_unpack_empty_string():
    assert unpack_embedding("") == []
    assert unpack_embedding("   ") == []


def test_unpack_invalid_json():
    assert unpack_embedding("not-json") == []
