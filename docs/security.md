# Security Guide

This project is designed for local or notebook-based analysis. Treat public exposure carefully because the backend can trigger scraping jobs, read local runtime data, and call model providers when keys are configured.

## Secrets

Never commit real values for:

- `RENTAL_LLM_API_KEY`
- `RENTAL_CLAUDE_API_KEY`
- `RENTAL_GEOAPIFY_API_KEY`
- `RENTAL_TAVILY_API_KEY`
- provider tokens, cookies, browser profiles, or ngrok tokens

Use `.env` locally and Colab variables in notebooks. `.env`, `.env.local`, and runtime data are ignored by Git.

## Runtime Data

Do not publish:

- `backend/data/`
- `backend/raw/`
- local SQLite databases
- raw capture payloads
- uploaded RAG memory files
- logs from model or browser sessions

These files can contain listing content, user prompts, captured payloads, or personal travel notes.

## Backend Exposure

Defaults are development-oriented:

- Backend port: `5002`
- Frontend port: `3000`
- Flask debug: disabled unless `BACKEND_DEBUG=true`
- CORS: restricted by `BACKEND_CORS_ORIGINS`

For public deployment, add authentication, rate limits, request size limits, and stricter CORS before exposing the API beyond your own machine or Colab runtime.

## Agent Runtime

Claude/Agent SDK features may read project files or call tools depending on runtime configuration. Keep these guardrails explicit:

- Use `RENTAL_AGENT_RUNTIME=deterministic` for no-key demos.
- Set `RENTAL_AGENT_SDK_DISALLOW_BUILTINS=true` unless you intentionally allow built-ins.
- Keep `RENTAL_AGENT_SDK_ALLOWED_BUILTINS` narrow.
- Do not expose agent endpoints publicly without authentication.

## GitHub Settings

After publishing:

1. Enable GitHub Secret Scanning.
2. Enable Push Protection.
3. Protect `main`.
4. Require CI before merges.
5. Rotate any key that was ever committed, even if deleted later.
