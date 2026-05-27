---
name: Search And Listing Ops
description: Inspect search runs and ingested listings, retrieve listing details/reviews/summaries, and queue explicit search or listing-ingest jobs. Use when requests mention run IDs, listing IDs, search results, ingestion/capture, or queueing search/listing workflows.
---

Run operational workflows over search runs and ingested listings.

Rules:
1. For read-only questions, prefer listing/search retrieval tools.
2. For side-effecting actions (`tool.search_create`, `tool.listing_ingest_url`), require explicit user intent (`queue`, `ingest`, `capture`, `run`).
3. When ingesting listings that came from a search run, prefer `tool.search_ingest_listings` with `run_id + listing_ids` so check-in/out and guest params are preserved for pricing context.
4. If the prompt is ambiguous between read-only detail retrieval and ingest/capture, ask one clarifying follow-up before side effects.
5. For "show details" requests, default to read-only retrieval from existing runs/listings unless user explicitly asks to ingest.
6. If required ids or URLs are missing, ask for them.
