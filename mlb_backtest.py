"""Backtest the MLB model's game-line picks against actual results.

Grades every Moneyline / Total / Runline pick stored in data/predictions_*.json
against final scores from the MLB Stats API (official, unlimited). The odds the
model saw are baked into each prediction JSON, so grading needs only the finals.

Reports, for three staking strategies (flat $100 stakes):
  1. RECOMMENDED  — the single headline pick per game (what the dashboard shows).
  2. POSITIVE-EV  — every game-line pick the model flagged as +EV.
  3. ALL LINES    — every game-line pick (used for calibration).

Plus a calibration table (predicted win% vs actual hit%) and closing-line value.

Usage:
    python mlb_backtest.py                     # grade all existing prediction JSONs
    python mlb_backtest.py 2026-08-01 2026-08-14   # grade a date range (existing JSONs only)
    python mlb_backtest.py --backfill 2026-08-01 2026-08-14
                                               # generate predictions for the range first,
                                               # THEN grade. Slow; see leakage caveat below.

LEAKAGE CAVEAT (read this): backfill runs the current scraper + predictor, which
pull *current* season stats (ERA, splits, Statcast). For a game on Aug 1 graded
with Aug 15 season stats, that is look-ahead bias and will FLATTER the results.
Treat backfilled numbers as an optimistic upper bound. The only clean test is the
forward one: grade predictions that were generated before the games were played
(the daily GitHub Action accumulates these automatically).
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

CWD = Path(__file__).parent
DATA_DIR = CWD / "data"
MLB_BASE = "https://statsapi.mlb.com/api/v1"
GAME_LINE_MARKETS = {"Moneyline", "Total", "Runline"}
STAKE = 100.0  # flat stake per bet, in dollars


# ---- Ground truth (MLB Stats API) -------------------------------------------

def _slug(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def fetch_actuals(date_str: str) -> dict[frozenset, dict]:
    """Return {frozenset({away_slug, home_slug}): {...final...}} for Final games."""
    url = f"{MLB_BASE}/schedule?sportId=1&date={date_str}"
    req = urllib.request.Request(url, headers={"User-Agent": "mlb-backtest/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  (actuals fetch failed for {date_str}: {e})", file=sys.stderr)
        return {}

    out: dict[frozenset, dict] = {}
    for day in data.get("dates", []):
        for g in day.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            away = g["teams"]["away"]
            home = g["teams"]["home"]
            a_name = away["team"]["name"]
            h_name = home["team"]["name"]
            a_score = away.get("score")
            h_score = home.get("score")
            if a_score is None or h_score is None:
                continue
            key = frozenset({_slug(a_name), _slug(h_name)})
            # If a doubleheader produced two Final results for the same matchup,
            # keep the first; the model already dedupes to one card per matchup.
            out.setdefault(key, {
                "away_team": a_name, "home_team": h_name,
                "away_score": int(a_score), "home_score": int(h_score),
                "total": int(a_score) + int(h_score),
                "home_margin": int(h_score) - int(a_score),
            })
    return out


# ---- Grading ----------------------------------------------------------------

def _payout(stake: float, odds: float) -> float:
    """Profit (not counting stake back) on a winning bet at American odds."""
    if odds > 0:
        return stake * odds / 100.0
    return stake * 100.0 / -odds


def grade_pick(pick: dict, actual: dict) -> dict | None:
    """Return {result: win|loss|push, profit: float} for a game-line pick, or None
    if it can't be graded."""
    market = pick.get("market")
    side = pick.get("side", "")
    line = pick.get("line")
    odds = pick.get("market_odds")
    if odds is None:
        return None
    try:
        odds = float(odds)
    except (TypeError, ValueError):
        return None

    a_score = actual["away_score"]
    h_score = actual["home_score"]
    total = actual["total"]
    home_margin = actual["home_margin"]

    won = None  # True / False / "push"

    if market == "Moneyline":
        # side like "Boston Red Sox ML" — match against home/away by team name
        team = side.replace(" ML", "").strip()
        if _slug(team) == _slug(actual["home_team"]):
            won = h_score > a_score
        elif _slug(team) == _slug(actual["away_team"]):
            won = a_score > h_score
        else:
            return None

    elif market == "Total":
        if line is None:
            return None
        line = float(line)
        if total == line:
            won = "push"
        elif side.startswith("Over"):
            won = total > line
        elif side.startswith("Under"):
            won = total < line
        else:
            return None

    elif market == "Runline":
        # side like "Boston Red Sox -1.5" / "New York Yankees +1.5"
        if line is None:
            return None
        line = float(line)
        # Determine which team the side refers to and apply the spread to margin.
        toks = side.rsplit(" ", 1)
        if len(toks) != 2:
            return None
        team = toks[0].strip()
        if _slug(team) == _slug(actual["home_team"]):
            adj = home_margin + line
        elif _slug(team) == _slug(actual["away_team"]):
            adj = (-home_margin) + line
        else:
            return None
        if adj == 0:
            won = "push"
        else:
            won = adj > 0
    else:
        return None

    if won == "push":
        return {"result": "push", "profit": 0.0}
    if won:
        return {"result": "win", "profit": _payout(STAKE, odds)}
    return {"result": "loss", "profit": -STAKE}


# ---- Strategy selection -----------------------------------------------------

def recommended_pick(game: dict) -> dict | None:
    """Mirror the dashboard's recommendation logic: highest-EV game-line pick the
    model gives >=50% to hit; else highest-EV; else None."""
    picks = [p for p in (game.get("picks") or []) if p.get("market") in GAME_LINE_MARKETS]
    positive = [p for p in picks if (p.get("ev_pct") or -100) >= 0]
    confident = [p for p in positive if (p.get("model_prob") or 0) >= 0.50]
    pool = confident if confident else positive
    if not pool:
        return None
    return max(pool, key=lambda p: p.get("ev_pct") or -100)


# ---- Aggregation ------------------------------------------------------------

class Ledger:
    def __init__(self, name: str):
        self.name = name
        self.n = 0
        self.wins = 0
        self.losses = 0
        self.pushes = 0
        self.staked = 0.0
        self.profit = 0.0
        self.by_market: dict[str, list] = defaultdict(lambda: [0, 0.0, 0.0])  # [n, staked, profit]

    def add(self, pick: dict, graded: dict):
        self.n += 1
        r = graded["result"]
        if r == "win": self.wins += 1
        elif r == "loss": self.losses += 1
        else: self.pushes += 1
        staked = 0.0 if r == "push" else STAKE
        self.staked += staked
        self.profit += graded["profit"]
        m = self.by_market[pick.get("market", "?")]
        m[0] += 1
        m[1] += staked
        m[2] += graded["profit"]

    def report(self) -> str:
        if self.n == 0:
            return f"  {self.name}: no gradeable picks"
        decided = self.wins + self.losses
        wr = (self.wins / decided * 100) if decided else 0.0
        roi = (self.profit / self.staked * 100) if self.staked else 0.0
        lines = [
            f"  {self.name}",
            f"    record      {self.wins}-{self.losses}-{self.pushes}  ({wr:.1f}% win)",
            f"    staked      ${self.staked:,.0f}   profit ${self.profit:+,.0f}   ROI {roi:+.1f}%",
        ]
        for m, (n, staked, profit) in sorted(self.by_market.items()):
            m_roi = (profit / staked * 100) if staked else 0.0
            lines.append(f"    {m:<10} {n:>3} bets   ${profit:+,.0f}   ROI {m_roi:+.1f}%")
        return "\n".join(lines)


def calibration_table(rows: list[tuple[float, bool]]) -> str:
    """rows = list of (model_prob, hit_bool). Buckets by predicted prob."""
    buckets = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
               (0.70, 0.80), (0.80, 1.01)]
    lines = ["  predicted   n    model%   actual%   gap"]
    for lo, hi in buckets:
        sel = [(p, h) for (p, h) in rows if lo <= p < hi]
        if not sel:
            continue
        n = len(sel)
        model_avg = sum(p for p, _ in sel) / n * 100
        actual = sum(1 for _, h in sel if h) / n * 100
        gap = actual - model_avg
        flag = "  <-- overconfident" if gap < -8 else ("  <-- underconfident" if gap > 8 else "")
        lines.append(f"  {lo*100:.0f}-{hi*100:.0f}%    {n:>3}   {model_avg:5.1f}%   {actual:5.1f}%   {gap:+5.1f}{flag}")
    return "\n".join(lines)


# ---- Backfill (optional) ----------------------------------------------------

def backfill_range(start: str, end: str) -> None:
    """Generate scraper CSV + predictions for each date in [start, end]."""
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    cur = d0
    while cur <= d1:
        ds = cur.strftime("%Y-%m-%d")
        pred_path = DATA_DIR / f"predictions_{ds}.json"
        if pred_path.exists():
            print(f"  {ds}: predictions already exist, skipping generate", file=sys.stderr)
        else:
            print(f"  {ds}: generating scraper + predictions (fast game-lines mode)...", file=sys.stderr)
            subprocess.run([sys.executable, "mlb_tonight_edates.py", ds, "--fast"], cwd=CWD, check=False)
            subprocess.run([sys.executable, "mlb_predict.py", ds, "--game-lines-only"], cwd=CWD, check=False)
        cur += timedelta(days=1)


# ---- Main -------------------------------------------------------------------

def run_backtest(dates: list[str]) -> None:
    rec_ledger = Ledger("RECOMMENDED (1 pick/game — the dashboard headline)")
    ev_ledger = Ledger("POSITIVE-EV (every +EV game-line pick)")
    all_ledger = Ledger("ALL GAME-LINE PICKS (calibration sample)")
    calib_rows: list[tuple[float, bool]] = []
    clv_samples: list[float] = []  # model_prob - market_prob for graded recs

    graded_dates = 0
    total_games = 0

    for ds in dates:
        pred_path = DATA_DIR / f"predictions_{ds}.json"
        if not pred_path.exists():
            continue
        with open(pred_path) as f:
            pred = json.load(f)
        actuals = fetch_actuals(ds)
        if not actuals:
            continue
        graded_dates += 1

        # Dedupe prediction games to one per matchup (grade the richer entry).
        seen: dict[frozenset, dict] = {}
        for g in pred.get("games", []):
            key = frozenset({_slug(g.get("away_team", "")), _slug(g.get("home_team", ""))})
            gl = [p for p in (g.get("picks") or []) if p.get("market") in GAME_LINE_MARKETS]
            if key not in seen or len(gl) > len(seen[key][1]):
                seen[key] = (g, gl)

        for key, (g, gl) in seen.items():
            actual = actuals.get(key)
            if not actual:
                continue
            total_games += 1

            # RECOMMENDED
            rec = recommended_pick(g)
            if rec:
                graded = grade_pick(rec, actual)
                if graded:
                    rec_ledger.add(rec, graded)
                    if graded["result"] != "push":
                        mp = rec.get("model_prob") or 0
                        clv_samples.append(mp - (rec.get("market_prob") or 0))

            # POSITIVE-EV + ALL + calibration
            for p in gl:
                graded = grade_pick(p, actual)
                if not graded:
                    continue
                all_ledger.add(p, graded)
                if (p.get("ev_pct") or -100) >= 0:
                    ev_ledger.add(p, graded)
                if graded["result"] != "push":
                    calib_rows.append((p.get("model_prob") or 0, graded["result"] == "win"))

    # ---- Report ----
    print("=" * 68)
    print("MLB MODEL BACKTEST")
    print("=" * 68)
    print(f"Dates graded: {graded_dates}   Games graded: {total_games}   Stake: ${STAKE:.0f}/bet")
    print()
    if total_games == 0:
        print("No completed games with predictions found in the given range.")
        print("(Predictions exist only for dates you've run mlb_predict.py on, and")
        print(" games must be Final. Use --backfill to generate a historical sample.)")
        return

    print(rec_ledger.report()); print()
    print(ev_ledger.report()); print()
    print(all_ledger.report()); print()

    print("CALIBRATION (does predicted win% match reality?)")
    print(calibration_table(calib_rows)); print()

    if clv_samples:
        avg_edge = sum(clv_samples) / len(clv_samples) * 100
        print(f"Avg model edge vs market on recommended picks: {avg_edge:+.1f} pts")
        print("  (model_prob - devigged market_prob; positive = model claims an edge.")
        print("   Whether that edge is real is what the ROI above actually tests.)")
    print()
    print("Reminder: break-even at -110 juice is 52.4% wins. Beating the closing")
    print("line over a LARGE sample (500+ bets) is the only real proof of edge.")


def main() -> int:
    args = [a for a in sys.argv[1:]]

    if args and args[0] == "--backfill":
        if len(args) < 3:
            print("usage: python mlb_backtest.py --backfill START_DATE END_DATE")
            return 1
        start, end = args[1], args[2]
        print(f"Backfilling predictions {start} -> {end} (this is slow + leaky; see header)...",
              file=sys.stderr)
        backfill_range(start, end)
        dates = _date_range(start, end)
    elif len(args) >= 2:
        dates = _date_range(args[0], args[1])
    elif len(args) == 1:
        dates = [args[0]]
    else:
        # Default: every prediction JSON we have
        dates = sorted(p.stem.replace("predictions_", "")
                       for p in DATA_DIR.glob("predictions_*.json"))

    run_backtest(dates)
    return 0


def _date_range(start: str, end: str) -> list[str]:
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    cur = d0
    while cur <= d1:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


if __name__ == "__main__":
    sys.exit(main())
