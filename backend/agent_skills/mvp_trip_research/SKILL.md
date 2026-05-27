---
name: Trip Research
description: Research Tripadvisor-style activities and itinerary ideas for a destination using Tavily. Use when users ask for things to do, activity recommendations, or itinerary options in a location.
---

Research destination activities and itinerary ideas.

Rules:
1. Prefer `tool.trip_research_tavily` with location-first queries.
2. Rank recommendations by rating and rating count when available.
3. If location is missing, ask for it explicitly.
4. If the Tavily key is not configured, explain that setup is required and avoid fabricating results.
5. Do not invent links; only provide `source_url` values returned by the tool output.
