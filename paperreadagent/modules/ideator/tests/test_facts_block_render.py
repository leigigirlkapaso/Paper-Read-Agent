"""tests for prompts/_facts_block.jinja2 rendering via CoreLLM.load_prompt."""
from pathlib import Path
from jinja2 import Environment, FileSystemLoader


def _render_facts(facts):
    """Render _facts_block.jinja2 standalone (mirrors CoreLLM.load_prompt behavior)."""
    prompts_dir = Path(__file__).parent.parent / "prompts"
    env = Environment(loader=FileSystemLoader(str(prompts_dir)), autoescape=False)
    tmpl = env.get_template("_facts_block.jinja2")
    return tmpl.render(facts=facts)


_FULL = {
    "paper_id": 1, "title": "Transformer for X",
    "arxiv_id": "2401.0001", "relevance_score": 0.9,
    "extraction": {
        "problem": "Solve X efficiently",
        "methods": ["attention", "MLP"],
        "datasets": ["ImageNet", "COCO"],
        "metrics": [{"name": "acc", "value": "92.3%", "condition": "ImageNet val"}],
        "baselines": ["ResNet", "ViT"],
        "limitations": ["needs GPU", "slow"],
        "contributions": ["new layer", "SOTA"],
    },
}


def test_render_empty_facts_returns_empty_string():
    out = _render_facts([])
    assert out.strip() == ""


def test_render_single_paper_full_fields():
    out = _render_facts([_FULL])
    assert "Transformer for X" in out
    assert "arxiv:2401.0001" in out
    # All 7 field labels render
    assert "问题" in out and "Solve X efficiently" in out
    assert "方法" in out and "attention" in out
    assert "数据集" in out and "ImageNet" in out
    assert "Baselines" in out and "ResNet" in out
    assert "局限" in out and "needs GPU" in out
    assert "贡献" in out and "new layer" in out


def test_render_metric_triplet_format():
    out = _render_facts([_FULL])
    # metrics rendered as: name=value @ condition
    assert "acc=92.3% @ ImageNet val" in out


def test_render_missing_field_shows_dash():
    empty_extract = {
        "paper_id": 1, "title": "Empty", "arxiv_id": None, "relevance_score": 0.5,
        "extraction": {
            "problem": "",
            "methods": [], "datasets": [], "metrics": [],
            "baselines": [], "limitations": [], "contributions": [],
        },
    }
    out = _render_facts([empty_extract])
    # All empty fields render as "—" (em-dash)
    assert out.count("—") >= 7  # problem + 5 list fields + metrics-empty fallback


def test_render_includes_paper_numbering_hint():
    out = _render_facts([_FULL])
    # The footer hint tells agents to cite by "论文 P1 / P2"
    assert "论文 P1" in out or "P1 / P2" in out


def test_render_handles_none_list_fields_gracefully():
    """Defense-in-depth: raw json.loads can yield None for list fields if
    extraction_json was written without _normalize. Template must not crash."""
    fact = {
        "paper_id": 1, "title": None, "arxiv_id": None, "relevance_score": 0.5,
        "extraction": {
            "problem": None, "methods": None, "datasets": None,
            "metrics": None, "baselines": None,
            "limitations": None, "contributions": None,
        },
    }
    out = _render_facts([fact])  # must not raise
    assert "—" in out
    assert "(untitled)" in out


def test_render_handles_partial_metric_dict():
    """Metric missing condition should not produce dangling '@ '."""
    fact = {
        "paper_id": 1, "title": "T", "arxiv_id": None, "relevance_score": 0.5,
        "extraction": {
            "problem": "p", "methods": [], "datasets": [],
            "metrics": [{"name": "acc", "value": "92%", "condition": ""}],
            "baselines": [], "limitations": [], "contributions": [],
        },
    }
    out = _render_facts([fact])
    # When condition is empty, the '@' separator should NOT appear for that metric
    # (it should look like 'acc=92%' alone, not 'acc=92% @ ')
    assert "@ " not in out or "acc=92%" not in out  # if @ appears it must be for a different metric