import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.personality_rag import PersonalityRagService
from services.storage import Storage


class PersonalityRagTests(unittest.TestCase):
    def _tmpdir(self) -> str:
        path = tempfile.mkdtemp(prefix="personality-rag-")
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
        return path

    def test_upload_txt_and_query_context(self):
        tmpdir = self._tmpdir()
        storage = Storage(db_path=os.path.join(tmpdir, "rag.db"))
        service = PersonalityRagService(storage)

        result = service.ingest_upload(
            user_id="u1",
            filename="trip.txt",
            mime_type="text/plain",
            raw_bytes=b"We loved hiking in the Adirondacks and cozy cabins near Keene.",
            title="Adirondacks notes",
            tags=["hiking", "couple"],
            metadata={"trip_id": "trip-1"},
        )
        self.assertEqual(result.get("duplicate"), False)
        self.assertGreater(int(result.get("chunk_count") or 0), 0)

        context = service.query_context(
            user_id="u1",
            query="cozy hiking cabins in adirondacks",
            limit=3,
            tags=["hiking"],
        )
        hits = context.get("hits") if isinstance(context, dict) else []
        self.assertTrue(hits)
        self.assertEqual(str(hits[0].get("citation", {}).get("title")), "Adirondacks notes")

    def test_duplicate_hash_detection(self):
        tmpdir = self._tmpdir()
        storage = Storage(db_path=os.path.join(tmpdir, "rag.db"))
        service = PersonalityRagService(storage)
        payload = b"Repeatable memory text for duplicate detection."

        first = service.ingest_upload(
            user_id="u1",
            filename="a.txt",
            mime_type="text/plain",
            raw_bytes=payload,
            title="First",
            tags=[],
            metadata={},
        )
        second = service.ingest_upload(
            user_id="u1",
            filename="b.txt",
            mime_type="text/plain",
            raw_bytes=payload,
            title="Second",
            tags=[],
            metadata={},
        )
        self.assertEqual(first.get("duplicate"), False)
        self.assertEqual(second.get("duplicate"), True)
        self.assertEqual(
            str(first.get("memory", {}).get("memory_id")),
            str(second.get("memory", {}).get("memory_id")),
        )

    def test_manual_upsert_and_delete(self):
        tmpdir = self._tmpdir()
        storage = Storage(db_path=os.path.join(tmpdir, "rag.db"))
        service = PersonalityRagService(storage)
        upsert = service.upsert_memory_text(
            user_id="u2",
            title="Manual trip summary",
            text="We prefer quiet cabins with lake access and short hiking trails.",
            tags=["quiet", "lake"],
            metadata={"source": "test"},
        )
        memory_id = str(upsert.get("memory", {}).get("memory_id") or "")
        self.assertTrue(memory_id)
        listed = service.list_memories(user_id="u2", limit=10)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].get("memory_id"), memory_id)

        deleted = service.delete_memory(memory_id=memory_id, user_id="u2")
        self.assertEqual(deleted, True)
        listed_after = service.list_memories(user_id="u2", limit=10)
        self.assertEqual(listed_after, [])


if __name__ == "__main__":
    unittest.main()
