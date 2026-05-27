import os
import json
import sqlite3


DB_PATH = os.getenv("RENTAL_DB_PATH", "backend/data/rental_dashboard.db")
LIMIT = int(os.getenv("RENTAL_TEST_SEARCH_LIMIT", "5"))


def main() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        run = conn.execute(
            "SELECT run_id, result_json, created_at FROM search_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not run:
            raise SystemExit("No search runs found. Run a search job first.")
        run_id = run[0]
        print("latest_run_id:", run_id)
        listings = conn.execute(
            "SELECT payload_json FROM search_listings WHERE run_id = ? LIMIT ?",
            (run_id, LIMIT),
        ).fetchall()
        print("listing_samples:", len(listings))
        parsed = [json.loads(row[0] or "{}") for row in listings]
        for item in parsed:
            print(json.dumps(item, ensure_ascii=False))
        if parsed:
            scores = [
                (item.get("validation") or {}).get("quality_score", 0.0)
                for item in parsed
            ]
            avg_score = round(sum(scores) / max(1, len(scores)), 2)
            error_count = sum(1 for item in parsed if (item.get("validation") or {}).get("errors"))
            print("avg_quality_score:", avg_score)
            print("samples_with_errors:", error_count)


if __name__ == "__main__":
    main()
