# Local Setup

This guide runs the dashboard on your own computer without ngrok or Colab.

## Prerequisites

1. Python 3.10+
2. Node.js 20+
3. Git
4. Playwright browser dependencies

Optional API keys:

- `RENTAL_LLM_API_KEY` for listing summaries/comparisons
- `RENTAL_CLAUDE_API_KEY` for Claude/Agent SDK chat
- `RENTAL_GEOAPIFY_API_KEY` for location autocomplete
- `RENTAL_TAVILY_API_KEY` for trip research search

## 1. Clone And Configure

```bash
git clone https://github.com/necalli/rental-dashboard.git
cd rental-dashboard
cp .env.example .env
```

On Windows PowerShell:

```powershell
git clone https://github.com/necalli/rental-dashboard.git
cd rental-dashboard
Copy-Item .env.example .env
```

Edit `.env` only for values you need locally. Do not commit `.env`.

## 2. Backend

```bash
cd backend
python -m venv .venv
```

Activate the environment:

```bash
# macOS/Linux
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install
```

Start the API:

```bash
python app.py
```

Health check:

```bash
curl http://localhost:5002/health
```

## 3. Worker

In a second terminal:

```bash
cd backend
python worker.py
```

For parallel capture, start additional workers with unique IDs:

```bash
RENTAL_WORKER_ID=local-w2 python worker.py
```

Windows PowerShell:

```powershell
$env:RENTAL_WORKER_ID = "local-w2"
python worker.py
```

## 4. Frontend

In a third terminal:

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 3000
```

Open `http://localhost:3000`.

The frontend defaults to `VITE_API_BASE_URL=http://localhost:5002`. If needed, create `frontend/.env.local`:

```text
VITE_API_BASE_URL=http://localhost:5002
VITE_ENABLE_AGENT_CHAT=true
```

## 5. Smoke Test

With backend and worker running:

```bash
RENTAL_API_BASE=http://localhost:5002 \
RENTAL_TEST_SEARCH_LOCATION="Flic en Flac, Mauritius" \
python scripts/smoke_test.py
```

Windows PowerShell:

```powershell
$env:RENTAL_API_BASE = "http://localhost:5002"
$env:RENTAL_TEST_SEARCH_LOCATION = "Flic en Flac, Mauritius"
python scripts/smoke_test.py
```

## Notes

- Runtime data is written under `backend/data/` and `backend/raw/`.
- Those folders are intentionally ignored by Git.
- Playwright capture can be sensitive to site changes and rate limits.
