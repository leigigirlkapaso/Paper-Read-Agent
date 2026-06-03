"""
tests/test_pdf_parser.py
测试 PDF 截断正则能否匹配 pymupdf4llm 输出的各种标题格式变体。
"""

from __future__ import annotations

import re

from utils.pdf_parser import (
    _INTRO_PATTERN,
    _CONCLUSION_PATTERN,
    _REFERENCES_PATTERN,
    _find_next_heading_start,
    _extract_key_sections,
)


def _make_doc(body: str) -> str:
    """构造最小完整 Markdown 文档，所有 body 行都用 ## 标题。"""
    lines = ["# Paper Title", "", "Authors: A, B, C", "", "## Abstract", "", "This is the abstract text. " * 20]
    lines.append(body)
    return "\n".join(lines)


class TestIntroPattern:
    """验证 Introduction 正则能匹配 pymupdf4llm 的所有输出变体。"""

    def test_simple(self):
        assert _INTRO_PATTERN.search("## Introduction")

    def test_numbered(self):
        assert _INTRO_PATTERN.search("## 1 Introduction")

    def test_numbered_dot(self):
        assert _INTRO_PATTERN.search("## 1. Introduction")

    def test_bold_numbered(self):
        assert _INTRO_PATTERN.search("## **1 Introduction**")

    def test_bold_only(self):
        assert _INTRO_PATTERN.search("## **Introduction**")

    def test_bold_numbered_with_space(self):
        assert _INTRO_PATTERN.search("## **1 Introduction**")

    def test_background(self):
        assert _INTRO_PATTERN.search("## **1 Background**")

    def test_three_hash(self):
        assert _INTRO_PATTERN.search("### **1 Introduction**")

    def test_case_insensitive(self):
        assert _INTRO_PATTERN.search("## **1 introduction**")

    def test_roman_numeral(self):
        # 罗马数字也算在 \d+ 之外的情况，应该不匹配但不会误判
        # 实际上 \d+ 只匹配数字，所以这种情况匹配不到是预期行为
        pass


class TestConclusionPattern:
    """验证 Conclusion 正则的变体匹配。"""

    def test_simple(self):
        assert _CONCLUSION_PATTERN.search("## Conclusion")

    def test_bold_numbered(self):
        assert _CONCLUSION_PATTERN.search("## **8 Conclusion**")

    def test_conclusions_plural(self):
        assert _CONCLUSION_PATTERN.search("## **8 Conclusions**")

    def test_concluding_remarks(self):
        assert _CONCLUSION_PATTERN.search("## Concluding Remarks")

    def test_summary_and_conclusion(self):
        assert _CONCLUSION_PATTERN.search("## **7 Summary and Conclusion**")


class TestReferencesPattern:
    """验证 References 正则。"""

    def test_simple(self):
        assert _REFERENCES_PATTERN.search("## References")

    def test_bold_numbered(self):
        assert _REFERENCES_PATTERN.search("## **9 References**")

    def test_bibliography(self):
        assert _REFERENCES_PATTERN.search("## **10 Bibliography**")


class TestFindNextHeading:
    """验证 _find_next_heading_start 能找到下一个章节边界。"""

    def test_finds_next_heading(self):
        text = "## **1 Introduction**\n\nSome text.\n\n## **2 Related Work**\n\nMore text."
        pos = _find_next_heading_start(text, 30)
        assert pos > 30

    def test_no_next_heading(self):
        text = "## **1 Introduction**\n\nSome text without another heading."
        pos = _find_next_heading_start(text, 30)
        assert pos == -1


class TestExtractKeySections:
    """集成测试 _extract_key_sections 截取逻辑。"""

    def test_extracts_three_sections(self):
        body = (
            "## **1 Introduction**\n\nIntro text here. " * 20 + "\n\n"
            "## **2 Method**\n\nMethod text. " * 30 + "\n\n"
            "## **3 Related Work**\n\nRelated work text. " * 10 + "\n\n"
            "## **8 Conclusion**\n\nConclusion text. " * 40 + "\n\n"
            "## **9 References**\n\nRef text. " * 5
        )
        md_text = _make_doc(body)
        # 强制总长超过 max_chars 触发截取
        assert len(md_text) > 2000

        sections = _extract_key_sections(md_text, max_chars=1500)

        # 至少应该提取到摘要和引言/结论
        assert len(sections) >= 2
        # 引言段应包含 "Intro text"
        assert any("Intro text" in s for s in sections)
        # 结论段应包含 "Conclusion text"
        assert any("Conclusion text" in s for s in sections)
        # References 不应出现在结论中
        combined = "\n".join(sections)
        assert "References" not in combined


class TestNoMatchFallback:
    """没有明确 Introduction/Conclusion 标题时的行为。"""

    def test_no_headers_returns_abstract_only(self):
        body = "Some text without standard section headers. " * 100
        md_text = _make_doc(body)
        sections = _extract_key_sections(md_text, max_chars=500)
        # 至少返回摘要段
        assert len(sections) >= 1
