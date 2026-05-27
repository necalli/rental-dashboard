import os
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.agent_skills import enabled_tool_names, load_skill_packages, select_skills_for_message, skill_system_prompt


class AgentSkillsTests(unittest.TestCase):
    def test_load_skill_packages(self):
        skills = load_skill_packages("backend/agent_skills")
        self.assertGreaterEqual(len(skills), 4)
        ids = {str(skill.get("skill_id")) for skill in skills}
        self.assertIn("mvp_pipeline_observability", ids)
        self.assertIn("mvp_listing_analysis", ids)
        self.assertIn("mvp_trip_research", ids)
        self.assertIn("future_personality_rag", ids)

    def test_enabled_tool_names_includes_personality_rag_tools(self):
        skills = load_skill_packages("backend/agent_skills")
        tools = enabled_tool_names(skills)
        self.assertIn("tool.metrics_jobs", tools)
        self.assertIn("tool.search_create", tools)
        self.assertIn("tool.listing_compare_create", tools)
        self.assertIn("tool.trip_research_tavily", tools)
        self.assertIn("tool.personality_rag_context", tools)
        self.assertIn("tool.personality_rag_upsert", tools)

    def test_skill_system_prompt_contains_enabled_skill_context(self):
        skills = load_skill_packages("backend/agent_skills")
        prompt = skill_system_prompt(skills)
        self.assertIn("Skill: Pipeline Observability", prompt)
        self.assertIn("tool.metrics_jobs", prompt)

    def test_runtime_json_tool_config_is_loaded(self):
        skills = load_skill_packages("backend/agent_skills")
        by_id = {str(item.get("skill_id")): item for item in skills}
        search_skill = by_id.get("mvp_search_and_listing_ops") or {}
        tools = search_skill.get("tools") if isinstance(search_skill.get("tools"), list) else []
        self.assertIn("tool.search_ingest_listings", tools)
        frontmatter = search_skill.get("raw_frontmatter") if isinstance(search_skill.get("raw_frontmatter"), dict) else {}
        self.assertNotIn("tools", frontmatter)

    def test_select_skills_for_message_scopes_pipeline_prompt(self):
        skills = load_skill_packages("backend/agent_skills")
        selected = select_skills_for_message(skills, "show me a pipeline health snapshot and failed jobs")
        selected_ids = {str(item.get("skill_id")) for item in selected}
        self.assertIn("mvp_pipeline_observability", selected_ids)
        self.assertNotIn("mvp_trip_research", selected_ids)


if __name__ == "__main__":
    unittest.main()
