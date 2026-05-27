# Security Policy

Security fixes are applied to the latest `main` branch.

## Reporting A Vulnerability

Do not open public issues for suspected vulnerabilities. Report security issues privately to the repository maintainers with reproduction steps, impact, affected endpoints/files, and any known exposed credentials.

If a credential may have been exposed, rotate it immediately.

## Security Expectations

1. Never commit real API keys, tokens, cookies, browser profiles, local databases, raw captures, or logs.
2. Keep secrets in environment variables or local `.env` files only.
3. Do not expose the backend publicly without authentication and rate limiting.
4. Preserve env-driven CORS and debug settings.
5. Keep runtime data under ignored folders such as `backend/data/` and `backend/raw/`.
