import os
import shutil
import sqlite3
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

    def test_claim_tracks_lease_and_recovers_stale_job(self) -> None:
        tmpdir = self._tmpdir()
        db_path = os.path.join(tmpdir, "jobs.db")
        storage = Storage(db_path=db_path)
        created = storage.create_job("listing_ingest", {"url": "https://example.com/rooms/1"})

        claimed = storage.claim_next_job(worker_id="worker-a")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.get("job_id"), created.get("job_id"))
        self.assertEqual(claimed.get("claimed_by"), "worker-a")
        self.assertEqual(claimed.get("attempts"), 1)

        recovered = storage.recover_stale_jobs(stale_after_seconds=1, max_attempts=3)
        self.assertEqual(recovered, 0)

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ?",
                (1, 1, created["job_id"]),
            )
            conn.commit()

        recovered = storage.recover_stale_jobs(stale_after_seconds=1, max_attempts=3)
        self.assertEqual(recovered, 1)
        job = storage.get_job(created["job_id"])
        self.assertEqual(job.get("status"), "queued")
        self.assertIsNone(job.get("claimed_by"))

    def test_insert_counts_ignore_duplicates(self) -> None:
        tmpdir = self._tmpdir()
        db_path = os.path.join(tmpdir, "jobs.db")
        storage = Storage(db_path=db_path)

        first_reviews = storage.add_reviews(
            "listing-1",
            [
                {"id": "review-1", "text": "one"},
                {"id": "review-1", "text": "duplicate"},
            ],
        )
        second_reviews = storage.add_reviews("listing-1", [{"id": "review-1", "text": "again"}])
        self.assertEqual(first_reviews, 1)
        self.assertEqual(second_reviews, 0)

        run_id = storage.add_search_run({}, {}, [])
        first_listings = storage.add_search_listings(
            run_id,
            [
                {"id": "listing-1", "title": "one"},
                {"id": "listing-1", "title": "duplicate"},
            ],
        )
        second_listings = storage.add_search_listings(run_id, [{"id": "listing-1", "title": "again"}])
        self.assertEqual(first_listings, 1)
        self.assertEqual(second_listings, 0)


if __name__ == "__main__":
    unittest.main()
