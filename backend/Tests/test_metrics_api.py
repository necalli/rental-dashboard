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
class MetricsApiTests(unittest.TestCase):
    def test_metrics_endpoint_returns_metrics_and_summary(self) -> None:
        client = app_module.app.test_client()
        sample = [
            {
                "metric_id": "m1",
                "job_id": "j1",
                "job_type": "listing_ingest",
                "status": "complete",
                "metrics": {
                    "job_total_ms": 1000,
                    "capture_duration_ms": 600,
                    "parse_ms": 120,
                    "persist_ms": 90,
                    "capture_timings": {"navigation_ms": 400, "review_pagination_ms": 80},
                },
                "created_at": 1,
            }
        ]
        with patch.object(app_module.storage, "list_job_metrics", side_effect=[sample, sample]):
            response = client.get("/api/v1/metrics/jobs?limit=10&summary_limit=20")
        self.assertEqual(response.status_code, 200)
        data = response.get_json() or {}
        self.assertEqual(len(data.get("metrics") or []), 1)
        summary = data.get("summary") or {}
        self.assertEqual(summary.get("count"), 1)
        self.assertEqual(summary.get("averages", {}).get("avg_job_total_ms"), 1000.0)


if __name__ == "__main__":
    unittest.main()
