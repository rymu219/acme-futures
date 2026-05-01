# Acme Futures · Web Watcher

Tiny FastAPI dashboard for remote monitoring. Reads from Supabase only — no broker code, no Topstep VPS-prohibition concern. Phone-friendly. Token-gated.

## Local test

```bash
# from the project root, with .env populated:
cd "/Users/ryanmurphy/Desktop/Acme Futures"
uv pip install fastapi uvicorn[standard]   # or use the web/requirements.txt
ACME_VIEW_TOKEN=dev-token uv run uvicorn web.app:app --reload --port 8000
# then open http://localhost:8000/?token=dev-token
```

## Railway deploy (one-time)

1. Sign in at railway.app, click **New Project → Deploy from GitHub** (or **Empty Project**).
2. If using Empty Project: install the Railway CLI and run from `/Users/ryanmurphy/Desktop/Acme Futures/web/`:
   ```bash
   npm install -g @railway/cli
   railway login
   railway init        # name it "acme-futures-watcher"
   railway up          # uploads this directory
   ```
3. In the Railway dashboard, set environment variables on the service:
   - `SUPABASE_URL` — same value as in `.env`
   - `SUPABASE_SERVICE_ROLE_KEY` — same value as in `.env`
   - `ACME_VIEW_TOKEN` — pick a long random string; you'll use it in the URL
4. In **Settings → Networking**, click **Generate Domain**. You'll get a URL like `acme-futures-watcher.up.railway.app`.
5. Open `https://acme-futures-watcher.up.railway.app/?token=YOUR_TOKEN` from anywhere — laptop, phone, work.

The page auto-refreshes every 5 seconds.

## Security notes

- The `service_role` key bypasses RLS. It lives only as a Railway env var, never sent to the browser.
- All access is gated by `ACME_VIEW_TOKEN` in the query string. Use a long random token (32+ chars).
- Bookmarking the URL with the token saves you typing.
- If the token leaks, change it in Railway env vars and the old URL stops working.
