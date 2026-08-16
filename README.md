# MLB Daily Model & Picks

A self-updating MLB dashboard: nightly it pulls the next day's slate, runs a
10,000-sim Monte Carlo model per game, compares the model to the market, and
publishes a static dashboard to GitHub Pages — with a recommended pick +
confidence per game, full context (pitchers, Statcast, weather, umpire,
injuries), and a projected boxscore.

**100% free data. No API keys, no paid tiers, no secrets to configure.**

## Data sources (all free, keyless)

| Data | Source |
|---|---|
| Odds — moneyline / total / runline, with prices | ESPN (`cdn.espn.com`, `sports.core.api.espn.com`) |
| Schedule, lineups, probable pitchers, player/team stats, final scores | MLB Stats API (`statsapi.mlb.com`) |
| Pitcher Statcast (xERA, K%, BB%, Barrel%…) | Baseball Savant |
| Weather at first pitch | Open-Meteo |

Player-prop *odds* have no free source, so prop bets are not generated (the
model still projects each batter's stat line in the boxscore).

## Scripts

| File | What it does |
|---|---|
| `mlb_tonight_edates.py` | Scraper — writes `mlb_tonight_edates.csv` (per-game context) |
| `mlb_predict.py` | Monte Carlo model + odds comparison — writes `data/predictions_<date>.json` |
| `mlb_dashboard.py` | Renders `mlb_dashboard.html` from the prediction JSONs |
| `mlb_backtest.py` | Grades past picks vs actual results (ROI, calibration) |

Everything is Python **standard library only** — no `pip install`.

## Run it locally

```bash
python mlb_tonight_edates.py            # today's slate (or pass YYYY-MM-DD)
python mlb_predict.py                   # 10k sims + picks
python mlb_dashboard.py && open mlb_dashboard.html
```

Backtest what's been predicted so far:

```bash
python mlb_backtest.py                  # grades every predictions_*.json vs finals
```

## Deploy to GitHub Pages

See **[DEPLOY.md](DEPLOY.md)** — no secrets needed. In short: push the repo,
enable Pages → Source: GitHub Actions, and run the workflow once. It then runs
itself nightly at 02:00 UTC (10pm ET during EDT).

## Honest note on betting

This is a research/context dashboard, not a proven money-maker. Backtesting
showed the raw model was overconfident, so probabilities are shrunk toward the
market (`MODEL_TRUST` in `mlb_predict.py`). The only real proof of edge is
beating the closing line over a large forward sample — which the nightly runs
accumulate over time. Bet responsibly, or not at all.
