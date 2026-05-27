---
name: Personality RAG Context
description: Retrieve and optionally update historical trip/activity memory for personalized planning and recommendations. Use when users ask for preference-aware suggestions, refer to past trips, or explicitly ask to store a memory/preference.
---

Use memory context to personalize recommendation-style responses.

Rules:
1. Use `tool.personality_rag_context` when the user asks for recommendations, itinerary ideas, or preference-aware suggestions.
2. Do not use personality RAG tools for core operational prompts (job status, metrics, listing ids, queue/capture commands) unless the user explicitly requests memory-aware personalization.
3. Use `tool.personality_rag_upsert` only on explicit user write intent (for example: "remember this", "save this preference").
4. Keep responses grounded in retrieved context and include citations from `/api/v1/memory/query` when context was used.
