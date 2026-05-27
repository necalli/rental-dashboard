import os
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.agent_chat_runtime import AgentChatRuntime, ClaudeAgentSdkRuntime, ClaudeSkillRuntime


class FakeStorage:
    def list_job_metrics(self, limit=100, job_type=None, status=None):
        return [
            {
                "job_id": "j1",
                "job_type": "search",
                "status": "complete",
                "metrics": {
                    "capture_duration_ms": 1000,
                    "job_total_ms": 1100,
                    "parse_ms": 10,
                    "persist_ms": 9,
                    "capture_timings": {"navigation_ms": 700},
                    "parser_drift": {"drift_detected": False},
                },
            }
        ]

    def list_jobs(self, limit=50):
        return [{"job_id": "job-1", "job_type": "search", "status": "complete"}]

    def get_job(self, job_id):
        return None

    def list_search_runs(self, limit=50):
        return []

    def list_search_listings(self, run_id, limit=200):
        return []

    def list_listings(self, limit=50):
        return []

    def get_listing(self, listing_id):
        return None

    def list_reviews(self, listing_id, limit=200):
        return []

    def get_latest_enrichment(self, listing_id, kind, model=None, prompt_version=None):
        return None

    def create_job(self, job_type, payload):
        return {"job_id": "queued-1", "job_type": job_type, "status": "queued", "payload": payload}


class FakePersistentStorage(FakeStorage):
    def __init__(self):
        self._agent_states = {}

    def upsert_agent_session_state(self, session_id, state):
        self._agent_states[str(session_id)] = dict(state or {})

    def get_agent_session_state(self, session_id):
        value = self._agent_states.get(str(session_id))
        return dict(value) if isinstance(value, dict) else None


class AgentChatRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._old_runtime = os.environ.get("RENTAL_AGENT_RUNTIME")
        self._old_key = os.environ.get("RENTAL_CLAUDE_API_KEY")
        self._old_fanout_enabled = os.environ.get("RENTAL_AGENT_FANOUT_ENABLED")
        self._old_fanout_workers = os.environ.get("RENTAL_AGENT_FANOUT_MAX_WORKERS")
        self._old_fanout_timeout = os.environ.get("RENTAL_AGENT_FANOUT_TIMEOUT_MS")
        self._old_clean_response_style = os.environ.get("RENTAL_AGENT_CLEAN_RESPONSE_STYLE")
        self._old_rag_skill_enabled = os.environ.get("RENTAL_RAG_SKILL_ENABLED")
        self._old_rag_skill_scope = os.environ.get("RENTAL_RAG_SKILL_SCOPE")
        self._old_sdk_permission_mode = os.environ.get("RENTAL_AGENT_SDK_PERMISSION_MODE")
        self._old_sdk_hooks_enabled = os.environ.get("RENTAL_AGENT_SDK_HOOKS_ENABLED")
        self._old_sdk_hooks_mode = os.environ.get("RENTAL_AGENT_SDK_HOOKS_MODE")
        self._old_sdk_structured_output_enabled = os.environ.get("RENTAL_AGENT_SDK_STRUCTURED_OUTPUT_ENABLED")
        self._old_sdk_continue_conversation_enabled = os.environ.get(
            "RENTAL_AGENT_SDK_CONTINUE_CONVERSATION_ENABLED"
        )
        self._old_sdk_stream_passthrough_enabled = os.environ.get(
            "RENTAL_AGENT_SDK_STREAM_PASSTHROUGH_ENABLED"
        )
        self._old_sdk_subagents_enabled = os.environ.get("RENTAL_AGENT_SDK_SUBAGENTS_ENABLED")
        self._old_sdk_subagent_model = os.environ.get("RENTAL_AGENT_SDK_SUBAGENT_MODEL")
        self._old_sdk_native_skills_enabled = os.environ.get("RENTAL_AGENT_SDK_NATIVE_SKILLS_ENABLED")
        self._old_sdk_native_skills_sync_enabled = os.environ.get("RENTAL_AGENT_SDK_NATIVE_SKILLS_SYNC_ENABLED")
        self._old_sdk_native_skills_dir = os.environ.get("RENTAL_AGENT_SDK_NATIVE_SKILLS_DIR")
        self._old_sdk_native_skills_sync_interval = os.environ.get(
            "RENTAL_AGENT_SDK_NATIVE_SKILLS_SYNC_INTERVAL_SECONDS"
        )

    def tearDown(self):
        if self._old_runtime is None:
            os.environ.pop("RENTAL_AGENT_RUNTIME", None)
        else:
            os.environ["RENTAL_AGENT_RUNTIME"] = self._old_runtime
        if self._old_key is None:
            os.environ.pop("RENTAL_CLAUDE_API_KEY", None)
        else:
            os.environ["RENTAL_CLAUDE_API_KEY"] = self._old_key
        if self._old_fanout_enabled is None:
            os.environ.pop("RENTAL_AGENT_FANOUT_ENABLED", None)
        else:
            os.environ["RENTAL_AGENT_FANOUT_ENABLED"] = self._old_fanout_enabled
        if self._old_fanout_workers is None:
            os.environ.pop("RENTAL_AGENT_FANOUT_MAX_WORKERS", None)
        else:
            os.environ["RENTAL_AGENT_FANOUT_MAX_WORKERS"] = self._old_fanout_workers
        if self._old_fanout_timeout is None:
            os.environ.pop("RENTAL_AGENT_FANOUT_TIMEOUT_MS", None)
        else:
            os.environ["RENTAL_AGENT_FANOUT_TIMEOUT_MS"] = self._old_fanout_timeout
        if self._old_clean_response_style is None:
            os.environ.pop("RENTAL_AGENT_CLEAN_RESPONSE_STYLE", None)
        else:
            os.environ["RENTAL_AGENT_CLEAN_RESPONSE_STYLE"] = self._old_clean_response_style
        if self._old_rag_skill_enabled is None:
            os.environ.pop("RENTAL_RAG_SKILL_ENABLED", None)
        else:
            os.environ["RENTAL_RAG_SKILL_ENABLED"] = self._old_rag_skill_enabled
        if self._old_rag_skill_scope is None:
            os.environ.pop("RENTAL_RAG_SKILL_SCOPE", None)
        else:
            os.environ["RENTAL_RAG_SKILL_SCOPE"] = self._old_rag_skill_scope
        if self._old_sdk_permission_mode is None:
            os.environ.pop("RENTAL_AGENT_SDK_PERMISSION_MODE", None)
        else:
            os.environ["RENTAL_AGENT_SDK_PERMISSION_MODE"] = self._old_sdk_permission_mode
        if self._old_sdk_hooks_enabled is None:
            os.environ.pop("RENTAL_AGENT_SDK_HOOKS_ENABLED", None)
        else:
            os.environ["RENTAL_AGENT_SDK_HOOKS_ENABLED"] = self._old_sdk_hooks_enabled
        if self._old_sdk_hooks_mode is None:
            os.environ.pop("RENTAL_AGENT_SDK_HOOKS_MODE", None)
        else:
            os.environ["RENTAL_AGENT_SDK_HOOKS_MODE"] = self._old_sdk_hooks_mode
        if self._old_sdk_structured_output_enabled is None:
            os.environ.pop("RENTAL_AGENT_SDK_STRUCTURED_OUTPUT_ENABLED", None)
        else:
            os.environ["RENTAL_AGENT_SDK_STRUCTURED_OUTPUT_ENABLED"] = self._old_sdk_structured_output_enabled
        if self._old_sdk_continue_conversation_enabled is None:
            os.environ.pop("RENTAL_AGENT_SDK_CONTINUE_CONVERSATION_ENABLED", None)
        else:
            os.environ["RENTAL_AGENT_SDK_CONTINUE_CONVERSATION_ENABLED"] = self._old_sdk_continue_conversation_enabled
        if self._old_sdk_stream_passthrough_enabled is None:
            os.environ.pop("RENTAL_AGENT_SDK_STREAM_PASSTHROUGH_ENABLED", None)
        else:
            os.environ["RENTAL_AGENT_SDK_STREAM_PASSTHROUGH_ENABLED"] = self._old_sdk_stream_passthrough_enabled
        if self._old_sdk_subagents_enabled is None:
            os.environ.pop("RENTAL_AGENT_SDK_SUBAGENTS_ENABLED", None)
        else:
            os.environ["RENTAL_AGENT_SDK_SUBAGENTS_ENABLED"] = self._old_sdk_subagents_enabled
        if self._old_sdk_subagent_model is None:
            os.environ.pop("RENTAL_AGENT_SDK_SUBAGENT_MODEL", None)
        else:
            os.environ["RENTAL_AGENT_SDK_SUBAGENT_MODEL"] = self._old_sdk_subagent_model
        if self._old_sdk_native_skills_enabled is None:
            os.environ.pop("RENTAL_AGENT_SDK_NATIVE_SKILLS_ENABLED", None)
        else:
            os.environ["RENTAL_AGENT_SDK_NATIVE_SKILLS_ENABLED"] = self._old_sdk_native_skills_enabled
        if self._old_sdk_native_skills_sync_enabled is None:
            os.environ.pop("RENTAL_AGENT_SDK_NATIVE_SKILLS_SYNC_ENABLED", None)
        else:
            os.environ["RENTAL_AGENT_SDK_NATIVE_SKILLS_SYNC_ENABLED"] = self._old_sdk_native_skills_sync_enabled
        if self._old_sdk_native_skills_dir is None:
            os.environ.pop("RENTAL_AGENT_SDK_NATIVE_SKILLS_DIR", None)
        else:
            os.environ["RENTAL_AGENT_SDK_NATIVE_SKILLS_DIR"] = self._old_sdk_native_skills_dir
        if self._old_sdk_native_skills_sync_interval is None:
            os.environ.pop("RENTAL_AGENT_SDK_NATIVE_SKILLS_SYNC_INTERVAL_SECONDS", None)
        else:
            os.environ[
                "RENTAL_AGENT_SDK_NATIVE_SKILLS_SYNC_INTERVAL_SECONDS"
            ] = self._old_sdk_native_skills_sync_interval

    def test_deterministic_runtime_default(self):
        os.environ["RENTAL_AGENT_RUNTIME"] = "deterministic"
        os.environ.pop("RENTAL_CLAUDE_API_KEY", None)
        runtime = AgentChatRuntime(storage=FakeStorage())
        out = runtime.chat(session_id="s1", message="show me pipeline health metrics")
        self.assertEqual(out["debug"]["runtime"], "deterministic")
        self.assertEqual(out["debug"]["intent"], "pipeline_health")

    def test_claude_runtime_without_key_falls_back_to_deterministic(self):
        os.environ["RENTAL_AGENT_RUNTIME"] = "claude"
        os.environ.pop("RENTAL_CLAUDE_API_KEY", None)
        runtime = AgentChatRuntime(storage=FakeStorage())
        out = runtime.chat(session_id="s2", message="show me pipeline health metrics")
        self.assertEqual(out["debug"]["runtime"], "deterministic")
        self.assertEqual(out["debug"]["intent"], "pipeline_health")

    def test_claude_failure_fallback(self):
        os.environ["RENTAL_AGENT_RUNTIME"] = "claude"
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = AgentChatRuntime(storage=FakeStorage())

        def fail_chat(*, session_id, message, user_id=None):  # noqa: ARG001
            raise RuntimeError("simulated_claude_failure")

        runtime.claude.chat = fail_chat  # type: ignore[assignment]
        out = runtime.chat(session_id="s3", message="show me pipeline health metrics")
        self.assertEqual(out["debug"]["runtime"], "deterministic_fallback")
        warnings = out["debug"].get("warnings") or []
        self.assertIn("claude_runtime_failed_fallback_deterministic", warnings)
        self.assertIn("claude_error", out["debug"])

    def test_agent_sdk_runtime_unavailable_falls_back_to_deterministic(self):
        os.environ["RENTAL_AGENT_RUNTIME"] = "agent_sdk"
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = AgentChatRuntime(storage=FakeStorage())
        runtime.agent_sdk.is_available = lambda: False  # type: ignore[assignment]
        out = runtime.chat(session_id="s-sdk-1", message="show me pipeline health metrics")
        self.assertEqual(out["debug"]["runtime"], "deterministic_fallback")
        warnings = out["debug"].get("warnings") or []
        self.assertIn("agent_sdk_runtime_unavailable_fallback_deterministic", warnings)

    def test_agent_sdk_runtime_uses_agent_sdk_when_available(self):
        os.environ["RENTAL_AGENT_RUNTIME"] = "agent_sdk"
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = AgentChatRuntime(storage=FakeStorage())

        def fake_chat(*, session_id, message, user_id=None):  # noqa: ARG001
            return {
                "session_id": "sdk-session",
                "trace_id": "trace",
                "reply": "sdk reply",
                "citations": [],
                "debug": {"runtime": "claude_agent_sdk"},
            }

        runtime.agent_sdk.is_available = lambda: True  # type: ignore[assignment]
        runtime.agent_sdk.chat = fake_chat  # type: ignore[assignment]
        out = runtime.chat(session_id="s-sdk-2", message="queue a search in Keene, NY")
        self.assertEqual(out["debug"]["runtime"], "claude_agent_sdk")
        self.assertEqual(out["reply"], "sdk reply")

    def test_agent_sdk_runtime_failure_falls_back_to_deterministic(self):
        os.environ["RENTAL_AGENT_RUNTIME"] = "agent_sdk"
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = AgentChatRuntime(storage=FakeStorage())

        def fail_chat(*, session_id, message, user_id=None):  # noqa: ARG001
            raise RuntimeError("simulated_sdk_failure")

        runtime.agent_sdk.is_available = lambda: True  # type: ignore[assignment]
        runtime.agent_sdk.chat = fail_chat  # type: ignore[assignment]
        out = runtime.chat(session_id="s-sdk-3", message="show me pipeline health metrics")
        self.assertEqual(out["debug"]["runtime"], "deterministic_fallback")
        warnings = out["debug"].get("warnings") or []
        self.assertIn("agent_sdk_runtime_failed_fallback_deterministic", warnings)
        self.assertIn("agent_sdk_error", out["debug"])

    def test_stream_chat_deterministic_emits_done(self):
        os.environ["RENTAL_AGENT_RUNTIME"] = "deterministic"
        runtime = AgentChatRuntime(storage=FakeStorage())
        events = list(runtime.stream_chat(session_id="s-stream-det", message="show me jobs"))
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[-1].get("event"), "done")
        response = events[-1].get("response") if isinstance(events[-1].get("response"), dict) else {}
        self.assertEqual(response.get("session_id"), "s-stream-det")

    def test_stream_chat_agent_sdk_unavailable_emits_warning_and_done(self):
        os.environ["RENTAL_AGENT_RUNTIME"] = "agent_sdk"
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = AgentChatRuntime(storage=FakeStorage())
        runtime.agent_sdk.is_available = lambda: False  # type: ignore[assignment]
        events = list(runtime.stream_chat(session_id="s-stream-sdk", message="show me pipeline health metrics"))
        names = [str(item.get("event") or "") for item in events if isinstance(item, dict)]
        self.assertIn("warning", names)
        self.assertEqual(names[-1], "done")

    def test_claude_tool_names_are_api_safe(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        defs = runtime._claude_tool_definitions(["tool.metrics_jobs", "tool.jobs_list"])  # noqa: SLF001
        self.assertEqual(len(defs), 2)
        for item in defs:
            name = item.get("name") or ""
            self.assertNotIn(".", name)
            self.assertLessEqual(len(name), 64)

    def test_agent_sdk_defaults_use_safe_permission_mode(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ.pop("RENTAL_AGENT_SDK_PERMISSION_MODE", None)
        runtime = ClaudeAgentSdkRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        self.assertEqual(runtime.sdk_permission_mode, "default")
        self.assertEqual(runtime.sdk_hooks_enabled, True)
        self.assertEqual(runtime.sdk_hooks_mode, "observability")
        self.assertEqual(runtime.sdk_structured_output_enabled, True)
        self.assertEqual(runtime.sdk_model_first_routing, True)
        self.assertEqual(runtime.sdk_continue_conversation_enabled, True)
        self.assertEqual(runtime.sdk_stream_passthrough_enabled, True)
        self.assertEqual(runtime.sdk_subagents_enabled, True)
        self.assertEqual(runtime.sdk_subagent_model, "haiku")
        self.assertEqual(runtime.sdk_native_skills_enabled, True)
        self.assertEqual(runtime.sdk_tools_preset, "claude_code")

    def test_agent_sdk_structured_output_schema_for_scoped_skill(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeAgentSdkRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        payload = runtime._sdk_structured_output_format(  # noqa: SLF001
            selected_skill_ids=["mvp_pipeline_observability"]
        )
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("type"), "json_schema")
        schema = payload.get("schema") if isinstance(payload.get("schema"), dict) else {}
        self.assertIn("answer", schema.get("properties") or {})

    def test_agent_sdk_model_first_scope_uses_enabled_skills(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeAgentSdkRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        selected, debug = runtime._select_enabled_tools_model_first()  # noqa: SLF001
        self.assertGreaterEqual(len(selected), 1)
        self.assertEqual(debug.get("selection_source"), "model_first")
        selected_ids = debug.get("selected_skill_ids") if isinstance(debug.get("selected_skill_ids"), list) else []
        self.assertGreaterEqual(len(selected_ids), 1)

    def test_agent_sdk_system_prompt_uses_preset_by_default(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeAgentSdkRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        prompt = runtime._sdk_system_prompt(selected_skill_ids=[])  # noqa: SLF001
        self.assertIsInstance(prompt, dict)
        self.assertEqual(prompt.get("type"), "preset")
        self.assertEqual(prompt.get("preset"), "claude_code")

    def test_agent_sdk_resume_options_enable_continue_conversation(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_AGENT_SDK_RESUME_ENABLED"] = "true"
        os.environ["RENTAL_AGENT_SDK_CONTINUE_CONVERSATION_ENABLED"] = "true"
        runtime = ClaudeAgentSdkRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        runtime._sdk_sessions["s-resume"] = "sdk-session-1"  # noqa: SLF001
        options = runtime._sdk_resume_options("s-resume")  # noqa: SLF001
        self.assertEqual(options.get("resume"), "sdk-session-1")
        self.assertEqual(options.get("continue_conversation"), True)

    def test_agent_sdk_subagents_build_from_selected_skills(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_AGENT_SDK_SUBAGENTS_ENABLED"] = "true"
        runtime = ClaudeAgentSdkRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        mapping = {
            "tool.metrics_jobs": "mcp__rental_ops__tool_metrics_jobs",
            "tool.jobs_list": "mcp__rental_ops__tool_jobs_list",
            "tool.job_get": "mcp__rental_ops__tool_job_get",
        }
        agents = runtime._sdk_skill_subagents(  # noqa: SLF001
            selected_skill_ids=["mvp_pipeline_observability"],
            internal_to_allowed_tool=mapping,
        )
        self.assertIsInstance(agents, dict)
        self.assertIn("mvp_pipeline_observability", agents)
        definition = agents.get("mvp_pipeline_observability")
        if isinstance(definition, dict):
            tools = definition.get("tools") if isinstance(definition.get("tools"), list) else []
        else:
            tools = list(getattr(definition, "tools", []) or [])
        self.assertIn("mcp__rental_ops__tool_metrics_jobs", tools)

    def test_agent_sdk_native_skills_sync_copies_skill_files_without_rewrite(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_AGENT_SDK_NATIVE_SKILLS_ENABLED"] = "true"
        os.environ["RENTAL_AGENT_SDK_NATIVE_SKILLS_SYNC_ENABLED"] = "true"
        os.environ["RENTAL_AGENT_SDK_NATIVE_SKILLS_SYNC_INTERVAL_SECONDS"] = "5"
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["RENTAL_AGENT_SDK_NATIVE_SKILLS_DIR"] = tmpdir
            runtime = ClaudeAgentSdkRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
            meta = runtime._sync_native_skills_to_project_dir(force=True)  # noqa: SLF001
            self.assertEqual(meta.get("ok"), True)
            source_path = BACKEND_DIR / "agent_skills" / "mvp_trip_research" / "SKILL.md"
            target_path = Path(tmpdir) / "mvp_trip_research" / "SKILL.md"
            self.assertTrue(target_path.exists())
            self.assertEqual(
                target_path.read_text(encoding="utf-8"),
                source_path.read_text(encoding="utf-8"),
            )

    def test_agent_sdk_disallowed_tools_allow_skill_when_native_skills_enabled(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_AGENT_SDK_NATIVE_SKILLS_ENABLED"] = "false"
        os.environ["RENTAL_AGENT_SDK_SUBAGENTS_ENABLED"] = "false"
        runtime = ClaudeAgentSdkRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        disallowed = runtime._sdk_disallowed_builtin_tools()  # noqa: SLF001
        self.assertIn("Skill", disallowed)
        self.assertIn("Task", disallowed)

        os.environ["RENTAL_AGENT_SDK_NATIVE_SKILLS_ENABLED"] = "true"
        os.environ["RENTAL_AGENT_SDK_SUBAGENTS_ENABLED"] = "true"
        runtime_native = ClaudeAgentSdkRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        disallowed_native = runtime_native._sdk_disallowed_builtin_tools()  # noqa: SLF001
        self.assertNotIn("Skill", disallowed_native)
        self.assertNotIn("Task", disallowed_native)

    def test_fanout_scaffold_defaults_disabled(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ.pop("RENTAL_AGENT_FANOUT_ENABLED", None)
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        state = runtime._fanout_debug_template()  # noqa: SLF001
        self.assertEqual(state["enabled"], False)
        self.assertEqual(state["attempted"], False)
        self.assertEqual(state["planned_branch_count"], 0)

    def test_fanout_scaffold_executes_parallel_branches_when_enabled(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_AGENT_FANOUT_ENABLED"] = "true"
        os.environ["RENTAL_AGENT_FANOUT_MAX_WORKERS"] = "2"
        os.environ["RENTAL_AGENT_FANOUT_TIMEOUT_MS"] = "4000"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        result = runtime._execute_fanout_plan(  # noqa: SLF001
            [
                {"branch_id": "b1", "tool": "tool.metrics_jobs", "input": {"limit": 5, "summary_limit": 5}},
                {"branch_id": "b2", "tool": "tool.jobs_list", "input": {"limit": 3}},
            ]
        )
        self.assertEqual(result["enabled"], True)
        self.assertEqual(result["attempted"], True)
        self.assertEqual(result["planned_branch_count"], 2)
        self.assertEqual(len(result["branches"]), 2)
        self.assertGreaterEqual(result["completed_branch_count"], 1)

    def test_fanout_plan_builds_for_composite_ops_prompt(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_AGENT_FANOUT_ENABLED"] = "true"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        plan = runtime._build_fanout_plan(  # noqa: SLF001
            message="Give me a pipeline health snapshot with jobs and search runs",
            tool_results=[
                {"tool": "tool.metrics_jobs", "result": {}},
                {"tool": "tool.search_create", "result": {"job_id": "queued-1"}},
            ],
        )
        tools = [str(item.get("tool") or "") for item in plan]
        self.assertEqual(len(plan), 2)
        self.assertIn("tool.jobs_list", tools)
        self.assertIn("tool.search_runs_list", tools)

    def test_fanout_plan_skips_non_composite_prompt(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_AGENT_FANOUT_ENABLED"] = "true"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        plan = runtime._build_fanout_plan(  # noqa: SLF001
            message="hello",
            tool_results=[],
        )
        self.assertEqual(plan, [])

    def test_fanout_enrichment_appends_ops_snapshot(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_AGENT_FANOUT_ENABLED"] = "true"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        warnings = []
        fanout_debug = {
            "attempted": True,
            "branches": [
                {
                    "branch_type": "ops_snapshot",
                    "tool": "tool.metrics_jobs",
                    "ok": True,
                    "result": {"summary": {"by_status": {"complete": 3, "failed": 1, "running": 2, "queued": 0}}},
                },
                {
                    "branch_type": "ops_snapshot",
                    "tool": "tool.jobs_list",
                    "ok": True,
                    "result": [
                        {"status": "complete"},
                        {"status": "running"},
                        {"status": "running"},
                    ],
                },
            ],
        }
        enriched = runtime._apply_fanout_enrichment("Base reply", fanout_debug, warnings)  # noqa: SLF001
        self.assertIn("Base reply", enriched)
        self.assertIn("Parallel ops snapshot:", enriched)
        self.assertIn("fanout_ops_snapshot_attached", warnings)

    def test_personality_tools_follow_planning_scope_without_prompt_heuristics(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_RAG_SKILL_ENABLED"] = "true"
        os.environ["RENTAL_RAG_SKILL_SCOPE"] = "planning"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        selected, debug = runtime._select_enabled_tools_model_first()  # noqa: SLF001
        self.assertIn("tool.personality_rag_context", selected)
        self.assertNotIn("tool.personality_rag_upsert", selected)
        self.assertEqual(debug["allow_personality_context"], True)
        self.assertEqual(debug["allow_personality_upsert"], False)
        self.assertEqual(debug["selection_source"], "model_first")

    def test_personality_tools_disabled_when_rag_disabled(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_RAG_SKILL_ENABLED"] = "false"
        os.environ["RENTAL_RAG_SKILL_SCOPE"] = "planning"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        selected, debug = runtime._select_enabled_tools_model_first()  # noqa: SLF001
        self.assertNotIn("tool.personality_rag_context", selected)
        self.assertNotIn("tool.personality_rag_upsert", selected)
        self.assertEqual(debug["allow_personality_context"], False)
        self.assertEqual(debug["allow_personality_upsert"], False)

    def test_personality_upsert_enabled_when_rag_scope_all(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_RAG_SKILL_ENABLED"] = "true"
        os.environ["RENTAL_RAG_SKILL_SCOPE"] = "all"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        selected, debug = runtime._select_enabled_tools_model_first()  # noqa: SLF001
        self.assertIn("tool.personality_rag_context", selected)
        self.assertIn("tool.personality_rag_upsert", selected)
        self.assertEqual(debug["allow_personality_upsert"], True)

    def test_reply_style_guardrail_strips_emojis(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_AGENT_CLEAN_RESPONSE_STYLE"] = "true"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        warnings = []
        cleaned = runtime._apply_reply_style_guardrails("Great trip plan 🌲✨\n\nLet’s go! 🚀", warnings)  # noqa: SLF001
        self.assertNotIn("🌲", cleaned)
        self.assertNotIn("✨", cleaned)
        self.assertNotIn("🚀", cleaned)
        self.assertIn("style_emoji_stripped", warnings)

    def test_reply_style_guardrail_can_be_disabled(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        os.environ["RENTAL_AGENT_CLEAN_RESPONSE_STYLE"] = "false"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        warnings = []
        raw = "Great trip plan 🌲"
        same = runtime._apply_reply_style_guardrails(raw, warnings)  # noqa: SLF001
        self.assertEqual(same, raw)
        self.assertEqual(warnings, [])

    def test_post_loop_finalizer_summarizes_jobs(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        reply = runtime._render_post_loop_finalizer(  # noqa: SLF001
            [
                {
                    "tool": "tool.search_create",
                    "result": {"job_id": "job-abc", "job_type": "search", "status": "queued"},
                },
                {
                    "tool": "tool.listing_ingest_url",
                    "result": {"job_id": "job-def", "job_type": "listing_ingest", "status": "queued"},
                },
            ]
        )
        self.assertIn("Completed actions:", reply)
        self.assertIn("job-abc", reply)
        self.assertIn("job-def", reply)

    def test_post_loop_finalizer_summarizes_listing_details(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        reply = runtime._render_post_loop_finalizer(  # noqa: SLF001
            [
                {
                    "tool": "tool.listing_get",
                    "result": {"listing_id": "l1", "title": "Cabin One", "location": "Keene, NY"},
                },
                {
                    "tool": "tool.listing_get",
                    "result": {"listing_id": "l2", "title": "Cabin Two", "location": "Keene, NY"},
                },
            ]
        )
        self.assertIn("grounded listing details", reply.lower())
        self.assertIn("l1", reply)
        self.assertIn("l2", reply)

    def test_chat_uses_post_loop_finalizer_when_no_final_text(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        defs = runtime._claude_tool_definitions(["tool.search_create"])  # noqa: SLF001
        tool_name = defs[0]["name"]
        responses = [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "u1",
                        "name": tool_name,
                        "input": {"location": "Keene, NY", "adults": 2},
                    }
                ]
            },
            {"content": []},
        ]

        def fake_call(*, messages, tools, system_prompt=None, max_tokens=None):  # noqa: ARG001
            if responses:
                return responses.pop(0)
            return {"content": []}

        runtime._call_claude = fake_call  # type: ignore[assignment]
        runtime._claude_tool_definitions = lambda tool_names: defs  # type: ignore[assignment]
        out = runtime.chat(session_id="s-finalizer", message="queue a search in Keene, NY")
        self.assertIn("Completed actions:", out["reply"])
        self.assertIn("queued-1", out["reply"])
        warnings = out["debug"].get("warnings") or []
        self.assertIn("claude_no_final_text", warnings)
        self.assertIn("post_loop_finalizer_used", warnings)

    def test_trip_research_link_grounding_rewrites_invalid_links(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        warnings = []
        reply = (
            "Try these:\n"
            "- https://www.tripadvisor.com/Attraction_Review-foo\n"
            "- https://example.com/not-grounded"
        )
        tool_results = [
            {
                "tool": "tool.trip_research_tavily",
                "result": {
                    "activities": [
                        {
                            "name": "Activity One",
                            "category": "things_to_do",
                            "rating": 4.8,
                            "rating_count": 1200,
                            "source_url": "https://www.tripadvisor.com/Attraction_Review-foo",
                        },
                        {
                            "name": "Activity Two",
                            "category": "tours",
                            "rating": 4.6,
                            "rating_count": 840,
                            "source_url": "https://www.tripadvisor.com/Attraction_Review-bar",
                        },
                    ]
                },
            }
        ]
        grounded = runtime._apply_post_tool_guardrails(reply, tool_results, warnings)  # noqa: SLF001
        self.assertIn("Here are top activity options based on grounded trip-research results", grounded)
        self.assertIn("https://www.tripadvisor.com/Attraction_Review-foo", grounded)
        self.assertIn("https://www.tripadvisor.com/Attraction_Review-bar", grounded)
        self.assertNotIn("https://example.com/not-grounded", grounded)
        self.assertIn("trip_research_link_grounding_rewrite", warnings)

    def test_trip_research_link_grounding_noop_when_links_are_grounded(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        warnings = []
        reply = "Use this link: https://www.tripadvisor.com/Attraction_Review-foo"
        tool_results = [
            {
                "tool": "tool.trip_research_tavily",
                "result": {
                    "activities": [
                        {
                            "name": "Activity One",
                            "source_url": "https://www.tripadvisor.com/Attraction_Review-foo",
                        }
                    ]
                },
            }
        ]
        grounded = runtime._apply_post_tool_guardrails(reply, tool_results, warnings)  # noqa: SLF001
        self.assertEqual(grounded, reply)
        self.assertEqual(warnings, [])

    def test_listing_compare_grounded_render_rewrites_reply(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        warnings = []
        original = "Free-form assistant prose that should be replaced."
        tool_results = [
            {
                "tool": "tool.listing_compare_create",
                "result": {
                    "status": "complete",
                    "summary": {
                        "summary": "Listing A has better value while Listing B has better location.",
                        "winner": {"listing_id": "listing_a", "reason": "Best overall balance."},
                        "sections": [
                            {
                                "section": "Location",
                                "winner_listing_id": "listing_b",
                                "notes": ["Closer to attractions", "More walkable"],
                            }
                        ],
                        "listing_notes": [
                            {
                                "listing_id": "listing_a",
                                "title": "Listing A",
                                "pros": ["Lower total price"],
                                "cons": ["Farther from downtown"],
                                "watchouts": ["Street noise"],
                            },
                            {
                                "listing_id": "listing_b",
                                "title": "Listing B",
                                "pros": ["Great location"],
                                "cons": ["Higher price"],
                                "watchouts": [],
                            },
                        ],
                        "tradeoffs": ["Pay more for better location"],
                        "confidence": "high",
                    },
                },
            }
        ]
        rendered = runtime._apply_post_tool_guardrails(original, tool_results, warnings)  # noqa: SLF001
        self.assertIn("## Listing Comparison", rendered)
        self.assertIn("## Winner", rendered)
        self.assertIn("## Category Breakdown", rendered)
        self.assertIn("## Listing Notes", rendered)
        self.assertIn("Confidence: high", rendered)
        self.assertIn("listing_compare_grounded_render", warnings)

    def test_listing_compare_guardrail_noop_without_summary(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        warnings = []
        original = "No rewrite expected."
        tool_results = [
            {"tool": "tool.listing_compare_create", "result": {"status": "queued", "job": {"job_id": "x"}}}
        ]
        rendered = runtime._apply_post_tool_guardrails(original, tool_results, warnings)  # noqa: SLF001
        self.assertEqual(rendered, original)
        self.assertEqual(warnings, [])

    def test_legacy_side_effect_heuristic_removed(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        self.assertFalse(hasattr(runtime, "_has_explicit_side_effect_intent"))

    def test_session_state_persists_and_rehydrates(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        storage = FakePersistentStorage()
        runtime_one = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=storage).local)
        runtime_one._sessions["persist-1"] = [  # noqa: SLF001
            {"role": "user", "content": "queue search"},
            {"role": "assistant", "content": "queued"},
        ]
        runtime_one._remember_session_scope(  # noqa: SLF001
            "persist-1",
            {
                "selected_skill_ids": ["mvp_search_and_listing_ops"],
                "selected_skill_names": ["Search And Listing Ops"],
                "selection_source": "keyword",
            },
        )
        runtime_one._remember_background_jobs(  # noqa: SLF001
            "persist-1",
            [
                {
                    "tool": "tool.search_create",
                    "result": {"job_id": "job-123", "job_type": "search", "status": "queued"},
                }
            ],
        )
        runtime_one._persist_session_state("persist-1")  # noqa: SLF001

        runtime_two = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=storage).local)
        runtime_two._load_session_state("persist-1")  # noqa: SLF001
        self.assertEqual(len(runtime_two._sessions.get("persist-1") or []), 2)  # noqa: SLF001
        restored_scope = runtime_two._session_scope.get("persist-1")  # noqa: SLF001
        self.assertIsInstance(restored_scope, dict)
        self.assertEqual(restored_scope.get("selected_skill_ids"), ["mvp_search_and_listing_ops"])
        restored_bg = runtime_two._session_background_jobs.get("persist-1")  # noqa: SLF001
        self.assertIsInstance(restored_bg, dict)
        self.assertEqual(str(restored_bg.get("latest_search_job_id") or "").strip(), "job-123")

    def test_background_search_job_id_is_promoted_to_run_id(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        search_job_id = "5dc1f9a1-71ce-4ac9-b8d8-f3ecf47e2e11"
        resolved_run_id = "6f2f50f3-5d89-4ced-a4e9-ebf42f0e75f3"
        runtime._remember_background_jobs(  # noqa: SLF001
            "s-bg",
            [
                {
                    "tool": "tool.search_create",
                    "result": {"job_id": search_job_id, "job_type": "search", "status": "queued"},
                }
            ],
        )

        def fake_execute_tool(name, args, tool_calls=None, warnings=None):
            if name == "tool.job_get":
                if isinstance(tool_calls, list):
                    tool_calls.append({"tool": "tool.job_get", "ok": True, "args": dict(args or {})})
                return {
                    "job_id": search_job_id,
                    "job_type": "search",
                    "status": "complete",
                    "result_ref": resolved_run_id,
                }
            return None

        runtime.orchestrator.execute_tool = fake_execute_tool  # type: ignore[assignment]
        runtime.orchestrator.get_tool_citation = lambda name, args: (  # type: ignore[assignment]
            f"/api/v1/jobs/{str((args or {}).get('job_id') or '').strip()}" if name == "tool.job_get" else ""
        )

        tool_calls = []
        warnings = []
        citations = []
        rewritten = runtime._rewrite_tool_input_from_background_context(  # noqa: SLF001
            session_id="s-bg",
            tool_name="tool.search_listings_list",
            tool_input={"run_id": search_job_id, "limit": 10},
            tool_calls=tool_calls,
            warnings=warnings,
            citations=citations,
        )
        self.assertEqual(rewritten.get("run_id"), resolved_run_id)
        self.assertIn("search_job_id_promoted_to_run_id", warnings)
        self.assertIn(f"/api/v1/jobs/{search_job_id}", citations)

    def test_background_latest_run_id_reused_when_missing(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        search_job_id = "14561ee7-c7bb-4f59-b0e8-463ba0ec8393"
        resolved_run_id = "89c6f12d-6f9e-467f-b2fb-07f31fd8ef10"
        runtime._remember_background_jobs(  # noqa: SLF001
            "s-bg-reuse",
            [
                {
                    "tool": "tool.search_create",
                    "result": {"job_id": search_job_id, "job_type": "search", "status": "queued"},
                }
            ],
        )

        def fake_execute_tool(name, args, tool_calls=None, warnings=None):
            if name == "tool.job_get":
                return {
                    "job_id": search_job_id,
                    "job_type": "search",
                    "status": "complete",
                    "result_ref": resolved_run_id,
                }
            return None

        runtime.orchestrator.execute_tool = fake_execute_tool  # type: ignore[assignment]
        runtime.orchestrator.get_tool_citation = lambda name, args: ""  # type: ignore[assignment]

        runtime._rewrite_tool_input_from_background_context(  # noqa: SLF001
            session_id="s-bg-reuse",
            tool_name="tool.search_run_get",
            tool_input={"run_id": search_job_id},
            tool_calls=[],
            warnings=[],
            citations=[],
        )
        warnings = []
        rewritten = runtime._rewrite_tool_input_from_background_context(  # noqa: SLF001
            session_id="s-bg-reuse",
            tool_name="tool.search_ingest_listings",
            tool_input={"listing_ids": ["12345678"]},
            tool_calls=[],
            warnings=warnings,
            citations=[],
        )
        self.assertEqual(rewritten.get("run_id"), resolved_run_id)
        self.assertIn("search_run_id_reused_from_session", warnings)

    def test_execution_claim_guardrail_rewrites_missing_ingest_execution(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        warnings = []
        rewritten = runtime._apply_execution_claim_guardrails(  # noqa: SLF001
            "Ingesting both now using run context.",
            [
                {
                    "tool": "tool.search_listings_list",
                    "result": [
                        {"id": "11111111", "title": "A"},
                        {"id": "22222222", "title": "B"},
                    ],
                }
            ],
            warnings,
        )
        self.assertIn("did not execute yet", rewritten.lower())
        self.assertIn("side_effect_claim_rewrite_missing_ingest_execution", warnings)

    def test_execution_claim_guardrail_noop_when_ingest_executed(self):
        os.environ["RENTAL_CLAUDE_API_KEY"] = "test-key"
        runtime = ClaudeSkillRuntime(orchestrator=AgentChatRuntime(storage=FakeStorage()).local)
        warnings = []
        original = "Ingesting both now using run context."
        rewritten = runtime._apply_execution_claim_guardrails(  # noqa: SLF001
            original,
            [
                {
                    "tool": "tool.search_ingest_listings",
                    "result": {"run_id": "r1", "jobs": [{"job_id": "job-1", "status": "queued"}]},
                }
            ],
            warnings,
        )
        self.assertEqual(rewritten, original)
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
