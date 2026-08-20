"""WNBA game prediction engine — 100% free, keyless data sources.

Data sources (no API keys, no paid tiers, no BALLDONTLIE):
  - Games + odds (moneyline / total / spread, with prices): ESPN's free API
      cdn.espn.com/core/wnba/scoreboard  and
      sports.core.api.espn.com/.../events/{id}/competitions/{id}/odds
  - Team points-for / points-against: ESPN core `/teams/{id}/record`
  - Player per-game season stats: stats.wnba.com (official league stats API)

Player-prop odds have NO free source, so prop bets are not generated (the model
still projects player stat lines in the boxscore).

Pipeline for one date:
  1. ESPN scoreboard -> games (+ event ids for odds).
  2. Team scoring from ESPN PPG-for/against; rosters from stats.wnba.com.
  3. 10,000 Monte Carlo sims -> team points + per-player stat lines.
  4. Compare model probs to the market (shrunk toward it) -> EV%. Rank picks.
  5. Write data/wnba_predictions_YYYY-MM-DD.json (consumed by wnba_dashboard.py).

Usage:
    python wnba_predict.py                 # today
    python wnba_predict.py 2026-08-15      # specific date
"""

from __future__ import annotations

import csv
import json
import os
import random
import statistics
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CWD = Path(__file__).parent
DATA_DIR = CWD / "data"
DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR = DATA_DIR / "cache_wnba"
CACHE_DIR.mkdir(exist_ok=True)

# Primary WNBA schedule + odds: The Odds API (keyed). ESPN is the free fallback.
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/basketball_wnba/odds/"
ODDS_API_CACHE = DATA_DIR / "oddsapi_wnba_cache.json"   # committed; TTL-limited to save quota
ODDS_API_TTL_HOURS = 5.0                                # ~4 fetches/day × 3 req = ~360/mo (< 500 free)
ESPN_SB = "https://cdn.espn.com/core/wnba/scoreboard"   # ?xhr=1&dates=YYYYMMDD (free fallback)
ESPN_ODDS = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/events/{eid}/competitions/{eid}/odds"
ESPN_TEAM_RECORD = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/seasons/{yr}/types/2/teams/{tid}/record"
WNBA_STATS = "https://stats.wnba.com/stats/leaguedashplayerstats"


def _odds_api_key() -> str:
    import os
    k = os.environ.get("ODDS_API_KEY")
    if k:
        return k.strip()
    cfg = CWD / "mlb_config.json"
    if cfg.exists():
        try:
            return (json.load(open(cfg)).get("odds_api_key") or "").strip()
        except Exception:
            return ""
    return ""

# League baselines (WNBA team ~81 pts/game). Used when a team's PPG is unknown.
LEAGUE_AVG_PTS = 81.5
N_SIMS = 10_000

# Regression: games at which a per-game average is ~half-stabilized. WNBA seasons
# are short (~40 games), so early-season averages are noisy — regress each rate
# toward what the player's MINUTES imply (league per-minute rate × minutes).
WNBA_REG_GAMES = 6.0
# League per-minute rates among rotation players, computed once from the season
# pool (self-calibrating); these fallbacks are used only before that runs.
_WNBA_LG_PERMIN = {"pts": 0.42, "reb": 0.185, "ast": 0.105,
                   "stl": 0.033, "blk": 0.022, "fg3m": 0.046}


def _compute_league_per_min(all_players: list[dict]) -> None:
    """Minutes-weighted league per-minute rates from rotation players (min ≥ 15,
    gp ≥ 3). Stored globally so the prop model can regress each player toward the
    production their minutes imply, instead of reading small-sample noise."""
    global _WNBA_LG_PERMIN
    acc = {k: 0.0 for k in _WNBA_LG_PERMIN}
    tot_min = 0.0
    for p in all_players:
        s = p.get("stats", {})
        gp = s.get("games_played") or 0
        mn = s.get("min") or 0
        if gp < 3 or mn < 15:
            continue
        pm = gp * mn
        tot_min += pm
        for k in acc:
            acc[k] += (s.get(k) or 0) / mn * pm  # per-min rate weighted by total minutes
    if tot_min > 0:
        _WNBA_LG_PERMIN = {k: acc[k] / tot_min for k in acc}

# Shrink model probs toward the devigged market (raw sim is overconfident).
MODEL_TRUST = 0.35

REFERENCE_VENDORS = ("draftkings", "fanduel", "betmgm", "caesars", "betrivers", "fanatics")
BOOK_LABEL = {
    "draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM",
    "caesars": "Caesars", "betrivers": "BetRivers", "fanatics": "Fanatics",
}

# ---- HTTP helpers (all free, keyless) --------------------------------------

_WNBA_STATS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.wnba.com/",
    "Origin": "https://www.wnba.com",
    "Accept": "application/json, text/plain, */*",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}


def _get_json(url: str, headers: dict | None = None, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0 (wnba)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _american(node) -> float | None:
    if node is None:
        return None
    if isinstance(node, (int, float)):
        return float(node)
    am = node.get("american") if isinstance(node, dict) else None
    if am is None:
        return None
    try:
        return float(str(am).replace("+", ""))
    except (TypeError, ValueError):
        return None


# ---- Games + odds (ESPN) ----------------------------------------------------

def _is_et_date(iso_utc: str, date_str: str) -> bool:
    """True if an ESPN UTC game time falls on `date_str` (YYYY-MM-DD) in ET."""
    if not iso_utc:
        return False
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(ET)
        return dt.strftime("%Y-%m-%d") == date_str
    except Exception:
        return False


def _read_odds_cache() -> tuple[float, list]:
    """Return (fetched_at_epoch, games) from the committed cache. The timestamp is
    stored INSIDE the file (git checkout resets mtimes, so mtime can't be trusted)."""
    try:
        c = json.load(open(ODDS_API_CACHE))
        return float(c.get("_fetched_at", 0)), (c.get("data") or [])
    except Exception:
        return 0.0, []


def _fetch_oddsapi() -> list[dict]:
    """All upcoming WNBA games with odds from The Odds API — one call, disk-cached
    with an in-file TTL to stay under the 500-request/month free quota (each call =
    3 requests). Returns the raw list (commence_time, teams, bookmakers) or []."""
    fetched_at, cached = _read_odds_cache()
    if cached and (time.time() - fetched_at) / 3600.0 < ODDS_API_TTL_HOURS:
        return cached
    key = _odds_api_key()
    if not key:
        return cached  # no key → serve whatever's cached (may be empty)
    params = {"apiKey": key, "regions": "us",
              "markets": "h2h,spreads,totals", "oddsFormat": "american"}
    try:
        d = _get_json(f"{ODDS_API_URL}?{urllib.parse.urlencode(params)}")
        with open(ODDS_API_CACHE, "w") as f:
            json.dump({"_fetched_at": time.time(), "data": d}, f)
        return d
    except Exception as e:
        print(f"  (Odds API fetch failed: {e})", file=sys.stderr)
        return cached  # serve stale rather than nothing


# Map a WNBA team's full name (from The Odds API) to the stats.wnba.com abbreviation
# by its nickname (last word) — unique across the league.
_WNBA_NICK_TO_ABBR = {
    "wings": "DAL", "valkyries": "GSV", "sparks": "LAS", "sun": "CON", "fever": "IND",
    "tempo": "TOR", "liberty": "NYL", "sky": "CHI", "dream": "ATL", "aces": "LVA",
    "storm": "SEA", "mercury": "PHX", "lynx": "MIN", "mystics": "WAS", "fire": "PDX",
}


def _oddsapi_games_for_date(date_str: str) -> list[dict]:
    """Normalized WNBA games for a date from The Odds API (correct dates + odds)."""
    out = []
    for g in _fetch_oddsapi():
        ct = g.get("commence_time", "")
        try:
            et = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            continue
        if et.strftime("%Y-%m-%d") != date_str:
            continue

        def team_obj(name):
            nick = (name or "").split()[-1].lower() if name else ""
            return {"id": None, "abbreviation": _WNBA_NICK_TO_ABBR.get(nick, ""),
                    "full_name": name, "name": (name or "").split()[-1]}

        out.append({
            "id": g.get("id"),
            "date": ct,
            "home_team": team_obj(g.get("home_team", "")),
            "visitor_team": team_obj(g.get("away_team", "")),
            "_oddsapi": g,   # raw odds, parsed by fetch_odds
        })
    return out


def fetch_games(date_str: str) -> list[dict]:
    """WNBA games for a date — The Odds API (keyed, reliable schedule + odds) first,
    ESPN free scoreboard as fallback. Both keep only games on the requested ET date."""
    games = _oddsapi_games_for_date(date_str)
    if games:
        return games

    # ---- ESPN fallback (free, keyless) ----
    base = datetime.strptime(date_str, "%Y-%m-%d").date()
    events, seen_ids = [], set()
    for delta in (0, -1, 1):
        yyyymmdd = (base + timedelta(days=delta)).strftime("%Y%m%d")
        try:
            d = _get_json(f"{ESPN_SB}?xhr=1&dates={yyyymmdd}")
        except Exception:
            continue
        for ev in (d.get("content", {}).get("sbData", {}).get("events") or d.get("events") or []):
            eid = ev.get("id")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                events.append(ev)

    out = []
    for ev in events:
        try:
            comp = ev["competitions"][0]
            comps = comp.get("competitors", [])
            home = next(c for c in comps if c.get("homeAway") == "home")
            away = next(c for c in comps if c.get("homeAway") == "away")
        except (KeyError, IndexError, StopIteration):
            continue

        iso = ev.get("date", "")
        if not _is_et_date(iso, date_str):
            continue

        def team_obj(c):
            t = c.get("team", {})
            return {"id": t.get("id"), "abbreviation": t.get("abbreviation", ""),
                    "full_name": t.get("displayName", ""),
                    "name": t.get("shortDisplayName", "") or t.get("name", "")}

        out.append({
            "id": ev.get("id"),
            "date": iso,
            "home_team": team_obj(home),
            "visitor_team": team_obj(away),  # keep old key name used downstream
        })
    return out


def _parse_oddsapi_game(g: dict) -> list[dict]:
    """Convert one The Odds API game (bookmakers/markets/outcomes) into our internal
    odds-dict shape. Prefers DraftKings, falls back to the first book with all lines."""
    home_name = g.get("home_team", ""); away_name = g.get("away_team", "")
    books = g.get("bookmakers", []) or []
    def rank(b):
        return 0 if b.get("key") == "draftkings" else 1
    for b in sorted(books, key=rank):
        mk = {m.get("key"): m for m in b.get("markets", [])}
        row = {"vendor": b.get("key", ""),
               "moneyline_home_odds": None, "moneyline_away_odds": None,
               "total_value": None, "total_over_odds": None, "total_under_odds": None,
               "spread_home_value": None, "spread_away_value": None,
               "spread_home_odds": None, "spread_away_odds": None}
        for o in (mk.get("h2h", {}) or {}).get("outcomes", []):
            if o.get("name") == home_name: row["moneyline_home_odds"] = o.get("price")
            elif o.get("name") == away_name: row["moneyline_away_odds"] = o.get("price")
        for o in (mk.get("totals", {}) or {}).get("outcomes", []):
            row["total_value"] = o.get("point")
            if o.get("name") == "Over": row["total_over_odds"] = o.get("price")
            elif o.get("name") == "Under": row["total_under_odds"] = o.get("price")
        for o in (mk.get("spreads", {}) or {}).get("outcomes", []):
            if o.get("name") == home_name:
                row["spread_home_value"] = o.get("point"); row["spread_home_odds"] = o.get("price")
            elif o.get("name") == away_name:
                row["spread_away_value"] = o.get("point"); row["spread_away_odds"] = o.get("price")
        if row["moneyline_home_odds"] is not None or row["total_value"] is not None:
            return [row]
    return []


def fetch_odds(event_id) -> list[dict]:
    """Odds for a game. Accepts either a game dict (The Odds API path — parses its
    embedded odds) or an ESPN event id (fallback → ESPN odds endpoint)."""
    if isinstance(event_id, dict):
        g = event_id
        if g.get("_oddsapi"):
            return _parse_oddsapi_game(g["_oddsapi"])
        event_id = g.get("id")
    if not event_id:
        return []
    try:
        d = _get_json(ESPN_ODDS.format(eid=event_id))
    except Exception as e:
        print(f"  (ESPN odds fetch failed for {event_id}: {e})", file=sys.stderr)
        return []
    items = [it for it in d.get("items", [])
             if "live" not in (it.get("provider", {}).get("name", "").lower())]
    if not items:
        return []
    items.sort(key=lambda it: 0 if "draftkings" in it.get("provider", {}).get("name", "").lower() else 1)
    it = items[0]
    home = it.get("homeTeamOdds", {}) or {}
    away = it.get("awayTeamOdds", {}) or {}
    try:
        spread_home = float(it.get("spread")) if it.get("spread") is not None else None
    except (TypeError, ValueError):
        spread_home = None

    def side_spread_odds(node):
        cur = node.get("current") or node.get("close") or node.get("open") or {}
        return _american(cur.get("spread"))

    return [{
        "vendor": "draftkings",
        "moneyline_home_odds": _american(home.get("moneyLine")),
        "moneyline_away_odds": _american(away.get("moneyLine")),
        "total_value": it.get("overUnder"),
        "total_over_odds": _american(it.get("overOdds")),
        "total_under_odds": _american(it.get("underOdds")),
        "spread_home_value": spread_home,
        "spread_away_value": (-spread_home) if spread_home is not None else None,
        "spread_home_odds": side_spread_odds(home),
        "spread_away_odds": side_spread_odds(away),
    }]


def fetch_player_props(event_id) -> list[dict]:
    """No free source for WNBA player-prop odds — always empty."""
    return []


# ---- Team scoring (ESPN record) + player stats (stats.wnba.com) -------------

_team_score_cache: dict[str, dict] = {}   # espn team id -> {pf, pa}
_players_cache: dict[int, list[dict]] = {}  # season -> list of player dicts


def fetch_team_scoring(espn_team_id, season: int) -> dict:
    """Return {'pf': avgPointsFor, 'pa': avgPointsAgainst} for an ESPN team id.
    Odds-API games carry no ESPN id → returns {} (model falls back to league avg)."""
    if not espn_team_id:
        return {"pf": None, "pa": None}
    key = str(espn_team_id)
    if key in _team_score_cache:
        return _team_score_cache[key]
    pf = pa = None
    try:
        d = _get_json(ESPN_TEAM_RECORD.format(yr=season, tid=espn_team_id), timeout=15)
        for item in d.get("items", []):
            for st in item.get("stats", []):
                if st.get("name") == "avgPointsFor":
                    pf = st.get("value")
                elif st.get("name") == "avgPointsAgainst":
                    pa = st.get("value")
    except Exception as e:
        print(f"  (ESPN team record failed for {espn_team_id}: {e})", file=sys.stderr)
    res = {"pf": pf, "pa": pa}
    _team_score_cache[key] = res
    return res


def fetch_all_player_stats(season: int) -> list[dict]:
    """All WNBA players' per-game season stats from the official stats API. stats.wnba.com
    is flaky (intermittent timeouts), so the result is cached to DISK — a good fetch
    persists across runs and covers runs where the API times out (otherwise box scores
    come back empty). Committed by the workflow so CI runs share it too."""
    if season in _players_cache:
        return _players_cache[season]
    disk = DATA_DIR / f"wnba_players_{season}.json"
    # Serve a fresh-enough disk cache without hitting the API.
    if disk.exists():
        try:
            c = json.load(open(disk))
            if c.get("data") and (time.time() - float(c.get("_fetched_at", 0))) < 24 * 3600:
                _players_cache[season] = c["data"]
                return c["data"]
        except Exception:
            pass
    params = {
        "College": "", "Conference": "", "Country": "", "DateFrom": "", "DateTo": "",
        "Division": "", "DraftPick": "", "DraftYear": "", "GameScope": "",
        "GameSegment": "", "Height": "", "LastNGames": "0", "LeagueID": "10",
        "Location": "", "MeasureType": "Base", "Month": "0", "OpponentTeamID": "0",
        "Outcome": "", "PORound": "0", "PaceAdjust": "N", "PerMode": "PerGame",
        "Period": "0", "PlayerExperience": "", "PlayerPosition": "", "PlusMinus": "N",
        "Rank": "N", "Season": str(season), "SeasonSegment": "",
        "SeasonType": "Regular Season", "ShotClockRange": "", "StarterBench": "",
        "TeamID": "0", "VsConference": "", "VsDivision": "", "Weight": "",
    }
    url = f"{WNBA_STATS}?{urllib.parse.urlencode(params)}"
    players: list[dict] = []
    try:
        d = _get_json(url, headers=_WNBA_STATS_HEADERS, timeout=40)
        rs = d["resultSets"][0]
        idx = {h: n for n, h in enumerate(rs["headers"])}
        for r in rs["rowSet"]:
            def g(col):
                return r[idx[col]] if col in idx else None
            players.append({
                "player_id": g("PLAYER_ID"),
                "name": g("PLAYER_NAME"),
                "team_id": g("TEAM_ID"),
                "team_abbr": g("TEAM_ABBREVIATION"),
                "stats": {
                    "games_played": g("GP"), "min": g("MIN"), "pts": g("PTS"),
                    "reb": g("REB"), "ast": g("AST"), "stl": g("STL"),
                    "blk": g("BLK"), "fg3m": g("FG3M"), "fgm": g("FGM"),
                    "fga": g("FGA"), "ftm": g("FTM"), "fta": g("FTA"),
                },
            })
    except Exception as e:
        print(f"  (WNBA player stats fetch failed: {e})", file=sys.stderr)

    if players:
        # Persist a good fetch to disk for future/flaky runs.
        try:
            with open(disk, "w") as f:
                json.dump({"_fetched_at": time.time(), "data": players}, f)
        except Exception:
            pass
    elif disk.exists():
        # Fetch failed — fall back to the last good disk cache (even if stale).
        try:
            players = json.load(open(disk)).get("data") or []
            print(f"  (using cached WNBA player stats — {len(players)} players)", file=sys.stderr)
        except Exception:
            pass
    _players_cache[season] = players
    return players


# ---- Odds helpers -----------------------------------------------------------

def american_to_prob(odds) -> float:
    if odds is None:
        return 0.0
    try:
        odds = float(odds)
    except (TypeError, ValueError):
        return 0.0
    if abs(odds) < 100:
        return 0.0
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return (-odds) / ((-odds) + 100.0)


def _valid_odds(o) -> bool:
    try:
        return o is not None and abs(float(o)) >= 100
    except (TypeError, ValueError):
        return False


def dev_from_market(a_odds, b_odds) -> tuple[float, float]:
    """Devig a two-way market to probabilities that sum to 1."""
    pa, pb = american_to_prob(a_odds), american_to_prob(b_odds)
    tot = pa + pb
    if tot <= 0:
        return 0.0, 0.0
    return pa / tot, pb / tot


def prob_to_american(p: float) -> int:
    if p <= 0 or p >= 1:
        return 0
    if p >= 0.5:
        return round(-100 * p / (1 - p))
    return round(100 * (1 - p) / p)


def ev_pct(model_prob: float, american_odds) -> float:
    if american_odds is None or model_prob <= 0:
        return -100.0
    try:
        odds = float(american_odds)
    except (TypeError, ValueError):
        return -100.0
    if abs(odds) < 100:
        return -100.0
    payout = (odds / 100.0) if odds > 0 else (100.0 / -odds)
    return (model_prob * payout - (1 - model_prob)) * 100.0


def _vig(p_over: float, p_under: float) -> float:
    return abs((p_over + p_under) - 1.0)


def _pick_market(entries: list[dict], over_key: str, under_key: str,
                 line_key: str | None = None, restrict_line=None) -> dict | None:
    candidates = []
    for e in entries:
        over = e.get(over_key)
        under = e.get(under_key)
        if over is None or under is None:
            continue
        try:
            over_f = float(over); under_f = float(under)
        except (TypeError, ValueError):
            continue
        if abs(over_f) < 100 or abs(under_f) < 100:
            continue
        if line_key is not None:
            lv = e.get(line_key)
            try:
                lv_f = float(lv) if lv is not None else None
            except (TypeError, ValueError):
                lv_f = None
            if restrict_line is not None and lv_f != restrict_line:
                continue
        vig = _vig(american_to_prob(over_f), american_to_prob(under_f))
        if vig >= 0.08:
            continue
        candidates.append((vig, e))
    if not candidates:
        return None
    def rank_key(pair):
        vig, e = pair
        vendor = e.get("vendor", "")
        try:
            v_rank = REFERENCE_VENDORS.index(vendor)
        except ValueError:
            v_rank = len(REFERENCE_VENDORS)
        return (v_rank, vig)
    candidates.sort(key=rank_key)
    return candidates[0][1]


def _modal_value(entries: list[dict], key: str, only_abs: float | None = None) -> float | None:
    vals = []
    for e in entries:
        v = e.get(key)
        if v is None or v == "":
            continue
        try:
            vf = float(v)
        except (TypeError, ValueError):
            continue
        if only_abs is not None and abs(vf) != only_abs:
            continue
        vals.append(vf)
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


# ---- Simulator --------------------------------------------------------------

def sim_game(away_ctx: dict, home_ctx: dict, n_sims: int = N_SIMS) -> dict:
    """Simulate n_sims games. Team scoring: Normal(mean, sd≈10.5) where the mean
    blends a team's own points-per-game with the opponent's points-allowed
    (both from ESPN). Player stats: independent per-player Normal draws."""
    rng = random.Random(42)

    def team_mean(off_ctx: dict, def_ctx: dict) -> float:
        # Team offense vs opponent defense: average the team's PPG-for with the
        # opponent's PPG-against. Missing values fall back to the league average.
        pf = off_ctx.get("pf") or LEAGUE_AVG_PTS
        opp_pa = def_ctx.get("pa") or LEAGUE_AVG_PTS
        return (pf + opp_pa) / 2.0

    away_mean = team_mean(away_ctx, home_ctx)
    home_mean = team_mean(home_ctx, away_ctx)
    # Home court is ~2 pts in WNBA
    home_mean += 1.5
    away_mean -= 0.5

    away_scores = [max(30, int(round(rng.gauss(away_mean, 10.5)))) for _ in range(n_sims)]
    home_scores = [max(30, int(round(rng.gauss(home_mean, 10.5)))) for _ in range(n_sims)]

    # Game pace factor: a faster/higher-scoring game gives EVERY player more
    # possessions → more counting stats. Proxy pace by the projected game total
    # relative to the league-average total, capped so it can't run away.
    pace_factor = max(0.90, min(1.12,
                                (away_mean + home_mean) / (2.0 * LEAGUE_AVG_PTS)))

    def _reg(stat_key, val, gp, minutes):
        """Regress a per-game average toward what the player's minutes imply."""
        prior = _WNBA_LG_PERMIN.get(stat_key, 0.0) * (minutes or 0)
        return (val * gp + prior * WNBA_REG_GAMES) / (gp + WNBA_REG_GAMES)

    # Player props — simulate top rotation players.
    def player_samples(players: list[dict], opp_def_factor: float) -> dict:
        out = {}
        # Scoring defense scaler: leaky defense (>1) lifts scoring, tough (<1) cuts it.
        # Damped (0.5 exponent) so one team's PPG-against doesn't swing props wildly.
        score_adj = opp_def_factor ** 0.5
        for p in players:
            stats = p.get("stats", {})
            gp = stats.get("games_played") or 1
            if gp < 3:
                continue  # too little data
            min_played = stats.get("min") or 0
            if min_played < 12:
                continue  # deep bench, skip

            # Regress each rate toward its minutes-implied prior (small samples pull
            # toward league), then apply pace to all and defense to scoring only.
            pts = _reg("pts", stats.get("pts") or 0, gp, min_played) * pace_factor * score_adj
            reb = _reg("reb", stats.get("reb") or 0, gp, min_played) * pace_factor
            ast = _reg("ast", stats.get("ast") or 0, gp, min_played) * pace_factor
            stl = _reg("stl", stats.get("stl") or 0, gp, min_played) * pace_factor
            blk = _reg("blk", stats.get("blk") or 0, gp, min_played) * pace_factor
            fg3m = _reg("fg3m", stats.get("fg3m") or 0, gp, min_played) * pace_factor * score_adj
            fgm = stats.get("fgm") or 0
            fga = stats.get("fga") or 0
            ftm = stats.get("ftm") or 0
            fta = stats.get("fta") or 0

            # Shared per-game "game-script" multiplier (minutes/usage swings — foul
            # trouble, blowouts, hot nights) drawn ONCE per sim and applied to all of
            # a player's lines, so pts/reb/ast rise and fall together. This gives
            # realistic correlation for combo props (PRA, P+R) and fatter, truer tails
            # than independent draws — the tier-1 stand-in for full minutes projection.
            pts_s, reb_s, ast_s, thr_s, stl_s, blk_s = [], [], [], [], [], []
            for _ in range(n_sims):
                gs = max(0.45, rng.gauss(1.0, 0.13))
                pts_s.append(max(0, rng.gauss(pts * gs, max(2.6, pts * 0.30))))
                reb_s.append(max(0, rng.gauss(reb * gs, max(1.4, reb * 0.34))))
                ast_s.append(max(0, rng.gauss(ast * gs, max(1.1, ast * 0.40))))
                thr_s.append(max(0, rng.gauss(fg3m * gs, max(0.8, fg3m * 0.50))))
                stl_s.append(max(0, rng.gauss(stl * gs, max(0.8, stl * 0.45))))
                blk_s.append(max(0, rng.gauss(blk * gs, max(0.6, blk * 0.50))))

            out[p["bdl_id"]] = {
                "name": p["name"],
                "pos": p.get("pos", ""),
                "min": min_played,
                "pts": pts_s,
                "reb": reb_s,
                "ast": ast_s,
                "threes": thr_s,
                "stl": stl_s,
                "blk": blk_s,
                "fgm_avg": fgm, "fga_avg": fga,
                "ftm_avg": ftm, "fta_avg": fta,
                "fg3m_avg": fg3m,
            }
        return out

    # Opp defense factor: opp points-allowed / league avg. >1 = leaky defense
    # (player scoring scales up); <1 = tough defense.
    away_opp_def_factor = (home_ctx.get("pa") or LEAGUE_AVG_PTS) / LEAGUE_AVG_PTS
    home_opp_def_factor = (away_ctx.get("pa") or LEAGUE_AVG_PTS) / LEAGUE_AVG_PTS

    away_player_samples = player_samples(away_ctx["roster"], away_opp_def_factor)
    home_player_samples = player_samples(home_ctx["roster"], home_opp_def_factor)

    return {
        "away_scores": away_scores,
        "home_scores": home_scores,
        "away_players": away_player_samples,
        "home_players": home_player_samples,
    }


# ---- Pick generation --------------------------------------------------------

def pct_over(samples: list[float], line: float) -> float:
    if not samples:
        return 0.0
    push = 0.0
    over = 0
    for s in samples:
        if s > line:
            over += 1
        elif s == line:
            push += 0.5
    return (over + push) / len(samples)


def generate_picks(sim: dict, odds: list[dict], props: list[dict],
                   away_ctx: dict, home_ctx: dict) -> list[dict]:
    picks = []
    n = len(sim["away_scores"])

    def _mk_pick(market, side, line, raw_prob, entry, odds_key, fair_prob=None):
        odds_val = entry.get(odds_key) if entry else None
        if odds_val is None:
            return
        vendor = entry.get("vendor", "")
        if fair_prob is not None and fair_prob > 0:
            used = MODEL_TRUST * raw_prob + (1 - MODEL_TRUST) * fair_prob
        else:
            used = raw_prob
        return {
            "market": market, "side": side, "line": line,
            "model_prob": round(used, 4),
            "raw_model_prob": round(raw_prob, 4),
            "market_fair_prob": round(fair_prob, 4) if fair_prob is not None else None,
            "market_odds": odds_val,
            "market_prob": round(american_to_prob(odds_val), 4),
            "ev_pct": round(ev_pct(used, odds_val), 2),
            "fair_odds": prob_to_american(used),
            "book": BOOK_LABEL.get(vendor, vendor or "—"),
        }

    # --- Moneyline ---
    away_wins = sum(1 for a, h in zip(sim["away_scores"], sim["home_scores"]) if a > h)
    ties = sum(1 for a, h in zip(sim["away_scores"], sim["home_scores"]) if a == h)
    p_away = (away_wins + ties * 0.5) / n
    p_home = 1 - p_away

    ml_entry = _pick_market(odds, "moneyline_home_odds", "moneyline_away_odds")
    if ml_entry:
        fair_home, fair_away = dev_from_market(ml_entry.get("moneyline_home_odds"),
                                               ml_entry.get("moneyline_away_odds"))
        for side_label, prob, key, fp in (
            (f"{away_ctx['team']} ML", p_away, "moneyline_away_odds", fair_away),
            (f"{home_ctx['team']} ML", p_home, "moneyline_home_odds", fair_home),
        ):
            pk = _mk_pick("Moneyline", side_label, None, prob, ml_entry, key, fair_prob=fp)
            if pk: picks.append(pk)

    # --- Total (main line = modal total value across vendors) ---
    totals = [a + h for a, h in zip(sim["away_scores"], sim["home_scores"])]
    main_total = _modal_value(odds, "total_value")
    if main_total is not None:
        entry = _pick_market(odds, "total_over_odds", "total_under_odds",
                             line_key="total_value", restrict_line=main_total)
        if entry:
            p_over = pct_over(totals, main_total)
            fair_over, fair_under = dev_from_market(entry.get("total_over_odds"),
                                                    entry.get("total_under_odds"))
            pk = _mk_pick("Total", f"Over {main_total:g}", main_total, p_over, entry, "total_over_odds", fair_prob=fair_over)
            if pk: picks.append(pk)
            pk = _mk_pick("Total", f"Under {main_total:g}", main_total, 1 - p_over, entry, "total_under_odds", fair_prob=fair_under)
            if pk: picks.append(pk)

    # --- Spread ---
    main_spread = _modal_value(odds, "spread_home_value")
    if main_spread is not None:
        entry = _pick_market(odds, "spread_home_odds", "spread_away_odds",
                             line_key="spread_home_value", restrict_line=main_spread)
        if entry:
            margins = [h - a for a, h in zip(sim["away_scores"], sim["home_scores"])]
            p_home_cover = sum(1 for m in margins if m + main_spread > 0) / n
            p_away_cover = 1 - p_home_cover
            fair_home, fair_away = dev_from_market(entry.get("spread_home_odds"),
                                                   entry.get("spread_away_odds"))
            pk = _mk_pick("Spread", f"{home_ctx['team']} {main_spread:+g}", main_spread,
                          p_home_cover, entry, "spread_home_odds", fair_prob=fair_home)
            if pk: picks.append(pk)
            pk = _mk_pick("Spread", f"{away_ctx['team']} {-main_spread:+g}", -main_spread,
                          p_away_cover, entry, "spread_away_odds", fair_prob=fair_away)
            if pk: picks.append(pk)

    # --- Player props ---
    prop_index = {}
    for src in (sim["away_players"], sim["home_players"]):
        for pid, samples in src.items():
            prop_index[pid] = samples

    grouped_all = defaultdict(list)
    for pr in props:
        grouped_all[(pr.get("player_id"), pr.get("prop_type"))].append(pr)

    prop_sample_key = {
        "points": "pts",
        "rebounds": "reb",
        "assists": "ast",
        "threes": "threes",
        "steals": "stl",
        "blocks": "blk",
        "points_rebounds_assists": None,  # combined
        "points_rebounds": None,
        "points_assists": None,
        "rebounds_assists": None,
    }
    prop_display_label = {
        "points": "Pts", "rebounds": "Reb", "assists": "Ast",
        "threes": "3PM", "steals": "Stl", "blocks": "Blk",
        "points_rebounds_assists": "PRA",
        "points_rebounds": "P+R", "points_assists": "P+A",
        "rebounds_assists": "R+A",
    }

    for (pid, ptype), all_entries in grouped_all.items():
        if ptype not in prop_sample_key:
            continue
        line_counts = Counter(e.get("line_value") for e in all_entries
                              if e.get("line_value") is not None)
        if not line_counts:
            continue
        main_line_str, main_line_count = line_counts.most_common(1)[0]
        if main_line_count < 2:
            continue  # need multi-vendor consensus
        try:
            main_line = float(main_line_str)
        except (TypeError, ValueError):
            continue
        entries = [e for e in all_entries if e.get("line_value") == main_line_str]
        vendor_rows = [
            {"vendor": e.get("vendor"),
             "over_odds":  e.get("market", {}).get("over_odds"),
             "under_odds": e.get("market", {}).get("under_odds")}
            for e in entries
        ]
        chosen = _pick_market(vendor_rows, "over_odds", "under_odds")
        if not chosen:
            continue

        p_data = prop_index.get(pid)
        if not p_data:
            continue

        # Build the sample series for this prop_type
        sub_key = prop_sample_key[ptype]
        if sub_key:
            samples = p_data.get(sub_key) or []
        else:
            # Combined prop — sum sub-series pointwise
            keys = []
            if ptype == "points_rebounds_assists": keys = ["pts", "reb", "ast"]
            elif ptype == "points_rebounds":       keys = ["pts", "reb"]
            elif ptype == "points_assists":        keys = ["pts", "ast"]
            elif ptype == "rebounds_assists":      keys = ["reb", "ast"]
            if not keys:
                continue
            arrays = [p_data.get(k) or [] for k in keys]
            if not all(arrays):
                continue
            samples = [sum(vals) for vals in zip(*arrays)]

        if not samples:
            continue

        label = f"{p_data['name']} {prop_display_label[ptype]}"
        p_over = pct_over(samples, main_line)
        pk = _mk_pick(label, f"Over {main_line:g}", main_line, p_over, chosen, "over_odds")
        if pk: picks.append(pk)
        pk = _mk_pick(label, f"Under {main_line:g}", main_line, 1 - p_over, chosen, "under_odds")
        if pk: picks.append(pk)

    def _keep(p):
        if not _valid_odds(p.get("market_odds")):
            return False
        mp = p.get("market_prob") or 0
        return 0.08 <= mp <= 0.92
    return [p for p in picks if _keep(p)]


# ---- Projected boxscore -----------------------------------------------------

def _mean(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def build_projected_boxscore(sim: dict, away_ctx: dict, home_ctx: dict) -> dict:
    def team_box(side: str, ctx: dict) -> dict:
        samples_map = sim[f"{side}_players"]
        rows = []
        totals = {"min": 0.0, "pts": 0.0, "reb": 0.0, "ast": 0.0,
                  "stl": 0.0, "blk": 0.0, "threes": 0.0}
        for p in ctx["roster"]:
            pid = p["bdl_id"]
            s = samples_map.get(pid)
            if not s:
                continue
            row = {
                "name": p["name"], "pos": p.get("pos", ""),
                "min": round(s["min"], 1),
                "pts": round(_mean(s["pts"]), 1),
                "reb": round(_mean(s["reb"]), 1),
                "ast": round(_mean(s["ast"]), 1),
                "stl": round(_mean(s["stl"]), 1),
                "blk": round(_mean(s["blk"]), 1),
                "threes": round(_mean(s["threes"]), 1),
                "fg":  f'{s["fgm_avg"]:.1f}/{s["fga_avg"]:.1f}' if s.get("fga_avg") else "—",
                "ft":  f'{s["ftm_avg"]:.1f}/{s["fta_avg"]:.1f}' if s.get("fta_avg") else "—",
            }
            rows.append(row)
            for f in totals:
                totals[f] += row[f]
        # Sort by projected minutes descending
        rows.sort(key=lambda r: -r["min"])
        team_score = _mean(sim[f"{side}_scores"])
        totals["pts"] = round(team_score, 1)
        totals = {k: round(v, 1) for k, v in totals.items()}
        return {"players": rows, "totals": totals}

    return {
        "away": team_box("away", away_ctx),
        "home": team_box("home", home_ctx),
    }


# ---- Context builder --------------------------------------------------------

# Normalize both ESPN and stats.wnba.com team abbreviations to a single canonical
# token so the two sources match. Any abbr not listed maps to itself, so only the
# genuinely-divergent codes need entries here.
_ESPN_ABBR_ALIASES = {
    "POR": "PDX",   # ESPN Portland -> stats PDX
    "LV": "LVA",    # ESPN Las Vegas -> stats LVA
    "CONN": "CON",  # ESPN Connecticut -> stats CON
    "WSH": "WAS",   # ESPN Washington -> stats WAS
    "NY": "NYL",    # ESPN New York -> stats NYL
    "LA": "LAS",    # ESPN Los Angeles -> stats LAS
    "GS": "GSV",    # ESPN Golden State -> stats GSV
    "PHO": "PHX",   # in case ESPN uses PHO
}


def _norm_abbr(a: str) -> str:
    a = (a or "").upper()
    return _ESPN_ABBR_ALIASES.get(a, a)


# ---- Injuries (ESPN core API — free, keyless) -------------------------------
# Which players are actually available tonight. OUT/Doubtful players are removed
# from the projection AND their minutes/usage are redistributed to teammates, so
# the remaining players' props rise the way they do in a real short-handed game.

INJURY_URL = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/"
              "teams/{tid}/injuries?lang=en&region=us")
ESPN_TEAMS_URL = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/"
                  "seasons/{yr}/teams?lang=en&region=us&limit=50")
_inj_mem: dict[str, list[dict]] = {}
_espn_team_idx: dict[int, dict] = {}


def _espn_team_index(season: int) -> dict:
    """Map every normalized team name/abbreviation → ESPN team id. Needed because
    games from the Odds API (the primary source) carry NO ESPN id, which otherwise
    silently disabled BOTH team-scoring and injury lookups on that path."""
    if season in _espn_team_idx:
        return _espn_team_idx[season]
    idx: dict = {}
    try:
        lst = _get_json(ESPN_TEAMS_URL.format(yr=season), timeout=20)
        for it in lst.get("items", []):
            ref = it.get("$ref")
            if not ref:
                continue
            t = _get_json(ref, timeout=15)
            tid = t.get("id")
            if not tid:
                continue
            for key in (t.get("displayName"), t.get("name"), t.get("location"),
                        t.get("shortDisplayName"), t.get("abbreviation")):
                if key:
                    idx[_norm_name(key)] = tid
    except Exception as e:
        print(f"  (ESPN team index failed: {e})", file=sys.stderr)
    _espn_team_idx[season] = idx
    return idx


def resolve_espn_team_id(team: dict, season: int):
    """ESPN team id for a game's team dict — its own id if present, else matched by
    normalized full name / nickname / abbreviation against the ESPN team index."""
    tid = team.get("id")
    if tid:
        return tid
    idx = _espn_team_index(season)
    for key in (team.get("full_name"), team.get("name"), team.get("abbreviation")):
        if key and _norm_name(key) in idx:
            return idx[_norm_name(key)]
    return None


def _injury_active_on(rec: dict, game_date: str) -> bool:
    """Is this injury in effect on the game date? Filters out stale records (the
    estimated return date has passed) and injuries dated after the game."""
    ret = (rec.get("return") or "")[:10]
    onset = (rec.get("onset") or "")[:10]
    if ret and ret <= game_date:      # expected back by game day
        return False
    if onset and onset > game_date:   # injury hasn't happened yet for this date
        return False
    return True


def _norm_name(s: str) -> str:
    """Accent/suffix/punctuation-insensitive name key for matching ESPN ↔ WNBA-stats."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = "".join(ch if (ch.isalnum() or ch == " ") else " " for ch in s)
    parts = [p for p in s.split() if p not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return " ".join(parts).strip()


def fetch_team_injuries(espn_team_id) -> list[dict]:
    """Injury records for one ESPN team id, each: {name, norm, status, cat, detail,
    comment}. cat = 'out' (out/doubtful → removed from projection) or 'gtd' (day-to-
    day/questionable → kept but flagged). Disk-cached 3h with stale fallback so a
    flaky fetch never blocks predictions."""
    if not espn_team_id:
        return []
    key = str(espn_team_id)
    if key in _inj_mem:
        return _inj_mem[key]
    disk = CACHE_DIR / f"inj_{key}.json"
    if disk.exists():
        try:
            c = json.load(open(disk))
            if time.time() - float(c.get("_fetched_at", 0)) < 3 * 3600:
                _inj_mem[key] = c["data"]
                return c["data"]
        except Exception:
            pass
    out: list[dict] = []
    try:
        lst = _get_json(INJURY_URL.format(tid=espn_team_id), timeout=15)
        for it in lst.get("items", []):
            ref = it.get("$ref")
            if not ref:
                continue
            rec = _get_json(ref, timeout=15)
            status = rec.get("status") or ""
            tname = (rec.get("type") or {}).get("name", "")
            if tname in ("INJURY_STATUS_OUT", "INJURY_STATUS_DOUBTFUL") or \
               status.lower() in ("out", "doubtful", "injured reserve", "suspension"):
                cat = "out"
            elif status:
                cat = "gtd"          # day-to-day / questionable / probable → likely plays
            else:
                cat = ""
            ath = rec.get("athlete") or {}
            name = ""
            if isinstance(ath, dict) and ath.get("$ref"):
                try:
                    name = _get_json(ath["$ref"], timeout=15).get("fullName", "")
                except Exception:
                    name = ""
            det = rec.get("details") or {}
            out.append({
                "name": name, "norm": _norm_name(name), "status": status, "cat": cat,
                "detail": det.get("type", ""),
                "onset": (rec.get("date") or "")[:10],
                "return": (det.get("returnDate") or "")[:10],
                "comment": (rec.get("shortComment") or "")[:160],
            })
        try:
            json.dump({"_fetched_at": time.time(), "data": out}, open(disk, "w"))
        except Exception:
            pass
    except Exception as e:
        print(f"  (injury fetch failed team {espn_team_id}: {e})", file=sys.stderr)
        if disk.exists():
            try:
                out = json.load(open(disk)).get("data", [])   # stale is better than none
            except Exception:
                out = []
    _inj_mem[key] = out
    return out


def _apply_injuries(roster: list[dict], injuries: list[dict]) -> list[dict]:
    """Remove OUT players and redistribute their minutes + usage to the remaining
    rotation. Returns the active roster (with boosted stats for those absorbing the
    load). Rebounds/assists redistribute more fully than points (a star's scoring is
    partly lost to weaker offense, but someone still grabs the board / makes the pass)."""
    name_cat = {r["norm"]: r["cat"] for r in injuries if r["norm"] and r["cat"]}
    active, out_players = [], []
    for p in roster:
        cat = name_cat.get(_norm_name(p.get("name", "")))
        p["injury"] = cat
        (out_players if cat == "out" else active).append(p)

    # Freed production from OUT players who were actually rotation pieces (min ≥ 10).
    freed = {"pts": 0.0, "reb": 0.0, "ast": 0.0, "fg3m": 0.0, "min": 0.0}
    for p in out_players:
        s = p.get("stats", {})
        if (s.get("min") or 0) < 10:
            continue
        for k in freed:
            freed[k] += (s.get(k) or 0)

    if freed["min"] > 0 and active:
        PASS = {"pts": 0.68, "fg3m": 0.68, "reb": 0.85, "ast": 0.80}
        tot = {k: (sum((a["stats"].get(k) or 0) for a in active) or 1.0) for k in PASS}
        active_min = sum((a["stats"].get("min") or 0) for a in active) or 1.0
        min_bump = min(200.0 / active_min, 1.20) if active_min < 200 else 1.0
        for a in active:
            s = dict(a["stats"])
            for k in PASS:                       # add each active player's usage share
                share = (s.get(k) or 0) / tot[k]
                s[k] = (s.get(k) or 0) + freed[k] * share * PASS[k]
            s["min"] = min(38.0, (s.get("min") or 0) * min_bump)
            a["stats"] = s
    return active


def build_game_context(game: dict, season: int, date_str: str = "") -> tuple[dict, dict]:
    home_team = game["home_team"]
    away_team = game["visitor_team"]

    all_players = fetch_all_player_stats(season)
    _compute_league_per_min(all_players)   # self-calibrate regression priors
    # Index players by normalized team abbreviation.
    by_abbr: dict[str, list[dict]] = {}
    for p in all_players:
        by_abbr.setdefault(_norm_abbr(p.get("team_abbr")), []).append(p)

    def team_ctx(team: dict) -> dict:
        # Resolve the ESPN team id even when the game came from the Odds API (no id),
        # so team scoring AND injuries both work on the primary data path.
        espn_id = resolve_espn_team_id(team, season)
        abbr = _norm_abbr(team.get("abbreviation"))
        scoring = fetch_team_scoring(espn_id, season)
        roster = []
        for p in by_abbr.get(abbr, []):
            roster.append({
                "bdl_id": p.get("player_id"),   # WNBA-stats player id (key name kept)
                "name": p.get("name", ""),
                "pos": "",
                "stats": dict(p.get("stats", {})),
            })
        roster.sort(key=lambda r: -(r["stats"].get("min") or 0))
        roster = roster[:12]   # widen so a replacement starter can absorb an OUT star

        # Injuries active on THIS game date (filters stale/future records), then drop
        # OUT players and redistribute their load to the active roster.
        injuries = [r for r in fetch_team_injuries(espn_id)
                    if r["cat"] and _injury_active_on(r, date_str)]
        roster = _apply_injuries(roster, injuries)[:10]
        inj_display = sorted(
            [{"name": r["name"], "status": r["status"], "cat": r["cat"],
              "detail": r["detail"], "return": r.get("return", "")} for r in injuries],
            key=lambda r: (r["cat"] != "out", r["name"]))

        return {
            "team": team.get("full_name") or team.get("name", ""),
            "abbr": team.get("abbreviation", ""),
            "pf": scoring.get("pf"), "pa": scoring.get("pa"),
            "roster": roster,
            "injuries": inj_display,
        }

    return team_ctx(away_team), team_ctx(home_team)


# ---- Orchestration ----------------------------------------------------------

def format_tipoff(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(ET).strftime("%-I:%M %p ET")
    except Exception:
        return ""


def run_for_date(date_str: str) -> Path:
    year = int(date_str[:4])
    games = fetch_games(date_str)
    print(f"WNBA predicting for {date_str} — {len(games)} games (Odds API / ESPN)", file=sys.stderr)

    all_output = {
        "date": date_str, "n_sims": N_SIMS,
        "generated_at": datetime.now(ET).isoformat(),
        "sport": "wnba",
        "games": [],
    }

    # Prime the player-stats cache once (one stats.wnba.com call for the league).
    print("  Loading WNBA player season stats...", file=sys.stderr)
    fetch_all_player_stats(year)

    for i, g in enumerate(games, 1):
        gid = g["id"]
        away = g["visitor_team"]
        home = g["home_team"]
        away_name = away.get("full_name") or away.get("name", "")
        home_name = home.get("full_name") or home.get("name", "")
        print(f"  [{i}/{len(games)}] {away_name} @ {home_name}", file=sys.stderr)

        try:
            away_ctx, home_ctx = build_game_context(g, year, date_str)
        except Exception as e:
            print(f"    context error: {e}", file=sys.stderr)
            continue

        try:
            odds = fetch_odds(g)   # parses embedded Odds API data, else ESPN by id
            props = []  # no free WNBA player-prop odds source
        except Exception as e:
            print(f"    odds error: {e}", file=sys.stderr)
            odds, props = [], []

        try:
            sim = sim_game(away_ctx, home_ctx, n_sims=N_SIMS)
        except Exception as e:
            print(f"    sim error: {e}", file=sys.stderr)
            continue

        try:
            picks = generate_picks(sim, odds, props, away_ctx, home_ctx)
        except Exception as e:
            print(f"    picks error: {e}", file=sys.stderr)
            picks = []

        try:
            proj_box = build_projected_boxscore(sim, away_ctx, home_ctx)
        except Exception as e:
            print(f"    boxscore error: {e}", file=sys.stderr)
            proj_box = {}

        p_away = sum(1 for a, h in zip(sim["away_scores"], sim["home_scores"]) if a > h) / len(sim["away_scores"])
        totals = [a + h for a, h in zip(sim["away_scores"], sim["home_scores"])]
        margins = [h - a for a, h in zip(sim["away_scores"], sim["home_scores"])]

        all_output["games"].append({
            "bdl_game_id": gid,
            "date": date_str,
            "tipoff_et": format_tipoff(g.get("date", "")),
            "status": g.get("status", ""),
            "away_team": away_name, "home_team": home_name,
            "away_abbr": away.get("abbreviation", ""), "home_abbr": home.get("abbreviation", ""),
            "model": {
                "away_win_pct": round(p_away, 4),
                "home_win_pct": round(1 - p_away, 4),
                "mean_away_pts": round(sum(sim["away_scores"]) / len(sim["away_scores"]), 1),
                "mean_home_pts": round(sum(sim["home_scores"]) / len(sim["home_scores"]), 1),
                "mean_total": round(sum(totals) / len(totals), 1),
                "mean_margin": round(sum(margins) / len(margins), 1),
            },
            "picks": sorted(picks, key=lambda p: p["ev_pct"], reverse=True),
            "projected_box": proj_box,
            "injuries": {
                "away": away_ctx.get("injuries", []),
                "home": home_ctx.get("injuries", []),
            },
        })

    out_path = DATA_DIR / f"wnba_predictions_{date_str}.json"
    with open(out_path, "w") as f:
        json.dump(all_output, f, indent=2)
    print(f"Wrote {out_path}", file=sys.stderr)
    return out_path


def main() -> int:
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now(ET).strftime("%Y-%m-%d")
    run_for_date(date_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
