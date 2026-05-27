import os
import requests


API_BASE = os.getenv("RENTAL_API_BASE", "http://localhost:5002").rstrip("/")
LISTING_ID = os.getenv("RENTAL_TEST_LISTING_ID", "").strip()


def main() -> None:
    if not LISTING_ID:
        raise SystemExit("Set RENTAL_TEST_LISTING_ID to validate a listing.")
    resp = requests.get(f"{API_BASE}/api/v1/listings/{LISTING_ID}")
    resp.raise_for_status()
    listing = resp.json().get("listing") or {}
    validation = listing.get("validation") or {}
    print("listing_id:", listing.get("id"))
    print("title:", listing.get("title"))
    print("validation:", validation)


if __name__ == "__main__":
    main()
