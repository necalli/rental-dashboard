# Rental Dashboard

A local-first rental listing analysis dashboard. The app can ingest listing URLs or search requests, capture listing/search payloads with Playwright, normalize extracted data into SQLite, generate AI summaries and comparisons, and display listing, chart, map, memory, and agent-chat workflows in a React UI.

## What It Includes

- Flask API in `backend/`
- Background worker queue for search/listing capture jobs
- Playwright-based capture and parser services
- SQLite storage plus raw payload references
- Optional OpenAI-compatible listing summaries and comparisons
- Optional Claude/Agent SDK runtime for workflow chat
- React + Vite frontend in `frontend/`
- Colab-friendly single-cell bootstrap for live testing

## Repo Layout

- `backend/` API, workers, services, tests, and agent skills
- `frontend/` React/Vite UI
- `scripts/` smoke and validation helpers
- `docs/` architecture, setup, security, and migration notes
- `.env.example` complete public-safe environment template

## Quick Start

Detailed runbooks:

- [Local setup](docs/setup_local.md)
- [Colab setup](docs/setup_colab.md)
- [Security guide](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)
- [GitHub migration checklist](docs/github_migration_execution_plan.md)

### Backend

```bash
cd backend
python -m venv .venv
python -m pip install -r requirements.txt
python -m playwright install
python app.py
```

Backend defaults to `http://localhost:5002`.

### Worker

Run at least one worker in a second terminal:

```bash
cd backend
python worker.py
```

### Frontend

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 3000
```

Open `http://localhost:3000`.

## Environment

Copy `.env.example` to `.env` for local backend settings. Keep real keys out of Git.

Optional keys:

- `RENTAL_LLM_API_KEY` for OpenAI-compatible listing summaries/comparisons
- `RENTAL_CLAUDE_API_KEY` for Claude-backed agent runtime
- `RENTAL_GEOAPIFY_API_KEY` for location autocomplete
- `RENTAL_TAVILY_API_KEY` for trip research search

## Colab

The recommended public-user flow is the single-cell GitHub clone bootstrap in [Single Cell Colab Flow Example.txt](./Single%20Cell%20Colab%20Flow%20Example.txt). It uses temporary Colab proxy URLs and keeps backend/worker processes local to the Colab runtime.

## Responsible Use

Use scraping/capture features responsibly and only where allowed by applicable site terms, law, and rate limits. Raw captures and local databases may contain sensitive or copyrighted third-party content; they are intentionally excluded from the public repo.
