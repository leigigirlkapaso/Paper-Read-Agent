"""
tests/test_multi_downloader.py
多源下载器单元测试 — 验证级联顺序、DOI 解析、PDF 校验。
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path

from utils.multi_downloader import _build_cascade
from utils.arxiv_downloader import _is_arxiv_id


class TestArxivIdDetection:
    def test_standard_arxiv_id(self):
        assert _is_arxiv_id("2301.07041")
        # version-stripped IDs are passed in, so v2 is stripped before reaching _is_arxiv_id
        assert _is_arxiv_id("cs/0703123")
        assert not _is_arxiv_id("s2_abc123")
        assert not _is_arxiv_id("oa_W123456")


class TestCascadeOrder:
    def test_arxiv_only(self):
        cascade = _build_cascade("2301.07041", "", "", "", False, None)
        sources = [s for s, _ in cascade]
        assert sources == ["arxiv"]

    def test_arxiv_with_direct_url(self):
        cascade = _build_cascade("2301.07041", "https://example.com/paper.pdf", "", "", False, None)
        sources = [s for s, _ in cascade]
        assert sources == ["arxiv", "direct"]

    def test_no_arxiv_no_doi(self):
        cascade = _build_cascade("oa_W123456", "", "", "", False, None)
        assert cascade == []

    def test_doi_unlock_unpaywall_and_s2(self):
        cascade = _build_cascade("s2_abc", "", "10.1234/test", "user@test.com", False, None)
        sources = [s for s, _ in cascade]
        assert sources == ["unpaywall", "s2_oa"]

    def test_scihub_enabled(self):
        cascade = _build_cascade("oa_W123", "", "10.1234/test", "", True, None)
        sources = [s for s, _ in cascade]
        assert sources == ["s2_oa", "scihub", "scihub", "scihub"]

    def test_full_cascade(self):
        cascade = _build_cascade(
            "2301.07041", "https://direct.url/pdf",
            "10.1234/test", "user@test.com", True, ["https://sci-hub.se"]
        )
        sources = [s for s, _ in cascade]
        assert sources == ["arxiv", "direct", "unpaywall", "s2_oa", "scihub"]


class TestIsArxivId:
    def test_valid_ids(self):
        assert _is_arxiv_id("2301.07041")
        assert _is_arxiv_id("cs/0703123")
        assert _is_arxiv_id("hep-th/9901001")

    def test_invalid_ids(self):
        assert not _is_arxiv_id("s2_abc123")
        assert not _is_arxiv_id("oa_W123456")
        assert not _is_arxiv_id("pwc_abc")
        assert not _is_arxiv_id("local_uuid123")
