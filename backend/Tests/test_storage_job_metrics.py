import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.storage import Storage

try:
    import app as app_module
except Exception:
    app_module = None


class StorageJobMetricsTests(unittest.TestCase):
    def _tmpdir(self) -> str:
        path = tempfile.mkdtemp(prefix="storage-metrics-")
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
        return path

    def test_add_and_list_job_metrics(self) -> None:
        tmpdir = self._tmpdir()
        db_path = os.path.join(tmpdir, "metrics.db")
        storage = Storage(db_path=db_path)
        storage.add_job_metric(
            "job-1",
            "listing_ingest",
            "complete",
            {"job_total_ms": 1200, "capture_timings": {"navigation_ms": 800}},
        )
        storage.add_job_metric(
            "job-2",
            "search",
            "failed",
            {"job_total_ms": 900},
        )

        all_items = storage.list_job_metrics(limit=10)
        self.assertEqual(len(all_items), 2)
        self.assertEqual({item.get("job_id") for item in all_items}, {"job-1", "job-2"})

        complete_items = storage.list_job_metrics(limit=10, status="complete")
        self.assertEqual(len(complete_items), 1)
        self.assertEqual(complete_items[0]["job_id"], "job-1")

        ingest_items = storage.list_job_metrics(limit=10, job_type="listing_ingest")
        self.assertEqual(len(ingest_items), 1)
        self.assertEqual(ingest_items[0]["status"], "complete")


@unittest.skipIf(app_module is None, "Flask app dependencies are unavailable")
class MetricsSummaryTests(unittest.TestCase):
    def test_summarize_job_metrics(self) -> None:
        items = [
            {
                "job_type": "listing_ingest",
                "status": "complete",
                "metrics": {
                    "job_total_ms": 1000,
                    "capture_duration_ms": 700,
                    "parse_ms": 100,
                    "persist_ms": 120,
                    "capture_timings": {"navigation_ms": 500, "review_pagination_ms": 150},
                },
            },
            {
                "job_type": "listing_ingest",
                "status": "failed",
                "metrics": {
                    "job_total_ms": 1400,
                    "capture_duration_ms": 900,
                    "parse_ms": 110,
                    "persist_ms": 0,
                    "capture_timings": {"navigation_ms": 650, "review_pagination_ms": 0},
                },
            },
        ]
        summary = app_module._summarize_job_metrics(items)
        self.assertEqual(summary.get("count"), 2)
        self.assertEqual(summary.get("by_status", {}).get("complete"), 1)
        self.assertEqual(summary.get("by_status", {}).get("failed"), 1)
        ingest = summary.get("by_job_type", {}).get("listing_ingest") or {}
        self.assertEqual(ingest.get("count"), 2)
        self.assertEqual(ingest.get("avg_job_total_ms"), 1200.0)
        self.assertEqual(summary.get("averages", {}).get("avg_navigation_ms"), 575.0)


if __name__ == "__main__":
    unittest.main()
