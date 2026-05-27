"""tests for thinker ProfileManager"""

import pytest


class TestProfileManager:
    def test_merge_appends_new_domains(self):
        from paperreadagent.modules.thinker.profile import ProfileManager
        pm = ProfileManager.__new__(ProfileManager)
        current = {"research_domains": ["NLP"], "knowledge_level": {},
                   "thinking_style": "", "long_term_goals": [], "interaction_prefs": {},
                   "confidence_scores": {}}
        updates = {"research_domains": ["知识图谱"]}
        merged = pm.merge(current, updates)
        assert "NLP" in merged["research_domains"]
        assert "知识图谱" in merged["research_domains"]
        assert merged["confidence_scores"]["research_domains"] > 0

    def test_merge_no_duplicate_domains(self):
        from paperreadagent.modules.thinker.profile import ProfileManager
        pm = ProfileManager.__new__(ProfileManager)
        current = {"research_domains": ["NLP"], "knowledge_level": {},
                   "thinking_style": "", "long_term_goals": [], "interaction_prefs": {},
                   "confidence_scores": {}}
        updates = {"research_domains": ["NLP"]}
        merged = pm.merge(current, updates)
        assert merged["research_domains"] == ["NLP"]

    def test_merge_scalar_overwrite(self):
        from paperreadagent.modules.thinker.profile import ProfileManager
        pm = ProfileManager.__new__(ProfileManager)
        current = {"research_domains": [], "knowledge_level": {},
                   "thinking_style": "", "long_term_goals": [], "interaction_prefs": {},
                   "confidence_scores": {}}
        updates = {"thinking_style": "data-driven"}
        merged = pm.merge(current, updates)
        assert merged["thinking_style"] == "data-driven"

    def test_merge_confidence_increment(self):
        from paperreadagent.modules.thinker.profile import ProfileManager
        pm = ProfileManager.__new__(ProfileManager)
        current = {"research_domains": [], "knowledge_level": {},
                   "thinking_style": "data-driven", "long_term_goals": [], "interaction_prefs": {},
                   "confidence_scores": {"thinking_style": 0.5}}
        updates = {"thinking_style": "data-driven"}
        merged = pm.merge(current, updates)
        assert merged["confidence_scores"]["thinking_style"] > 0.5
