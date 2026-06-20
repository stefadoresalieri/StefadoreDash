# StefadoreDash

Live YouTube engagement dashboard for the Stefadore channel.

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

## Live YouTube Refreshes

The hosted app includes a bundled snapshot so the dashboard can load even when YouTube blocks public scraping from cloud servers.

For reliable live refreshes on Render, add an environment variable:

```text
YOUTUBE_API_KEY=your_google_youtube_data_api_key
```

Without this key, the app may show a message that YouTube blocked the public scraper, but the dashboard will keep serving the bundled snapshot instead of going blank.
