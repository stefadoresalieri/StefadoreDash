# StefadoreDash

Live YouTube engagement dashboard for the Stefadore channel.

The dashboard can filter the visible data by video type:

- All videos
- Shorts
- Regular videos

## Local Preview

From the repo root:

```powershell
cd stefadore_live_dashboard
.\start_live_dashboard.ps1
```

Then open:

```text
http://127.0.0.1:8787/
```

## Deploy Online

This app needs Python because it refreshes YouTube data in the background, so GitHub Pages is not enough for the live version.

Recommended beginner path:

1. Create a Render account.
2. In Render, choose **New Web Service**.
3. Connect this GitHub repository.
4. Use these settings if Render does not detect `render.yaml` automatically:
   - Build command: `pip install -r requirements.txt`
   - Start command: `python start.py`
5. Deploy and open the public Render URL.

The dashboard reads Render's `PORT` environment variable automatically.

## Password Protection

To require a login before anyone can view the dashboard, add these Render environment variables:

```text
DASHBOARD_USERNAME=choose_a_username
DASHBOARD_PASSWORD=choose_a_strong_password
DASHBOARD_SESSION_SECRET=choose_a_long_random_phrase
```

`DASHBOARD_PASSWORD` is the switch that turns login protection on. If it is not set, the app stays open, which is useful for local development.

Do not put the password in the code or commit it to GitHub. Store it only in Render's environment variables.

## Live YouTube Refreshes

The hosted app includes a bundled snapshot so the dashboard can load even when YouTube blocks public scraping from cloud servers.

For reliable live refreshes on Render, add an environment variable:

```text
YOUTUBE_API_KEY=your_google_youtube_data_api_key
```

Without this key, the app may show a message that YouTube blocked the public scraper, but the dashboard will keep serving the bundled snapshot instead of going blank.

Public scraping is disabled by default on hosted deployments to avoid YouTube bot-check errors in the logs. If you need to test the fallback scraper locally, set:

```text
ALLOW_PUBLIC_YOUTUBE_SCRAPER=true
```
