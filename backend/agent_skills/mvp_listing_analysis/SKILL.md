---
name: Listing Analysis
description: Compare listings, identify winners, and explain tradeoffs with grounded summary/review context. Use when users ask which listing is better, request side-by-side analysis, or need decision support between options.
---

Produce grounded listing comparisons and decision guidance.

Rules:
1. For explicit comparison requests, call `tool.listing_compare_create` with provided listing IDs.
2. Default to `sync=true` for direct user answers unless the user asks to queue asynchronously.
3. Keep `review_limit` bounded (default 24) and honor coverage policy fields when supplied.
4. If coverage is blocked (`comparison_coverage_blocked`), explain the violation and recommend fetching full reviews before retrying.
5. Cite `/api/v1/enrich/compare` when using comparison output.
6. Prefer grounded presentation of compare output over creative rewrite; preserve category separations.
