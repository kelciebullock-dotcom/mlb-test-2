# Deploying the MLB dashboard to GitHub Pages

The workflow at `.github/workflows/mlb-daily.yml` runs the full pipeline once
per day and publishes the dashboard to your Pages URL.

## One-time setup

### 1. Push the repo to GitHub

From this folder:

```bash
git init
git add .
git commit -m "initial import"
git branch -M main
git remote add origin git@github.com:<you>/<repo>.git
git push -u origin main
```

Public repo is fine (dashboard is world-readable). For a private repo you
need a paid GitHub plan for Pages.

### 2. No API keys needed

The pipeline uses only free, keyless sources (ESPN for odds, MLB Stats API for
schedule/lineups/stats/scores, Baseball Savant for Statcast, Open-Meteo for
weather). There are **no secrets to configure** — skip straight to Pages setup.

### 3. Enable GitHub Pages via Actions

- Repo → **Settings → Pages**
- Under **Source**, select **GitHub Actions** (not "Deploy from a branch")

### 4. Trigger the first run manually

- Repo → **Actions → MLB daily update → Run workflow**
- First run takes ~2–4 min (no rate limits — free sources are unmetered) for the prediction
  for each date; cached from then on).
- When it finishes, the deployment step prints the Pages URL — usually
  `https://<you>.github.io/<repo>/`.

## What runs and when

- **Schedule:** `cron: '0 2 * * *'` — every day at **02:00 UTC**.
  - March–November (EDT): **10:00 pm ET the previous day** — matches what you asked for.
  - November–March (EST): **9:00 pm ET the previous day** — shifts one hour earlier because GitHub cron is UTC-only.
  - If you want fixed 10 pm ET year-round, add a second cron entry:
    ```yaml
    schedule:
      - cron: '0 2 * * *'   # EDT
      - cron: '0 3 * * *'   # EST
    ```
    You'll get one extra run per day during transition weeks — harmless, the second one is a no-op because caches are fresh.
- **Manual button:** the `workflow_dispatch:` trigger lets you re-run any time from the Actions tab.
- **No rate limits:** ESPN and the MLB Stats API are free and unmetered, so a full
  slate generates in ~2–4 minutes with no throttling.

## What the workflow does each run

1. Scraper writes fresh MLB StatsAPI + Statcast + weather data.
2. Predictor generates `data/predictions_<date>.json` (10k sims + picks).
3. Dashboard renders `mlb_dashboard.html`.
4. **Commits** the new `data/predictions_*.json` back to `main` — this is what
   keeps the calendar populated with older dates going forward.
5. **Deploys** the HTML to Pages as `index.html`.

## Reliability caveats

- GitHub scheduled workflows can lag **5–30 min** during peak times. If
  10 pm ET sharp matters, use a dedicated cron host (Cloudflare Workers,
  Fly.io, or a $5/mo VPS).
- If a run fails, Pages keeps serving the previous day's dashboard until
  the next successful run.
- ESPN's odds API is undocumented (free, but no SLA); if its shape ever changes
  or a game has no posted line, that game shows no picks and the run still
  completes for every other game.

## Local development still works

Everything runs the same way locally:

```bash
python mlb_tonight_edates.py
python mlb_predict.py
python mlb_dashboard.py && open mlb_dashboard.html
```
