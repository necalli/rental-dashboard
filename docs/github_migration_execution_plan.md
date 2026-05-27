# GitHub Migration Execution Plan

Goal: publish a clean public GitHub repo that a new user can clone and run locally or in Colab without receiving personal files, secrets, local paths, runtime captures, or stale hostnames.

## P0 Before First Public Push

1. Start from a clean copy or fresh repo.
2. Exclude runtime artifacts:
   - `backend/data/`
   - `backend/raw/`
   - `frontend/.env.local`
   - `.env`
   - logs
   - local notebooks or personal documents
3. Confirm `.env.example` contains only placeholders.
4. Confirm Flask debug is off by default.
5. Confirm CORS is env-driven by `BACKEND_CORS_ORIGINS`.
6. Confirm the Colab bootstrap clones from GitHub rather than copying from Google Drive.
7. Run backend tests and frontend build.

## Clean Copy Command

PowerShell example from a parent directory:

```powershell
$PERSONAL = "C:\path\to\rental-dashboard"
$PUBLIC = "C:\path\to\rental-dashboard-public"

New-Item -ItemType Directory -Force $PUBLIC | Out-Null

robocopy $PERSONAL $PUBLIC /E `
  /XD .git node_modules dist .venv venv backend\data backend\raw frontend\dist `
  /XF .env .env.local *.log chatlog_*.txt .codex_write_probe
```

Then inspect the public folder manually before `git init`.

## Validation

```bash
cd backend
python -m unittest discover -s Tests -p "test*.py" -v

cd ../frontend
npm install
npm run build
```

Run the single-cell Colab bootstrap from the public GitHub URL and verify:

1. Backend health is `ok`.
2. Frontend loads from the printed Colab proxy URL.
3. Search jobs queue and complete.
4. Listing ingest jobs queue and complete.
5. Agent chat works in deterministic mode without keys.
6. Claude-backed chat works when `RENTAL_CLAUDE_API_KEY` is supplied.

## Publish

```bash
git init -b main
git add .
git commit -m "Initial public release"
git remote add origin https://github.com/necalli/rental-dashboard.git
git push -u origin main
```

After publishing, enable GitHub Secret Scanning, Push Protection, branch protection, and required CI checks.
