import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.storage import Storage


class StorageClaimingTests(unittest.TestCase):
    def _tmpdir(self) -> str:
        path = tempfile.mkdtemp(prefix="storage-claiming-")
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
        return path

    def test_claim_next_job_single_winner_under_contention(self) -> None:
        tmpdir = self._tmpdir()
        db_path = os.path.join(tmpdir, "jobs.db")
        creator = Storage(db_path=db_path)
        created = creator.create_job("listing_ingest", {"url": "https://example.com/rooms/1"})
        self.assertEqual(created.get("status"), "queued")

        barrier = threading.Barrier(3)
        results = [None, None]
        failures = []

        def _claim(slot: int) -> None:
            try:
                storage = Storage(db_path=db_path)
                barrier.wait(timeout=2.0)
                results[slot] = storage.claim_next_job()
            except Exception as exc:  # pragma: no cover
                failures.append(exc)

        t1 = threading.Thread(target=_claim, args=(0,))
        t2 = threading.Thread(target=_claim, args=(1,))
        t1.start()
        t2.start()
        barrier.wait(timeout=2.0)
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        self.assertEqual(failures, [])
        claimed = [item for item in results if item]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].get("status"), "running")
        self.assertEqual(claimed[0].get("job_id"), created.get("job_id"))

    def test_get_job_status_counts(self) -> None:
        tmpdir = self._tmpdir()
        db_path = os.path.join(tmpdir, "jobs.db")
        storage = Storage(db_path=db_path)
        one = storage.create_job("search", {"location": "Keene, NY"})
        two = storage.create_job("listing_ingest", {"url": "https://example.com/rooms/2"})
        three = storage.create_job("listing_ingest", {"url": "https://example.com/rooms/3"})
        storage.update_job(one["job_id"], status="complete", result_ref="run-1")
        storage.update_job(two["job_id"], status="failed", error="boom")
        storage.update_job(three["job_id"], status="running")

        counts = storage.get_job_status_counts()
        self.assertEqual(counts.get("queued"), 0)
        self.assertEqual(counts.get("running"), 1)
        self.assertEqual(counts.get("complete"), 1)
        self.assertEqual(counts.get("failed"), 1)
        self.assertEqual(counts.get("total"), 3)


if __name__ == "__main__":
    unittest.main()
