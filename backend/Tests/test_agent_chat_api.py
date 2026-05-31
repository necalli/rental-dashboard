import sys
import unittest
import os
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ["RENTAL_AGENT_RUNTIME"] = "deterministic"

try:
    import app as app_module
except Exception:
    app_module = None


@unittest.skipIf(app_module is None, "Flask app dependencies are unavailable")
class AgentChatApiTests(unittest.TestCase):
    def test_agent_chat_requires_message(self) -> None:
        client = app_module.app.test_client()
        response = client.post("/api/v1/agent/chat", json={"session_id": "abc"})
        self.assertEqual(response.status_code, 400)
        payload = response.get_json() or {}
        self.assertIn("error", payload)

    def test_agent_chat_pipeline_health_path(self) -> None:
        client = app_module.app.test_client()
        sample_metrics = [
            {
                "metric_id": "m1",
                "job_id": "j1",
                "job_type": "listing_ingest",
                "status": "complete",
                "metrics": {
                    "job_total_ms": 1000,
                    "capture_duration_ms": 600,
                    "parse_ms": 100,
                    "persist_ms": 90,
                    "capture_timings": {"navigation_ms": 400},
                    "parser_drift": {"drift_detected": False},
                },
                "created_at": 1,
            }
        ]
        with patch.object(
            app_module.storage,
            "list_job_metrics",
            side_effect=[sample_metrics, sample_metrics],
        ):
            response = client.post(
                "/api/v1/agent/chat",
                json={"session_id": "s1", "message": "show me pipeline health metrics"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json() or {}
        self.assertEqual(data.get("session_id"), "s1")
        self.assertIsInstance(data.get("trace_id"), str)
        self.assertIn("Pipeline health snapshot", data.get("reply") or "")
        self.assertEqual(data.get("debug", {}).get("intent"), "pipeline_health")
        self.assertIn("guardrails", data.get("debug", {}))
        tool_calls = data.get("debug", {}).get("tool_calls") or []
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].get("tool"), "tool.metrics_jobs")

    def test_agent_chat_jobs_list_path(self) -> None:
        client = app_module.app.test_client()
        sample_jobs = [
            {"job_id": "j1", "job_type": "search", "status": "complete"},
            {"job_id": "j2", "job_type": "listing_ingest", "status": "running"},
        ]
        with patch.object(app_module.storage, "list_jobs", return_value=sample_jobs):
            response = client.post(
                "/api/v1/agent/chat",
                json={"session_id": "s2", "message": "show jobs"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json() or {}
        self.assertEqual(data.get("debug", {}).get("intent"), "jobs_list")
        self.assertIn("Recent jobs", data.get("reply") or "")
        tool_calls = data.get("debug", {}).get("tool_calls") or []
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].get("tool"), "tool.jobs_list")

    def test_agent_search_assist_queues_search_only_job(self) -> None:
        client = app_module.app.test_client()
        fake_job = {"job_id": "search-1", "job_type": "search", "status": "queued"}
        with (
            patch("services.search_assist.suggest_locations", return_value=[]),
            patch.object(app_module.storage, "create_job", return_value=fake_job) as mocked_create_job,
        ):
            response = client.post(
                "/api/v1/agent/search-assist",
                json={
                    "prompt": (
                        "Find a private room near Phoenicia July 18-25, 2026 "
                        "for 4 adults with a dog, hot tub, 2 bedrooms, under $400"
                    )
                },
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json() or {}
        self.assertEqual(data.get("status"), "queued")
        self.assertEqual(data.get("job"), fake_job)
        self.assertEqual(mocked_create_job.call_count, 1)
        job_type, payload = mocked_create_job.call_args.args
        self.assertEqual(job_type, "search")
        self.assertEqual(payload.get("location"), "Phoenicia")
        self.assertEqual(payload.get("check_in"), "2026-07-18")
        self.assertEqual(payload.get("check_out"), "2026-07-25")
        self.assertEqual(payload.get("adults"), 4)
        self.assertEqual(payload.get("pets"), 1)
        self.assertEqual(payload.get("max_price_nightly"), 400)
        self.assertEqual(payload.get("max_price"), 2800)
        self.assertEqual((payload.get("price_filter") or {}).get("basis"), "nightly")
        self.assertEqual(payload.get("room_type"), "Private room")
        self.assertIn("hot tub", payload.get("amenities") or [])
        self.assertEqual(payload.get("min_bedrooms"), 2)

    def test_agent_search_assist_requires_destination(self) -> None:
        client = app_module.app.test_client()
        with (
            patch("services.search_assist.suggest_locations", return_value=[]),
            patch.object(app_module.storage, "create_job") as mocked_create_job,
        ):
            response = client.post(
                "/api/v1/agent/search-assist",
                json={"prompt": "Find something nice with a hot tub under $300"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json() or {}
        self.assertEqual(data.get("status"), "clarification_needed")
        self.assertIn("destination", data.get("message") or "")
        self.assertEqual(mocked_create_job.call_count, 0)

    def test_agent_search_assist_rejects_irrelevant_model_classification(self) -> None:
        client = app_module.app.test_client()
        model_payload = {
            "status": "rejected",
            "intent": {},
            "message": "This bar only runs rental listing searches.",
            "unsupported_or_uncertain_requests": ["not a listing search"],
            "confidence": 0.9,
        }
        with (
            patch.object(app_module.agent_chat.claude, "api_key", "test-key"),
            patch.object(app_module.agent_chat.claude, "parse_search_assist_prompt", return_value=model_payload),
            patch.object(app_module.storage, "create_job") as mocked_create_job,
        ):
            response = client.post(
                "/api/v1/agent/search-assist",
                json={"prompt": "write a poem about breakfast"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json() or {}
        self.assertEqual(data.get("status"), "rejected")
        self.assertEqual(mocked_create_job.call_count, 0)


if __name__ == "__main__":
    unittest.main()
