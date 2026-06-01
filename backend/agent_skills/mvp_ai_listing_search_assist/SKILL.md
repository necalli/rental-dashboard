---
name: AI Listing Search Assist
description: Convert natural-language rental listing search-bar prompts into structured Airbnb-style search criteria. Use only for the constrained AI search bar workflow, including prompt classification, hard filter extraction, soft preference capture, unsupported preference reporting, and clarification/rejection decisions.
---

Convert one user search-bar prompt into one JSON object. Return JSON only, with no markdown or extra prose.

Output schema:

```json
{
  "status": "ready | clarification_needed | rejected",
  "intent": {
    "location": "string",
    "check_in": "YYYY-MM-DD",
    "check_out": "YYYY-MM-DD",
    "adults": 1,
    "children": 0,
    "infants": 0,
    "pets": 0,
    "min_price": null,
    "max_price": null,
    "min_price_nightly": null,
    "max_price_nightly": null,
    "min_price_total": null,
    "max_price_total": null,
    "price_basis": "nightly | total | unknown",
    "room_type": "string",
    "amenities": ["string"],
    "flexible_cancellation": false,
    "min_bedrooms": null,
    "min_beds": null,
    "min_bathrooms": null
  },
  "soft_preferences": ["string"],
  "unsupported_or_uncertain_requests": ["string"],
  "message": "string",
  "confidence": 0.0
}
```

Rules:

1. Use `ready` only when the prompt is a rental listing search and has a destination. Dates are optional unless the user mentions date intent but gives an incomplete or impossible range.
2. Use `clarification_needed` when the prompt is search-related but missing a destination, has ambiguous location intent, has incomplete dates, or has conflicting requirements.
3. Use `rejected` for non-search requests, analysis/comparison requests, listing ingestion/capture requests, summarization requests, document requests, or general chat.
4. Do not invent dates. If a date range omits the year, use the current year unless that range has already passed; then use next year.
5. Default adults to `1` and children, infants, and pets to `0` only when the prompt is otherwise a valid listing search.
6. Put direct Airbnb-style constraints in `intent`: destination, dates, guests, pets, price range, bedrooms, beds, bathrooms, room type, supported amenities, and flexible cancellation.
7. Put preferences that should influence later ranking or explanation in `soft_preferences`, not in `intent`. Examples: secluded, romantic, quiet, scenic, walkable, near hiking, remote-work friendly, family friendly, close to restaurants, good for groups.
8. Put unsupported, vague, or uncertain requests in `unsupported_or_uncertain_requests`. Do not silently drop user preferences.
9. Keep amenities as short lowercase labels, such as `hot tub`, `wifi`, `pool`, `kitchen`, `free parking`, `washer`, `dryer`, `air conditioning`, `fireplace`, `dedicated workspace`, `waterfront`, `beachfront`, `ev charger`, `crib`, or `gym`.
10. Use room type only for Airbnb-supported place types: `Entire home/apt`, `Private room`, or `Shared room`. Treat property words like cabin, cottage, house, chalet, bungalow, or apartment as soft preferences unless the backend schema explicitly supports them.
11. Price is optional. Do not ask for price clarification when the user did not provide any price amount.
12. For prices, preserve the user's basis:
    - If the user says "per night", "nightly", "/night", or "a night", use `min_price_nightly` or `max_price_nightly` and set `price_basis` to `nightly`.
    - If the user says "total", "all in", "for the stay", or "for the week", use `min_price_total` or `max_price_total` and set `price_basis` to `total`.
    - If the user gives a price without a basis, use `clarification_needed`, set `price_basis` to `unknown`, preserve the amount in `max_price` or `min_price`, and ask whether the amount is nightly or total.
    - Do not put nightly values directly into `min_price` or `max_price`; those legacy fields are total-style URL caps.
13. Mention in `message` which important preferences were not applied as hard filters and whether a price basis needs clarification.
14. Keep `confidence` between `0` and `1`.

Examples:

User: `Find a pet-friendly cabin near Phoenicia July 18-25 for 4 adults under $400 with a hot tub and wifi`

Output:

```json
{
  "status": "ready",
  "intent": {
    "location": "Phoenicia",
    "check_in": "2026-07-18",
    "check_out": "2026-07-25",
    "adults": 4,
    "children": 0,
    "infants": 0,
    "pets": 1,
    "min_price": null,
    "max_price": null,
    "min_price_nightly": null,
    "max_price_nightly": 400,
    "min_price_total": null,
    "max_price_total": null,
    "price_basis": "nightly",
    "room_type": null,
    "amenities": ["hot tub", "wifi"],
    "flexible_cancellation": false,
    "min_bedrooms": null,
    "min_beds": null,
    "min_bathrooms": null
  },
  "soft_preferences": ["cabin"],
  "unsupported_or_uncertain_requests": [],
  "message": "I can search Phoenicia with dates, guests, pets, nightly price, hot tub, and wifi. I will keep cabin as a preference.",
  "confidence": 0.9
}
```

User: `Find a cozy cabin for 4 with a hot tub`

Output:

```json
{
  "status": "clarification_needed",
  "intent": {
    "adults": 4,
    "children": 0,
    "infants": 0,
    "pets": 0,
    "amenities": ["hot tub"]
  },
  "soft_preferences": ["cozy", "cabin"],
  "unsupported_or_uncertain_requests": [],
  "message": "What destination should I search?",
  "confidence": 0.55
}
```

User: `Home in Keene NY for 6 people, pet friendly, from 7-20-26 to 7-27-26. Max price is $2500`

Output:

```json
{
  "status": "clarification_needed",
  "intent": {
    "location": "Keene NY",
    "check_in": "2026-07-20",
    "check_out": "2026-07-27",
    "adults": 6,
    "children": 0,
    "infants": 0,
    "pets": 1,
    "max_price": 2500,
    "price_basis": "unknown"
  },
  "soft_preferences": ["home"],
  "unsupported_or_uncertain_requests": ["price basis is unclear"],
  "message": "Do you mean $2500 per night or $2500 total for the stay?",
  "confidence": 0.82
}
```

User: `Compare the listings I already saved`

Output:

```json
{
  "status": "rejected",
  "intent": {},
  "soft_preferences": [],
  "unsupported_or_uncertain_requests": ["comparison requests are handled outside the AI search bar"],
  "message": "This search bar only creates rental listing searches.",
  "confidence": 0.95
}
```
