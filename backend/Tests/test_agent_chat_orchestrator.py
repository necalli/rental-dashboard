import sys
import time
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.agent_chat import AgentChatOrchestrator


class FakeStorage:
    def __init__(self) -> None:
        self.created_jobs = []

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
        return [
            {"job_id": "job-1", "job_type": "search", "status": "complete"},
            {"job_id": "job-2", "job_type": "listing_ingest", "status": "running"},
        ]

    def get_job(self, job_id):
        if job_id == "11111111-1111-4111-8111-111111111111":
            return {
                "job_id": job_id,
                "job_type": "search",
                "status": "complete",
                "result_ref": "run-1",
                "error": None,
            }
        return None

    def list_search_runs(self, limit=50):
        return [
            {
                "run_id": "22222222-2222-4222-8222-222222222222",
                "params": {"location": "Keene, NY"},
                "result": {"listing_count": 18, "captured_url": "https://example.test"},
            }
        ]

    def list_search_listings(self, run_id, limit=200):
        if run_id != "22222222-2222-4222-8222-222222222222":
            return []
        return [
            {"id": "123456789", "title": "Cabin", "location": "Keene"},
            {"id": "987654321", "title": "A-frame", "location": "Lake Placid"},
        ]

    def list_listings(self, limit=50):
        return [
            {
                "id": "123456789",
                "title": "Cabin",
                "capture_stage": "reviews_lite_ready",
                "reviews_captured_count": 24,
                "reviews_total_count": 50,
                "pricing": {"price_total": "$900"},
            }
        ]

    def get_listing(self, listing_id):
        if listing_id == "123456789":
            return {
                "id": "123456789",
                "title": "Cabin",
                "location": {
                    "name": "Keene",
                    "details": {"subtitle": "Keene, New York, United States"},
                },
                "capture_stage": "reviews_lite_ready",
                "reviews_captured_count": 24,
                "reviews_total_count": 50,
                "pricing": {"price_total": "$900"},
            }
        if listing_id == "987654321":
            return {
                "id": "987654321",
                "title": "A-frame",
                "location": {
                    "name": "Lake Placid",
                    "details": {"subtitle": "Lake Placid, New York, United States"},
                },
                "capture_stage": "reviews_lite_ready",
                "reviews_captured_count": 3,
                "reviews_total_count": 50,
                "pricing": {"price_total": "$980"},
            }
        return None

    def list_reviews(self, listing_id, limit=200):
        if listing_id == "123456789":
            return [
                {"reviewer_name": "A", "rating": 5, "text": "Great stay."},
                {"reviewer_name": "B", "rating": 4, "text": "Nice host."},
            ]
        if listing_id == "987654321":
            return [
                {"reviewer_name": "C", "rating": 4, "text": "Solid overall."},
                {"reviewer_name": "D", "rating": 4, "text": "Good location."},
                {"reviewer_name": "E", "rating": 3, "text": "Small room."},
            ]
        return []

    def get_latest_enrichment(self, listing_id, kind, model=None, prompt_version=None):
        if listing_id == "123456789" and kind == "listing_summary":
            return {"summary": "Strong value listing with good location and reviews."}
        return None

    def create_job(self, job_type, payload):
        job_id = f"queued-{len(self.created_jobs) + 1}"
        job = {"job_id": job_id, "job_type": job_type, "status": "queued", "payload": payload}
        self.created_jobs.append(job)
        return job


class FakeTavilyClient:
    enabled = True

    def search(
        self,
        query,
        *,
        max_results=10,
        search_depth="basic",
        include_domains=None,
        exclude_domains=None,
    ):
        _ = (query, max_results, search_depth, include_domains, exclude_domains)
        return {
            "results": [
                {
                    "title": "Top hiking trail - Tripadvisor 4.8/5 (1,240 reviews)",
                    "content": "Best outdoor trail in town. $35",
                    "url": "https://www.tripadvisor.com/Attraction_Review-hike",
                },
                {
                    "title": "Downtown food tour - Tripadvisor 4.5 stars 640 reviews",
                    "content": "Guided food walk",
                    "url": "https://www.tripadvisor.com/Attraction_Review-food-tour",
                },
                {
                    "title": "Generic blog result",
                    "content": "Not tripadvisor",
                    "url": "https://example.com/activities",
                },
            ],
            "warning": None,
            "error": None,
        }


class AgentChatOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.storage = FakeStorage()
        self.orchestrator = AgentChatOrchestrator(
            storage=self.storage,
            tavily_client=FakeTavilyClient(),
        )

    def test_pipeline_health_intent(self):
        out = self.orchestrator.chat(session_id="s1", message="show pipeline health metrics")
        self.assertEqual(out["debug"]["intent"], "pipeline_health")
        self.assertIn("Pipeline health snapshot", out["reply"])
        self.assertEqual(out["debug"]["tool_calls"][0]["tool"], "tool.metrics_jobs")

    def test_jobs_list_intent(self):
        out = self.orchestrator.chat(session_id="s2", message="show jobs")
        self.assertEqual(out["debug"]["intent"], "jobs_list")
        self.assertIn("Recent jobs", out["reply"])
        self.assertEqual(out["debug"]["tool_calls"][0]["tool"], "tool.jobs_list")

    def test_listing_get_by_url(self):
        out = self.orchestrator.chat(
            session_id="s3",
            message="show listing details https://www.airbnb.com/rooms/123456789",
        )
        self.assertEqual(out["debug"]["intent"], "listing_get")
        self.assertIn("Listing detail", out["reply"])
        self.assertIn("Location: Keene, New York, United States", out["reply"])
        self.assertEqual(out["debug"]["entities"]["listing_id"], "123456789")

    def test_reviews_intent(self):
        out = self.orchestrator.chat(session_id="s4", message="show reviews for listing 123456789")
        self.assertEqual(out["debug"]["intent"], "reviews_list")
        self.assertIn("Recent reviews", out["reply"])
        self.assertEqual(out["debug"]["tool_calls"][0]["tool"], "tool.reviews_list")

    def test_listing_summary_intent(self):
        out = self.orchestrator.chat(session_id="s5", message="listing summary 123456789")
        self.assertEqual(out["debug"]["intent"], "listing_summary_get")
        self.assertIn("Stored listing summary", out["reply"])

    def test_queue_search_intent(self):
        out = self.orchestrator.chat(session_id="s6", message="queue search in Keene, NY")
        self.assertEqual(out["debug"]["intent"], "search_create")
        self.assertIn("Queued search job", out["reply"])
        self.assertEqual(self.storage.created_jobs[-1]["job_type"], "search")

    def test_queue_ingest_url_intent(self):
        out = self.orchestrator.chat(
            session_id="s7",
            message="ingest this listing https://www.airbnb.com/rooms/123456789",
        )
        self.assertEqual(out["debug"]["intent"], "listing_ingest_url")
        self.assertIn("Queued listing ingest job", out["reply"])
        self.assertEqual(self.storage.created_jobs[-1]["job_type"], "listing_ingest")

    def test_tool_timeout_guardrail(self):
        class SlowStorage(FakeStorage):
            def list_jobs(self, limit=50):
                time.sleep(0.2)
                return super().list_jobs(limit=limit)

        orchestrator = AgentChatOrchestrator(
            storage=SlowStorage(),
            tool_timeout_ms_default=5,
            tool_timeout_overrides={"tool.jobs_list": 5},
            tool_max_workers=1,
        )
        out = orchestrator.chat(session_id="timeout-case", message="show jobs")
        self.assertEqual(out["debug"]["intent"], "jobs_list")
        self.assertEqual(out["debug"]["guardrails"]["degraded"], True)
        self.assertEqual(out["debug"]["guardrails"]["tool_timeout_count"], 1)
        self.assertEqual(out["debug"]["tool_calls"][0]["tool"], "tool.jobs_list")
        self.assertEqual(out["debug"]["tool_calls"][0]["timeout"], True)
        self.assertIn("I couldn't load jobs right now.", out["reply"])

    def test_tool_failure_guardrail(self):
        class BrokenStorage(FakeStorage):
            def list_jobs(self, limit=50):
                raise RuntimeError("db unavailable")

        orchestrator = AgentChatOrchestrator(storage=BrokenStorage())
        out = orchestrator.chat(session_id="failure-case", message="show jobs")
        self.assertEqual(out["debug"]["intent"], "jobs_list")
        self.assertEqual(out["debug"]["guardrails"]["degraded"], True)
        self.assertEqual(out["debug"]["guardrails"]["tool_failure_count"], 1)
        self.assertEqual(out["debug"]["guardrails"]["tool_timeout_count"], 0)
        self.assertEqual(out["debug"]["tool_calls"][0]["ok"], False)
        self.assertIn("I couldn't load jobs right now.", out["reply"])

    def test_trip_research_tool(self):
        tool_calls = []
        warnings = []
        result = self.orchestrator.execute_tool(
            "tool.trip_research_tavily",
            {"location": "Woodstock, NY", "max_results": 5, "focus": ["outdoors"]},
            tool_calls=tool_calls,
            warnings=warnings,
        )
        activities = result.get("activities") if isinstance(result, dict) else []
        self.assertEqual(len(activities), 2)
        self.assertEqual(activities[0]["name"], "Top hiking trail")
        self.assertEqual(activities[0]["rating"], 4.8)
        self.assertEqual(activities[0]["rating_count"], 1240)
        self.assertEqual(activities[0]["category"], "outdoors")
        self.assertEqual(tool_calls[0]["tool"], "tool.trip_research_tavily")
        self.assertEqual(tool_calls[0]["ok"], True)

    def test_trip_research_intent_route(self):
        out = self.orchestrator.chat(
            session_id="trip-1",
            message="find top things to do in Woodstock, NY on Tripadvisor",
        )
        self.assertEqual(out["debug"]["intent"], "trip_research")
        self.assertIn("Top activity candidates for `Woodstock, NY`", out["reply"])

    def test_listing_compare_tool_queued(self):
        tool_calls = []
        warnings = []
        result = self.orchestrator.execute_tool(
            "tool.listing_compare_create",
            {
                "listing_ids": ["123456789", "987654321"],
                "sync": False,
                "require_min_coverage": False,
            },
            tool_calls=tool_calls,
            warnings=warnings,
        )
        self.assertEqual(result.get("status"), "queued")
        self.assertEqual(self.storage.created_jobs[-1]["job_type"], "listing_compare")
        self.assertEqual(tool_calls[0]["tool"], "tool.listing_compare_create")
        self.assertEqual(tool_calls[0]["ok"], True)

    def test_listing_compare_tool_coverage_blocked(self):
        tool_calls = []
        warnings = []
        result = self.orchestrator.execute_tool(
            "tool.listing_compare_create",
            {
                "listing_ids": ["123456789", "987654321"],
                "sync": False,
                "require_min_coverage": True,
                "min_review_coverage": 0.5,
                "review_limit": 24,
            },
            tool_calls=tool_calls,
            warnings=warnings,
        )
        self.assertEqual(result.get("code"), "comparison_coverage_blocked")
        self.assertTrue(result.get("violations"))


if __name__ == "__main__":
    unittest.main()
