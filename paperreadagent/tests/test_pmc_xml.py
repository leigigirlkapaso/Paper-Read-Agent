"""
tests/test_pmc_xml.py
Test PMC JATS XML → Markdown conversion functions.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from utils.pdf_parser import (
    _local_tag,
    _find_elem_by_local,
    _extract_inline_text,
    _pmc_block_to_markdown,
    _try_pmc_xml,
)


# ── _local_tag ──────────────────────────────────────────────────

def test_local_tag_plain():
    assert _local_tag(ET.fromstring("<p>text</p>")) == "p"


def test_local_tag_with_namespace():
    elem = ET.fromstring('<p xmlns="http://example.com">text</p>')
    assert _local_tag(elem) == "p"


def test_local_tag_no_text():
    assert _local_tag(ET.fromstring("<br />")) == "br"


# ── _find_elem_by_local ────────────────────────────────────────

def test_find_direct_child():
    root = ET.fromstring("<root><body>content</body></root>")
    assert _find_elem_by_local(root, "body") is not None


def test_find_deeply_nested():
    root = ET.fromstring(
        "<root><a><b><c><body>content</body></c></b></a></root>"
    )
    found = _find_elem_by_local(root, "body")
    assert found is not None
    assert found.text == "content"


def test_find_with_namespace():
    root = ET.fromstring(
        '<root xmlns:x="http://x"><x:body>content</x:body></root>'
    )
    found = _find_elem_by_local(root, "body")
    assert found is not None


def test_find_not_found():
    root = ET.fromstring("<root><a>text</a></root>")
    assert _find_elem_by_local(root, "body") is None


# ── _extract_inline_text ───────────────────────────────────────

def test_inline_plain_text():
    elem = ET.fromstring("<p>Hello world</p>")
    assert _extract_inline_text(elem) == "Hello world"


def test_inline_nested_tags():
    elem = ET.fromstring("<p>This is <italic>very</italic> important</p>")
    assert _extract_inline_text(elem) == "This is very important"


def test_inline_mixed_tags():
    elem = ET.fromstring(
        "<p>A <bold>B</bold> C <sup>D</sup> E <sub>F</sub> G</p>"
    )
    assert _extract_inline_text(elem) == "A B C D E F G"


def test_inline_xref_stripped():
    elem = ET.fromstring('<p>See <xref ref-type="bibr" rid="ref1">[1]</xref> for details</p>')
    assert _extract_inline_text(elem) == "See [1] for details"


def test_inline_tail_text():
    elem = ET.fromstring("<p>Start <italic>middle</italic> end</p>")
    assert _extract_inline_text(elem) == "Start middle end"


def test_inline_deep_nesting():
    elem = ET.fromstring(
        "<p>A <italic>B <bold>C</bold> D</italic> E</p>"
    )
    assert _extract_inline_text(elem) == "A B C D E"


def test_inline_empty():
    elem = ET.fromstring("<p></p>")
    assert _extract_inline_text(elem) == ""


# ── _pmc_block_to_markdown ─────────────────────────────────────

def test_block_sec_with_title():
    xml = "<sec><title>Introduction</title><p>This is the intro.</p></sec>"
    root = ET.fromstring(xml)
    lines: list[str] = []
    _pmc_block_to_markdown(root, lines)
    assert lines == ["### Introduction", "This is the intro."]


def test_block_plain_paragraph():
    xml = "<p>Simple paragraph.</p>"
    root = ET.fromstring(xml)
    lines: list[str] = []
    _pmc_block_to_markdown(root, lines)
    assert lines == ["Simple paragraph."]


def test_block_fig_with_caption():
    xml = "<fig><caption>Comparison of methods.</caption></fig>"
    root = ET.fromstring(xml)
    lines: list[str] = []
    _pmc_block_to_markdown(root, lines)
    assert lines == ["> **Figure**: Comparison of methods."]


def test_block_table_wrap_skipped():
    xml = "<table-wrap><table><tr><td>data</td></tr></table></table-wrap>"
    root = ET.fromstring(xml)
    lines: list[str] = []
    _pmc_block_to_markdown(root, lines)
    assert lines == []


def test_block_nested_sec():
    xml = (
        "<sec>"
        "<title>Methods</title>"
        "<sec><title>Participants</title><p>N=30.</p></sec>"
        "<sec><title>Procedure</title><p>Done in lab.</p></sec>"
        "</sec>"
    )
    root = ET.fromstring(xml)
    lines: list[str] = []
    _pmc_block_to_markdown(root, lines)
    assert lines == [
        "### Methods",
        "### Participants",
        "N=30.",
        "### Procedure",
        "Done in lab.",
    ]


def test_block_unknown_element_recurses():
    xml = "<boxed-text><p>Inside a box.</p></boxed-text>"
    root = ET.fromstring(xml)
    lines: list[str] = []
    _pmc_block_to_markdown(root, lines)
    assert lines == ["Inside a box."]


def test_block_title_outside_sec():
    xml = "<title>Abstract</title>"
    root = ET.fromstring(xml)
    lines: list[str] = []
    _pmc_block_to_markdown(root, lines)
    assert lines == ["### Abstract"]


def test_block_empty_paragraph_skipped():
    xml = "<p></p>"
    root = ET.fromstring(xml)
    lines: list[str] = []
    _pmc_block_to_markdown(root, lines)
    assert lines == []


# ── End-to-end: JATS XML → Markdown via _try_pmc_xml (mock) ────

_SAMPLE_JATS_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<article>
<front><title>Test Paper</title></front>
<body>
<sec>
<title>Introduction</title>
<p>This is the <bold>first</bold> paragraph of the introduction.</p>
<p>This is the second paragraph with <italic>emphasis</italic>.</p>
</sec>
<sec>
<title>Methods</title>
<sec>
<title>Participants</title>
<p>Thirty participants were recruited from <xref>University Campus</xref>.</p>
</sec>
<sec>
<title>Procedure</title>
<p>The experiment was conducted using standard <sup>1</sup> protocols.</p>
</sec>
</sec>
<sec>
<title>Results</title>
<p>The main finding was significant (p&lt;0.01).</p>
<fig>
<caption>Figure 1: Experimental results across conditions.</caption>
</fig>
<table-wrap><table><tr><td>data</td></tr></table></table-wrap>
</sec>
</body>
</article>"""


def test_full_jats_to_markdown():
    """End-to-end: parse a realistic JATS XML body and produce Markdown."""
    root = ET.fromstring(_SAMPLE_JATS_BODY)
    body = _find_elem_by_local(root, "body")
    assert body is not None

    lines: list[str] = []
    for child in body:
        _pmc_block_to_markdown(child, lines)

    md = "\n\n".join(lines)
    # Verify key sections are present
    assert "### Introduction" in md
    assert "This is the first paragraph" in md
    assert "### Methods" in md
    assert "### Participants" in md
    assert "### Procedure" in md
    assert "### Results" in md
    assert "> **Figure**: Figure 1" in md
    # Verify inline tags stripped
    assert "<bold>" not in md
    assert "<italic>" not in md
    assert "<sup>" not in md
    # Verify table skipped
    assert "data" not in md


def test_pmc_xml_with_namespaced_body():
    """JATS XML with namespace — body should still be found."""
    xml = (
        '<article xmlns:xlink="http://www.w3.org/1999/xlink"'
        ' xmlns:mml="http://www.w3.org/1998/Math/MathML">'
        "<body><sec><title>Test</title><p>Content</p></sec></body>"
        "</article>"
    )
    root = ET.fromstring(xml)
    body = _find_elem_by_local(root, "body")
    assert body is not None
    lines: list[str] = []
    for child in body:
        _pmc_block_to_markdown(child, lines)
    assert lines == ["### Test", "Content"]


def test_pmc_xml_no_body():
    """XML without body element returns None from _find_elem_by_local."""
    xml = "<article><front><title>No Body</title></front></article>"
    root = ET.fromstring(xml)
    assert _find_elem_by_local(root, "body") is None
