import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    import app as app_module
except Exception:
    app_module = None


@unittest.skipIf(app_module is None, "Flask app dependencies are unavailable")
class CompareCoverageGuardTests(unittest.TestCase):
    def test_compare_blocks_when_min_coverage_not_met(self) -> None:
        client = app_module.app.test_client()
        listing_a = {
            "id": "a",
            "listing_id": "a",
            "title": "Listing A",
            "reviews_captured_count": 3,
            "reviews_total_count": 212,
        }
        listing_b = {
            "id": "b",
            "listing_id": "b",
            "title": "Listing B",
            "reviews_captured_count": 24,
            "reviews_total_count": 40,
        }

        with patch.object(app_module.storage, "get_listing", side_effect=[listing_a, listing_b]), patch.object(
            app_module.storage,
            "list_reviews",
            side_effect=[[{"id": "r1"}] * 3, [{"id": "r2"}] * 24],
        ), patch.object(app_module.storage, "create_job") as mocked_create_job:
            response = client.post(
                "/api/v1/enrich/compare",
                json={
                    "listing_ids": ["a", "b"],
                    "sync": True,
                    "review_limit": 24,
                    "require_min_coverage": True,
                    "min_review_coverage": 0.5,
                },
            )

        self.assertEqual(response.status_code, 409)
        payload = response.get_json() or {}
        self.assertEqual(payload.get("code"), "comparison_coverage_blocked")
        self.assertEqual(mocked_create_job.call_count, 0)
        violations = payload.get("violations") or []
        self.assertGreaterEqual(len(violations), 1)
        self.assertEqual(violations[0].get("listing_id"), "a")

    def test_compare_job_payload_includes_coverage_policy_fields(self) -> None:
        client = app_module.app.test_client()
        listing_a = {
            "id": "a",
            "listing_id": "a",
            "title": "Listing A",
            "reviews_captured_count": 20,
            "reviews_total_count": 20,
        }
        listing_b = {
            "id": "b",
            "listing_id": "b",
            "title": "Listing B",
            "reviews_captured_count": 20,
            "reviews_total_count": 20,
        }
        fake_job = {"job_id": "cmp-1", "job_type": "listing_compare", "status": "queued"}

        with patch.object(app_module.storage, "get_listing", side_effect=[listing_a, listing_b]), patch.object(
            app_module.storage,
            "list_reviews",
            side_effect=[[{"id": "r1"}] * 20, [{"id": "r2"}] * 20],
        ), patch.object(app_module.storage, "get_enrichment_by_hash", return_value=None), patch.object(
            app_module.storage, "create_job", return_value=fake_job
        ) as mocked_create_job:
            response = client.post(
                "/api/v1/enrich/compare",
                json={
                    "listing_ids": ["a", "b"],
                    "sync": False,
                    "review_limit": 24,
                    "require_min_coverage": True,
                    "min_review_coverage": 0.6,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked_create_job.call_count, 1)
        _, job_payload = mocked_create_job.call_args.args
        self.assertEqual(job_payload.get("review_limit"), 24)
        self.assertEqual(job_payload.get("require_min_coverage"), True)
        self.assertEqual(job_payload.get("min_review_coverage"), 0.6)

    def test_compare_job_payload_includes_trip_memory_fields(self) -> None:
        client = app_module.app.test_client()
        listing_a = {"id": "a", "listing_id": "a", "title": "Listing A"}
        listing_b = {"id": "b", "listing_id": "b", "title": "Listing B"}
        fake_job = {"job_id": "cmp-2", "job_type": "listing_compare", "status": "queued"}

        with patch.object(app_module.storage, "get_listing", side_effect=[listing_a, listing_b]), patch.object(
            app_module.storage,
            "list_reviews",
            side_effect=[[], []],
        ), patch.object(app_module.app.config["personality_rag"], "query_context", return_value={
            "user_id": "rental-dashboard",
            "query": "quiet cabin",
            "hits": [
                {
                    "score": 0.9,
                    "text": "Prior trips favored quiet wooded stays.",
                    "citation": {"memory_id": "m1", "title": "Trip notes"},
                }
            ],
        }) as mocked_query, patch.object(
            app_module.storage, "get_enrichment_by_hash", return_value=None
        ), patch.object(
            app_module.storage, "create_job", return_value=fake_job
        ) as mocked_create_job:
            response = client.post(
                "/api/v1/enrich/compare",
                json={
                    "listing_ids": ["a", "b"],
                    "sync": False,
                    "use_personality_rag": True,
                    "user_id": "rental-dashboard",
                    "memory_focus": "quiet wooded stays",
                    "memory_limit": 4,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked_query.call_count, 1)
        _, job_payload = mocked_create_job.call_args.args
        self.assertEqual(job_payload.get("use_personality_rag"), True)
        self.assertEqual(job_payload.get("user_id"), "rental-dashboard")
        self.assertEqual(job_payload.get("memory_focus"), "quiet wooded stays")
        self.assertEqual(job_payload.get("memory_limit"), 4)


if __name__ == "__main__":
    unittest.main()
