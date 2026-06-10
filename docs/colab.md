# Colab Setup

This guide runs the dashboard in Google Colab from the public GitHub repo.

## Recommended Flow

Use the one-cell bootstrap in:

- [Single Cell Colab Flow Example.txt](../Single%20Cell%20Colab%20Flow%20Example.txt)

The bootstrap:

1. Clones the dashboard from GitHub into `/content/rental-dashboard`.
2. Installs Python, Node, and Playwright dependencies.
3. Starts the Flask backend on port `5002`.
4. Starts two background workers.
5. Starts the Vite frontend on port `3000`.
6. Prints temporary Colab proxy URLs for the frontend and backend.

## Runtime Values To Set

At the top of the cell, set only values you need:

```python
DASHBOARD_REPO = "https://github.com/necalli/rental-dashboard.git"
DASHBOARD_BRANCH = "main"
RENTAL_LLM_API_KEY = ""
RENTAL_CLAUDE_API_KEY = ""
RENTAL_GEOAPIFY_API_KEY = ""
RENTAL_TAVILY_API_KEY = ""
```

Do not commit real keys or Colab-generated URLs.

## Service Order

1. System dependencies
2. Python dependencies and Playwright browsers
3. Backend API
4. Worker 1
5. Worker 2
6. Frontend with a Colab Vite proxy
7. Colab proxy URL printout

## Expected Ready State

The cell should end with:

- `FRONTEND_URL: https://...googleusercontent.com/...`
- `BACKEND_URL : https://...googleusercontent.com/...`
- `Backend health: {'service': 'rental-dashboard', 'status': 'ok'}`

Open the frontend URL.

## Notes

- This flow uses temporary Colab proxy URLs, not persistent ngrok hostnames.
- Backend and workers remain inside the Colab runtime.
- The generated `frontend/.env.local` contains `VITE_API_BASE_URL=.` so frontend `/api` calls route through Vite's local proxy to the backend.
- Runtime databases and captures live only in `/content/rental-dashboard/backend/data` and `/content/rental-dashboard/backend/raw` unless you explicitly copy them elsewhere.
- Performance testing options are opt-in. Set `RENTAL_WORKER_LISTING_CONCURRENCY=2` or `3` for bounded parallel listing ingest, and set `RENTAL_PLAYWRIGHT_REUSE_BROWSER=true` to reuse a worker-owned browser between captures. Keep these conservative in Colab to avoid memory pressure or site throttling.

## Photo Metadata Diagnostic

Run this in Colab after ingesting one or more listings to inspect whether Airbnb supplied useful photo captions, inferred room/area labels, and representative image buckets.

```python
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

LISTING_ID = ""  # optional: paste a listing id. Leave blank for the most recently updated listing.

db = Path(os.environ.get("RENTAL_DB_PATH", "/content/rental-dashboard/backend/data/rental_dashboard.db"))
if not db.exists():
    raise FileNotFoundError(f"Database not found: {db}")

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

if LISTING_ID:
    row = con.execute(
        "SELECT listing_id, title, payload_json FROM listings WHERE listing_id = ?",
        (str(LISTING_ID),),
    ).fetchone()
else:
    row = con.execute(
        "SELECT listing_id, title, payload_json FROM listings ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()

if not row:
    raise LookupError("No listing row found.")

payload = json.loads(row["payload_json"] or "{}")
photos = payload.get("photos") if isinstance(payload.get("photos"), list) else []
representatives = payload.get("representative_photos")
if not isinstance(representatives, dict):
    representatives = {}

def photo_url(photo):
    if isinstance(photo, str):
        return photo
    if isinstance(photo, dict):
        return (
            photo.get("url")
            or photo.get("baseUrl")
            or photo.get("originalPicture")
            or photo.get("picture")
            or photo.get("large")
            or photo.get("xlPicture")
            or photo.get("thumbnailUrl")
        )
    return None

def photo_area(photo):
    if isinstance(photo, dict):
        return photo.get("room_or_area") or photo.get("roomType") or photo.get("roomTitle") or "Unlabeled"
    return "Unlabeled"

def photo_caption(photo):
    if isinstance(photo, dict):
        return photo.get("localized_caption") or photo.get("localizedCaption") or photo.get("caption") or photo.get("title")
    return None

area_counts = Counter(photo_area(photo) for photo in photos if photo_url(photo))

print("db:", db)
print("listing_id:", row["listing_id"])
print("title:", row["title"])
print("photo_count:", len(photos))
print("area_counts:", dict(area_counts))
print("representative_areas:", list(representatives.keys()))

print("\nREPRESENTATIVE PHOTOS")
for area, photo in representatives.items():
    print(f"- {area}: {photo_url(photo)}")
    caption = photo_caption(photo)
    if caption:
        print(f"  caption: {caption[:180]}")

print("\nFIRST 20 PHOTOS")
for idx, photo in enumerate(photos[:20], 1):
    print(f"{idx}. area={photo_area(photo)} url={photo_url(photo)}")
    caption = photo_caption(photo)
    if caption:
        print(f"   caption: {caption[:180]}")
```
