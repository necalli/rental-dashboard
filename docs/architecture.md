# Architecture

## Core flow
1) Ingest (listing URL or search request)
2) Playwright capture (network responses + raw payloads)
3) Parse and normalize into canonical schema
4) Persist normalized entities + raw payload pointers
5) Enrich with LLM summaries and review themes
6) Serve API to UI for comparisons and exploration

## Canonical schema (draft)

### Listing
- id
- source
- url
- title
- property_type
- location: { city, region, country, lat, lng }
- capacity: { guests, bedrooms, beds, baths }
- description
- house_rules
- cancellation_policy
- safety_notes[]
- amenities[]: { group, items[] }
- photos[]
- host: { id, name, superhost, response_rate, response_time, rating, review_count }
- pricing: { currency, nightly, total, fees[] }
- availability: { check_in, check_out, is_available }
- reviews_summary: { overall_rating, count, category_ratings[], distribution[] }
- captured_at
- raw_payload_refs[]

### Review
- id
- listing_id
- rating
- date
- language
- text
- reviewer: { name, location }
- source_url
- captured_at
- raw_payload_ref

### SearchRun
- id
- params: { location, dates, guests, price, filters }
- results: [ { listing_id, rank } ]
- captured_at
- raw_payload_refs[]

## Storage
- Raw payloads kept long-term on disk.
- Normalized data stored in SQLite (initially) with paths to raw payloads.

## Observability
- Schema diff reports against recent payloads.
- Completeness scoring per listing and per parse run.
- Error logs for parser drift.
