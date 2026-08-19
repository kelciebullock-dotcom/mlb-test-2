"""MLB game prediction engine — 100% free, keyless data sources.

Data sources (no API keys, no paid tiers, no BALLDONTLIE):
  - Games + odds (moneyline / total / runline, with prices): ESPN's free API
      cdn.espn.com/core/mlb/scoreboard  and
      sports.core.api.espn.com/.../events/{id}/competitions/{id}/odds
  - Schedule, lineups, probable pitchers, player/team stats, final scores:
      MLB Stats API (statsapi.mlb.com — official, unlimited, no key)
  - Starter Statcast metrics + team splits + park/weather: mlb_tonight_edates.csv
      (written by the scraper, which also uses only free sources)

Player-prop odds are NOT available from any free source, so prop bets are not
generated. The model still projects player stat lines in the boxscore.

Pipeline for one date:
  1. ESPN scoreboard -> games (+ event ids for odds).
  2. Per game: ESPN odds + MLB Stats API lineups + scraper context.
  3. 10,000 Monte Carlo sims -> team runs, starter Ks, batter H/HR.
  4. Compare model probs to the market line (shrunk toward it) -> EV%. Rank picks.
  5. Write data/predictions_YYYY-MM-DD.json (consumed by mlb_dashboard.py).

Usage:
    python mlb_predict.py                 # today
    python mlb_predict.py 2026-08-15      # specific date
"""

from __future__ import annotations

import csv
import json
import os
import random
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CWD = Path(__file__).parent
DATA_DIR = CWD / "data"
DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

MLB_BASE = "https://statsapi.mlb.com/api/v1"
# ESPN's undocumented but free, keyless, unmetered endpoints.
ESPN_SB = "https://cdn.espn.com/core/mlb/scoreboard"      # ?xhr=1&dates=YYYYMMDD
ESPN_ODDS = "https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/events/{eid}/competitions/{eid}/odds"
LEAGUE_AVG_RPG = 4.5
LEAGUE_AVG_ERA = 4.10
LEAGUE_AVG_OPS = 0.720
LEAGUE_AVG_K_PCT = 22.5
N_SIMS = 10_000

# ---- Run-scoring model constants (sabermetric rebuild) ----------------------
# Sources: Bill James log5 / odds-ratio method; negative-binomial run distribution
# (variance ≈ 2.2× mean — Poisson understates it, mispricing totals/run-lines);
# FIP/xERA as pitcher true-talent; home-field ≈ 0.13 runs; temperature ≈ +1%/°F.
LG_RPG = 4.30            # league runs per game per team (recent seasons)
LG_ERA = 4.05            # league ERA
HFA_RUNS = 0.13          # home-field advantage, in runs
RUN_DISPERSION = 1.15    # negative-binomial theta: var ≈ mean × (1 + theta) ≈ 2.15× mean
STARTER_IP = 5.2         # league-avg innings per start (rest goes to the bullpen)
UNEARNED_FACTOR = 0.92   # earned runs ≈ 92% of total runs (converts ERA → total run rate)
LG_HR9 = 1.20            # league HR allowed per 9 IP (for the HR projection model)

# Probability shrinkage toward the market. The raw sim is systematically
# overconfident (backtest: it says 92% when reality is ~50%), so every reported
# probability is blended toward the devigged market consensus:
#     p_used = MODEL_TRUST * p_sim + (1 - MODEL_TRUST) * p_market_fair
# MODEL_TRUST < 1 caps how far we'll disagree with the sharpest available price.
# 0.50 = weight the model and the market equally. Raised from 0.35 after the
# sabermetric rebuild (negative-binomial runs, odds-ratio offense/pitching,
# xERA talent, starter/bullpen split): a sharper model earns more trust, but we
# still respect the market since the improvements aren't yet CLV-validated. The
# backtest's beat-close % is the scoreboard for tuning this number over time.
MODEL_TRUST = 0.50

# ---- HTTP helpers (all free, keyless) --------------------------------------

def _http_get_json(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (mlb-tonight)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def mlb_get(path: str, **params) -> dict:
    """MLB Stats API — official, free, unlimited, no key."""
    if params:
        url = f"{MLB_BASE}{path}?{urllib.parse.urlencode(params)}"
    else:
        url = f"{MLB_BASE}{path}"
    return _http_get_json(url, timeout=20)


def _american(node: dict) -> float | None:
    """Pull an American-odds int from an ESPN price node like
    {'american': '+126'} or a bare number."""
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


# ---- Games (MLB Stats API schedule) + odds (ESPN, matched by matchup) --------

def _espn_event_map(date_str: str) -> dict:
    """{frozenset(team slugs): espn_event_id} from ESPN's scoreboard around a date.
    ESPN mis-dates games, so scan date-1..date+1 and key by matchup (unique per day)."""
    out = {}
    base = datetime.strptime(date_str, "%Y-%m-%d").date()
    for delta in (0, -1, 1):
        yyyymmdd = (base + timedelta(days=delta)).strftime("%Y%m%d")
        try:
            j = _http_get_json(f"{ESPN_SB}?xhr=1&dates={yyyymmdd}")
        except Exception:
            continue
        for ev in (j.get("content", {}).get("sbData", {}).get("events") or j.get("events") or []):
            try:
                comp = ev["competitions"][0]
                comps = comp.get("competitors", [])
                home = next(c for c in comps if c.get("homeAway") == "home")
                away = next(c for c in comps if c.get("homeAway") == "away")
                key = frozenset({_slug_norm(home["team"]["displayName"]),
                                 _slug_norm(away["team"]["displayName"])})
                out.setdefault(key, ev.get("id"))
            except Exception:
                continue
    return out


def fetch_games(date_str: str) -> list[dict]:
    """Games for a date from the AUTHORITATIVE MLB Stats API schedule (correct dates
    + probable pitchers), each matched to its ESPN event id for odds. Using StatsAPI
    for the schedule fixes ESPN's loose date filter that mislabeled adjacent days."""
    try:
        d = mlb_get("/schedule", sportId=1, date=date_str, hydrate="probablePitcher,team")
    except Exception as e:
        print(f"  (MLB schedule fetch failed: {e})", file=sys.stderr)
        return []
    espn_map = _espn_event_map(date_str)
    games = []
    for day in d.get("dates", []):
        for g in day.get("games", []):
            try:
                a = g["teams"]["away"]["team"]; h = g["teams"]["home"]["team"]
            except KeyError:
                continue
            key = frozenset({_slug_norm(a.get("name", "")), _slug_norm(h.get("name", ""))})
            # Doubleheaders are real, distinct games (own gamePk/time/pitchers) — keep
            # both. StatsAPI is authoritative so there are no phantom duplicates.
            games.append({
                "id": espn_map.get(key),          # ESPN event id for odds (None until posted)
                "gamePk": g.get("gamePk"),
                "home_team_name": h.get("name", ""),
                "away_team_name": a.get("name", ""),
                "home_team": {"id": h.get("id"), "abbreviation": h.get("abbreviation", ""),
                              "name": h.get("name", "")},
                "away_team": {"id": a.get("id"), "abbreviation": a.get("abbreviation", ""),
                              "name": a.get("name", "")},
        })
    return games


def fetch_odds(event_id) -> list[dict]:
    """Return a one-element list with the pregame DraftKings line from ESPN,
    normalized to the internal odds-dict shape the pick generator expects."""
    if not event_id:
        return []
    try:
        d = _http_get_json(ESPN_ODDS.format(eid=event_id))
    except Exception as e:
        print(f"  (ESPN odds fetch failed for {event_id}: {e})", file=sys.stderr)
        return []

    # Prefer the plain provider line (not "... - Live Odds"); DraftKings first.
    items = [it for it in d.get("items", [])
             if "live" not in (it.get("provider", {}).get("name", "").lower())]
    if not items:
        return []
    def rank(it):
        name = it.get("provider", {}).get("name", "").lower()
        return 0 if "draftkings" in name else 1
    it = sorted(items, key=rank)[0]

    home = it.get("homeTeamOdds", {}) or {}
    away = it.get("awayTeamOdds", {}) or {}
    # Spread: ESPN 'spread' is the home line (e.g. -1.5). Prices live under
    # each team's current/close spread node.
    try:
        spread_home_value = float(it.get("spread")) if it.get("spread") is not None else None
    except (TypeError, ValueError):
        spread_home_value = None

    def side_spread_odds(node):
        cur = node.get("current") or node.get("close") or node.get("open") or {}
        return _american(cur.get("spread"))

    row = {
        "vendor": "draftkings",
        "moneyline_home_odds": _american(home.get("moneyLine")),
        "moneyline_away_odds": _american(away.get("moneyLine")),
        "total_value": it.get("overUnder"),
        "total_over_odds": _american(it.get("overOdds")),
        "total_under_odds": _american(it.get("underOdds")),
        "spread_home_value": spread_home_value,
        "spread_away_value": (-spread_home_value) if spread_home_value is not None else None,
        "spread_home_odds": side_spread_odds(home),
        "spread_away_odds": side_spread_odds(away),
    }
    return [row]


def fetch_player_props(event_id) -> list[dict]:
    """No free source for player-prop odds — always empty. (ESPN exposes a
    propBets $ref but not usable prop lines on the free tier.)"""
    return []


# ---- Lineups (MLB Stats API, free) -----------------------------------------

_LINEUP_CACHE: dict[str, dict] = {}


_PROJ_LINEUP_CACHE: dict[int, list] = {}


def _projected_lineup(team_id: int, year: int) -> list[dict]:
    """Fallback batting order when the official lineup isn't posted yet (Preview
    games, e.g. tomorrow): the team's 9 position players with the most plate
    appearances this season, ordered by PA. Lets box scores populate a day ahead."""
    if not team_id:
        return []
    if team_id in _PROJ_LINEUP_CACHE:
        return _PROJ_LINEUP_CACHE[team_id]
    players = []
    try:
        r = mlb_get(f"/teams/{team_id}/roster", rosterType="active")
        for entry in r.get("roster", []):
            pos = entry.get("position", {}) or {}
            if pos.get("type") == "Pitcher":
                continue
            p = entry.get("person", {}) or {}
            pid = p.get("id")
            if not pid:
                continue
            stats = get_batter_season_stats(pid, year)
            players.append({
                "id": pid,
                "fullName": p.get("fullName", ""),
                "primaryPosition": {"abbreviation": pos.get("abbreviation", "")},
                "_pa": (stats or {}).get("pa", 0),
            })
        players.sort(key=lambda x: -(x.get("_pa") or 0))
        players = players[:9]
    except Exception as e:
        print(f"    (projected lineup failed for team {team_id}: {e})", file=sys.stderr)
        players = []
    _PROJ_LINEUP_CACHE[team_id] = players
    return players


def _load_lineups_for_date(date_str: str, year: int | None = None) -> dict[frozenset, dict]:
    """{frozenset(team slugs): {away, home, away_pp, home_pp, away_id, home_id}}.
    Uses official lineups when posted; projects from the roster otherwise."""
    if date_str in _LINEUP_CACHE:
        return _LINEUP_CACHE[date_str]
    if year is None:
        year = int(date_str[:4])
    out: dict[frozenset, dict] = {}
    try:
        d = mlb_get("/schedule", sportId=1, date=date_str,
                    hydrate="lineups,probablePitcher,team")
        for day in d.get("dates", []):
            for g in day.get("games", []):
                at = g["teams"]["away"]["team"]; ht = g["teams"]["home"]["team"]
                a, h = at["name"], ht["name"]
                lu = g.get("lineups", {}) or {}
                away_players = lu.get("awayPlayers", []) or []
                home_players = lu.get("homePlayers", []) or []
                # Project from the roster when the official card isn't up yet.
                if not away_players:
                    away_players = _projected_lineup(at.get("id"), year)
                if not home_players:
                    home_players = _projected_lineup(ht.get("id"), year)
                entry = {
                    "away": away_players, "home": home_players,
                    "away_pp": (g["teams"]["away"].get("probablePitcher") or {}),
                    "home_pp": (g["teams"]["home"].get("probablePitcher") or {}),
                }
                gpk = str(g.get("gamePk") or "").strip()
                if gpk:
                    out[f"pk:{gpk}"] = entry     # unique per game (doubleheaders)
                out.setdefault(f"mu:{frozenset({_slug_norm(a), _slug_norm(h)})}", entry)
    except Exception as e:
        print(f"  (MLB lineup fetch failed for {date_str}: {e})", file=sys.stderr)
    _LINEUP_CACHE[date_str] = out
    return out


_TEAM_SCORING: dict[int, dict] = {}


def load_team_scoring(year: int) -> dict[str, dict]:
    """Team runs-scored and runs-allowed per game from the MLB Stats API standings.
    Returns {normalized_team_name: {"rpg": float, "rapg": float}}. One call, cached."""
    if _TEAM_SCORING:
        return _TEAM_SCORING
    try:
        d = mlb_get("/standings", leagueId="103,104", season=year,
                    standingsTypes="regularSeason", hydrate="team")
        for record in d.get("records", []):
            for tr in record.get("teamRecords", []):
                name = tr.get("team", {}).get("name", "")
                gp = tr.get("gamesPlayed") or 0
                rs = tr.get("runsScored")
                ra = tr.get("runsAllowed")
                if not name or not gp:
                    continue
                _TEAM_SCORING[_slug_norm(name)] = {
                    "rpg": (rs / gp) if rs is not None else None,
                    "rapg": (ra / gp) if ra is not None else None,
                }
    except Exception as e:
        print(f"  (team scoring fetch failed: {e})", file=sys.stderr)
    return _TEAM_SCORING


def team_scoring_for(name: str) -> dict:
    return _TEAM_SCORING.get(_slug_norm(name), {})


def _extra_scoring(scraper_row: dict, side_key: str, team_name: str) -> dict:
    """Run-model inputs beyond OPS/pitcher: team runs-for/against per game (standings)
    and bullpen fatigue (recent 3-day IP + back-to-back arms, from the scraper CSV)."""
    sc = team_scoring_for(team_name)
    b2b, pen_ip3 = 0, 0.0
    if scraper_row:
        try:
            b2b = int(scraper_row.get(f"{side_key}_bullpen_b2b_arms") or 0)
        except (TypeError, ValueError):
            b2b = 0
        ip = str(scraper_row.get(f"{side_key}_bullpen_ip_last3d") or "")
        try:  # baseball IP notation "16.2" = 16 and 2/3
            whole, _, frac = ip.partition(".")
            pen_ip3 = int(whole or 0) + int(frac or 0) / 3.0
        except Exception:
            pen_ip3 = 0.0
    return {
        "off_rpg": sc.get("rpg"),
        "def_rpg": sc.get("rapg"),
        "bullpen_b2b": b2b,
        "bullpen_ip3": pen_ip3,
    }


# ---- MLB StatsAPI stats (with disk cache) ----------------------------------

def _cache_path(kind: str, key: str) -> Path:
    return CACHE_DIR / f"{kind}_{key}.json"


def _cache_get(kind: str, key: str, ttl_hours: float = 24) -> dict | None:
    p = _cache_path(kind, key)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > ttl_hours * 3600:
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _cache_put(kind: str, key: str, data: dict) -> None:
    with open(_cache_path(kind, key), "w") as f:
        json.dump(data, f)


def get_batter_season_stats(mlb_id: int, year: int) -> dict:
    """Return {avg, obp, slg, ops, k_pct, bb_pct, hr_pa, pa} for a batter, or {}."""
    cached = _cache_get("batter", f"{mlb_id}_{year}")
    if cached is not None:
        return cached
    try:
        d = mlb_get(f"/people/{mlb_id}/stats", stats="season", group="hitting", season=year)
        splits = (d.get("stats") or [{}])[0].get("splits") or []
        if not splits:
            _cache_put("batter", f"{mlb_id}_{year}", {})
            return {}
        s = splits[0].get("stat", {})
        pa = int(s.get("plateAppearances", 0) or 0)
        result = {
            "avg": float(s.get("avg", 0) or 0),
            "obp": float(s.get("obp", 0) or 0),
            "slg": float(s.get("slg", 0) or 0),
            "ops": float(s.get("ops", 0) or 0),
            "k_pct": (int(s.get("strikeOuts", 0) or 0) / pa * 100) if pa else 0.0,
            "bb_pct": (int(s.get("baseOnBalls", 0) or 0) / pa * 100) if pa else 0.0,
            "hr_pa": (int(s.get("homeRuns", 0) or 0) / pa) if pa else 0.0,
            "hits_pa": (int(s.get("hits", 0) or 0) / pa) if pa else 0.0,
            "pa": pa,
        }
        _cache_put("batter", f"{mlb_id}_{year}", result)
        return result
    except Exception as e:
        print(f"      (batter stat fetch failed for {mlb_id}: {e})", file=sys.stderr)
        return {}


def _ip_to_float(ip) -> float:
    """Baseball IP notation to decimal: '120.1' = 120 + 1/3, '.2' = 2/3."""
    try:
        whole, _, frac = str(ip).partition(".")
        return int(whole or 0) + {"": 0, "0": 0, "1": 1, "2": 2}.get(frac, 0) / 3.0
    except Exception:
        return 0.0


def get_pitcher_start_profile(mlb_id: int, year: int) -> dict:
    """The pitcher's REAL per-start line from their season totals (MLB StatsAPI),
    lightly regressed to league for small samples. Returns per-start ip/k/bb/h/hr/er
    plus starts, or {} if unavailable. Drives the projected boxscore so each starter
    is projected from their own workload, not a generic number."""
    if not mlb_id:
        return {}
    cached = _cache_get("pitchprof", f"{mlb_id}_{year}")
    if cached is not None:
        return cached
    try:
        d = mlb_get(f"/people/{mlb_id}/stats", stats="season", group="pitching", season=year)
        splits = (d.get("stats") or [{}])[0].get("splits") or []
        if not splits:
            _cache_put("pitchprof", f"{mlb_id}_{year}", {})
            return {}
        s = splits[0].get("stat", {})
        gs = int(s.get("gamesStarted", 0) or 0)
        ip = _ip_to_float(s.get("inningsPitched", "0"))
        if gs < 1 or ip < 1:
            _cache_put("pitchprof", f"{mlb_id}_{year}", {})
            return {}
        # Per-9 RATES from season totals — role-agnostic, so relief innings don't
        # distort them (unlike total/starts, which inflates swingmen). Applied over
        # a realistic per-start innings total below.
        k = int(s.get("strikeOuts", 0) or 0)
        bb = int(s.get("baseOnBalls", 0) or 0)
        h = int(s.get("hits", 0) or 0)
        hr = int(s.get("homeRuns", 0) or 0)
        er = int(s.get("earnedRuns", 0) or 0)
        # Innings/start regressed toward league (5.2) by start count, then clamped to
        # a realistic 3.5–7.0 (no starter truly averages 8+; that was the swingman bug).
        k_reg = 8.0
        ip_per_start = (ip + 5.2 * k_reg) / (gs + k_reg)   # ip already ≈ starter innings for pure SPs
        ip_per_start = max(3.5, min(7.0, ip_per_start))
        prof = {
            "starts": gs,
            "ip_per_start": ip_per_start,
            "k9": k / ip * 9.0,
            "bb9": bb / ip * 9.0,
            "h9": h / ip * 9.0,
            "hr9": hr / ip * 9.0,
            "er9": er / ip * 9.0,
        }
        _cache_put("pitchprof", f"{mlb_id}_{year}", prof)
        return prof
    except Exception as e:
        print(f"      (pitcher profile fetch failed for {mlb_id}: {e})", file=sys.stderr)
        return {}


def search_mlb_player_by_name(name: str, year: int) -> int | None:
    """Map a BDL player name -> MLB StatsAPI player id via /people/search."""
    cached = _cache_get("player_id", name.replace(" ", "_"), ttl_hours=24*30)
    if cached is not None:
        return cached.get("id")
    try:
        d = mlb_get(f"/people/search", names=name, sportIds=1)
        people = d.get("people", [])
        if not people:
            _cache_put("player_id", name.replace(" ", "_"), {"id": None})
            return None
        # Prefer an active player
        pid = people[0].get("id")
        _cache_put("player_id", name.replace(" ", "_"), {"id": pid})
        return pid
    except Exception:
        return None


# ---- Model utilities --------------------------------------------------------

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


def dev_from_market(over_odds, under_odds) -> tuple[float, float]:
    """Devig a two-way market to implied probabilities that sum to 1."""
    if over_odds is None or under_odds is None:
        return 0.0, 0.0
    po, pu = american_to_prob(over_odds), american_to_prob(under_odds)
    total = po + pu
    if total <= 0:
        return 0.0, 0.0
    return po / total, pu / total


REFERENCE_VENDORS = ("draftkings", "fanduel", "betmgm", "caesars", "betrivers", "fanatics")

# Human-facing sportsbook labels
BOOK_LABEL = {
    "draftkings": "DraftKings",
    "fanduel":    "FanDuel",
    "betmgm":     "BetMGM",
    "caesars":    "Caesars",
    "betrivers":  "BetRivers",
    "fanatics":   "Fanatics",
}


def _vig(p_over: float, p_under: float) -> float:
    return abs((p_over + p_under) - 1.0)


def _pick_market(entries: list[dict], over_key: str, under_key: str,
                 line_key: str | None = None, restrict_line=None) -> dict | None:
    """From a list of vendor entries pick one representing the MAIN market for this
    over/under pair. Filters out entries whose implied vig is unrealistic (>=8%),
    prefers our REFERENCE_VENDORS order, and (if line_key given) only keeps entries
    whose line matches `restrict_line` (used to pin totals/spreads to the modal main line)."""
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
            continue  # skip alt / stale / 3-way markets
        candidates.append((vig, e))
    if not candidates:
        return None
    # Prefer configured vendor order, tie-break by lowest vig
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
    """Return the value of `key` most common across vendors — the main line.
    `only_abs` (e.g. 1.5 for MLB runline) forces filtering to |value| == only_abs first."""
    from collections import Counter
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


def prob_to_american(p: float) -> int:
    """Convert probability to American odds (no vig)."""
    if p <= 0 or p >= 1:
        return 0
    if p >= 0.5:
        return round(-100 * p / (1 - p))
    return round(100 * (1 - p) / p)


def ev_pct(model_prob: float, american_odds) -> float:
    """Expected value per $1 stake, as a percentage."""
    if american_odds is None or model_prob <= 0:
        return -100.0
    try:
        odds = float(american_odds)
    except (TypeError, ValueError):
        return -100.0
    # American odds have a floor of |100|. Anything below that is a data glitch
    # (BDL sometimes returns 0 for missing entries and the median of two Nones
    # after prob_to_american rounding can produce integers near 0).
    if abs(odds) < 100:
        return -100.0
    payout = (odds / 100.0) if odds > 0 else (100.0 / -odds)
    return (model_prob * payout - (1 - model_prob)) * 100.0


# ---- Run model (shared by the sim and the market-calibration pre-pass) ------

def _weather_mult(weather: dict) -> float:
    """Temperature/wind/precip multiplier on run scoring. ~0.3%/°F around 70°F
    (the ~1%/°F figure is HR-only), capped ±5%. Dome/unknown → neutral."""
    m = 1.0
    temp = weather.get("temp_f")
    if isinstance(temp, (int, float)):
        m *= max(0.95, min(1.05, 1.0 + 0.003 * (temp - 70.0)))
    wind = weather.get("wind_mph")
    if isinstance(wind, (int, float)) and wind > 12:
        m *= 1.01
    precip = weather.get("precip_pct")
    if isinstance(precip, (int, float)) and precip > 50:
        m *= 0.98
    return m


def _starter_run_rate(pit: dict) -> float:
    """Starter's expected TOTAL runs allowed per 9, from xERA (primary, Statcast)
    blended with ERA, regressed toward league for stability."""
    xera = pit.get("xera"); era = pit.get("era")
    parts = []
    if isinstance(xera, (int, float)) and xera > 0:
        parts.append((0.70, xera))
    if isinstance(era, (int, float)) and era > 0:
        parts.append((0.30 if parts else 1.0, era))
    base = (sum(w * v for w, v in parts) / sum(w for w, _ in parts)) if parts else LG_ERA
    base = 0.82 * base + 0.18 * LG_ERA          # regress toward league
    return base / UNEARNED_FACTOR               # earned ERA → total run rate


def _lineup_ops_factor(lineup: list) -> float | None:
    """Aggregate offensive strength of the ACTUAL projected batting order, weighted
    by batting-order PA (top of order sees more). Returns OPS relative to league,
    or None if the lineup/stats aren't available (e.g. game-lines-only mode)."""
    pa_w = {1: 4.7, 2: 4.6, 3: 4.5, 4: 4.4, 5: 4.3, 6: 4.2, 7: 4.1, 8: 4.0, 9: 3.9}
    num = den = 0.0
    for b in lineup or []:
        ops = (b.get("stats") or {}).get("ops")
        if not isinstance(ops, (int, float)) or ops <= 0:
            continue
        w = pa_w.get(b.get("slot") or 5, 4.2)
        num += w * ops
        den += w
    if den == 0:
        return None
    return (num / den) / LEAGUE_AVG_OPS


def team_run_mean(off_ctx: dict, def_ctx: dict, park_mult: float,
                  weather_mult: float, is_home: bool) -> float:
    """Expected runs for off_ctx's offense vs def_ctx's pitching (odds-ratio combine
    of team offense × opponent starter/bullpen ÷ league, then park/weather/HFA)."""
    lg_sp_rate = LG_ERA / UNEARNED_FACTOR
    off_rpg = off_ctx.get("off_rpg") or LG_RPG
    team_factor = off_rpg / LG_RPG
    ops = off_ctx.get("team_ops_vs_hand")
    plat_factor = (ops / LEAGUE_AVG_OPS) if ops else team_factor
    # Lineup-level offense (the ACTUAL batters tonight) is the sharpest signal when
    # available — the market prices confirmed lineups, so we should too.
    lineup_factor = _lineup_ops_factor(off_ctx.get("lineup"))
    if lineup_factor is not None:
        off_factor = 0.45 * team_factor + 0.25 * plat_factor + 0.30 * lineup_factor
    else:
        off_factor = 0.70 * team_factor + 0.30 * plat_factor

    sp_factor = _starter_run_rate(def_ctx.get("pitcher", {})) / lg_sp_rate
    pen_rpg = def_ctx.get("def_rpg") or LG_RPG
    b2b = def_ctx.get("bullpen_b2b") or 0
    pen_ip3 = def_ctx.get("bullpen_ip3") or 0.0
    fatigue = 1.0 + 0.012 * b2b + (0.02 if pen_ip3 >= 12 else 0.0)
    pen_factor = (pen_rpg / LG_RPG) * fatigue
    sp_w = STARTER_IP / 9.0
    pitch_factor = sp_w * sp_factor + (1 - sp_w) * pen_factor

    exp = LG_RPG * off_factor * pitch_factor * park_mult * weather_mult
    exp += (HFA_RUNS / 2.0) if is_home else (-HFA_RUNS / 2.0)  # margin edge, total-neutral
    return max(2.0, min(9.5, exp))


def game_run_means(away_ctx: dict, home_ctx: dict, park_factor: float,
                   weather: dict) -> tuple[float, float]:
    """(away_mean, home_mean) expected runs, before market calibration."""
    park_mult = park_factor / 100.0 if park_factor else 1.0
    wm = _weather_mult(weather)
    return (team_run_mean(away_ctx, home_ctx, park_mult, wm, is_home=False),
            team_run_mean(home_ctx, away_ctx, park_mult, wm, is_home=True))


# ---- Simulator --------------------------------------------------------------

def sim_game(away_ctx: dict, home_ctx: dict, park_factor: float,
             weather: dict, n_sims: int = N_SIMS, run_scale: float = 1.0) -> dict:
    """Return arrays of length n_sims: away_runs, home_runs, plus per-player samples.

    Team scoring uses a sabermetric run model (odds-ratio offense×pitching,
    xERA-based starter talent + bullpen split, home-field, park, temperature) and
    samples from a NEGATIVE BINOMIAL (variance ≈ 2.2× mean) instead of Poisson —
    Poisson understates run variance, which mis-prices totals and run lines."""
    park_mult = park_factor / 100.0 if park_factor else 1.0

    # Run means from the shared model, then apply the per-slate market calibration
    # (run_scale) so the model's overall run level matches the market's — a
    # persistent over/under lean is bias, not edge; the game-to-game deviations are.
    away_mean, home_mean = game_run_means(away_ctx, home_ctx, park_factor, weather)
    away_mean = max(1.5, away_mean * run_scale)
    home_mean = max(1.5, home_mean * run_scale)

    # HR-specific weather: warm, thin air carries the ball — ~2%/°F above 70°F
    # (much stronger for home runs than for total runs), capped. Dome → neutral.
    hr_weather_mult = 1.0
    _t = weather.get("temp_f")
    if isinstance(_t, (int, float)):
        hr_weather_mult = max(0.85, min(1.20, 1.0 + 0.02 * (_t - 70.0)))
    _w = weather.get("wind_mph")
    if isinstance(_w, (int, float)) and _w > 12:
        hr_weather_mult *= 1.03  # windy (direction unknown) → mild HR bump

    rng = random.Random(42)

    def poisson(lam: float) -> int:
        # Knuth's method — fine for small lambda (baseball runs ≤ ~15)
        L = pow(2.71828, -lam)
        k = 0
        p = 1.0
        while True:
            k += 1
            p *= rng.random()
            if p <= L:
                return k - 1

    def sample_runs(mean: float) -> int:
        """Negative binomial via Gamma-Poisson mixture: draw a per-game rate from
        Gamma(shape=mean/theta, scale=theta) then Poisson it. Gives variance ≈
        mean×(1+theta) ≈ 2.15× mean, matching real MLB run overdispersion."""
        shape = max(0.05, mean / RUN_DISPERSION)
        lam = rng.gammavariate(shape, RUN_DISPERSION)
        return poisson(lam)

    def binomial(n: int, p: float) -> int:
        if p <= 0 or n <= 0:
            return 0
        if p >= 1:
            return n
        k = 0
        for _ in range(n):
            if rng.random() < p:
                k += 1
        return k

    away_runs = [sample_runs(away_mean) for _ in range(n_sims)]
    home_runs = [sample_runs(home_mean) for _ in range(n_sims)]

    # Pitcher K props: sample BF ~ triangular(18,24,30), then Binomial(BF, K%)
    def pitcher_ks(pit: dict) -> list[int]:
        k_pct = pit.get("k_pct")
        if not k_pct:
            k_pct = LEAGUE_AVG_K_PCT
        p = k_pct / 100.0
        out = []
        for _ in range(n_sims):
            bf = int(rng.triangular(16, 30, 24))
            out.append(binomial(bf, p))
        return out

    away_pitcher_ks = pitcher_ks(away_ctx["pitcher"])
    home_pitcher_ks = pitcher_ks(home_ctx["pitcher"])

    # Batter props: for each batter, sample PA + hit + hr + walk + K per PA
    def batter_props(lineup: list[dict], opp_pitcher: dict, park_hr_factor: float) -> dict:
        p_pit_k = (opp_pitcher.get("k_pct") or LEAGUE_AVG_K_PCT) / 100.0
        p_pit_bb = (opp_pitcher.get("bb_pct") or 8.0) / 100.0
        # Opposing starter's HR-suppression: their HR/9 relative to league.
        opp_hr9 = opp_pitcher.get("hr9")
        pit_hr_factor = (opp_hr9 / LG_HR9) if isinstance(opp_hr9, (int, float)) and opp_hr9 > 0 else 1.0
        pit_hr_factor = max(0.6, min(1.7, pit_hr_factor))
        out = {}
        for b in lineup:
            slot = b.get("slot") or 5
            pa_mean = {1: 4.7, 2: 4.6, 3: 4.5, 4: 4.4, 5: 4.3,
                       6: 4.2, 7: 4.1, 8: 4.0, 9: 3.9}.get(slot, 4.2)
            stats = b.get("stats", {})
            bat_k = (stats.get("k_pct") or 22.0) / 100.0
            bat_bb = (stats.get("bb_pct") or 8.0) / 100.0
            eff_k = min(0.6, max(0.05, bat_k * (p_pit_k / (LEAGUE_AVG_K_PCT / 100.0))))
            eff_bb = min(0.3, max(0.02, bat_bb * (p_pit_bb / 0.08)))
            hit_p = max(0.05, min(0.55,
                (stats.get("hits_pa") or 0.240) * (1.0 - (eff_k - 0.22))))
            # HR model: batter HR/PA × park × opposing-pitcher HR/9 × warm-weather.
            hr_p = max(0.002, min(0.15,
                (stats.get("hr_pa") or 0.028) * park_hr_factor * pit_hr_factor * hr_weather_mult))

            samples_h, samples_hr, samples_bb, samples_k, samples_pa = [], [], [], [], []
            for _ in range(n_sims):
                pa = int(rng.triangular(3, 6, pa_mean))
                samples_pa.append(pa)
                samples_h.append(binomial(pa, hit_p))
                samples_hr.append(binomial(pa, hr_p))
                samples_bb.append(binomial(pa, eff_bb))
                samples_k.append(binomial(pa, eff_k))
            out[b["mlb_id"]] = {
                "name": b["name"],
                "slot": slot,
                "pos": b.get("pos", ""),
                "hits": samples_h,
                "hrs": samples_hr,
                "walks": samples_bb,
                "ks": samples_k,
                "pas": samples_pa,
                "slg": stats.get("slg") or 0.400,
                "obp": stats.get("obp") or 0.320,
            }
        return out

    # Park HR factor: rough proxy of park factor amplified
    park_hr = 0.7 + 0.6 * park_mult  # PF 100 -> 1.3, PF 90 -> 1.24, PF 112 -> 1.37
    away_props = batter_props(away_ctx["lineup"], home_ctx["pitcher"], park_hr)
    home_props = batter_props(home_ctx["lineup"], away_ctx["pitcher"], park_hr)

    return {
        "away_runs": away_runs,
        "home_runs": home_runs,
        "away_pitcher_ks": away_pitcher_ks,
        "home_pitcher_ks": home_pitcher_ks,
        "away_batter_props": away_props,
        "home_batter_props": home_props,
    }


# ---- Pick generation --------------------------------------------------------

def pct_over(samples: list[int], line: float) -> float:
    if not samples:
        return 0.0
    push_credit = 0.0
    over = 0
    for s in samples:
        if s > line:
            over += 1
        elif s == line:
            push_credit += 0.5  # treat integer pushes as a wash on both sides
    return (over + push_credit) / len(samples)


def generate_picks(sim: dict, odds: list[dict], props: list[dict],
                   away_ctx: dict, home_ctx: dict) -> list[dict]:
    """Emit MAIN-market picks only (no alt lines), each attributed to a sportsbook."""
    picks = []
    n = len(sim["away_runs"])

    def _mk_pick(market, side, line, raw_prob, entry, odds_key, fair_prob=None):
        odds_val = entry.get(odds_key) if entry else None
        if odds_val is None:
            return
        vendor = entry.get("vendor", "")
        # Shrink the raw sim probability toward the devigged market consensus.
        if fair_prob is not None and fair_prob > 0:
            used_prob = MODEL_TRUST * raw_prob + (1 - MODEL_TRUST) * fair_prob
        else:
            used_prob = raw_prob
        return {
            "market": market,
            "side": side,
            "line": line,
            "model_prob": round(used_prob, 4),        # shrunk — drives EV, confidence, picks
            "raw_model_prob": round(raw_prob, 4),     # unshrunk sim output, for transparency
            "market_fair_prob": round(fair_prob, 4) if fair_prob is not None else None,
            "market_odds": odds_val,
            "market_prob": round(american_to_prob(odds_val), 4),  # raw implied (incl. vig)
            "ev_pct": round(ev_pct(used_prob, odds_val), 2),
            "fair_odds": prob_to_american(used_prob),
            "book": BOOK_LABEL.get(vendor, vendor or "—"),
        }

    # --- Moneyline (no alts to filter; the ML is what it is per vendor) ---
    away_wins = sum(1 for a, h in zip(sim["away_runs"], sim["home_runs"]) if a > h)
    ties = sum(1 for a, h in zip(sim["away_runs"], sim["home_runs"]) if a == h)
    p_away = (away_wins + ties * 0.5) / n
    p_home = 1 - p_away

    ml_entry = _pick_market(odds, "moneyline_home_odds", "moneyline_away_odds")
    if ml_entry:
        fair_home, fair_away = dev_from_market(ml_entry.get("moneyline_home_odds"),
                                               ml_entry.get("moneyline_away_odds"))
        pk = _mk_pick("Moneyline", f"{away_ctx['team']} ML", None, p_away,
                      ml_entry, "moneyline_away_odds", fair_prob=fair_away)
        if pk: picks.append(pk)
        pk = _mk_pick("Moneyline", f"{home_ctx['team']} ML", None, p_home,
                      ml_entry, "moneyline_home_odds", fair_prob=fair_home)
        if pk: picks.append(pk)

    # --- Total (main line = modal total across vendors) ---
    totals = [a + h for a, h in zip(sim["away_runs"], sim["home_runs"])]
    main_total = _modal_value(odds, "total_value")
    if main_total is not None:
        total_entry = _pick_market(odds, "total_over_odds", "total_under_odds",
                                   line_key="total_value", restrict_line=main_total)
        if total_entry:
            p_over = pct_over(totals, main_total)
            fair_over, fair_under = dev_from_market(total_entry.get("total_over_odds"),
                                                    total_entry.get("total_under_odds"))
            pk = _mk_pick("Total", f"Over {main_total:g}", main_total, p_over,
                          total_entry, "total_over_odds", fair_prob=fair_over)
            if pk: picks.append(pk)
            pk = _mk_pick("Total", f"Under {main_total:g}", main_total, 1 - p_over,
                          total_entry, "total_under_odds", fair_prob=fair_under)
            if pk: picks.append(pk)

    # --- Runline (main = |spread| == 1.5 in MLB) ---
    main_spread = _modal_value(odds, "spread_home_value", only_abs=1.5)
    if main_spread is not None:
        spread_entry = _pick_market(odds, "spread_home_odds", "spread_away_odds",
                                    line_key="spread_home_value", restrict_line=main_spread)
        if spread_entry:
            margins = [h - a for a, h in zip(sim["away_runs"], sim["home_runs"])]
            p_home_cover = sum(1 for m in margins if m + main_spread > 0) / n
            p_away_cover = 1 - p_home_cover
            fair_home, fair_away = dev_from_market(spread_entry.get("spread_home_odds"),
                                                   spread_entry.get("spread_away_odds"))
            pk = _mk_pick("Runline", f"{home_ctx['team']} {main_spread:+g}", main_spread,
                          p_home_cover, spread_entry, "spread_home_odds", fair_prob=fair_home)
            if pk: picks.append(pk)
            pk = _mk_pick("Runline", f"{away_ctx['team']} {-main_spread:+g}", -main_spread,
                          p_away_cover, spread_entry, "spread_away_odds", fair_prob=fair_away)
            if pk: picks.append(pk)

    # --- Player props (main line per player+prop_type = modal line_value) ---
    prop_index_by_bdl = {}
    for side_props, side_lineup, ctx in (
        (sim["away_batter_props"], away_ctx["lineup"], away_ctx),
        (sim["home_batter_props"], home_ctx["lineup"], home_ctx),
    ):
        for b in side_lineup:
            if b["bdl_id"] is None:
                continue
            samples = side_props.get(b["mlb_id"])
            if samples:
                prop_index_by_bdl[b["bdl_id"]] = {
                    "name": b["name"], "team": ctx["team"], "samples": samples,
                }

    pitcher_prop_by_bdl = {}
    for side, ctx in (("away", away_ctx), ("home", home_ctx)):
        p = ctx["pitcher"]
        if p.get("bdl_id"):
            pitcher_prop_by_bdl[p["bdl_id"]] = {
                "name": p.get("name"), "team": ctx["team"],
                "ks_samples": sim[f"{side}_pitcher_ks"],
            }

    # Group props by (player, prop_type). Within each group, find the modal
    # line_value across vendors — that's the MAIN market. Discard alt lines.
    from collections import Counter, defaultdict
    grouped_all = defaultdict(list)
    for pr in props:
        grouped_all[(pr.get("player_id"), pr.get("prop_type"))].append(pr)

    for (pid, ptype), all_entries in grouped_all.items():
        line_counts = Counter(e.get("line_value") for e in all_entries if e.get("line_value") is not None)
        if not line_counts:
            continue
        main_line_str, main_line_count = line_counts.most_common(1)[0]
        # Require the main line to be posted by at least 2 vendors, otherwise
        # a single-book outlier (or a thin BDL feed) will show as a top pick.
        if main_line_count < 2:
            continue
        try:
            main_line = float(main_line_str)
        except (TypeError, ValueError):
            continue
        # Only vendors offering the main line qualify
        entries = [e for e in all_entries if e.get("line_value") == main_line_str]

        # Flatten to the pickable {vendor, over_odds, under_odds} shape
        vendor_rows = [
            {"vendor": e.get("vendor"),
             "over_odds":  e.get("market", {}).get("over_odds"),
             "under_odds": e.get("market", {}).get("under_odds")}
            for e in entries
        ]
        chosen = _pick_market(vendor_rows, "over_odds", "under_odds")
        if not chosen:
            continue
        fair_over, fair_under = dev_from_market(chosen.get("over_odds"), chosen.get("under_odds"))

        if ptype in ("hits", "home_runs"):
            b = prop_index_by_bdl.get(pid)
            if not b:
                continue
            samples = b["samples"]["hits" if ptype == "hits" else "hrs"]
            label = "Hits" if ptype == "hits" else "HR"
            p_over = pct_over(samples, main_line)
            pk = _mk_pick(f"{b['name']} {label}", f"Over {main_line:g}", main_line,
                          p_over, chosen, "over_odds", fair_prob=fair_over)
            if pk: picks.append(pk)
            pk = _mk_pick(f"{b['name']} {label}", f"Under {main_line:g}", main_line,
                          1 - p_over, chosen, "under_odds", fair_prob=fair_under)
            if pk: picks.append(pk)

        elif ptype in ("strikeouts", "pitcher_strikeouts"):
            pp = pitcher_prop_by_bdl.get(pid)
            if not pp:
                continue
            p_over = pct_over(pp["ks_samples"], main_line)
            pk = _mk_pick(f"{pp['name']} K", f"Over {main_line:g}", main_line,
                          p_over, chosen, "over_odds", fair_prob=fair_over)
            if pk: picks.append(pk)
            pk = _mk_pick(f"{pp['name']} K", f"Under {main_line:g}", main_line,
                          1 - p_over, chosen, "under_odds", fair_prob=fair_under)
            if pk: picks.append(pk)

    # Final calibration guard against longshot noise (main lines only, so this
    # rarely bites — but keep it as a safety net).
    def _keep(p):
        if not _valid_odds(p.get("market_odds")):
            return False
        mp = p.get("market_prob") or 0
        return 0.08 <= mp <= 0.92
    picks = [p for p in picks if _keep(p)]
    return picks


def _mean(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def extract_hr_projections(sim: dict, away_ctx: dict, home_ctx: dict) -> list[dict]:
    """Per-batter home-run projection for the game: P(hits ≥1 HR tonight) from the
    sim's HR samples, plus expected HR count. Sorted most-likely first. A model
    projection (batter HR rate × park × opp-pitcher HR/9 × weather), NOT a bet."""
    out = []
    for side, ctx in (("away", away_ctx), ("home", home_ctx)):
        props = sim.get(f"{side}_batter_props", {})
        opp = home_ctx if side == "away" else away_ctx
        for b in ctx.get("lineup", []):
            s = props.get(b.get("mlb_id"))
            if not s:
                continue
            hrs = s.get("hrs") or []
            if not hrs:
                continue
            n = len(hrs)
            p_hr = sum(1 for x in hrs if x >= 1) / n
            out.append({
                "name": b.get("name", ""),
                "team": ctx.get("abbr") or ctx.get("team", ""),
                "opp": opp.get("abbr") or opp.get("team", ""),
                "slot": b.get("slot"),
                "p_hr": round(p_hr, 4),           # probability of 1+ HR
                "hr_mean": round(sum(hrs) / n, 3),
                "fair_odds": prob_to_american(p_hr),
            })
    out.sort(key=lambda x: -x["p_hr"])
    return out


def build_projected_boxscore(sim: dict, away_ctx: dict, home_ctx: dict,
                             year: int = 0, park_mult: float = 1.0) -> dict:
    """Compute expected per-player lines from the sim. The starter line is built
    from the pitcher's OWN season per-start averages (innings, K, BB, H, HR),
    lightly adjusted for the opposing offense and park — not a generic number."""

    def team_box(side: str, ctx: dict, opp_ctx: dict) -> dict:
        opp_pitcher = opp_ctx.get("pitcher", {})
        props_by_id = sim[f"{side}_batter_props"]
        team_runs_mean = _mean(sim[f"{side}_runs"])
        # OBP / SLG totals for proportional R/RBI allocation
        obp_sum = sum(props_by_id[b["mlb_id"]]["obp"] for b in ctx["lineup"]
                      if b["mlb_id"] in props_by_id) or 1.0
        slg_sum = sum(props_by_id[b["mlb_id"]]["slg"] for b in ctx["lineup"]
                      if b["mlb_id"] in props_by_id) or 1.0

        batters = []
        totals = {"ab": 0.0, "r": 0.0, "h": 0.0, "hr": 0.0, "rbi": 0.0,
                  "bb": 0.0, "k": 0.0}
        for b in ctx["lineup"]:
            samples = props_by_id.get(b["mlb_id"])
            if not samples:
                # No batter stats; still show the row with dashes
                batters.append({
                    "slot": b["slot"], "pos": b.get("pos", ""), "name": b["name"],
                    "ab": None, "r": None, "h": None, "hr": None,
                    "rbi": None, "bb": None, "k": None,
                })
                continue
            pa = _mean(samples["pas"])
            h  = _mean(samples["hits"])
            hr = _mean(samples["hrs"])
            bb = _mean(samples["walks"])
            k  = _mean(samples["ks"])
            ab = max(0.0, pa - bb - 0.02 * pa)  # ~2% HBP/SF
            r  = team_runs_mean * (samples["obp"] / obp_sum)
            rbi = team_runs_mean * (samples["slg"] / slg_sum)
            row = {"slot": b["slot"], "pos": b.get("pos", ""), "name": b["name"],
                   "ab": round(ab, 1), "r": round(r, 1), "h": round(h, 1),
                   "hr": round(hr, 2), "rbi": round(rbi, 1),
                   "bb": round(bb, 1), "k": round(k, 1)}
            batters.append(row)
            for f in totals:
                if row[f] is not None:
                    totals[f] += row[f]

        totals_row = {k: round(v, 1) if k != "hr" else round(v, 2) for k, v in totals.items()}
        totals_row["r"] = round(team_runs_mean, 1)

        # Starter pitching line — from THIS pitcher's own season per-start averages.
        pit = ctx["pitcher"]
        era = pit.get("blend_era") or LG_ERA
        # Opposing offense strength (this team is batting vs the OTHER team's starter;
        # for the starter line we want the offense THIS starter faces = the opponent).
        opp_rpg = opp_ctx.get("off_rpg") or LG_RPG
        opp_factor = opp_rpg / LG_RPG            # >1 = tougher lineup → more runs/hits

        prof = get_pitcher_start_profile(pit.get("bdl_id") or pit.get("mlb_id"), year) if year else {}
        if prof:
            exp_ip = round(prof["ip_per_start"], 1)
            ip9 = exp_ip / 9.0                                  # scale per-9 rates to the start
            exp_k = prof["k9"] * ip9 * opp_factor ** 0.3        # slight matchup nudge
            exp_bb = prof["bb9"] * ip9 * opp_factor ** 0.2
            exp_h = prof["h9"] * ip9 * opp_factor * park_mult
            exp_hr = prof["hr9"] * ip9 * opp_factor * park_mult
            exp_er = prof["er9"] * ip9 * opp_factor * park_mult
        else:
            # Fallback when the pitcher has no season starts yet (rookie/TBD/opener).
            exp_ip = round(max(3.5, min(7.0, STARTER_IP * (LG_ERA / max(era, 2.5)) ** 0.5)), 1)
            exp_bf = exp_ip * 4.25
            exp_k = _mean(sim[f"{side}_pitcher_ks"]) * (exp_bf / 24.0)
            exp_bb = exp_bf * ((pit.get("bb_pct") or 8.0) / 100.0)
            exp_h = exp_bf * 0.235 * (era / LG_ERA) * opp_factor
            exp_er = era * exp_ip / 9.0 * opp_factor
            exp_hr = 1.15 * exp_ip / 9.0 * park_mult

        starter = {
            "name": pit.get("name", ""),
            "ip": exp_ip,
            "h": round(exp_h, 1),
            "r": round(exp_er, 1),
            "er": round(exp_er, 1),
            "bb": round(exp_bb, 1),
            "k": round(exp_k, 1),
            "hr": round(exp_hr, 2),
            "starts": prof.get("starts") if prof else None,
        }
        return {"batters": batters, "totals": totals_row, "starter": starter}

    return {
        "away": team_box("away", away_ctx, home_ctx),
        "home": team_box("home", home_ctx, away_ctx),
    }


# ---- Orchestration ----------------------------------------------------------

def _slug_norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def load_scraper_context(date_str: str) -> dict[str, dict]:
    """Load the mlb_tonight_edates.csv (park factor, pitcher xERA, team splits, weather).
    Key by frozenset({away_team, home_team})."""
    p = CWD / "mlb_tonight_edates.csv"
    if not p.exists():
        print("  (note: mlb_tonight_edates.csv missing - context will be limited)", file=sys.stderr)
        return {}
    with open(p) as f:
        rows = list(csv.DictReader(f))
    if rows and rows[0].get("date") != date_str:
        # Scraper CSV is from a different date; still use it for cross-reference maps
        pass
    # Key by gamePk (unique per game — distinguishes doubleheaders); fall back to
    # matchup for older CSVs written before game_pk existed.
    idx = {}
    for r in rows:
        gpk = str(r.get("game_pk") or "").strip()
        if gpk:
            idx[f"pk:{gpk}"] = r
        idx.setdefault(f"mu:{frozenset({_slug_norm(r['away_team']), _slug_norm(r['home_team'])})}", r)
    return idx


def _pitcher_from_scraper(scraper_row: dict, side_key: str) -> dict:
    """Build a pitcher ctx dict from the scraper CSV alone (no BDL lineup call)."""
    name = scraper_row.get(f"{side_key}_pitcher", "") if scraper_row else ""
    pit = {"bdl_id": None, "name": name}
    if scraper_row:
        try:
            era = float(scraper_row.get(f"{side_key}_pitcher_era") or 0)
        except (TypeError, ValueError):
            era = 0
        try:
            xera = float(scraper_row.get(f"{side_key}_pitcher_xera") or 0)
        except (TypeError, ValueError):
            xera = 0
        try:
            k_pct = float(scraper_row.get(f"{side_key}_pitcher_k_pct") or 0)
        except (TypeError, ValueError):
            k_pct = 0
        pit["era"] = era or None
        pit["xera"] = xera or None
        pit["blend_era"] = (era + xera) / 2 if (era and xera) else (era or xera or None)
        pit["k_pct"] = k_pct or None
    return pit


def build_game_context(bdl_game: dict, scraper_row: dict, year: int,
                       game_lines_only: bool = False,
                       date_str: str = "") -> tuple[dict, dict, float, dict]:
    """Prepare per-team ctx dicts for the simulator, plus park factor and weather.

    game_lines_only=True skips the lineup lookup and per-batter stat enrichment
    (used by the backtester, which grades only ML/Total/Runline)."""
    away_team = bdl_game["away_team_name"]
    home_team = bdl_game["home_team_name"]

    if game_lines_only:
        away_pitcher = _pitcher_from_scraper(scraper_row, "away")
        home_pitcher = _pitcher_from_scraper(scraper_row, "home")

        def team_ops_only(side_key: str) -> float | None:
            if not scraper_row:
                return None
            cell = scraper_row.get(f"{side_key}_vs_opp_hand", "")
            try:
                for part in cell.split("/"):
                    part = part.strip()
                    if part.startswith("OPS"):
                        return float(part.replace("OPS", "").strip())
            except Exception:
                return None
            return None

        away_ctx = {
            "team": away_team, "abbr": bdl_game["away_team"]["abbreviation"],
            "lineup": [], "pitcher": away_pitcher,
            "team_ops_vs_hand": team_ops_only("away"),
            **_extra_scoring(scraper_row, "away", away_team),
        }
        home_ctx = {
            "team": home_team, "abbr": bdl_game["home_team"]["abbreviation"],
            "lineup": [], "pitcher": home_pitcher,
            "team_ops_vs_hand": team_ops_only("home"),
            **_extra_scoring(scraper_row, "home", home_team),
        }
        park_factor = None
        weather = {}
        if scraper_row:
            try:
                park_factor = float(scraper_row.get("park_factor") or 0) or None
            except ValueError:
                park_factor = None
        return away_ctx, home_ctx, park_factor or 100.0, weather

    # Lineups from the free MLB Stats API — by gamePk (doubleheader-correct), else matchup.
    lineups = _load_lineups_for_date(date_str) if date_str else {}
    gpk = str(bdl_game.get("gamePk") or "").strip()
    lu = (lineups.get(f"pk:{gpk}") if gpk else None) or \
        lineups.get(f"mu:{frozenset({_slug_norm(away_team), _slug_norm(home_team)})}", {})

    def side_pieces(side_key: str) -> tuple[list[dict], dict]:
        players = lu.get(side_key, []) or []
        batters = []
        for i, p in enumerate(players, 1):
            name = p.get("fullName") or ""
            if not name:
                continue
            batters.append({
                "bdl_id": p.get("id"),   # MLB player id (kept under old key name)
                "name": name,
                "slot": i,               # array order = batting order
                "pos": (p.get("primaryPosition", {}) or {}).get("abbreviation", ""),
                "mlb_id": p.get("id"),
                "stats": {},
            })
        pp = lu.get(f"{side_key}_pp", {}) or {}
        pitcher = {"bdl_id": pp.get("id"), "name": pp.get("fullName", "")}
        return batters, pitcher

    away_batters, away_pitcher = side_pieces("away")
    home_batters, home_pitcher = side_pieces("home")

    # Enrich batters with MLB StatsAPI season stats (player id already known — no name search)
    for b in away_batters + home_batters:
        if b["mlb_id"]:
            b["stats"] = get_batter_season_stats(b["mlb_id"], year)

    # Enrich pitchers from scraper CSV (already has ERA/xERA/K%)
    def enrich_pitcher(pit: dict, side_key: str) -> None:
        if scraper_row:
            name_key = f"{side_key}_pitcher"
            if scraper_row.get(name_key, "").strip() == pit.get("name", "").strip():
                try:
                    era = float(scraper_row.get(f"{side_key}_pitcher_era") or 0)
                except ValueError:
                    era = 0
                try:
                    xera = float(scraper_row.get(f"{side_key}_pitcher_xera") or 0)
                except ValueError:
                    xera = 0
                try:
                    k_pct = float(scraper_row.get(f"{side_key}_pitcher_k_pct") or 0)
                except ValueError:
                    k_pct = 0
                blend = (era + xera) / 2 if (era and xera) else (era or xera or None)
                pit["era"] = era or None
                pit["xera"] = xera or None
                pit["blend_era"] = blend
                pit["k_pct"] = k_pct or None
                return
        # Fallback: pull from MLB StatsAPI
        pid = search_mlb_player_by_name(pit.get("name", ""), year)
        pit["mlb_id"] = pid
        if pid:
            try:
                d = mlb_get(f"/people/{pid}/stats", stats="season", group="pitching", season=year)
                splits = (d.get("stats") or [{}])[0].get("splits") or []
                if splits:
                    s = splits[0].get("stat", {})
                    era = float(s.get("era") or 0) or None
                    tbf = int(s.get("battersFaced", 0) or 0)
                    ks = int(s.get("strikeOuts", 0) or 0)
                    pit["era"] = era
                    pit["blend_era"] = era
                    pit["k_pct"] = (ks / tbf * 100) if tbf else None
            except Exception:
                pass

    enrich_pitcher(away_pitcher, "away")
    enrich_pitcher(home_pitcher, "home")

    # Attach each starter's HR/9 allowed (for the batter-HR projection model).
    for pit in (away_pitcher, home_pitcher):
        prof = get_pitcher_start_profile(pit.get("bdl_id") or pit.get("mlb_id"), year)
        if prof:
            pit["hr9"] = prof.get("hr9")

    # Team offense OPS vs opposing hand — from scraper CSV
    def team_ops_vs(side_key: str, opp_hand: str | None) -> float | None:
        if not scraper_row:
            return None
        cell = scraper_row.get(f"{side_key}_vs_opp_hand", "")
        # format: "AVG .240 / OPS .729 / 113 HR"
        try:
            for part in cell.split("/"):
                part = part.strip()
                if part.startswith("OPS"):
                    return float(part.replace("OPS", "").strip())
        except Exception:
            return None
        return None

    away_ctx = {
        "team": away_team, "abbr": bdl_game["away_team"]["abbreviation"],
        "lineup": away_batters, "pitcher": away_pitcher,
        "team_ops_vs_hand": team_ops_vs("away", home_pitcher.get("k_pct")),
        **_extra_scoring(scraper_row, "away", away_team),
    }
    home_ctx = {
        "team": home_team, "abbr": bdl_game["home_team"]["abbreviation"],
        "lineup": home_batters, "pitcher": home_pitcher,
        "team_ops_vs_hand": team_ops_vs("home", away_pitcher.get("k_pct")),
        **_extra_scoring(scraper_row, "home", home_team),
    }

    park_factor = None
    weather = {}
    if scraper_row:
        try:
            park_factor = float(scraper_row.get("park_factor") or 0) or None
        except ValueError:
            park_factor = None
        try:
            temp = scraper_row.get("weather_temp_f", "")
            if temp and temp != "DOME":
                weather["temp_f"] = float(temp)
            wind_str = scraper_row.get("weather_wind", "")
            if wind_str:
                # e.g. "3.7 mph SE"
                weather["wind_mph"] = float(wind_str.split()[0])
                weather["wind_dir"] = wind_str.split()[-1]
            pp = scraper_row.get("weather_precip_pct", "")
            if pp:
                weather["precip_pct"] = float(pp)
        except Exception:
            pass

    return away_ctx, home_ctx, park_factor or 100.0, weather


def run_for_date(date_str: str, game_lines_only: bool = False) -> Path:
    year = int(date_str[:4])
    mode = " (game-lines-only, fast)" if game_lines_only else ""
    print(f"Predicting for {date_str}{mode}", file=sys.stderr)

    # Load supporting context from the existing scraper output
    scraper_idx = load_scraper_context(date_str)
    load_team_scoring(year)  # team runs-for/against per game (one StatsAPI call, cached)

    games = fetch_games(date_str)
    print(f"  ESPN returned {len(games)} games", file=sys.stderr)

    all_output = {"date": date_str, "n_sims": N_SIMS, "generated_at": datetime.now(ET).isoformat(),
                  "games": []}

    # ---- Pass 1: build contexts + odds, compute raw model totals vs market ----
    prepared = []
    model_tot_sum = market_tot_sum = 0.0
    for i, g in enumerate(games, 1):
        gid = g["id"]
        away_name = g["away_team_name"]; home_name = g["home_team_name"]
        print(f"  [{i}/{len(games)}] {away_name} @ {home_name}", file=sys.stderr)
        # Match the scraper row by gamePk (correct for doubleheaders), else matchup.
        gpk = str(g.get("gamePk") or "").strip()
        scraper_row = (scraper_idx.get(f"pk:{gpk}") if gpk else None) or \
            scraper_idx.get(f"mu:{frozenset({_slug_norm(away_name), _slug_norm(home_name)})}") or {}
        try:
            away_ctx, home_ctx, park_factor, weather = build_game_context(
                g, scraper_row, year, game_lines_only=game_lines_only, date_str=date_str)
        except Exception as e:
            print(f"    context error: {e}", file=sys.stderr)
            continue
        try:
            odds = fetch_odds(gid)
        except Exception as e:
            print(f"    odds fetch error: {e}", file=sys.stderr)
            odds = []
        am, hm = game_run_means(away_ctx, home_ctx, park_factor, weather)
        market_total = _modal_value(odds, "total_value")
        if market_total is not None and (am + hm) > 0:
            model_tot_sum += (am + hm)
            market_tot_sum += market_total
        prepared.append((g, gid, away_name, home_name, scraper_row,
                         away_ctx, home_ctx, park_factor, weather, odds))

    # Per-slate calibration: scale the model's run level to the market's average so
    # a systematic over/under lean (bias, not edge) is removed. Clamp to a sane band.
    run_scale = 1.0
    if model_tot_sum > 0 and market_tot_sum > 0:
        run_scale = max(0.80, min(1.20, market_tot_sum / model_tot_sum))
    print(f"  run calibration scale = {run_scale:.3f}", file=sys.stderr)

    # ---- Pass 2: simulate with the calibrated run level, generate picks ----
    for (g, gid, away_name, home_name, scraper_row,
         away_ctx, home_ctx, park_factor, weather, odds) in prepared:
        props = []  # no free player-prop odds source
        try:
            sim = sim_game(away_ctx, home_ctx, park_factor, weather,
                           n_sims=N_SIMS, run_scale=run_scale)
        except Exception as e:
            print(f"    sim error: {e}", file=sys.stderr)
            continue

        try:
            picks = generate_picks(sim, odds, props, away_ctx, home_ctx)
        except Exception as e:
            print(f"    picks error: {e}", file=sys.stderr)
            picks = []

        try:
            proj_box = build_projected_boxscore(
                sim, away_ctx, home_ctx, year=year,
                park_mult=(park_factor / 100.0 if park_factor else 1.0))
        except Exception as e:
            print(f"    boxscore error: {e}", file=sys.stderr)
            proj_box = {}

        try:
            hr_proj = extract_hr_projections(sim, away_ctx, home_ctx)
        except Exception as e:
            print(f"    hr projection error: {e}", file=sys.stderr)
            hr_proj = []

        # Distributions summary for display
        away_runs = sim["away_runs"]; home_runs = sim["home_runs"]
        totals = [a + h for a, h in zip(away_runs, home_runs)]
        margins = [h - a for a, h in zip(away_runs, home_runs)]

        p_away = sum(1 for a, h in zip(away_runs, home_runs) if a > h) / len(away_runs)
        p_home = 1 - p_away - sum(1 for a, h in zip(away_runs, home_runs) if a == h) / len(away_runs)

        all_output["games"].append({
            "bdl_game_id": gid,
            "game_pk": g.get("gamePk"),   # unique per game — distinguishes doubleheaders
            "date": date_str,
            "away_team": away_name,
            "home_team": home_name,
            "away_abbr": g["away_team"]["abbreviation"],
            "home_abbr": g["home_team"]["abbreviation"],
            "venue": (g.get("venue") if isinstance(g.get("venue"), str) else (g.get("venue") or {}).get("name", "")),
            "away_pitcher": away_ctx["pitcher"].get("name"),
            "home_pitcher": home_ctx["pitcher"].get("name"),
            "park_factor": park_factor,
            "weather": weather,
            "model": {
                "away_win_pct": round(p_away, 4),
                "home_win_pct": round(1 - p_away, 4),
                "mean_away_runs": round(sum(away_runs) / len(away_runs), 2),
                "mean_home_runs": round(sum(home_runs) / len(home_runs), 2),
                "mean_total": round(sum(totals) / len(totals), 2),
                "mean_margin": round(sum(margins) / len(margins), 2),
            },
            "picks": sorted(picks, key=lambda p: p["ev_pct"], reverse=True),
            "projected_box": proj_box,
            "hr_projections": hr_proj,
            # Embed the per-game scraper context so the dashboard is self-contained
            # per date (never grafts a stale transient CSV onto the wrong day).
            "context": scraper_row,
        })

    out_path = DATA_DIR / f"predictions_{date_str}.json"
    with open(out_path, "w") as f:
        json.dump(all_output, f, indent=2)
    print(f"Wrote {out_path}", file=sys.stderr)
    return out_path


def main() -> int:
    game_lines_only = "--game-lines-only" in sys.argv
    date_arg = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    date_str = date_arg or datetime.now(ET).strftime("%Y-%m-%d")
    run_for_date(date_str, game_lines_only=game_lines_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
