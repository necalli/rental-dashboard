# Troubleshooting

## Backend Will Not Start

Check Python and dependencies:

```bash
python --version
cd backend
python -m pip install -r requirements.txt
```

If port `5002` is already in use, stop the other process or set:

```bash
BACKEND_PORT=5100 python app.py
```

## Frontend Cannot Reach Backend

Check backend health:

```bash
curl http://localhost:5002/health
```

For local Vite, confirm `frontend/.env.local` if present:

```text
VITE_API_BASE_URL=http://localhost:5002
```

For Colab, the generated `.env.local` should be:

```text
VITE_API_BASE_URL=.
```

Restart Vite after changing env values.

## Playwright Browser Errors

Install browsers:

```bash
python -m playwright install
```

In Linux/Colab, install OS dependencies:

```bash
python -m playwright install --with-deps
```

## Worker Jobs Stay Queued

Make sure a worker process is running:

```bash
cd backend
python worker.py
```

Check recent jobs:

```bash
curl http://localhost:5002/api/v1/jobs?limit=20
```

## Search Or Listing Capture Times Out

Useful runtime knobs:

- `RENTAL_CAPTURE_TIMEOUT_MS`
- `RENTAL_CAPTURE_MAX_RESPONSES`
- `RENTAL_CAPTURE_MIN_INTERVAL_MS`
- `RENTAL_CAPTURE_BLOCK_RESOURCES`
- `RENTAL_CAPTURE_RESPONSE_URL_ALLOWLIST`

Sites can change markup or network payloads. Parser drift is expected over time.

## Agent Chat Reports Missing Key

Use deterministic mode when you do not want live Claude calls:

```bash
RENTAL_AGENT_RUNTIME=deterministic
```

Use Claude-backed runtime only after setting:

```bash
RENTAL_CLAUDE_API_KEY=...
```

## LLM Summary Fails

Set:

```bash
RENTAL_LLM_API_KEY=...
RENTAL_LLM_MODEL=gpt-5-mini
```

The summary/comparison endpoints are optional. Core listing/search capture can run without an LLM key.
