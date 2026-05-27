---
name: Pipeline Observability
description: Diagnose pipeline health, latency, parser drift, and job outcomes. Use when requests ask for health snapshots, metrics trends, failed/running jobs, or status for a specific job id.
---

Run health checks, latency diagnostics, and job status triage.

Preferred sequence:
1. Start with `tool.metrics_jobs` for high-level state.
2. Use `tool.jobs_list` to identify recent failures or slow jobs.
3. Use `tool.job_get` only when the user references a specific job id.

If identifiers are missing, ask directly for the job id instead of guessing.
