"""Render the full multi-day MLB dashboard.

Reads all prediction files (data/predictions_*.json) and the per-day
scraper CSV (mlb_tonight_edates.csv for today, or archived copies), then
emits a single self-contained mlb_dashboard.html with:
  - Left rail calendar (30 days back + 3 days forward) — click a date to switch
  - Legend explaining every color/badge
  - Full game cards with all stats + model picks panel ranked by EV%

Usage:
    python mlb_dashboard.py
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CWD = Path(__file__).parent
DATA_DIR = CWD / "data"
OUTPUT = CWD / "mlb_dashboard.html"


# ---- Data loading -----------------------------------------------------------

def load_predictions() -> dict[str, dict]:
    """Return {date_str: prediction_dict} for every JSON in data/."""
    out = {}
    if not DATA_DIR.exists():
        return out
    for p in sorted(DATA_DIR.glob("predictions_*.json")):
        try:
            with open(p) as f:
                d = json.load(f)
            out[d.get("date", p.stem.replace("predictions_", ""))] = d
        except Exception as e:
            print(f"  skip {p}: {e}", file=sys.stderr)
    return out


def render_performance() -> str:
    """Sidebar panel showing the model's realized record + closing-line value.
    Reads data/performance.json (written by mlb_backtest.py). CLV is the headline
    metric — whether the market moves toward our picks — so it leads."""
    p = DATA_DIR / "performance.json"
    if not p.exists():
        return ""
    try:
        with open(p) as f:
            perf = json.load(f)
    except Exception:
        return ""

    rec = perf.get("recommended", {}) or {}
    clv = perf.get("clv_recommended", {}) or {}
    games = perf.get("graded_games", 0)
    if not games:
        return ""

    beat = clv.get("beat_close_pct")
    avg_clv = clv.get("avg_clv")
    n_clv = clv.get("n", 0)
    # CLV verdict color: >52% beat-close is the edge threshold
    if beat is None:
        clv_cls, clv_txt = "perf-na", "—"
    elif beat >= 55:
        clv_cls, clv_txt = "perf-good", f"{beat:.0f}%"
    elif beat >= 50:
        clv_cls, clv_txt = "perf-mid", f"{beat:.0f}%"
    else:
        clv_cls, clv_txt = "perf-bad", f"{beat:.0f}%"

    wl = f'{rec.get("wins","–")}-{rec.get("losses","–")}'
    roi = rec.get("roi_pct")
    roi_txt = f'{roi:+.1f}%' if isinstance(roi, (int, float)) else "—"
    avg_clv_txt = f'{avg_clv:+.2f}' if isinstance(avg_clv, (int, float)) else "—"

    small = n_clv < 40
    note = ('<div class="perf-note">Small sample — CLV needs ~50+ graded picks '
            'to trust. Building daily.</div>') if small else (
            '<div class="perf-note">CLV &gt; 52% = real edge vs the market.</div>')

    return (
        '<div class="perf-panel">'
        '<div class="perf-title">MODEL PERFORMANCE</div>'
        '<div class="perf-hero">'
        f'<div class="perf-hero-val {clv_cls}">{clv_txt}</div>'
        '<div class="perf-hero-lab">beat the close<br>(recommended picks)</div>'
        '</div>'
        '<div class="perf-row"><span>Avg CLV</span><b>{}</b></div>'.format(avg_clv_txt) +
        f'<div class="perf-row"><span>Record</span><b>{wl}</b></div>'
        f'<div class="perf-row"><span>ROI (flat)</span><b>{roi_txt}</b></div>'
        f'<div class="perf-row"><span>Graded picks</span><b>{n_clv}</b></div>'
        f'{note}'
        '</div>'
    )


def load_scraper_csv() -> list[dict]:
    p = CWD / "mlb_tonight_edates.csv"
    if not p.exists():
        return []
    with open(p) as f:
        return list(csv.DictReader(f))


def _slug(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def merge_scraper_into_predictions(pred: dict, scraper_rows: list[dict]) -> dict:
    """Attach each game's display context and sort by first pitch time.

    Context comes from the context embedded in each game's own prediction JSON
    (written by mlb_predict.py) so every date is self-contained and can never
    show another day's pitchers/weather. Falls back to matching a passed-in
    scraper CSV only for older JSONs that predate the embedded `context`."""
    idx = {frozenset({_slug(r["away_team"]), _slug(r["home_team"])}): r
           for r in scraper_rows}
    for g in pred.get("games", []):
        ctx = g.get("context")
        if ctx:
            g["_scraper"] = ctx
        else:
            key = frozenset({_slug(g["away_team"]), _slug(g["home_team"])})
            g["_scraper"] = idx.get(key, {})

    def _sort_key(g: dict):
        fpe = (g.get("_scraper") or {}).get("first_pitch_et", "")
        if not fpe:
            return (1, "")  # games without a time sort to the end
        try:
            # e.g. "7:10 PM ET" -> parse the time portion
            t = datetime.strptime(fpe.replace(" ET", "").strip(), "%I:%M %p")
            return (0, t.hour * 60 + t.minute)
        except ValueError:
            return (1, fpe)

    sorted_games = sorted(pred.get("games", []), key=_sort_key)

    # Dedupe by (away_team, home_team) — BDL returns both games of a doubleheader
    # under separate IDs; user wants one card per matchup. Prefer the entry with
    # the more complete game-line market data: rank by whether it can yield a
    # confident (>=50%) game-line recommendation, then by the count of two-sided
    # game-line picks, then by total pick count. (Raw total-pick count alone is
    # dominated by player props and can favor an entry with junk one-sided odds.)
    def _dedup_score(g: dict) -> tuple:
        gl = [p for p in (g.get("picks") or [])
              if p.get("market") in ("Moneyline", "Total", "Runline")]
        has_confident = any((p.get("ev_pct") or -100) >= 0 and (p.get("model_prob") or 0) >= 0.50
                            for p in gl)
        return (1 if has_confident else 0, len(gl), len(g.get("picks") or []))

    seen: dict[tuple[str, str], dict] = {}
    for g in sorted_games:
        key = (g.get("away_team", ""), g.get("home_team", ""))
        existing = seen.get(key)
        if existing is None or _dedup_score(g) > _dedup_score(existing):
            seen[key] = g
    pred["games"] = sorted(seen.values(), key=_sort_key)
    return pred


# ---- Card rendering (small helpers, minimal templates) ----------------------

def _fmt(v, suffix="") -> str:
    if v is None or v == "" or v == "None":
        return "—"
    return f"{v}{suffix}"


def _era_class(era) -> str:
    try:
        v = float(era)
    except (TypeError, ValueError):
        return "era-na"
    if v < 3.50: return "era-good"
    if v < 4.50: return "era-mid"
    return "era-bad"


def _pen_class(ip) -> str:
    try:
        whole, _, frac = str(ip).partition(".")
        v = int(whole or 0) + int(frac or 0) / 3.0
    except Exception:
        return "pen-na"
    if v >= 12: return "pen-heavy"
    if v >= 6:  return "pen-mid"
    if v > 0:   return "pen-fresh"
    return "pen-na"


def _ev_class(ev) -> str:
    try:
        v = float(ev)
    except (TypeError, ValueError):
        return "ev-na"
    if v >= 8: return "ev-strong"
    if v >= 3: return "ev-mid"
    if v >= 0: return "ev-thin"
    return "ev-neg"


def _pf_pill(pf) -> str:
    if pf in ("", None):
        return '<span class="pf-pill pf-na">PF —</span>'
    try:
        n = int(float(pf))
        cls = "pf-hitter" if n >= 103 else ("pf-pitcher" if n <= 97 else "pf-neutral")
        return f'<span class="pf-pill {cls}">PF {n}</span>'
    except Exception:
        return f'<span class="pf-pill pf-neutral">PF {escape(str(pf))}</span>'


def render_last3(cell: str) -> str:
    if not cell:
        return '<div class="last3-empty">no recent starts</div>'
    rows = []
    for chunk in cell.split(" | "):
        if ": " not in chunk:
            continue
        d, s = chunk.split(": ", 1)
        rows.append(
            f'<div class="last3-row"><span class="last3-date">{escape(d[5:])}</span>'
            f'<span class="last3-stats">{escape(s)}</span></div>'
        )
    return "".join(rows)


def render_stat_grid(scraper_row: dict, side: str) -> str:
    xera = scraper_row.get(f"{side}_pitcher_xera", "")
    k = scraper_row.get(f"{side}_pitcher_k_pct", "")
    bb = scraper_row.get(f"{side}_pitcher_bb_pct", "")
    barrel = scraper_row.get(f"{side}_pitcher_barrel_pct", "")
    return (
        '<div class="stat-grid">'
        f'<div class="stat"><div class="stat-label">xERA</div><div class="stat-val {_era_class(xera)}">{escape(_fmt(xera))}</div></div>'
        f'<div class="stat"><div class="stat-label">K%</div><div class="stat-val">{escape(_fmt(k))}</div></div>'
        f'<div class="stat"><div class="stat-label">BB%</div><div class="stat-val">{escape(_fmt(bb))}</div></div>'
        f'<div class="stat"><div class="stat-label">Barrel%</div><div class="stat-val">{escape(_fmt(barrel))}</div></div>'
        '</div>'
    )


def render_pitcher_side(label: str, side: str, scraper_row: dict) -> str:
    team = scraper_row.get(f"{side}_team", "")
    record = scraper_row.get(f"{side}_record", "")
    vs_hand = scraper_row.get(f"{side}_vs_opp_hand", "")
    il_count = scraper_row.get(f"{side}_il_count", 0) or 0
    il_names = scraper_row.get(f"{side}_il_names", "")
    name = scraper_row.get(f"{side}_pitcher", "")
    hand = scraper_row.get(f"{side}_pitcher_hand", "")
    era = scraper_row.get(f"{side}_pitcher_era", "")
    last3 = scraper_row.get(f"{side}_pitcher_last3", "")
    pen_ip = scraper_row.get(f"{side}_bullpen_ip_last3d", "")
    b2b = scraper_row.get(f"{side}_bullpen_b2b_arms", 0)
    try:
        il_int = int(il_count)
    except (TypeError, ValueError):
        il_int = 0
    try:
        b2b_int = int(b2b)
    except (TypeError, ValueError):
        b2b_int = 0
    hand_badge = f'<span class="hand-badge">{escape(hand)}HP</span>' if hand else ""
    il_html = (f'<span class="il-chip" title="{escape(il_names)}">IL {il_int}</span>'
               if il_int > 0 else "")

    return (
        '<div class="side">'
        f'<div class="side-label">{label} · {escape(team)}</div>'
        f'<div class="team-form">{escape(record) or "—"}{il_html}</div>'
        f'<div class="vs-hand">vs opp SP: <b>{escape(vs_hand) or "—"}</b></div>'
        '<div class="pitcher-name-row">'
        f'<div class="pitcher-name">{escape(name) or "TBD"}</div>{hand_badge}'
        '</div>'
        '<div class="era-row">'
        f'<span class="era-pill {_era_class(era)}">{escape(_fmt(era))}</span>'
        '<span class="era-caption">ERA</span>'
        '</div>'
        f'{render_stat_grid(scraper_row, side)}'
        f'<div class="last3">{render_last3(last3)}</div>'
        f'<div class="pen {_pen_class(pen_ip)}">'
        '<span class="pen-label">Bullpen 3d</span>'
        f'<span class="pen-ip">{escape(_fmt(pen_ip))} IP</span>'
        f'<span class="pen-b2b" title="Relievers who pitched both of the last 2 days">B2B {b2b_int}</span>'
        '</div>'
        '</div>'
    )


def _confidence_tier(prob: float) -> tuple[str, str]:
    if prob >= 0.70:
        return ("STRONG", "conf-strong")
    if prob >= 0.60:
        return ("HIGH", "conf-high")
    if prob >= 0.55:
        return ("MEDIUM", "conf-mid")
    return ("LEAN", "conf-lean")


def _recommend(game: dict) -> dict:
    """Highest-EV game-line pick (ML / Total / Runline). Falls back to raw
    model winner if no positive-EV game-line pick exists."""
    picks = game.get("picks", []) or []
    game_line_markets = {"Moneyline", "Total", "Runline"}
    game_picks = [p for p in picks if p.get("market") in game_line_markets]

    # Prefer the highest-EV pick the model gives at least a coin-flip chance to
    # hit — a "recommended" pick should not be one the model thinks loses more
    # often than it wins, even when a longshot price makes its EV look large.
    # Only if nothing clears 50% do we fall back to the best-EV pick outright.
    positive = [p for p in game_picks if (p.get("ev_pct") or -100) >= 0]
    confident = [p for p in positive if (p.get("model_prob") or 0) >= 0.50]
    pool = confident if confident else positive

    best = None
    for p in pool:
        if best is None or (p.get("ev_pct") or -100) > (best.get("ev_pct") or -100):
            best = p

    if best:
        prob = best.get("model_prob") or 0
        label, cls = _confidence_tier(prob)
        return {
            "market": best["market"], "side": best["side"],
            "book": best.get("book", "—"), "odds": best.get("market_odds"),
            "confidence": int(round(prob * 100)),
            "confidence_label": label, "confidence_class": cls,
            "ev_pct": best.get("ev_pct"), "source": "ev",
        }

    model = game.get("model") or {}
    away_wp = model.get("away_win_pct") or 0
    home_wp = model.get("home_win_pct") or 0
    if away_wp == 0 and home_wp == 0:
        return {}
    if home_wp >= away_wp:
        side = f"{game.get('home_team', 'HOME')} to win"
        prob = home_wp
    else:
        side = f"{game.get('away_team', 'AWAY')} to win"
        prob = away_wp
    label, cls = _confidence_tier(prob)
    return {
        "market": "Winner", "side": side,
        "book": "—", "odds": None,
        "confidence": int(round(prob * 100)),
        "confidence_label": label, "confidence_class": cls,
        "ev_pct": None, "source": "model",
    }


def render_recommendation(game: dict) -> str:
    rec = _recommend(game)
    if not rec:
        return ""
    odds_str = f' @ {int(rec["odds"]):+d}' if isinstance(rec.get("odds"), (int, float)) else ""
    ev_bit = f' · <span class="rec-ev">{rec["ev_pct"]:+.1f}% EV</span>' if rec.get("ev_pct") is not None else ""
    book_bit = f' · <span class="rec-book">{escape(rec["book"])}</span>' if rec.get("book") and rec["book"] != "—" else ""
    note = "" if rec["source"] == "ev" else ' <span class="rec-note">(no market edge — model pick)</span>'
    return (
        '<div class="rec">'
        '<div class="rec-label">RECOMMENDED PICK</div>'
        '<div class="rec-body">'
        f'<span class="rec-market">{escape(rec["market"])}</span>'
        f'<span class="rec-side">{escape(rec["side"])}{odds_str}</span>'
        f'<span class="rec-conf {rec["confidence_class"]}">{rec["confidence_label"]} · {rec["confidence"]}%</span>'
        '</div>'
        f'<div class="rec-meta">Confidence = model probability the pick hits{book_bit}{ev_bit}{note}</div>'
        '</div>'
    )


def render_picks_panel(game: dict) -> str:
    picks = game.get("picks", [])
    model = game.get("model", {})
    if not picks and not model:
        return ""

    away_wp = model.get("away_win_pct", 0)
    home_wp = model.get("home_win_pct", 0)
    mt = model.get("mean_total", 0)
    ma = model.get("mean_away_runs", 0)
    mh = model.get("mean_home_runs", 0)

    # Rank by EV, keep all positive picks; if none positive, show top 5 anyway
    positive = [p for p in picks if (p.get("ev_pct") or 0) >= 0]
    show = positive if positive else picks[:5]

    rows = []
    for p in show:
        odds = p.get("market_odds")
        odds_str = f"{odds:+.0f}" if isinstance(odds, (int, float)) else "—"
        mp = (p.get("model_prob") or 0) * 100
        mkt_p = (p.get("market_prob") or 0) * 100
        ev = p.get("ev_pct", 0)
        book = escape(p.get("book") or "—")
        rows.append(
            '<div class="pick">'
            f'<div class="pick-line1">'
            f'<span class="pick-market">{escape(p["market"])}</span>'
            f'<span class="pick-side">{escape(p["side"])}</span>'
            f'<span class="pick-ev {_ev_class(ev)}">{ev:+.1f}%</span>'
            '</div>'
            f'<div class="pick-line2">'
            f'<span class="pick-book">{book}</span>'
            f'<span class="pick-odds-tag">{odds_str}</span>'
            f'<span class="pick-prob">model <b>{mp:.0f}%</b></span>'
            f'<span class="pick-prob">mkt <b>{mkt_p:.0f}%</b></span>'
            '</div>'
            '</div>'
        )
    if not rows:
        rows.append('<div class="pick-empty">no positive-EV picks on main markets</div>')

    rec_html = render_recommendation(game)

    return (
        '<details class="picks-wrap" open>'
        '<summary class="picks-header">'
        '<span class="picks-title">MODEL &amp; PICKS</span>'
        f'<span class="model-line">Away win <b>{away_wp*100:.0f}%</b> · '
        f'Home win <b>{home_wp*100:.0f}%</b> · '
        f'Runs <b>{ma:.1f} – {mh:.1f}</b> · '
        f'Total <b>{mt:.1f}</b></span>'
        f'<span class="picks-count">{len(show)} pick{"s" if len(show) != 1 else ""}</span>'
        '</summary>'
        f'{rec_html}'
        '<div class="pick-list">'
        f'{"".join(rows)}'
        '</div>'
        '</details>'
    )


def render_boxscore(game: dict) -> str:
    box = game.get("projected_box") or {}
    if not box:
        return ""

    def bat_row(row: dict) -> str:
        def cell(v, fmt=".1f"):
            if v is None:
                return "—"
            if fmt == ".2f":
                return f"{v:.2f}"
            return f"{v:.1f}"
        return (
            '<tr>'
            f'<td class="ord">{escape(str(row.get("slot") or ""))}</td>'
            f'<td class="pos">{escape(row.get("pos") or "")}</td>'
            f'<td class="nm">{escape(row.get("name") or "")}</td>'
            f'<td>{cell(row.get("ab"))}</td>'
            f'<td>{cell(row.get("r"))}</td>'
            f'<td>{cell(row.get("h"))}</td>'
            f'<td>{cell(row.get("hr"), ".2f")}</td>'
            f'<td>{cell(row.get("rbi"))}</td>'
            f'<td>{cell(row.get("bb"))}</td>'
            f'<td>{cell(row.get("k"))}</td>'
            '</tr>'
        )

    def team_block(side_key: str, label: str) -> str:
        side = box.get(side_key) or {}
        batters = side.get("batters") or []
        totals = side.get("totals") or {}
        starter = side.get("starter") or {}
        rows = "".join(bat_row(b) for b in batters)
        # Totals row (a bit different — no order/pos/name columns)
        tot = (
            '<tr class="totals">'
            '<td colspan="3">Totals</td>'
            f'<td>{totals.get("ab", 0):.1f}</td>'
            f'<td>{totals.get("r", 0):.1f}</td>'
            f'<td>{totals.get("h", 0):.1f}</td>'
            f'<td>{totals.get("hr", 0):.2f}</td>'
            f'<td>{totals.get("rbi", 0):.1f}</td>'
            f'<td>{totals.get("bb", 0):.1f}</td>'
            f'<td>{totals.get("k", 0):.1f}</td>'
            '</tr>'
        )
        starter_html = (
            '<div class="pitching-line">'
            f'<span class="pit-label">SP</span>'
            f'<span class="pit-name">{escape(starter.get("name") or "TBD")}</span>'
            f'<span class="pit-stat">IP <b>{starter.get("ip","—")}</b></span>'
            f'<span class="pit-stat">H <b>{starter.get("h","—")}</b></span>'
            f'<span class="pit-stat">R <b>{starter.get("r","—")}</b></span>'
            f'<span class="pit-stat">ER <b>{starter.get("er","—")}</b></span>'
            f'<span class="pit-stat">BB <b>{starter.get("bb","—")}</b></span>'
            f'<span class="pit-stat">K <b>{starter.get("k","—")}</b></span>'
            f'<span class="pit-stat">HR <b>{starter.get("hr","—")}</b></span>'
            '</div>'
        )
        return (
            f'<div class="box-team">'
            f'<div class="box-team-label">{escape(label)}</div>'
            '<table class="boxscore"><thead><tr>'
            '<th>#</th><th>Pos</th><th>Batter</th>'
            '<th>AB</th><th>R</th><th>H</th><th>HR</th><th>RBI</th><th>BB</th><th>K</th>'
            '</tr></thead><tbody>'
            f'{rows}{tot}'
            '</tbody></table>'
            f'{starter_html}'
            '</div>'
        )

    away_label = game.get("away_team", "AWAY")
    home_label = game.get("home_team", "HOME")

    return (
        '<details class="box-wrap"><summary>Projected Boxscore</summary>'
        '<div class="box-teams">'
        f'{team_block("away", away_label)}'
        f'{team_block("home", home_label)}'
        '</div>'
        '<div class="box-note">Projected values, mean of 10,000 sims. R and RBI proportional to OBP/SLG contribution. Starter line: expected ≈ 24 BF (6 IP).</div>'
        '</details>'
    )


def render_card(game: dict) -> str:
    scraper_row = game.get("_scraper", {})

    away_team = game.get("away_team", "")
    home_team = game.get("home_team", "")
    venue = game.get("venue") or scraper_row.get("venue", "")
    first_pitch = scraper_row.get("first_pitch_et", "")
    park_factor = game.get("park_factor") or scraper_row.get("park_factor", "")
    pf_pill = _pf_pill(park_factor)

    # Weather
    weather = game.get("weather") or {}
    if weather:
        temp = weather.get("temp_f")
        wind_mph = weather.get("wind_mph")
        wind_dir = weather.get("wind_dir", "")
        precip = weather.get("precip_pct")
        precip_int = int(precip) if isinstance(precip, (int, float)) else 0
        precip_cls = "rain-high" if precip_int >= 40 else ("rain-mid" if precip_int >= 20 else "rain-low")
        wx_html = ""
        if temp is not None:
            wx_html += f'<span class="wx-temp">{temp}°F</span>'
        if wind_mph is not None:
            wx_html += f'<span class="wx-wind">💨 {wind_mph} mph {escape(str(wind_dir))}</span>'
        wx_html += f'<span class="wx-precip {precip_cls}">☔ {precip_int}%</span>'
    else:
        # Fallback: use scraper row weather
        t = scraper_row.get("weather_temp_f", "")
        if t == "DOME":
            wx_html = '<span class="wx-dome">🏟 Indoors</span>'
        elif t:
            wind = scraper_row.get("weather_wind", "")
            prec = scraper_row.get("weather_precip_pct", "0")
            try:
                pi = int(prec)
            except (TypeError, ValueError):
                pi = 0
            pcls = "rain-high" if pi >= 40 else ("rain-mid" if pi >= 20 else "rain-low")
            wx_html = (f'<span class="wx-temp">{escape(t)}°F</span>'
                       f'<span class="wx-wind">💨 {escape(wind)}</span>'
                       f'<span class="wx-precip {pcls}">☔ {pi}%</span>')
        else:
            wx_html = '<span class="wx-dim">weather —</span>'

    umpire = scraper_row.get("umpire_hp", "")
    ump_html = f'<span class="ump" title="Home plate umpire">👤 {escape(umpire)}</span>' if umpire else ""

    away = render_pitcher_side("AWAY", "away", scraper_row) if scraper_row else ""
    home = render_pitcher_side("HOME", "home", scraper_row) if scraper_row else ""
    picks_html = render_picks_panel(game)
    boxscore_html = render_boxscore(game)

    matchup_html = (
        '<div class="matchup-grid">'
        f'{away}<div class="vs">vs</div>{home}'
        '</div>'
    ) if scraper_row else ""

    return (
        '<article class="card">'
        '<header class="card-head">'
        '<div class="matchup">'
        f'<span class="team away">{escape(away_team)}</span>'
        '<span class="at">@</span>'
        f'<span class="team home">{escape(home_team)}</span>'
        '</div>'
        '<div class="meta">'
        f'<span class="time">{escape(first_pitch) or "TBD"}</span>'
        '<span class="dot">·</span>'
        f'<span class="venue">{escape(venue) or "—"}</span>'
        f'{pf_pill}'
        '</div>'
        f'<div class="context-strip">{wx_html}{ump_html}</div>'
        '</header>'
        f'{picks_html}'
        f'{matchup_html}'
        f'{boxscore_html}'
        '</article>'
    )


# ---- Calendar / navigation --------------------------------------------------

def calendar_html(available_dates: set[str], selected: str) -> str:
    """30 days back + 3 forward, week-grouped."""
    today = datetime.now(ET).date()
    start = today - timedelta(days=30)
    end = today + timedelta(days=3)
    cur = start
    # Align to Sunday
    while cur.weekday() != 6:
        cur -= timedelta(days=1)
    weeks = []
    week = []
    while cur <= end + timedelta(days=6):
        cls = ["cal-day"]
        d_str = cur.strftime("%Y-%m-%d")
        if cur < start or cur > end:
            cls.append("cal-out")
        if d_str in available_dates:
            cls.append("cal-has")
        if d_str == selected:
            cls.append("cal-sel")
        if cur == today:
            cls.append("cal-today")
        label = cur.strftime("%-d")
        week.append(f'<button class="{" ".join(cls)}" data-date="{d_str}">{label}</button>')
        if cur.weekday() == 5:  # Saturday ends a week row
            weeks.append('<div class="cal-week">' + "".join(week) + '</div>')
            week = []
        cur += timedelta(days=1)
    if week:
        weeks.append('<div class="cal-week">' + "".join(week) + '</div>')
    header = '<div class="cal-week cal-header">' + "".join(
        f'<span>{d}</span>' for d in ["S", "M", "T", "W", "T", "F", "S"]
    ) + '</div>'
    return '<div class="calendar">' + header + "".join(weeks) + '</div>'


# ---- HTML shell -------------------------------------------------------------

LEGEND_HTML = """
<div class="legend-panel">
  <div class="legend-title">LEGEND</div>
  <div class="legend-grp"><b>ERA / xERA</b>
    <span class="chip era-good">&lt;3.50</span>
    <span class="chip era-mid">3.50–4.50</span>
    <span class="chip era-bad">&gt;4.50</span>
  </div>
  <div class="legend-grp"><b>Park Factor</b>
    <span class="chip pf-pitcher">≤97</span>
    <span class="chip pf-neutral">98–102</span>
    <span class="chip pf-hitter">≥103</span>
    <span class="legend-note">runs env vs league</span>
  </div>
  <div class="legend-grp"><b>Bullpen 3d IP</b>
    <span class="chip pen-fresh">&lt;6 fresh</span>
    <span class="chip pen-mid">6–12 mid</span>
    <span class="chip pen-heavy">&gt;12 heavy</span>
    <span class="chip pen-b2b-legend">B2B = relievers who threw last 2 days</span>
  </div>
  <div class="legend-grp"><b>Rain %</b>
    <span class="chip rain-low">&lt;20 low</span>
    <span class="chip rain-mid">20–39 watch</span>
    <span class="chip rain-high">≥40 high</span>
  </div>
  <div class="legend-grp"><b>Pick EV</b>
    <span class="chip ev-strong">≥+8% strong</span>
    <span class="chip ev-mid">+3 to +8%</span>
    <span class="chip ev-thin">0 to +3%</span>
    <span class="chip ev-neg">&lt;0 (fade)</span>
  </div>
  <div class="legend-grp"><b>Rec confidence</b>
    <span class="chip conf-strong">STRONG ≥70%</span>
    <span class="chip conf-high">HIGH 60–70%</span>
    <span class="chip conf-mid">MEDIUM 55–60%</span>
    <span class="chip conf-lean">LEAN &lt;55%</span>
    <span class="legend-note">= model probability the recommended pick hits</span>
  </div>
  <div class="legend-grp"><b>IL chip</b>
    <span class="chip il-chip">IL n</span>
    <span class="legend-note">hover for names</span>
  </div>
  <div class="legend-grp"><b>Sources</b>
    <span class="legend-note">MLB StatsAPI · Baseball Savant · Open-Meteo · ESPN (odds). All free, no API key. Player-prop odds unavailable free, so props are not offered.</span>
  </div>
</div>
"""


def build_dashboard() -> str:
    preds = load_predictions()
    # Each game carries its own embedded context now, so every date renders with
    # its correct pitchers/weather/times. The transient CSV is only a fallback
    # for old JSONs written before `context` was embedded.
    scraper_rows = load_scraper_csv()
    for pred in preds.values():
        merge_scraper_into_predictions(pred, scraper_rows)

    available = set(preds.keys())
    today = datetime.now(ET).strftime("%Y-%m-%d")
    selected = today if today in available else (
        max(available) if available else today
    )

    # Serialize a lightweight bundle for client-side date switching
    bundle = {}
    for d, p in preds.items():
        bundle[d] = {
            "date": d,
            "generated_at": p.get("generated_at"),
            "n_sims": p.get("n_sims"),
            "cards_html": "\n".join(render_card(g) for g in p.get("games", [])) or
                          '<p class="empty">No games this date.</p>',
            "n_games": len(p.get("games", [])),
        }

    cal = calendar_html(available, selected)
    initial = bundle.get(selected, {"cards_html": "<p class=\"empty\">No predictions yet — run mlb_predict.py.</p>", "n_games": 0, "date": selected, "generated_at": ""})

    return HTML_SHELL.format(
        title=f"MLB · {selected}",
        selected_date=selected,
        selected_pretty=datetime.strptime(selected, "%Y-%m-%d").strftime("%A, %B %-d, %Y"),
        n_games=initial["n_games"],
        generated=initial["generated_at"] or "—",
        calendar=cal,
        performance=render_performance(),
        legend=LEGEND_HTML,
        cards=initial["cards_html"],
        bundle_json=json.dumps(bundle),
    )


HTML_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #F7F5F0; --card: #FFFFFF; --ink: #1A1D23; --muted: #6B7280;
    --rule: #E4E1DA; --accent: #B8342E; --good: #16A34A; --mid: #D97706;
    --bad: #B8342E; --chip-bg: #EEEAE1;
    --ev-strong-bg: #DEF5E5; --ev-strong-fg: #14532D;
    --ev-mid-bg: #EAF3D2; --ev-mid-fg: #365314;
    --ev-thin-bg: #F0EEE8; --ev-thin-fg: #4B5563;
    --ev-neg-bg: #F5E7E6; --ev-neg-fg: #7C1F1F;
    color-scheme: light;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #0E1116; --card: #171B22; --ink: #E7EAEE; --muted: #8B94A3;
      --rule: #262C36; --accent: #E85A50; --good: #4ADE80; --mid: #FBBF24;
      --bad: #F87171; --chip-bg: #1F252E;
      --ev-strong-bg: #1F3A29; --ev-strong-fg: #86EFAC;
      --ev-mid-bg: #2E3A22; --ev-mid-fg: #BEF264;
      --ev-thin-bg: #1F252E; --ev-thin-fg: #94A3B8;
      --ev-neg-bg: #3A1F1F; --ev-neg-fg: #FCA5A5;
      color-scheme: dark;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #0E1116; --card: #171B22; --ink: #E7EAEE; --muted: #8B94A3;
    --rule: #262C36; --accent: #E85A50; --good: #4ADE80; --mid: #FBBF24;
    --bad: #F87171; --chip-bg: #1F252E;
    --ev-strong-bg: #1F3A29; --ev-strong-fg: #86EFAC;
    --ev-mid-bg: #2E3A22; --ev-mid-fg: #BEF264;
    --ev-thin-bg: #1F252E; --ev-thin-fg: #94A3B8;
    --ev-neg-bg: #3A1F1F; --ev-neg-fg: #FCA5A5;
    color-scheme: dark;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    font-size: 14.5px; line-height: 1.45; -webkit-font-smoothing: antialiased;
  }}

  /* Page shell */
  .app {{ display: grid; grid-template-columns: 300px 1fr; min-height: 100vh; }}
  aside {{
    border-right: 1px solid var(--rule);
    padding: 24px 20px; background: var(--card);
    display: flex; flex-direction: column; gap: 22px;
    position: sticky; top: 0; align-self: start; max-height: 100vh; overflow-y: auto;
  }}
  main {{ padding: 32px 32px 64px; max-width: 1200px; }}

  /* Sidebar */
  .brand {{ display: flex; flex-direction: column; gap: 4px; }}
  .brand-mark {{ font-weight: 800; letter-spacing: -0.02em; font-size: 20px; color: var(--accent); }}
  .brand-sub {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.14em; }}

  /* MLB / WNBA sport toggle */
  .sport-tabs {{ display: flex; gap: 4px; background: var(--chip-bg); padding: 3px; border-radius: 8px; }}
  .sport-tab {{
    flex: 1; text-align: center; text-decoration: none;
    padding: 7px 10px; border-radius: 6px;
    font-size: 12.5px; font-weight: 700; letter-spacing: 0.02em;
    color: var(--muted); transition: background 0.12s, color 0.12s;
  }}
  .sport-tab:hover {{ color: var(--ink); }}
  .sport-tab.active {{ background: var(--card); color: var(--ink); box-shadow: 0 1px 2px rgba(0,0,0,0.08); }}

  .calendar {{ display: flex; flex-direction: column; gap: 2px; }}
  .cal-week {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }}
  .cal-header span {{
    font-size: 10px; text-align: center; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.1em; padding: 4px 0;
  }}
  .cal-day {{
    all: unset; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    aspect-ratio: 1; border-radius: 6px;
    font-size: 12px; font-variant-numeric: tabular-nums;
    color: var(--ink); background: transparent;
    border: 1px solid transparent;
    transition: background 0.1s;
  }}
  .cal-day:hover {{ background: var(--chip-bg); }}
  .cal-out {{ color: var(--muted); opacity: 0.35; }}
  .cal-has {{ background: color-mix(in srgb, var(--accent) 12%, var(--card)); color: var(--accent); font-weight: 600; }}
  .cal-today {{ border-color: var(--accent); }}
  .cal-sel {{ background: var(--accent) !important; color: white !important; font-weight: 700; }}

  /* Legend */
  /* Model performance panel */
  .perf-panel {{
    display: flex; flex-direction: column; gap: 6px;
    padding: 12px 14px; border-radius: 8px;
    background: var(--card); border: 1px solid var(--rule);
  }}
  .perf-title {{ font-size: 10px; letter-spacing: 0.16em; color: var(--muted); text-transform: uppercase; font-weight: 700; }}
  .perf-hero {{ display: flex; align-items: center; gap: 10px; padding: 4px 0 6px; border-bottom: 1px solid var(--rule); margin-bottom: 2px; }}
  .perf-hero-val {{ font-size: 30px; font-weight: 800; letter-spacing: -0.03em; font-variant-numeric: tabular-nums; line-height: 1; }}
  .perf-hero-lab {{ font-size: 10.5px; color: var(--muted); line-height: 1.25; }}
  .perf-good {{ color: var(--good); }}
  .perf-mid  {{ color: var(--mid); }}
  .perf-bad  {{ color: var(--bad); }}
  .perf-na   {{ color: var(--muted); }}
  .perf-row {{ display: flex; justify-content: space-between; font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }}
  .perf-row b {{ color: var(--ink); font-weight: 600; }}
  .perf-note {{ font-size: 10px; color: var(--muted); font-style: italic; line-height: 1.35; margin-top: 4px; }}

  .legend-panel {{ display: flex; flex-direction: column; gap: 10px; font-size: 11.5px; }}
  .legend-title {{
    font-size: 10px; letter-spacing: 0.16em; color: var(--muted);
    text-transform: uppercase; font-weight: 700;
  }}
  .legend-grp {{ display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }}
  .legend-grp b {{ display: block; width: 100%; font-size: 10.5px;
                    color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 2px; }}
  .legend-note {{ color: var(--muted); font-size: 10.5px; font-style: italic; }}
  .chip {{
    display: inline-flex; align-items: center;
    padding: 2px 7px; border-radius: 999px; font-size: 10px;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-weight: 600; letter-spacing: 0.02em;
    background: var(--chip-bg); color: var(--ink); border: 1px solid var(--rule);
  }}
  .chip.era-good {{ color: var(--good); border-color: color-mix(in srgb, var(--good) 30%, var(--rule)); }}
  .chip.era-mid  {{ color: var(--mid);  border-color: color-mix(in srgb, var(--mid) 30%, var(--rule)); }}
  .chip.era-bad  {{ color: var(--bad);  border-color: color-mix(in srgb, var(--bad) 30%, var(--rule)); }}
  .chip.pf-hitter {{ color: var(--bad); }}
  .chip.pf-pitcher {{ color: var(--good); }}
  .chip.pf-neutral {{ color: var(--muted); }}
  .chip.pen-fresh {{ color: var(--good); }}
  .chip.pen-mid   {{ color: var(--mid); }}
  .chip.pen-heavy {{ color: var(--bad); }}
  .chip.pen-b2b-legend {{ color: var(--muted); }}
  .chip.rain-low {{ color: var(--muted); }}
  .chip.rain-mid {{ color: var(--mid); }}
  .chip.rain-high {{ color: var(--bad); }}
  .chip.ev-strong {{ background: var(--ev-strong-bg); color: var(--ev-strong-fg); border: none; }}
  .chip.ev-mid    {{ background: var(--ev-mid-bg);    color: var(--ev-mid-fg); border: none; }}
  .chip.ev-thin   {{ background: var(--ev-thin-bg);   color: var(--ev-thin-fg); border: none; }}
  .chip.ev-neg    {{ background: var(--ev-neg-bg);    color: var(--ev-neg-fg); border: none; }}
  .chip.il-chip   {{ color: var(--bad); background: color-mix(in srgb, var(--bad) 16%, var(--card));
                     border-color: color-mix(in srgb, var(--bad) 30%, var(--rule)); }}

  /* Main content */
  .top-info {{
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 20px; padding-bottom: 20px; margin-bottom: 24px;
    border-bottom: 1px solid var(--rule);
  }}
  .top-info h1 {{
    margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -0.02em;
  }}
  .top-info .meta-right {{
    color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums;
  }}
  .top-info .meta-right b {{ color: var(--ink); font-weight: 600; margin-right: 2px; }}

  .grid {{ display: grid; grid-template-columns: 1fr; gap: 20px; }}
  @media (min-width: 1000px) {{ .grid {{ grid-template-columns: repeat(2, 1fr); }} }}

  .card {{
    background: var(--card); border: 1px solid var(--rule); border-radius: 10px;
    padding: 18px 20px; display: flex; flex-direction: column; gap: 18px;
  }}
  .card-head {{ display: flex; flex-direction: column; gap: 8px; padding-bottom: 12px; border-bottom: 1px solid var(--rule); }}
  .matchup {{ display: flex; align-items: baseline; gap: 12px; font-weight: 800; letter-spacing: -0.02em; font-size: 19px; text-wrap: balance; }}
  .matchup .at {{ color: var(--muted); font-weight: 500; font-size: 14px; }}
  .meta {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; color: var(--muted); font-size: 12.5px; font-variant-numeric: tabular-nums; }}
  .meta .time {{ color: var(--ink); font-weight: 600; }}
  .meta .dot {{ opacity: 0.5; }}
  .meta .venue {{ flex: 1; min-width: 0; }}

  .pf-pill {{
    display: inline-flex; align-items: center; font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 10.5px; font-weight: 600; letter-spacing: 0.04em; padding: 3px 8px; border-radius: 999px;
    background: var(--chip-bg); border: 1px solid var(--rule); color: var(--ink);
  }}
  .pf-hitter {{ color: var(--bad); border-color: color-mix(in srgb, var(--bad) 30%, var(--rule)); }}
  .pf-pitcher {{ color: var(--good); border-color: color-mix(in srgb, var(--good) 30%, var(--rule)); }}
  .pf-neutral {{ color: var(--muted); }}
  .pf-na {{ color: var(--muted); opacity: 0.6; }}

  .context-strip {{ display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding-top: 8px; font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }}
  .wx-temp {{ font-weight: 700; color: var(--ink); font-family: "SF Mono", Menlo, ui-monospace, monospace; }}
  .wx-wind, .wx-precip {{ display: inline-flex; align-items: center; gap: 4px; }}
  .wx-dome {{ color: var(--muted); font-style: italic; }}
  .wx-dim  {{ color: var(--muted); opacity: 0.5; }}
  .rain-low  {{ color: var(--muted); }}
  .rain-mid  {{ color: var(--mid); }}
  .rain-high {{ color: var(--bad); font-weight: 600; }}
  .ump {{ margin-left: auto; }}

  /* Picks panel (collapsible) */
  .picks-wrap {{
    padding: 10px 14px; border-radius: 8px;
    background: var(--bg); border: 1px solid var(--rule);
  }}
  .picks-header {{
    display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
    cursor: pointer; user-select: none; list-style: none;
    padding-bottom: 8px;
  }}
  .picks-header::-webkit-details-marker {{ display: none; }}
  .picks-header::before {{
    content: "▾ "; color: var(--muted); font-size: 11px;
  }}
  .picks-wrap:not([open]) .picks-header::before {{ content: "▸ "; }}
  .picks-wrap[open] .picks-header {{ border-bottom: 1px solid var(--rule); margin-bottom: 8px; }}
  .picks-title {{ font-size: 10px; font-weight: 700; letter-spacing: 0.16em; color: var(--accent); }}
  .model-line {{ font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }}
  .model-line b {{ color: var(--ink); font-weight: 600; }}
  .picks-count {{ margin-left: auto; font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); font-weight: 700; }}

  /* Recommended pick — the headline pick per game */
  .rec {{
    margin: 8px 0 12px;
    padding: 10px 12px;
    border-radius: 6px;
    background: color-mix(in srgb, var(--accent) 6%, var(--card));
    border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--rule));
  }}
  .rec-label {{
    font-size: 9.5px; letter-spacing: 0.18em; color: var(--accent);
    text-transform: uppercase; font-weight: 800; margin-bottom: 6px;
  }}
  .rec-body {{
    display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  }}
  .rec-market {{
    font-size: 10.5px; font-weight: 700; letter-spacing: 0.06em;
    text-transform: uppercase; color: var(--muted);
    padding: 2px 7px; border-radius: 3px; background: var(--chip-bg);
  }}
  .rec-side {{
    font-size: 15px; font-weight: 700; letter-spacing: -0.01em;
    color: var(--ink); flex: 1; min-width: 0;
  }}
  .rec-conf {{
    font-size: 11px; font-weight: 800; letter-spacing: 0.06em;
    padding: 4px 10px; border-radius: 4px;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }}
  .conf-strong {{ background: var(--ev-strong-bg); color: var(--ev-strong-fg); }}
  .conf-high   {{ background: var(--ev-mid-bg);    color: var(--ev-mid-fg); }}
  .conf-mid    {{ background: var(--ev-thin-bg);   color: var(--ev-thin-fg); }}
  .conf-lean   {{ background: var(--chip-bg);      color: var(--muted); }}
  .rec-meta {{
    margin-top: 5px; font-size: 10.5px; color: var(--muted);
    font-variant-numeric: tabular-nums;
  }}
  .rec-book, .rec-ev {{ color: var(--ink); font-weight: 600; }}
  .rec-note {{ color: var(--muted); font-style: italic; }}

  .pick-list {{ display: flex; flex-direction: column; gap: 6px; }}
  .pick {{
    padding: 8px 10px; border-radius: 6px;
    background: var(--card); border: 1px solid var(--rule);
    display: flex; flex-direction: column; gap: 4px;
  }}
  .pick-line1 {{
    display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  }}
  .pick-market {{
    color: var(--ink); font-weight: 700; font-size: 12.5px;
    letter-spacing: -0.005em; flex-shrink: 0;
  }}
  .pick-side {{
    color: var(--muted); font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 11.5px; flex: 1; min-width: 0;
  }}
  .pick-line2 {{
    display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
    font-size: 10.5px; font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-variant-numeric: tabular-nums; color: var(--muted);
  }}
  .pick-book {{
    background: var(--chip-bg); color: var(--ink); font-weight: 600;
    padding: 1px 6px; border-radius: 3px; font-size: 10px;
    letter-spacing: 0.02em;
  }}
  .pick-odds-tag {{ color: var(--ink); font-weight: 700; }}
  .pick-prob {{ color: var(--muted); }}
  .pick-prob b {{ color: var(--ink); font-weight: 600; }}
  .pick-ev {{
    margin-left: auto;
    font-family: "SF Mono", Menlo, ui-monospace, monospace; font-weight: 700;
    padding: 2px 8px; border-radius: 4px; font-size: 12px;
    flex-shrink: 0;
  }}
  .ev-strong {{ background: var(--ev-strong-bg); color: var(--ev-strong-fg); }}
  .ev-mid    {{ background: var(--ev-mid-bg);    color: var(--ev-mid-fg); }}
  .ev-thin   {{ background: var(--ev-thin-bg);   color: var(--ev-thin-fg); }}
  .ev-neg    {{ background: var(--ev-neg-bg);    color: var(--ev-neg-fg); }}
  .pick-empty {{ font-size: 12px; color: var(--muted); font-style: italic; padding: 6px 0; }}

  /* Matchup / pitcher panels (reused from scraper HTML) */
  .matchup-grid {{ display: grid; grid-template-columns: 1fr auto 1fr; gap: 18px; align-items: stretch; }}
  .vs {{ align-self: center; color: var(--muted); font-size: 12px; letter-spacing: 0.2em; text-transform: uppercase; writing-mode: vertical-rl; transform: rotate(180deg); padding: 8px 0; }}
  .side {{ display: flex; flex-direction: column; gap: 10px; min-width: 0; }}
  .side-label {{ font-size: 10px; letter-spacing: 0.18em; color: var(--muted); text-transform: uppercase; font-weight: 600; }}
  .pitcher-name {{ font-size: 16px; font-weight: 700; letter-spacing: -0.01em; text-wrap: balance; }}
  .pitcher-name-row {{ display: flex; align-items: baseline; gap: 8px; margin-top: 4px; }}
  .hand-badge {{ font-size: 9.5px; font-weight: 700; letter-spacing: 0.06em; padding: 2px 5px; border-radius: 3px; background: var(--chip-bg); color: var(--muted); font-family: "SF Mono", Menlo, ui-monospace, monospace; }}
  .team-form {{ display: flex; align-items: center; gap: 8px; font-family: "SF Mono", Menlo, ui-monospace, monospace; font-size: 11px; font-variant-numeric: tabular-nums; color: var(--muted); }}
  .il-chip {{ font-size: 10px; font-weight: 700; letter-spacing: 0.04em; padding: 2px 6px; border-radius: 4px; background: color-mix(in srgb, var(--bad) 16%, var(--card)); color: var(--bad); border: 1px solid color-mix(in srgb, var(--bad) 30%, var(--rule)); cursor: help; }}
  .vs-hand {{ font-size: 11px; color: var(--muted); }}
  .vs-hand b {{ color: var(--ink); font-weight: 600; font-family: "SF Mono", Menlo, ui-monospace, monospace; font-variant-numeric: tabular-nums; }}
  .era-row {{ display: flex; align-items: baseline; gap: 8px; }}
  .era-pill {{ display: inline-flex; align-items: center; font-family: "SF Mono", Menlo, ui-monospace, monospace; font-size: 14px; font-weight: 700; padding: 3px 9px; border-radius: 6px; background: var(--chip-bg); font-variant-numeric: tabular-nums; }}
  .era-caption {{ font-size: 9.5px; letter-spacing: 0.16em; color: var(--muted); text-transform: uppercase; }}
  .era-good {{ background: color-mix(in srgb, var(--good) 18%, var(--card)); color: var(--good); }}
  .era-mid  {{ background: color-mix(in srgb, var(--mid) 20%, var(--card)); color: var(--mid); }}
  .era-bad  {{ background: color-mix(in srgb, var(--bad) 18%, var(--card)); color: var(--bad); }}
  .era-na   {{ color: var(--muted); }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }}
  .stat {{ display: flex; flex-direction: column; align-items: center; padding: 5px 4px; border-radius: 5px; background: var(--bg); border: 1px solid var(--rule); }}
  .stat-label {{ font-size: 9px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }}
  .stat-val {{ font-family: "SF Mono", Menlo, ui-monospace, monospace; font-size: 11.5px; font-weight: 700; margin-top: 2px; font-variant-numeric: tabular-nums; }}
  .stat-val.era-good {{ color: var(--good); }}
  .stat-val.era-mid  {{ color: var(--mid); }}
  .stat-val.era-bad  {{ color: var(--bad); }}
  .last3 {{ display: flex; flex-direction: column; gap: 2px; padding: 6px 10px; background: var(--bg); border-radius: 6px; border: 1px solid var(--rule); }}
  .last3-row {{ display: flex; justify-content: space-between; gap: 10px; font-family: "SF Mono", Menlo, ui-monospace, monospace; font-size: 11px; font-variant-numeric: tabular-nums; }}
  .last3-date {{ color: var(--muted); }}
  .last3-empty {{ font-size: 11px; color: var(--muted); font-style: italic; }}
  .pen {{ margin-top: auto; display: flex; align-items: baseline; justify-content: space-between; gap: 8px; padding: 5px 10px; border-radius: 6px; background: var(--chip-bg); font-size: 11px; }}
  .pen-label {{ color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase; font-size: 9.5px; font-weight: 600; }}
  .pen-ip {{ font-family: "SF Mono", Menlo, ui-monospace, monospace; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .pen-fresh .pen-ip {{ color: var(--good); }}
  .pen-mid   .pen-ip {{ color: var(--mid); }}
  .pen-heavy .pen-ip {{ color: var(--bad); }}
  .pen-na    .pen-ip {{ color: var(--muted); }}
  .pen-b2b {{ margin-left: auto; font-family: "SF Mono", Menlo, ui-monospace, monospace; font-size: 10px; font-weight: 600; color: var(--muted); padding: 2px 6px; border-radius: 4px; background: var(--bg); border: 1px solid var(--rule); }}
  .empty {{ color: var(--muted); font-style: italic; }}

  /* Projected boxscore (collapsible) */
  .box-wrap {{
    border-top: 1px solid var(--rule); padding-top: 12px;
  }}
  .box-wrap summary {{
    cursor: pointer; font-size: 10.5px; letter-spacing: 0.14em;
    text-transform: uppercase; font-weight: 700; color: var(--accent);
    padding: 4px 0; list-style: none;
    user-select: none;
  }}
  .box-wrap summary::-webkit-details-marker {{ display: none; }}
  .box-wrap summary::before {{
    content: "▸ "; display: inline-block; transition: transform 0.15s; color: var(--muted);
  }}
  .box-wrap[open] summary::before {{ content: "▾ "; }}
  .box-teams {{ display: flex; flex-direction: column; gap: 16px; margin-top: 10px; }}
  .box-team-label {{
    font-size: 10px; letter-spacing: 0.18em; color: var(--muted);
    text-transform: uppercase; font-weight: 700; margin-bottom: 4px;
  }}
  table.boxscore {{
    width: 100%; border-collapse: collapse;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 10.5px; font-variant-numeric: tabular-nums;
  }}
  table.boxscore th {{
    text-align: right; color: var(--muted); font-weight: 600;
    padding: 4px 4px; border-bottom: 1px solid var(--rule);
    font-size: 9.5px; letter-spacing: 0.06em; text-transform: uppercase;
  }}
  table.boxscore th:nth-child(1),
  table.boxscore th:nth-child(2),
  table.boxscore th:nth-child(3) {{ text-align: left; }}
  table.boxscore td {{
    text-align: right; padding: 3px 4px;
    border-bottom: 1px solid var(--rule);
    color: var(--ink);
  }}
  table.boxscore td.ord {{ color: var(--muted); width: 20px; }}
  table.boxscore td.pos {{ color: var(--muted); width: 32px; text-align: left; }}
  table.boxscore td.nm {{ text-align: left; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; font-weight: 600; font-size: 11px; }}
  table.boxscore tr.totals td {{
    font-weight: 700; border-top: 1px solid var(--rule); border-bottom: none;
    color: var(--ink); padding-top: 6px;
  }}
  table.boxscore tr.totals td:first-child {{ text-align: left; color: var(--muted); text-transform: uppercase; font-size: 9.5px; letter-spacing: 0.08em; }}

  .pitching-line {{
    margin-top: 8px; padding: 6px 10px; border-radius: 5px;
    background: var(--bg); border: 1px solid var(--rule);
    display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: baseline;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 10.5px; font-variant-numeric: tabular-nums;
    color: var(--muted);
  }}
  .pit-label {{ font-size: 9px; letter-spacing: 0.16em; text-transform: uppercase; font-weight: 700; color: var(--accent); }}
  .pit-name {{
    color: var(--ink); font-weight: 700;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    font-size: 11.5px; letter-spacing: -0.005em;
  }}
  .pit-stat {{ color: var(--muted); }}
  .pit-stat b {{ color: var(--ink); font-weight: 700; }}

  .box-note {{
    margin-top: 10px; font-size: 10.5px; color: var(--muted); font-style: italic;
    line-height: 1.5;
  }}

  @media (max-width: 900px) {{
    .app {{ grid-template-columns: 1fr; }}
    aside {{ position: static; max-height: none; }}
    main {{ padding: 24px 16px 48px; }}
    .matchup-grid {{ grid-template-columns: 1fr; }}
    .vs {{ writing-mode: horizontal-tb; transform: none; text-align: center; }}
  }}
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="brand">
      <span class="brand-mark">MLB TONIGHT</span>
      <span class="brand-sub">daily model &amp; picks</span>
    </div>
    <nav class="sport-tabs">
      <a class="sport-tab active" href="index.html">⚾ MLB</a>
      <a class="sport-tab" href="wnba.html">🏀 WNBA</a>
    </nav>
    {calendar}
    {performance}
    {legend}
  </aside>
  <main>
    <div class="top-info">
      <h1 id="date-title">{selected_pretty}</h1>
      <div class="meta-right"><span><b id="games-count">{n_games}</b> games</span> · <span>generated <span id="gen-at">{generated}</span></span></div>
    </div>
    <div class="grid" id="cards-root">
{cards}
    </div>
  </main>
</div>
<script>
  const BUNDLE = {bundle_json};
  const cal = document.querySelector('.calendar');
  const cardsRoot = document.getElementById('cards-root');
  const dateTitle = document.getElementById('date-title');
  const gamesCount = document.getElementById('games-count');
  const genAt = document.getElementById('gen-at');
  cal.addEventListener('click', (e) => {{
    const btn = e.target.closest('.cal-day');
    if (!btn) return;
    const d = btn.dataset.date;
    if (!BUNDLE[d]) {{ /* no prediction file for this date — but still switch view */
      cardsRoot.innerHTML = '<p class="empty">No predictions for this date. Run: <code>python mlb_predict.py ' + d + '</code></p>';
    }} else {{
      cardsRoot.innerHTML = BUNDLE[d].cards_html;
      gamesCount.textContent = BUNDLE[d].n_games;
      genAt.textContent = BUNDLE[d].generated_at || '—';
    }}
    const pretty = new Date(d + 'T12:00:00').toLocaleDateString('en-US',
      {{weekday: 'long', month: 'long', day: 'numeric', year: 'numeric'}});
    dateTitle.textContent = pretty;
    document.querySelectorAll('.cal-sel').forEach(x => x.classList.remove('cal-sel'));
    btn.classList.add('cal-sel');
  }});
</script>
</body>
</html>
"""


def main() -> int:
    html = build_dashboard()
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
