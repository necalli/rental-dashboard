# Contributing

## Local Setup

Follow [docs/setup_local.md](docs/setup_local.md).

## Checks

Backend:

```bash
cd backend
python -m unittest discover -s Tests -p "test*.py" -v
```

Frontend:

```bash
cd frontend
npm install
npm run build
```

## Security Rules

1. Do not commit `.env`, `.env.local`, logs, runtime data, raw captures, local databases, or personal documents.
2. Do not include real API keys in tests, docs, screenshots, or sample configs.
3. Keep public docs GitHub-clone based, not personal Google Drive path based.
4. Keep backend debug mode disabled by default.
5. Keep CORS controlled by environment variables.
