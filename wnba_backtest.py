"""Backtest the WNBA model's game-line picks — record, ROI, and closing-line value.

Grades every Moneyline / Total / Spread pick stored in data/wnba_predictions_*.json
against final scores and CLOSING lines from ESPN's free API. Writes
data/wnba_performance.json for the dashboard's MODEL PERFORMANCE panel.

Usage:
    python wnba_backtest.py                       # grade all existing prediction JSONs
    python wnba_backtest.py 2026-08-01 2026-08-16 # grade a date range
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

CWD = Path(__file__).parent
DATA_DIR = CWD / "data"
ESPN_SB = "https://cdn.espn.com/core/wnba/scoreboard"
ESPN_ODDS = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/events/{eid}/competitions/{eid}/odds"
GAME_LINE_MARKETS = {"Moneyline", "Total", "Spread"}
STAKE = 100.0


def _slug(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (wnba-backtest)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---- Actuals + closing lines (ESPN) ----------------------------------------

def fetch_actuals(date_str: str) -> dict[frozenset, dict]:
    """{frozenset(team slugs): {...final...}} for completed WNBA games on date_str,
    plus the ESPN event id. Scans date-1..date+1 (ESPN mis-dates) and keeps only
    games whose ET tip-off matches, so grading never counts an adjacent day."""
    base = datetime.strptime(date_str, "%Y-%m-%d").date()
    events, seen_ids = [], set()
    for delta in (0, -1, 1):
        try:
            d = _get(f"{ESPN_SB}?xhr=1&dates={(base + timedelta(days=delta)).strftime('%Y%m%d')}")
        except Exception:
            continue
        for ev in (d.get("content", {}).get("sbData", {}).get("events") or []):
            eid = ev.get("id")
            if eid and eid not in seen_ids:
                seen_ids.add(eid); events.append(ev)
    out = {}
    for ev in events:
        # Only grade games whose ET tip-off matches the requested date.
        iso = ev.get("date", "")
        try:
            if not iso or datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d") != date_str:
                continue
        except Exception:
            continue
        try:
            comp = ev["competitions"][0]
            if comp.get("status", {}).get("type", {}).get("state") != "post":
                continue
            comps = comp.get("competitors", [])
            home = next(c for c in comps if c.get("homeAway") == "home")
            away = next(c for c in comps if c.get("homeAway") == "away")
            hs = int(home.get("score")); as_ = int(away.get("score"))
        except (KeyError, IndexError, StopIteration, TypeError, ValueError):
            continue
        h_name = home.get("team", {}).get("displayName", "")
        a_name = away.get("team", {}).get("displayName", "")
        key = frozenset({_slug(a_name), _slug(h_name)})
        out.setdefault(key, {
            "event_id": ev.get("id"),
            "away_team": a_name, "home_team": h_name,
            "away_score": as_, "home_score": hs,
            "total": as_ + hs, "home_margin": hs - as_,
        })
    return out


def _am(node):
    if node is None:
        return None
    if isinstance(node, (int, float)):
        return float(node)
    am = node.get("american") if isinstance(node, dict) else None
    try:
        return float(str(am).replace("+", "")) if am is not None else None
    except (TypeError, ValueError):
        return None


def _p(odds):
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return 0.0
    if abs(o) < 100:
        return 0.0
    return 100.0 / (o + 100.0) if o > 0 else (-o) / ((-o) + 100.0)


def _devig(a, b):
    pa, pb = _p(a), _p(b)
    t = pa + pb
    return (pa / t, pb / t) if t > 0 else (0.0, 0.0)


_CLOSE_CACHE = {}


def fetch_closing_probs(event_id) -> dict:
    if not event_id:
        return {}
    if event_id in _CLOSE_CACHE:
        return _CLOSE_CACHE[event_id]
    try:
        d = _get(ESPN_ODDS.format(eid=event_id))
    except Exception:
        _CLOSE_CACHE[event_id] = {}
        return {}
    items = [it for it in d.get("items", [])
             if "live" not in it.get("provider", {}).get("name", "").lower()]
    if not items:
        _CLOSE_CACHE[event_id] = {}
        return {}
    items.sort(key=lambda it: 0 if "draftkings" in it.get("provider", {}).get("name", "").lower() else 1)
    it = items[0]
    home = it.get("homeTeamOdds", {}) or {}
    away = it.get("awayTeamOdds", {}) or {}
    close = it.get("close") or {}

    def ml_close(node):
        c = node.get("close") or node.get("current") or {}
        v = _am(c.get("moneyLine"))
        return v if v is not None else _am(node.get("moneyLine"))

    def spr_close(node):
        c = node.get("close") or node.get("current") or node.get("open") or {}
        return _am(c.get("spread"))

    out = {}
    fh, fa = _devig(ml_close(home), ml_close(away))
    out["ml_home"], out["ml_away"] = fh, fa
    over_odds = _am(close.get("over")) if close else _am(it.get("overOdds"))
    under_odds = _am(close.get("under")) if close else _am(it.get("underOdds"))
    fo, fu = _devig(over_odds, under_odds)
    out["over"], out["under"] = fo, fu
    try:
        out["total_line"] = float((close.get("total") or {}).get("american")) if close.get("total") else float(it.get("overUnder"))
    except (TypeError, ValueError, AttributeError):
        out["total_line"] = None
    sh, sa = spr_close(home), spr_close(away)
    frh, fra = _devig(sh, sa)
    out["sp_home"], out["sp_away"] = frh, fra
    try:
        out["sp_home_line"] = float(it.get("spread")) if it.get("spread") is not None else None
    except (TypeError, ValueError):
        out["sp_home_line"] = None
    _CLOSE_CACHE[event_id] = out
    return out


# ---- Grading ----------------------------------------------------------------

def _payout(stake, odds):
    return stake * odds / 100.0 if odds > 0 else stake * 100.0 / -odds


def grade_pick(pick, actual):
    market, side, line = pick.get("market"), pick.get("side", ""), pick.get("line")
    odds = pick.get("market_odds")
    try:
        odds = float(odds)
    except (TypeError, ValueError):
        return None
    total, margin = actual["total"], actual["home_margin"]
    hs, as_ = actual["home_score"], actual["away_score"]
    won = None
    if market == "Moneyline":
        team = side.replace(" ML", "").strip()
        if _slug(team) == _slug(actual["home_team"]):
            won = hs > as_
        elif _slug(team) == _slug(actual["away_team"]):
            won = as_ > hs
        else:
            return None
    elif market == "Total":
        if line is None:
            return None
        line = float(line)
        won = "push" if total == line else (total > line if side.startswith("Over") else total < line)
    elif market == "Spread":
        if line is None:
            return None
        line = float(line)
        toks = side.rsplit(" ", 1)
        if len(toks) != 2:
            return None
        team = toks[0].strip()
        if _slug(team) == _slug(actual["home_team"]):
            adj = margin + line
        elif _slug(team) == _slug(actual["away_team"]):
            adj = (-margin) + line
        else:
            return None
        won = "push" if adj == 0 else adj > 0
    else:
        return None
    if won == "push":
        return {"result": "push", "profit": 0.0}
    return {"result": "win", "profit": _payout(STAKE, odds)} if won else {"result": "loss", "profit": -STAKE}


def clv_for_pick(pick, closing, actual):
    entry = pick.get("market_fair_prob")
    if entry is None or not closing:
        return None
    market, side = pick.get("market"), pick.get("side", "")
    if market == "Moneyline":
        team = side.replace(" ML", "").strip()
        if _slug(team) == _slug(actual["home_team"]):
            cf = closing.get("ml_home")
        elif _slug(team) == _slug(actual["away_team"]):
            cf = closing.get("ml_away")
        else:
            return None
    elif market == "Total":
        if pick.get("line") is None or closing.get("total_line") != pick.get("line"):
            return None
        cf = closing.get("over") if side.startswith("Over") else closing.get("under")
    elif market == "Spread":
        cl = closing.get("sp_home_line")
        if cl is None:
            return None
        toks = side.rsplit(" ", 1)
        if len(toks) != 2:
            return None
        team = toks[0].strip()
        if _slug(team) == _slug(actual["home_team"]):
            if cl != pick.get("line"):
                return None
            cf = closing.get("sp_home")
        elif _slug(team) == _slug(actual["away_team"]):
            if -cl != pick.get("line"):
                return None
            cf = closing.get("sp_away")
        else:
            return None
    else:
        return None
    if not cf:
        return None
    return (cf - entry) * 100.0


def recommended_pick(game):
    picks = [p for p in (game.get("picks") or []) if p.get("market") in GAME_LINE_MARKETS]
    positive = [p for p in picks if (p.get("ev_pct") or -100) >= 0]
    confident = [p for p in positive if (p.get("model_prob") or 0) >= 0.50]
    pool = confident if confident else positive
    return max(pool, key=lambda p: p.get("ev_pct") or -100) if pool else None


def model_winner_pick(game):
    """Moneyline pick on the model's predicted winner (higher win %)."""
    m = game.get("model") or {}
    aw, hw = m.get("away_win_pct", 0), m.get("home_win_pct", 0)
    if aw == 0 and hw == 0:
        return None
    want = game.get("home_team", "") if hw >= aw else game.get("away_team", "")
    for p in (game.get("picks") or []):
        if p.get("market") == "Moneyline" and _slug(p.get("side", "").replace(" ML", "")) == _slug(want):
            return p
    return None


def model_total_pick(game):
    """Total pick on the model's over/under lean vs the market line."""
    m = game.get("model") or {}
    mt = m.get("mean_total")
    totals = [p for p in (game.get("picks") or []) if p.get("market") == "Total"]
    if mt is None or not totals or totals[0].get("line") is None:
        return None
    over = mt > totals[0]["line"]
    for p in totals:
        if p.get("side", "").startswith("Over" if over else "Under"):
            return p
    return None


class Ledger:
    def __init__(self):
        self.w = self.l = self.p = 0
        self.staked = self.profit = 0.0

    def add(self, g):
        r = g["result"]
        if r == "win": self.w += 1
        elif r == "loss": self.l += 1
        else: self.p += 1
        self.staked += 0.0 if r == "push" else STAKE
        self.profit += g["profit"]

    def summary(self):
        dec = self.w + self.l
        return {"wins": self.w, "losses": self.l, "pushes": self.p,
                "win_pct": round(self.w / dec * 100, 1) if dec else None,
                "roi_pct": round(self.profit / self.staked * 100, 1) if self.staked else None,
                "profit": round(self.profit, 0), "staked": round(self.staked, 0)}


def run(dates):
    rec_led, all_led = Ledger(), Ledger()
    winner_led, total_led = Ledger(), Ledger()
    rec_clv, all_clv = [], []
    gdates = games = 0
    for ds in dates:
        p = DATA_DIR / f"wnba_predictions_{ds}.json"
        if not p.exists():
            continue
        pred = json.load(open(p))
        actuals = fetch_actuals(ds)
        if not actuals:
            continue
        gdates += 1
        seen = {}
        for g in pred.get("games", []):
            k = frozenset({_slug(g.get("away_team", "")), _slug(g.get("home_team", ""))})
            gl = [x for x in (g.get("picks") or []) if x.get("market") in GAME_LINE_MARKETS]
            if k not in seen or len(gl) > len(seen[k][1]):
                seen[k] = (g, gl)
        for k, (g, gl) in seen.items():
            actual = actuals.get(k)
            if not actual:
                continue
            games += 1
            closing = fetch_closing_probs(actual.get("event_id"))
            rec = recommended_pick(g)
            if rec:
                gr = grade_pick(rec, actual)
                if gr: rec_led.add(gr)
                c = clv_for_pick(rec, closing, actual)
                if c is not None: rec_clv.append(c)
            wp = model_winner_pick(g)
            if wp:
                gr = grade_pick(wp, actual)
                if gr: winner_led.add(gr)
            tp = model_total_pick(g)
            if tp:
                gr = grade_pick(tp, actual)
                if gr: total_led.add(gr)
            for pk in gl:
                gr = grade_pick(pk, actual)
                if not gr: continue
                all_led.add(gr)
                c = clv_for_pick(pk, closing, actual)
                if c is not None: all_clv.append(c)

    print("=" * 60)
    print(f"WNBA BACKTEST — {gdates} dates, {games} games, ${STAKE:.0f}/bet")
    print("=" * 60)

    def clv_block(s):
        if not s:
            return {"n": 0, "beat_close_pct": None, "avg_clv": None}
        n = len(s)
        return {"n": n, "beat_close_pct": round(sum(1 for x in s if x > 0) / n * 100, 0),
                "avg_clv": round(sum(s) / n, 2)}

    def acc_block(led):
        dec = led.w + led.l
        return {"n": dec,
                "accuracy_pct": round(led.w / dec * 100, 1) if dec else None,
                "record": f"{led.w}-{led.l}" + (f"-{led.p}" if led.p else ""),
                "roi_pct": round(led.profit / led.staked * 100, 1) if led.staked else None}

    rc, ac = clv_block(rec_clv), clv_block(all_clv)
    win, tot = acc_block(winner_led), acc_block(total_led)
    print(f"WINNER   {win['record']}  acc {win['accuracy_pct']}%  ROI {win['roi_pct']}% (n={win['n']})")
    print(f"TOTALS   {tot['record']}  acc {tot['accuracy_pct']}%  ROI {tot['roi_pct']}% (n={tot['n']})")
    r = rec_led.summary()
    print(f"RECOMMENDED  {r['wins']}-{r['losses']}  ROI {r['roi_pct']}%   "
          f"beat-close {rc['beat_close_pct']}% (n={rc['n']}) avgCLV {rc['avg_clv']}")

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "graded_dates": gdates, "graded_games": games,
        "winner": win, "totals": tot,
        "recommended": rec_led.summary(), "all_lines": all_led.summary(),
        "clv_recommended": rc, "clv_all": ac,
    }
    with open(DATA_DIR / "wnba_performance.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {DATA_DIR / 'wnba_performance.json'}")


def _range(a, b):
    d0 = datetime.strptime(a, "%Y-%m-%d").date()
    d1 = datetime.strptime(b, "%Y-%m-%d").date()
    out, cur = [], d0
    while cur <= d1:
        out.append(cur.strftime("%Y-%m-%d")); cur += timedelta(days=1)
    return out


def main():
    a = sys.argv[1:]
    if len(a) >= 2:
        dates = _range(a[0], a[1])
    elif len(a) == 1:
        dates = [a[0]]
    else:
        dates = sorted(p.stem.replace("wnba_predictions_", "")
                       for p in DATA_DIR.glob("wnba_predictions_*.json"))
    run(dates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
