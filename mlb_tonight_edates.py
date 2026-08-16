"""Scrape tonight's MLB slate from the public MLB Stats API and write a CSV.

Standard library only (no `requests` install needed). Run:
    python mlb_tonight_edates.py

Output: mlb_tonight_edates.csv in the current directory.
"""

from __future__ import annotations

import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE_URL = "https://statsapi.mlb.com/api/v1"
STATS_API_11 = "https://statsapi.mlb.com/api/v1.1"
SAVANT_URL = "https://baseballsavant.mlb.com/leaderboard/custom"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ET = ZoneInfo("America/New_York")
OUTPUT_FILE = "mlb_tonight_edates.csv"
HTML_FILE = "mlb_tonight.html"
REQUEST_PAUSE = 0.1  # seconds between calls, be polite

# --fast skips display-only / sim-irrelevant per-game work (bullpen boxscores,
# umpire, injuries, last-3-starts, weather). Used by the backtester. Set in main().
FAST = False

# Stadium coords + dome flag. Domed stadiums skip weather.
# Source: publicly available stadium coordinates.
VENUE_META = {
    "Angel Stadium":               (33.8003, -117.8827, False),
    "American Family Field":       (43.0280, -87.9712, True),   # retractable roof, treat as domed
    "Busch Stadium":               (38.6226, -90.1928, False),
    "Camden Yards":                (39.2839, -76.6218, False),
    "Oriole Park at Camden Yards": (39.2839, -76.6218, False),
    "Chase Field":                 (33.4453, -112.0667, True),  # retractable, usually closed in AZ heat
    "Citi Field":                  (40.7571, -73.8458, False),
    "Citizens Bank Park":          (39.9061, -75.1665, False),
    "Comerica Park":               (42.3390, -83.0485, False),
    "Coors Field":                 (39.7562, -104.9942, False),
    "Daikin Park":                 (29.7573, -95.3555, True),   # Minute Maid renamed
    "Minute Maid Park":            (29.7573, -95.3555, True),
    "Dodger Stadium":              (34.0739, -118.2400, False),
    "Fenway Park":                 (42.3467, -71.0972, False),
    "George M. Steinbrenner Field":(27.9800, -82.5069, False),  # Rays temp home
    "Globe Life Field":            (32.7473, -97.0847, True),   # retractable
    "Great American Ball Park":    (39.0975, -84.5069, False),
    "Guaranteed Rate Field":       (41.8300, -87.6339, False),
    "Rate Field":                  (41.8300, -87.6339, False),
    "Kauffman Stadium":            (39.0517, -94.4803, False),
    "loanDepot park":              (25.7781, -80.2197, True),   # retractable
    "Nationals Park":              (38.8730, -77.0074, False),
    "Oracle Park":                 (37.7786, -122.3893, False),
    "PNC Park":                    (40.4469, -80.0057, False),
    "Petco Park":                  (32.7073, -117.1566, False),
    "Progressive Field":           (41.4962, -81.6852, False),
    "Rogers Centre":               (43.6414, -79.3894, True),   # retractable
    "Sutter Health Park":          (38.5804, -121.5133, False), # A's temp home Sacramento
    "T-Mobile Park":               (47.5914, -122.3325, True),  # retractable
    "Target Field":                (44.9817, -93.2776, False),
    "Truist Park":                 (33.8908, -84.4678, False),
    "Wrigley Field":               (41.9484, -87.6553, False),
    "Yankee Stadium":              (40.8296, -73.9262, False),
}

# Park factors: 100 = neutral. Source: Statcast 3-yr rolling wRC-based factors
# (approximate values; refresh yearly from baseballsavant.mlb.com/leaderboard/statcast-park-factors).
PARK_FACTORS = {
    "Coors Field": 112,
    "Great American Ball Park": 108,
    "Fenway Park": 107,
    "Globe Life Field": 105,
    "Chase Field": 103,
    "Wrigley Field": 102,
    "Citizens Bank Park": 102,
    "Camden Yards": 101,
    "Oriole Park at Camden Yards": 101,
    "Yankee Stadium": 101,
    "Target Field": 100,
    "Rogers Centre": 100,
    "Nationals Park": 100,
    "American Family Field": 100,
    "Angel Stadium": 100,
    "Minute Maid Park": 100,
    "Daikin Park": 100,
    "Kauffman Stadium": 99,
    "Truist Park": 99,
    "Comerica Park": 99,
    "Progressive Field": 98,
    "PNC Park": 98,
    "Guaranteed Rate Field": 98,
    "Rate Field": 98,
    "Busch Stadium": 97,
    "T-Mobile Park": 97,
    "loanDepot park": 96,
    "Dodger Stadium": 96,
    "Citi Field": 95,
    "Oracle Park": 94,
    "Petco Park": 94,
    "Sutter Health Park": 100,
    "George M. Steinbrenner Field": 102,
}


def http_get_json(path: str, **params) -> dict:
    """GET a JSON payload from the Stats API."""
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{BASE_URL}{path}?{qs}"
    else:
        url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "mlb-tonight/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    time.sleep(REQUEST_PAUSE)
    return data


def tonight_date_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def get_tonight_schedule(date_str: str) -> list[dict]:
    payload = http_get_json(
        "/schedule",
        sportId=1,
        date=date_str,
        hydrate="probablePitcher,team,venue",
    )
    games: list[dict] = []
    for day in payload.get("dates", []):
        games.extend(day.get("games", []))
    return games


def format_first_pitch_et(iso_utc: str) -> str:
    dt_utc = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    dt_et = dt_utc.astimezone(ET)
    # Strip leading zero on hour for readability: "7:05 PM ET"
    return dt_et.strftime("%-I:%M %p ET")


def get_pitcher_season_era(pid: int, year: int) -> str:
    try:
        data = http_get_json(
            f"/people/{pid}/stats",
            stats="season",
            group="pitching",
            season=year,
        )
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        if not splits:
            return ""
        return str(splits[0].get("stat", {}).get("era", ""))
    except Exception:
        return ""


def get_pitcher_last_three(pid: int, year: int) -> str:
    """Return a compact string of the pitcher's last three starts."""
    try:
        data = http_get_json(
            f"/people/{pid}/stats",
            stats="gameLog",
            group="pitching",
            season=year,
        )
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        starts = [s for s in splits if s.get("stat", {}).get("gamesStarted", 0) == 1]
        last3 = starts[-3:]
        parts = []
        for s in last3:
            st = s.get("stat", {})
            date = s.get("date", "")
            ip = st.get("inningsPitched", "0.0")
            er = st.get("earnedRuns", 0)
            k = st.get("strikeOuts", 0)
            parts.append(f"{date}: {ip} IP, {er} ER, {k} K")
        return " | ".join(parts)
    except Exception:
        return ""


def _ip_to_thirds(ip_str: str) -> int:
    """Convert baseball IP notation ('6.1' = 6 and 1/3) to integer thirds."""
    if not ip_str:
        return 0
    try:
        whole_str, _, frac = str(ip_str).partition(".")
        whole = int(whole_str or 0)
        frac_thirds = {"": 0, "0": 0, "1": 1, "2": 2}.get(frac, 0)
        return whole * 3 + frac_thirds
    except Exception:
        return 0


def _thirds_to_ip(thirds: int) -> str:
    whole, remainder = divmod(thirds, 3)
    return f"{whole}.{remainder}"


def get_bullpen_usage(team_id: int, end_date_str: str, days: int = 3) -> str:
    """Sum bullpen (non-starter) innings pitched over the last `days` calendar days
    prior to (and not including) the target date."""
    end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    start = end - timedelta(days=days)
    yesterday = end - timedelta(days=1)
    try:
        sched = http_get_json(
            "/schedule",
            sportId=1,
            teamId=team_id,
            startDate=start.strftime("%Y-%m-%d"),
            endDate=yesterday.strftime("%Y-%m-%d"),
        )
    except Exception:
        return ""

    total_thirds = 0
    for day in sched.get("dates", []):
        for game in day.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            game_pk = game.get("gamePk")
            if not game_pk:
                continue
            try:
                box = http_get_json(f"/game/{game_pk}/boxscore")
            except Exception:
                continue
            teams = box.get("teams", {})
            for side in ("home", "away"):
                side_data = teams.get(side, {})
                if side_data.get("team", {}).get("id") != team_id:
                    continue
                pitcher_ids = side_data.get("pitchers", [])
                players = side_data.get("players", {})
                if not pitcher_ids:
                    continue
                # First pitcher in the list is the starter.
                starter_id = pitcher_ids[0]
                for pid in pitcher_ids[1:]:
                    p = players.get(f"ID{pid}", {})
                    ip = p.get("stats", {}).get("pitching", {}).get("inningsPitched", "0.0")
                    total_thirds += _ip_to_thirds(ip)
                # Safety: also skip anyone flagged as starter explicitly
                _ = starter_id  # noqa: F841
    return _thirds_to_ip(total_thirds)


_SAVANT_CACHE: dict[int, dict] = {}


def load_savant_pitcher_leaderboard(year: int) -> None:
    """Fetch the full Statcast pitcher leaderboard once, cache by player_id.
    Populates _SAVANT_CACHE with {pid: {xera, k_pct, bb_pct, barrel_pct, whiff_pct, hardhit_pct}}."""
    if _SAVANT_CACHE:
        return
    params = {
        "year": year,
        "type": "pitcher",
        "filter": "",
        "min": "0",  # include all pitchers, not just qualified
        "selections": "xera,k_percent,bb_percent,barrel_batted_rate,whiff_percent,hard_hit_percent",
        "csv": "true",
    }
    url = f"{SAVANT_URL}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mlb-tonight/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8-sig")  # strip BOM
        reader = csv.DictReader(text.splitlines())
        for row in reader:
            try:
                pid = int(row.get("player_id", "") or 0)
            except ValueError:
                continue
            if not pid:
                continue
            _SAVANT_CACHE[pid] = {
                "xera": row.get("xera", "") or "",
                "k_pct": row.get("k_percent", "") or "",
                "bb_pct": row.get("bb_percent", "") or "",
                "barrel_pct": row.get("barrel_batted_rate", "") or "",
                "whiff_pct": row.get("whiff_percent", "") or "",
                "hardhit_pct": row.get("hard_hit_percent", "") or "",
            }
        time.sleep(REQUEST_PAUSE)
    except Exception as e:
        print(f"  (savant leaderboard fetch failed: {e})", file=sys.stderr)


def get_savant_stats(pid: int) -> dict:
    return _SAVANT_CACHE.get(pid, {})


def get_weather(venue_name: str, first_pitch_iso_utc: str) -> dict:
    """Return weather at first-pitch hour for open-air stadiums. Returns {} for domes."""
    meta = VENUE_META.get(venue_name)
    if not meta:
        return {}
    lat, lon, is_domed = meta
    if is_domed:
        return {"dome": True}
    try:
        dt_utc = datetime.fromisoformat(first_pitch_iso_utc.replace("Z", "+00:00"))
        dt_et = dt_utc.astimezone(ET)
        target_hour = dt_et.strftime("%Y-%m-%dT%H:00")
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,precipitation_probability",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "America/New_York",
            "forecast_days": 2,
        }
        url = f"{OPEN_METEO_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "mlb-tonight/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        time.sleep(REQUEST_PAUSE)
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        if target_hour not in times:
            return {}
        idx = times.index(target_hour)
        return {
            "temp_f": hourly["temperature_2m"][idx],
            "wind_mph": hourly["wind_speed_10m"][idx],
            "wind_dir": hourly["wind_direction_10m"][idx],
            "precip_pct": hourly["precipitation_probability"][idx],
        }
    except Exception as e:
        print(f"  (weather fetch failed for {venue_name}: {e})", file=sys.stderr)
        return {}


def _wind_dir_str(deg: float) -> str:
    """Convert compass degrees to 8-point label."""
    if deg is None:
        return ""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) // 45) % 8]


_STANDINGS_CACHE: dict[int, dict] = {}


def load_standings(year: int) -> None:
    if _STANDINGS_CACHE:
        return
    try:
        data = http_get_json(
            "/standings",
            leagueId="103,104",
            season=year,
            standingsTypes="regularSeason",
        )
        for record in data.get("records", []):
            for tr in record.get("teamRecords", []):
                team_id = tr.get("team", {}).get("id")
                if not team_id:
                    continue
                splits = {s["type"]: s for s in tr.get("records", {}).get("splitRecords", [])}
                l10 = splits.get("lastTen", {})
                _STANDINGS_CACHE[team_id] = {
                    "wins": tr.get("wins"),
                    "losses": tr.get("losses"),
                    "run_diff": tr.get("runDifferential"),
                    "streak": tr.get("streak", {}).get("streakCode", ""),
                    "l10": f"{l10.get('wins','?')}-{l10.get('losses','?')}" if l10 else "",
                    "vs_lhp": splits.get("left", {}),
                    "vs_rhp": splits.get("right", {}),
                }
    except Exception as e:
        print(f"  (standings fetch failed: {e})", file=sys.stderr)


def get_team_form(team_id: int) -> dict:
    return _STANDINGS_CACHE.get(team_id, {})


def get_team_splits_vs_hand(team_id: int, year: int) -> dict:
    """Team hitting splits vs LHP and vs RHP for the season."""
    try:
        data = http_get_json(
            f"/teams/{team_id}/stats",
            season=year,
            group="hitting",
            stats="statSplits",
            sitCodes="vl,vr",
        )
        out = {}
        for grp in data.get("stats", []):
            for s in grp.get("splits", []):
                code = s.get("split", {}).get("code")
                stat = s.get("stat", {})
                if code in ("vl", "vr"):
                    out[code] = {
                        "avg": stat.get("avg", ""),
                        "ops": stat.get("ops", ""),
                        "hr": stat.get("homeRuns", ""),
                    }
        return out
    except Exception as e:
        print(f"  (team splits fetch failed for {team_id}: {e})", file=sys.stderr)
        return {}


def get_umpire(game_pk: int) -> str:
    # /v1.1 for feed/live; call directly since base URL differs from v1
    try:
        url = f"{STATS_API_11}/game/{game_pk}/feed/live"
        req = urllib.request.Request(url, headers={"User-Agent": "mlb-tonight/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        time.sleep(REQUEST_PAUSE)
        for o in data.get("liveData", {}).get("boxscore", {}).get("officials", []):
            if o.get("officialType") == "Home Plate":
                return o.get("official", {}).get("fullName", "")
    except Exception as e:
        print(f"  (umpire fetch failed for game {game_pk}: {e})", file=sys.stderr)
    return ""


def get_injured_list_count(team_id: int) -> tuple[int, str]:
    """Return (count_on_IL, comma-separated names of top 5) for a team's 40-man roster."""
    try:
        data = http_get_json(
            f"/teams/{team_id}/roster",
            rosterType="40Man",
            hydrate="person",
        )
        il_names = []
        for p in data.get("roster", []):
            code = p.get("status", {}).get("code", "")
            # IL status codes: D7, D10, D15, D60. DL is legacy; RM/RES = other.
            if code in ("D7", "D10", "D15", "D60", "DL"):
                il_names.append(p.get("person", {}).get("fullName", ""))
        return len(il_names), ", ".join(il_names[:5])
    except Exception as e:
        print(f"  (roster fetch failed for {team_id}: {e})", file=sys.stderr)
        return 0, ""


_PITCHER_HAND_CACHE: dict[int, str] = {}


def get_pitcher_hand(pid: int) -> str:
    if pid in _PITCHER_HAND_CACHE:
        return _PITCHER_HAND_CACHE[pid]
    try:
        data = http_get_json(f"/people/{pid}")
        hand = ""
        for p in data.get("people", []):
            hand = p.get("pitchHand", {}).get("code", "")
            break
        _PITCHER_HAND_CACHE[pid] = hand
        return hand
    except Exception:
        _PITCHER_HAND_CACHE[pid] = ""
        return ""


def get_bullpen_detail(team_id: int, end_date_str: str) -> dict:
    """Bullpen IP over last 3 days + count of relievers appearing on back-to-back days
    (a proxy for who's unavailable tonight)."""
    end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    start = end - timedelta(days=3)
    yesterday = end - timedelta(days=1)
    try:
        sched = http_get_json(
            "/schedule",
            sportId=1,
            teamId=team_id,
            startDate=start.strftime("%Y-%m-%d"),
            endDate=yesterday.strftime("%Y-%m-%d"),
        )
    except Exception:
        return {"ip": "", "b2b": 0}

    total_thirds = 0
    appearances_by_date: dict[str, set[int]] = {}
    for day in sched.get("dates", []):
        game_date = day.get("date", "")
        for game in day.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            game_pk = game.get("gamePk")
            if not game_pk:
                continue
            try:
                box = http_get_json(f"/game/{game_pk}/boxscore")
            except Exception:
                continue
            teams = box.get("teams", {})
            for side in ("home", "away"):
                sd = teams.get(side, {})
                if sd.get("team", {}).get("id") != team_id:
                    continue
                pitcher_ids = sd.get("pitchers", [])
                if not pitcher_ids:
                    continue
                # skip starter (first in list)
                relievers = pitcher_ids[1:]
                players = sd.get("players", {})
                appearances_by_date.setdefault(game_date, set()).update(relievers)
                for pid in relievers:
                    p = players.get(f"ID{pid}", {})
                    ip = p.get("stats", {}).get("pitching", {}).get("inningsPitched", "0.0")
                    total_thirds += _ip_to_thirds(ip)
    # back-to-back count: pitchers who appear on yesterday AND the day before
    dates_sorted = sorted(appearances_by_date.keys())
    b2b = 0
    if len(dates_sorted) >= 2:
        last_two = appearances_by_date[dates_sorted[-1]] & appearances_by_date[dates_sorted[-2]]
        b2b = len(last_two)
    return {"ip": _thirds_to_ip(total_thirds), "b2b": b2b}


def build_row(game: dict, date_str: str, season_year: int) -> dict:
    teams = game.get("teams", {})
    away = teams.get("away", {})
    home = teams.get("home", {})
    away_team = away.get("team", {}).get("name", "")
    home_team = home.get("team", {}).get("name", "")
    away_team_id = away.get("team", {}).get("id")
    home_team_id = home.get("team", {}).get("id")

    venue_name = game.get("venue", {}).get("name", "")
    park_factor = PARK_FACTORS.get(venue_name, "")
    game_pk = game.get("gamePk")

    first_pitch = format_first_pitch_et(game.get("gameDate", ""))
    game_date_iso = game.get("gameDate", "")

    away_pp = away.get("probablePitcher") or {}
    home_pp = home.get("probablePitcher") or {}

    def pitcher_block(pp: dict) -> dict:
        pid = pp.get("id")
        name = pp.get("fullName", "TBD")
        if not pid:
            return {"name": "TBD", "era": "", "last3": "", "hand": "",
                    "xera": "", "k_pct": "", "bb_pct": "", "barrel_pct": "",
                    "whiff_pct": "", "hardhit_pct": ""}
        era = get_pitcher_season_era(pid, season_year)
        last3 = "" if FAST else get_pitcher_last_three(pid, season_year)  # display-only
        savant = get_savant_stats(pid)
        hand = pp.get("pitchHand", {}).get("code", "") or get_pitcher_hand(pid)
        return {
            "name": name,
            "era": era,
            "last3": last3,
            "hand": hand,
            "xera": savant.get("xera", ""),
            "k_pct": savant.get("k_pct", ""),
            "bb_pct": savant.get("bb_pct", ""),
            "barrel_pct": savant.get("barrel_pct", ""),
            "whiff_pct": savant.get("whiff_pct", ""),
            "hardhit_pct": savant.get("hardhit_pct", ""),
        }

    ap = pitcher_block(away_pp)
    hp = pitcher_block(home_pp)

    # Bullpen/umpire/injuries are display-only (not sim inputs) — skip in FAST mode.
    # The bullpen boxscore fetches are the single biggest per-game cost.
    if FAST:
        away_pen = home_pen = {"ip": "", "b2b": 0}
        umpire = ""
        away_il_count = home_il_count = 0
        away_il_names = home_il_names = ""
        weather = {}  # negligible sim input; skip the per-game Open-Meteo call
    else:
        away_pen = get_bullpen_detail(away_team_id, date_str) if away_team_id else {"ip": "", "b2b": 0}
        home_pen = get_bullpen_detail(home_team_id, date_str) if home_team_id else {"ip": "", "b2b": 0}
        weather = get_weather(venue_name, game_date_iso)
        umpire = get_umpire(game_pk) if game_pk else ""
        away_il_count, away_il_names = get_injured_list_count(away_team_id) if away_team_id else (0, "")
        home_il_count, home_il_names = get_injured_list_count(home_team_id) if home_team_id else (0, "")

    # Team form + splits ARE sim inputs (splits → team OPS vs hand) — always fetch.
    away_form = get_team_form(away_team_id) if away_team_id else {}
    home_form = get_team_form(home_team_id) if home_team_id else {}

    away_splits = get_team_splits_vs_hand(away_team_id, season_year) if away_team_id else {}
    home_splits = get_team_splits_vs_hand(home_team_id, season_year) if home_team_id else {}

    def team_form_str(f: dict) -> str:
        if not f:
            return ""
        parts = []
        if f.get("wins") is not None and f.get("losses") is not None:
            parts.append(f"{f['wins']}-{f['losses']}")
        if f.get("l10"):
            parts.append(f"L10 {f['l10']}")
        if f.get("streak"):
            parts.append(f["streak"])
        if f.get("run_diff") is not None:
            rd = f["run_diff"]
            parts.append(f"RD {rd:+d}" if isinstance(rd, int) else f"RD {rd}")
        return " · ".join(parts)

    def split_str(splits: dict, hand_code: str) -> str:
        """Pick vl or vr based on opposing starter handedness."""
        if not splits or not hand_code:
            return ""
        key = "vl" if hand_code == "L" else "vr"
        s = splits.get(key, {})
        if not s:
            return ""
        return f"AVG {s.get('avg','—')} / OPS {s.get('ops','—')} / {s.get('hr','—')} HR"

    row = {
        "date": date_str,
        "away_team": away_team,
        "home_team": home_team,
        "first_pitch_et": first_pitch,
        "venue": venue_name,
        "park_factor": park_factor,
        "umpire_hp": umpire,

        # Weather
        "weather_temp_f": weather.get("temp_f", "") if not weather.get("dome") else "DOME",
        "weather_wind": (f"{weather.get('wind_mph','')} mph {_wind_dir_str(weather.get('wind_dir'))}"
                        if weather and not weather.get("dome") and weather.get("wind_mph") is not None else ""),
        "weather_precip_pct": weather.get("precip_pct", "") if not weather.get("dome") else "",

        # Away side
        "away_record": team_form_str(away_form),
        "away_vs_opp_hand": split_str(away_splits, hp["hand"]),
        "away_il_count": away_il_count,
        "away_il_names": away_il_names,
        "away_pitcher": ap["name"],
        "away_pitcher_hand": ap["hand"],
        "away_pitcher_era": ap["era"],
        "away_pitcher_xera": ap["xera"],
        "away_pitcher_k_pct": ap["k_pct"],
        "away_pitcher_bb_pct": ap["bb_pct"],
        "away_pitcher_barrel_pct": ap["barrel_pct"],
        "away_pitcher_whiff_pct": ap["whiff_pct"],
        "away_pitcher_hardhit_pct": ap["hardhit_pct"],
        "away_pitcher_last3": ap["last3"],
        "away_bullpen_ip_last3d": away_pen["ip"],
        "away_bullpen_b2b_arms": away_pen["b2b"],

        # Home side
        "home_record": team_form_str(home_form),
        "home_vs_opp_hand": split_str(home_splits, ap["hand"]),
        "home_il_count": home_il_count,
        "home_il_names": home_il_names,
        "home_pitcher": hp["name"],
        "home_pitcher_hand": hp["hand"],
        "home_pitcher_era": hp["era"],
        "home_pitcher_xera": hp["xera"],
        "home_pitcher_k_pct": hp["k_pct"],
        "home_pitcher_bb_pct": hp["bb_pct"],
        "home_pitcher_barrel_pct": hp["barrel_pct"],
        "home_pitcher_whiff_pct": hp["whiff_pct"],
        "home_pitcher_hardhit_pct": hp["hardhit_pct"],
        "home_pitcher_last3": hp["last3"],
        "home_bullpen_ip_last3d": home_pen["ip"],
        "home_bullpen_b2b_arms": home_pen["b2b"],
    }
    return row


FIELDS = [
    "date", "away_team", "home_team", "first_pitch_et", "venue", "park_factor",
    "umpire_hp",
    "weather_temp_f", "weather_wind", "weather_precip_pct",
    "away_record", "away_vs_opp_hand", "away_il_count", "away_il_names",
    "away_pitcher", "away_pitcher_hand", "away_pitcher_era", "away_pitcher_xera",
    "away_pitcher_k_pct", "away_pitcher_bb_pct", "away_pitcher_barrel_pct",
    "away_pitcher_whiff_pct", "away_pitcher_hardhit_pct",
    "away_pitcher_last3", "away_bullpen_ip_last3d", "away_bullpen_b2b_arms",
    "home_record", "home_vs_opp_hand", "home_il_count", "home_il_names",
    "home_pitcher", "home_pitcher_hand", "home_pitcher_era", "home_pitcher_xera",
    "home_pitcher_k_pct", "home_pitcher_bb_pct", "home_pitcher_barrel_pct",
    "home_pitcher_whiff_pct", "home_pitcher_hardhit_pct",
    "home_pitcher_last3", "home_bullpen_ip_last3d", "home_bullpen_b2b_arms",
]


def _era_class(era_str: str) -> str:
    try:
        v = float(era_str)
    except (TypeError, ValueError):
        return "era-na"
    if v < 3.50:
        return "era-good"
    if v < 4.50:
        return "era-mid"
    return "era-bad"


def _pen_class(ip_str: str) -> str:
    """Bullpen fatigue over last 3 days. >12 IP = heavy, 6-12 medium, <6 fresh."""
    thirds = _ip_to_thirds(ip_str)
    if thirds == 0:
        return "pen-na"
    ip = thirds / 3.0
    if ip >= 12:
        return "pen-heavy"
    if ip >= 6:
        return "pen-mid"
    return "pen-fresh"


def _esc(s) -> str:
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _render_last3(cell: str) -> str:
    if not cell:
        return '<div class="last3-empty">no recent starts</div>'
    rows_html = []
    for chunk in cell.split(" | "):
        if ": " not in chunk:
            continue
        date, stats = chunk.split(": ", 1)
        rows_html.append(
            f'<div class="last3-row"><span class="last3-date">{_esc(date[5:])}</span>'
            f'<span class="last3-stats">{_esc(stats)}</span></div>'
        )
    return "".join(rows_html)


def _fmt(v, suffix: str = "") -> str:
    """Format a possibly-empty stat cell."""
    if v is None or v == "" or v == "None":
        return "—"
    return f"{v}{suffix}"


def _render_stat_grid(row: dict, side: str) -> str:
    """4-up stat grid for advanced pitcher metrics."""
    xera = row.get(f"{side}_pitcher_xera", "")
    k = row.get(f"{side}_pitcher_k_pct", "")
    bb = row.get(f"{side}_pitcher_bb_pct", "")
    barrel = row.get(f"{side}_pitcher_barrel_pct", "")
    return (
        '<div class="stat-grid">'
        f'<div class="stat"><div class="stat-label">xERA</div><div class="stat-val {_era_class(xera)}">{_esc(_fmt(xera))}</div></div>'
        f'<div class="stat"><div class="stat-label">K%</div><div class="stat-val">{_esc(_fmt(k))}</div></div>'
        f'<div class="stat"><div class="stat-label">BB%</div><div class="stat-val">{_esc(_fmt(bb))}</div></div>'
        f'<div class="stat"><div class="stat-label">Barrel%</div><div class="stat-val">{_esc(_fmt(barrel))}</div></div>'
        '</div>'
    )


def _render_pitcher_side(label: str, side: str, row: dict) -> str:
    team = row.get(f"{side}_team", "")
    record = row.get(f"{side}_record", "")
    vs_hand = row.get(f"{side}_vs_opp_hand", "")
    il_count = row.get(f"{side}_il_count", 0) or 0
    il_names = row.get(f"{side}_il_names", "")

    name = row.get(f"{side}_pitcher", "")
    hand = row.get(f"{side}_pitcher_hand", "")
    era = row.get(f"{side}_pitcher_era", "")
    last3 = row.get(f"{side}_pitcher_last3", "")
    pen_ip = row.get(f"{side}_bullpen_ip_last3d", "")
    b2b = row.get(f"{side}_bullpen_b2b_arms", 0)
    try:
        il_int = int(il_count)
    except (TypeError, ValueError):
        il_int = 0
    try:
        b2b_int = int(b2b)
    except (TypeError, ValueError):
        b2b_int = 0

    hand_badge = f'<span class="hand-badge">{_esc(hand)}HP</span>' if hand else ""
    il_html = ""
    if il_int > 0:
        il_html = (
            f'<span class="il-chip" title="{_esc(il_names)}">IL {il_int}</span>'
        )

    return (
        '<div class="side">'
        f'<div class="side-label">{label} · {_esc(team)}</div>'
        f'<div class="team-form">{_esc(record) or "—"}{il_html}</div>'
        f'<div class="vs-hand">vs opp SP: <b>{_esc(vs_hand) or "—"}</b></div>'
        '<div class="pitcher-name-row">'
        f'<div class="pitcher-name">{_esc(name) or "TBD"}</div>{hand_badge}'
        '</div>'
        '<div class="era-row">'
        f'<span class="era-pill {_era_class(era)}">{_esc(_fmt(era))}</span>'
        '<span class="era-caption">ERA</span>'
        '</div>'
        f'{_render_stat_grid(row, side)}'
        f'<div class="last3">{_render_last3(last3)}</div>'
        f'<div class="pen {_pen_class(pen_ip)}">'
        '<span class="pen-label">Bullpen 3d</span>'
        f'<span class="pen-ip">{_esc(_fmt(pen_ip))} IP</span>'
        f'<span class="pen-b2b" title="Relievers who pitched both of the last 2 days">B2B {b2b_int}</span>'
        '</div>'
        '</div>'
    )


def _render_card(row: dict) -> str:
    pf = row.get("park_factor", "")
    if pf == "" or pf is None:
        pf_pill = '<span class="pf-pill pf-na">PF —</span>'
    else:
        try:
            pfn = int(pf)
            pf_cls = "pf-hitter" if pfn >= 103 else ("pf-pitcher" if pfn <= 97 else "pf-neutral")
            pf_pill = f'<span class="pf-pill {pf_cls}">PF {pfn}</span>'
        except (TypeError, ValueError):
            pf_pill = f'<span class="pf-pill pf-neutral">PF {_esc(pf)}</span>'

    # Weather strip
    temp = row.get("weather_temp_f", "")
    wind = row.get("weather_wind", "")
    precip = row.get("weather_precip_pct", "")
    ump = row.get("umpire_hp", "")
    if temp == "DOME":
        weather_html = '<span class="wx-dome">🏟 Indoors</span>'
    elif temp not in ("", None):
        try:
            precip_int = int(precip) if precip not in ("", None) else 0
        except (TypeError, ValueError):
            precip_int = 0
        precip_cls = "rain-high" if precip_int >= 40 else ("rain-mid" if precip_int >= 20 else "rain-low")
        weather_html = (
            f'<span class="wx-temp">{_esc(temp)}°F</span>'
            f'<span class="wx-wind">💨 {_esc(wind) or "—"}</span>'
            f'<span class="wx-precip {precip_cls}">☔ {_esc(precip) or "0"}%</span>'
        )
    else:
        weather_html = '<span class="wx-dim">weather —</span>'

    ump_html = f'<span class="ump" title="Home plate umpire">👤 {_esc(ump)}</span>' if ump else ""

    away = _render_pitcher_side("AWAY", "away", row)
    home = _render_pitcher_side("HOME", "home", row)

    return (
        '<article class="card">'
        '<header class="card-head">'
        '<div class="matchup">'
        f'<span class="team away">{_esc(row["away_team"])}</span>'
        '<span class="at">@</span>'
        f'<span class="team home">{_esc(row["home_team"])}</span>'
        '</div>'
        '<div class="meta">'
        f'<span class="time">{_esc(row["first_pitch_et"])}</span>'
        '<span class="dot">·</span>'
        f'<span class="venue">{_esc(row["venue"])}</span>'
        f'{pf_pill}'
        '</div>'
        f'<div class="context-strip">{weather_html}{ump_html}</div>'
        '</header>'
        '<div class="matchup-grid">'
        f'{away}'
        '<div class="vs">vs</div>'
        f'{home}'
        '</div>'
        '</article>'
    )


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Tonight · __DATE_PRETTY__</title>
<style>
  :root {
    --bg: #F7F5F0;
    --card: #FFFFFF;
    --ink: #1A1D23;
    --muted: #6B7280;
    --rule: #E4E1DA;
    --accent: #B8342E;
    --good: #16A34A;
    --mid: #D97706;
    --bad: #B8342E;
    --chip-bg: #EEEAE1;
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0E1116;
      --card: #171B22;
      --ink: #E7EAEE;
      --muted: #8B94A3;
      --rule: #262C36;
      --accent: #E85A50;
      --good: #4ADE80;
      --mid: #FBBF24;
      --bad: #F87171;
      --chip-bg: #1F252E;
      color-scheme: dark;
    }
  }
  :root[data-theme="dark"] {
    --bg: #0E1116;
    --card: #171B22;
    --ink: #E7EAEE;
    --muted: #8B94A3;
    --rule: #262C36;
    --accent: #E85A50;
    --good: #4ADE80;
    --mid: #FBBF24;
    --bad: #F87171;
    --chip-bg: #1F252E;
    color-scheme: dark;
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    font-size: 15px;
    line-height: 1.45;
    -webkit-font-smoothing: antialiased;
  }

  .wrap { max-width: 1240px; margin: 0 auto; padding: 32px 24px 64px; }

  header.top {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 24px;
    padding-bottom: 20px;
    margin-bottom: 28px;
    border-bottom: 1px solid var(--rule);
  }
  .brand { display: flex; align-items: baseline; gap: 12px; }
  .brand-mark { font-weight: 800; letter-spacing: -0.02em; font-size: 22px; color: var(--accent); }
  .brand-sub { color: var(--muted); font-size: 13px; text-transform: uppercase; letter-spacing: 0.12em; }
  .top-right {
    display: flex; gap: 20px; align-items: baseline;
    color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums;
  }
  .top-right strong { color: var(--ink); font-weight: 600; margin-right: 4px; }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 520px), 1fr));
    gap: 20px;
  }

  .card {
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 10px;
    padding: 20px 22px;
    display: flex; flex-direction: column; gap: 18px;
  }

  .card-head {
    display: flex; flex-direction: column; gap: 8px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--rule);
  }
  .matchup {
    display: flex; align-items: baseline; gap: 12px;
    font-weight: 800; letter-spacing: -0.02em; font-size: 20px;
    text-wrap: balance;
  }
  .matchup .at { color: var(--muted); font-weight: 500; font-size: 15px; }
  .meta {
    display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
    color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums;
  }
  .meta .time { color: var(--ink); font-weight: 600; }
  .meta .dot { opacity: 0.5; }
  .meta .venue { flex: 1; min-width: 0; }

  .pf-pill {
    display: inline-flex; align-items: center;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 11px; font-weight: 600; letter-spacing: 0.04em;
    padding: 3px 8px; border-radius: 999px;
    background: var(--chip-bg); color: var(--ink);
    border: 1px solid var(--rule);
  }
  .pf-hitter { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 30%, var(--rule)); }
  .pf-pitcher { color: var(--good); border-color: color-mix(in srgb, var(--good) 30%, var(--rule)); }
  .pf-neutral { color: var(--muted); }
  .pf-na { color: var(--muted); opacity: 0.6; }

  .matchup-grid {
    display: grid; grid-template-columns: 1fr auto 1fr; gap: 18px; align-items: stretch;
  }
  .vs {
    align-self: center; color: var(--muted);
    font-size: 12px; letter-spacing: 0.2em; text-transform: uppercase;
    writing-mode: vertical-rl; transform: rotate(180deg); padding: 8px 0;
  }
  .side { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .side-label {
    font-size: 10px; letter-spacing: 0.18em; color: var(--muted);
    text-transform: uppercase; font-weight: 600;
  }
  .pitcher-name {
    font-size: 17px; font-weight: 700; letter-spacing: -0.01em; text-wrap: balance;
  }

  .era-row { display: flex; align-items: baseline; gap: 8px; }
  .era-pill {
    display: inline-flex; align-items: center;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 15px; font-weight: 700;
    padding: 4px 10px; border-radius: 6px;
    background: var(--chip-bg);
    font-variant-numeric: tabular-nums;
  }
  .era-caption {
    font-size: 10px; letter-spacing: 0.16em; color: var(--muted); text-transform: uppercase;
  }
  .era-good { background: color-mix(in srgb, var(--good) 18%, var(--card)); color: var(--good); }
  .era-mid  { background: color-mix(in srgb, var(--mid) 20%, var(--card)); color: var(--mid); }
  .era-bad  { background: color-mix(in srgb, var(--bad) 18%, var(--card)); color: var(--bad); }
  .era-na   { color: var(--muted); }

  .last3 {
    display: flex; flex-direction: column; gap: 2px;
    padding: 8px 10px;
    background: var(--bg);
    border-radius: 6px;
    border: 1px solid var(--rule);
  }
  .last3-row {
    display: flex; justify-content: space-between; gap: 10px;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 11.5px; font-variant-numeric: tabular-nums;
  }
  .last3-date { color: var(--muted); }
  .last3-stats { color: var(--ink); }
  .last3-empty { font-size: 11.5px; color: var(--muted); font-style: italic; }

  .pen {
    margin-top: auto;
    display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
    padding: 6px 10px; border-radius: 6px;
    background: var(--chip-bg); font-size: 11.5px;
  }
  .pen-label {
    color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase;
    font-size: 10px; font-weight: 600;
  }
  .pen-ip {
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-weight: 700; font-variant-numeric: tabular-nums;
  }
  .pen-fresh .pen-ip { color: var(--good); }
  .pen-mid   .pen-ip { color: var(--mid); }
  .pen-heavy .pen-ip { color: var(--bad); }
  .pen-na    .pen-ip { color: var(--muted); }
  .pen-b2b {
    margin-left: auto;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 10.5px; font-weight: 600;
    color: var(--muted);
    padding: 2px 6px; border-radius: 4px;
    background: var(--bg);
    border: 1px solid var(--rule);
  }

  /* Weather + umpire strip below the header */
  .context-strip {
    display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
    padding-top: 8px;
    font-size: 12px; color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .wx-temp { font-weight: 700; color: var(--ink); font-family: "SF Mono", Menlo, ui-monospace, monospace; }
  .wx-wind, .wx-precip { display: inline-flex; align-items: center; gap: 4px; }
  .wx-dome { color: var(--muted); font-style: italic; }
  .wx-dim  { color: var(--muted); opacity: 0.5; }
  .rain-low  { color: var(--muted); }
  .rain-mid  { color: var(--mid); }
  .rain-high { color: var(--bad); font-weight: 600; }
  .ump { margin-left: auto; }

  /* Team form line */
  .team-form {
    display: flex; align-items: center; gap: 8px;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 11.5px; font-variant-numeric: tabular-nums;
    color: var(--muted);
  }
  .il-chip {
    font-size: 10px; font-weight: 700; letter-spacing: 0.04em;
    padding: 2px 6px; border-radius: 4px;
    background: color-mix(in srgb, var(--bad) 16%, var(--card));
    color: var(--bad);
    border: 1px solid color-mix(in srgb, var(--bad) 30%, var(--rule));
    cursor: help;
  }
  .vs-hand {
    font-size: 11.5px; color: var(--muted);
  }
  .vs-hand b {
    color: var(--ink); font-weight: 600;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-variant-numeric: tabular-nums;
  }

  /* Pitcher name row with handedness badge */
  .pitcher-name-row { display: flex; align-items: baseline; gap: 8px; margin-top: 4px; }
  .hand-badge {
    font-size: 9.5px; font-weight: 700; letter-spacing: 0.06em;
    padding: 2px 5px; border-radius: 3px;
    background: var(--chip-bg); color: var(--muted);
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
  }

  /* 4-up advanced stat grid */
  .stat-grid {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px;
  }
  .stat {
    display: flex; flex-direction: column; align-items: center;
    padding: 6px 4px; border-radius: 5px;
    background: var(--bg); border: 1px solid var(--rule);
  }
  .stat-label {
    font-size: 9.5px; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--muted); font-weight: 600;
  }
  .stat-val {
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 12.5px; font-weight: 700; margin-top: 2px;
    font-variant-numeric: tabular-nums;
  }
  .stat-val.era-good { color: var(--good); }
  .stat-val.era-mid  { color: var(--mid); }
  .stat-val.era-bad  { color: var(--bad); }
  .stat-val.era-na   { color: var(--muted); font-weight: 500; }

  footer {
    margin-top: 40px; color: var(--muted); font-size: 12px; text-align: center;
  }
  footer code {
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    background: var(--chip-bg); padding: 2px 6px; border-radius: 4px;
  }

  @media (max-width: 560px) {
    .wrap { padding: 20px 14px 40px; }
    .matchup-grid { grid-template-columns: 1fr; }
    .vs { writing-mode: horizontal-tb; transform: none; text-align: center; }
    header.top { flex-direction: column; align-items: flex-start; gap: 8px; }
  }
</style>
</head>
<body>
  <div class="wrap">
    <header class="top">
      <div class="brand">
        <span class="brand-mark">MLB TONIGHT</span>
        <span class="brand-sub">__DATE_PRETTY__</span>
      </div>
      <div class="top-right">
        <span><strong>__COUNT__</strong> games</span>
        <span>generated __GENERATED_AT__</span>
      </div>
    </header>
    <main class="grid">
__CARDS__
    </main>
    <footer>
      Re-run <code>python mlb_tonight_edates.py</code> to refresh. Data: MLB Stats API. Park factors: Statcast 3-yr.
    </footer>
  </div>
</body>
</html>
"""


def render_html(rows: list[dict], date_str: str) -> str:
    date_dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_pretty = date_dt.strftime("%A, %B %-d, %Y")
    generated_at = datetime.now(ET).strftime("%-I:%M %p ET")
    cards = "\n".join(_render_card(r) for r in rows) or (
        '<p style="color:var(--muted)">No games scheduled tonight.</p>'
    )
    return (HTML_TEMPLATE
            .replace("__DATE_PRETTY__", date_pretty)
            .replace("__COUNT__", str(len(rows)))
            .replace("__GENERATED_AT__", generated_at)
            .replace("__CARDS__", cards))


def main() -> int:
    global FAST
    FAST = "--fast" in sys.argv
    # Accept an optional YYYY-MM-DD arg; default to tonight in ET.
    date_str = tonight_date_et()
    for a in sys.argv[1:]:
        if len(a) == 10 and a[4] == "-" and a[7] == "-":
            date_str = a
            break

    # Fast path: `python mlb_tonight_edates.py --html-only` re-renders the HTML
    # from the existing CSV without hitting the API again.
    if "--html-only" in sys.argv:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        with open(HTML_FILE, "w", encoding="utf-8") as f:
            f.write(render_html(rows, rows[0]["date"] if rows else date_str))
        print(f"Rendered {len(rows)} games to {HTML_FILE}")
        return 0

    season_year = int(date_str[:4])
    games = get_tonight_schedule(date_str)

    # Prime bulk caches once so per-game work is just lookups.
    print("  Loading Statcast pitcher leaderboard (Baseball Savant)...", file=sys.stderr)
    load_savant_pitcher_leaderboard(season_year)
    print(f"    -> {len(_SAVANT_CACHE)} pitchers cached", file=sys.stderr)
    print("  Loading standings (L10, streak, run diff)...", file=sys.stderr)
    load_standings(season_year)
    print(f"    -> {len(_STANDINGS_CACHE)} teams cached", file=sys.stderr)

    rows = []
    for i, game in enumerate(games, 1):
        print(f"  [{i}/{len(games)}] {game.get('teams', {}).get('away', {}).get('team', {}).get('name', '?')} @ "
              f"{game.get('teams', {}).get('home', {}).get('team', {}).get('name', '?')}", file=sys.stderr)
        rows.append(build_row(game, date_str, season_year))

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(render_html(rows, date_str))

    print(f"Wrote {len(rows)} games to {OUTPUT_FILE} and {HTML_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
