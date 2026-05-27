# Colab Setup

This guide runs the dashboard in Google Colab from the public GitHub repo.

## Recommended Flow

Use the one-cell bootstrap in:

- [Single Cell Colab Flow Example.txt](../Single%20Cell%20Colab%20Flow%20Example.txt)

The bootstrap:

1. Clones the dashboard from GitHub into `/content/rental-dashboard`.
2. Installs Python, Node, and Playwright dependencies.
3. Starts the Flask backend on port `5002`.
4. Starts two background workers.
5. Starts the Vite frontend on port `3000`.
6. Prints temporary Colab proxy URLs for the frontend and backend.

## Runtime Values To Set

At the top of the cell, set only values you need:

```python
DASHBOARD_REPO = "https://github.com/necalli/rental-dashboard.git"
DASHBOARD_BRANCH = "main"
RENTAL_LLM_API_KEY = ""
RENTAL_CLAUDE_API_KEY = ""
RENTAL_GEOAPIFY_API_KEY = ""
RENTAL_TAVILY_API_KEY = ""
```

Do not commit real keys or Colab-generated URLs.

## Service Order

1. System dependencies
2. Python dependencies and Playwright browsers
3. Backend API
4. Worker 1
5. Worker 2
6. Frontend with a Colab Vite proxy
7. Colab proxy URL printout

## Expected Ready State

The cell should end with:

- `FRONTEND_URL: https://...googleusercontent.com/...`
- `BACKEND_URL : https://...googleusercontent.com/...`
- `Backend health: {'service': 'rental-dashboard', 'status': 'ok'}`

Open the frontend URL.

## Notes

- This flow uses temporary Colab proxy URLs, not persistent ngrok hostnames.
- Backend and workers remain inside the Colab runtime.
- The generated `frontend/.env.local` contains `VITE_API_BASE_URL=.` so frontend `/api` calls route through Vite's local proxy to the backend.
- Runtime databases and captures live only in `/content/rental-dashboard/backend/data` and `/content/rental-dashboard/backend/raw` unless you explicitly copy them elsewhere.
