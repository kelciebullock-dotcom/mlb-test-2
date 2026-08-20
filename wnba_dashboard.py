"""Render the WNBA multi-day dashboard.

Reads data/wnba_predictions_*.json and writes wnba_dashboard.html — a self-
contained page with left-rail calendar navigation, legend, and per-game
cards (collapsible model+picks panel, collapsible projected boxscore).

Usage:
    python wnba_dashboard.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CWD = Path(__file__).parent
DATA_DIR = CWD / "data"
OUTPUT = CWD / "wnba_dashboard.html"


# ---- Data loading -----------------------------------------------------------

def load_predictions() -> dict[str, dict]:
    out = {}
    if not DATA_DIR.exists():
        return out
    for p in sorted(DATA_DIR.glob("wnba_predictions_*.json")):
        try:
            with open(p) as f:
                d = json.load(f)
            out[d.get("date", p.stem.replace("wnba_predictions_", ""))] = d
        except Exception as e:
            print(f"  skip {p}: {e}", file=sys.stderr)
    return out


def render_performance() -> str:
    """Sidebar panel leading with the model's WINNER and TOTAL (over/under)
    prediction accuracy + ROI (from data/wnba_performance.json)."""
    p = DATA_DIR / "wnba_performance.json"
    if not p.exists():
        return ""
    try:
        perf = json.load(open(p))
    except Exception:
        return ""
    if not perf.get("graded_games"):
        return ""
    winner = perf.get("winner") or {}
    totals = perf.get("totals") or {}

    def acc_color(pct):
        if pct is None: return "perf-na"
        if pct >= 55: return "perf-good"
        if pct >= 50: return "perf-mid"
        return "perf-bad"

    def hero(label, blk):
        acc = blk.get("accuracy_pct")
        txt = f"{acc:.0f}%" if isinstance(acc, (int, float)) else "—"
        roi = blk.get("roi_pct")
        roi_txt = f"{roi:+.1f}%" if isinstance(roi, (int, float)) else "—"
        rec = blk.get("record") or "—"
        return ('<div class="perf-hero">'
                f'<div class="perf-hero-val {acc_color(acc)}">{txt}</div>'
                f'<div class="perf-hero-lab">{label} correct<br>{rec} · ROI {roi_txt}</div>'
                '</div>')

    n = max(winner.get("n", 0), totals.get("n", 0))
    clv = perf.get("clv_recommended") or {}
    beat = clv.get("beat_close_pct")
    clv_txt = f"{beat:.0f}%" if isinstance(beat, (int, float)) else "—"
    note = ('<div class="perf-note">Small sample — needs ~40+ games to trust. '
            'Building daily.</div>') if n < 40 else (
            '<div class="perf-note">Winner &gt;53% and Totals &gt;53% = the model '
            'is genuinely predictive.</div>')
    return (
        '<div class="perf-panel">'
        '<div class="perf-title">MODEL PERFORMANCE</div>'
        f'{hero("Winner", winner)}'
        f'{hero("Total O/U", totals)}'
        f'<div class="perf-row"><span>Games graded</span><b>{n}</b></div>'
        f'<div class="perf-row"><span>Beat close (CLV)</span><b>{clv_txt}</b></div>'
        f'{note}'
        '</div>'
    )


# ---- Helpers ----------------------------------------------------------------

def _fmt(v, suffix="") -> str:
    if v is None or v == "" or v == "None":
        return "—"
    return f"{v}{suffix}"


def _ev_class(ev) -> str:
    try:
        v = float(ev)
    except (TypeError, ValueError):
        return "ev-na"
    if v >= 8: return "ev-strong"
    if v >= 3: return "ev-mid"
    if v >= 0: return "ev-thin"
    return "ev-neg"


# ---- Card rendering ---------------------------------------------------------

def render_picks_panel(game: dict) -> str:
    picks = game.get("picks", [])
    model = game.get("model", {})
    if not picks and not model:
        return ""

    away_wp = model.get("away_win_pct", 0)
    home_wp = model.get("home_win_pct", 0)
    ma = model.get("mean_away_pts", 0)
    mh = model.get("mean_home_pts", 0)
    mt = model.get("mean_total", 0)

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
            '<div class="pick-line1">'
            f'<span class="pick-market">{escape(p["market"])}</span>'
            f'<span class="pick-side">{escape(p["side"])}</span>'
            f'<span class="pick-ev {_ev_class(ev)}">{ev:+.1f}%</span>'
            '</div>'
            '<div class="pick-line2">'
            f'<span class="pick-book">{book}</span>'
            f'<span class="pick-odds-tag">{odds_str}</span>'
            f'<span class="pick-prob">model <b>{mp:.0f}%</b></span>'
            f'<span class="pick-prob">mkt <b>{mkt_p:.0f}%</b></span>'
            '</div>'
            '</div>'
        )
    if not rows:
        rows.append('<div class="pick-empty">no positive-EV picks on main markets</div>')

    return (
        '<details class="picks-wrap" open>'
        '<summary class="picks-header">'
        '<span class="picks-title">MODEL &amp; PICKS</span>'
        f'<span class="model-line">Away win <b>{away_wp*100:.0f}%</b> · '
        f'Home win <b>{home_wp*100:.0f}%</b> · '
        f'Score <b>{ma:.1f} – {mh:.1f}</b> · '
        f'Total <b>{mt:.1f}</b></span>'
        f'<span class="picks-count">{len(show)} pick{"s" if len(show) != 1 else ""}</span>'
        '</summary>'
        '<div class="pick-list">'
        f'{"".join(rows)}'
        '</div>'
        '</details>'
    )


def render_boxscore(game: dict) -> str:
    box = game.get("projected_box") or {}
    if not box:
        return ""

    def row_html(r: dict) -> str:
        return (
            '<tr>'
            f'<td class="pos">{escape(r.get("pos") or "")}</td>'
            f'<td class="nm">{escape(r.get("name") or "")}</td>'
            f'<td>{r.get("min","—")}</td>'
            f'<td>{r.get("pts","—")}</td>'
            f'<td>{r.get("reb","—")}</td>'
            f'<td>{r.get("ast","—")}</td>'
            f'<td>{r.get("stl","—")}</td>'
            f'<td>{r.get("blk","—")}</td>'
            f'<td>{r.get("threes","—")}</td>'
            f'<td>{r.get("fg","—")}</td>'
            f'<td>{r.get("ft","—")}</td>'
            '</tr>'
        )

    def team_block(side: str, label: str) -> str:
        s = box.get(side) or {}
        players = s.get("players") or []
        totals = s.get("totals") or {}
        rows = "".join(row_html(r) for r in players)
        tot = (
            '<tr class="totals">'
            f'<td colspan="2">Totals</td>'
            f'<td>{totals.get("min", 0):.1f}</td>'
            f'<td>{totals.get("pts", 0):.1f}</td>'
            f'<td>{totals.get("reb", 0):.1f}</td>'
            f'<td>{totals.get("ast", 0):.1f}</td>'
            f'<td>{totals.get("stl", 0):.1f}</td>'
            f'<td>{totals.get("blk", 0):.1f}</td>'
            f'<td>{totals.get("threes", 0):.1f}</td>'
            '<td>—</td><td>—</td>'
            '</tr>'
        )
        return (
            f'<div class="box-team">'
            f'<div class="box-team-label">{escape(label)}</div>'
            '<table class="boxscore"><thead><tr>'
            '<th>Pos</th><th>Player</th><th>MIN</th>'
            '<th>PTS</th><th>REB</th><th>AST</th><th>STL</th><th>BLK</th><th>3PM</th>'
            '<th>FG</th><th>FT</th>'
            '</tr></thead><tbody>'
            f'{rows}{tot}'
            '</tbody></table>'
            '</div>'
        )

    return (
        '<details class="box-wrap"><summary>Projected Boxscore</summary>'
        '<div class="box-teams">'
        f'{team_block("away", game.get("away_team", "AWAY"))}'
        f'{team_block("home", game.get("home_team", "HOME"))}'
        '</div>'
        '<div class="box-note">Projected values, mean of 10,000 sims. Per-player MIN from season averages; scoring scaled by opponent defensive rating.</div>'
        '</details>'
    )


def render_injuries(game: dict) -> str:
    """Injury report per side — OUT players (removed from the projection, their
    minutes/usage redistributed to teammates) and game-time decisions (still
    projected). Nothing renders when both teams are at full strength."""
    inj = game.get("injuries") or {}
    away = inj.get("away") or []
    home = inj.get("home") or []
    if not away and not home:
        return ""

    def side_block(label: str, players: list) -> str:
        if not players:
            return ""
        chips = []
        for p in players:
            out = p.get("cat") == "out"
            tag = "OUT" if out else (escape(p.get("status") or "GTD"))
            det = escape(p.get("detail") or "")
            det_html = f' <span class="inj-det">{det}</span>' if det else ""
            chips.append(
                f'<span class="inj-chip {"inj-out" if out else "inj-gtd"}">'
                f'<span class="inj-tag">{tag}</span> {escape(p.get("name") or "")}{det_html}</span>'
            )
        return (f'<div class="inj-side"><span class="inj-team">{escape(label)}</span>'
                f'{"".join(chips)}</div>')

    return (
        '<details class="inj-wrap"><summary>🚑 Injury Report'
        f'<span class="inj-count">{sum(1 for p in away+home if p.get("cat")=="out")} out</span></summary>'
        f'{side_block(game.get("away_abbr") or game.get("away_team",""), away)}'
        f'{side_block(game.get("home_abbr") or game.get("home_team",""), home)}'
        '<div class="inj-note">OUT players are removed from the projection and their '
        'minutes/usage redistributed to teammates. Game-time decisions are still '
        'projected. Source: ESPN, refreshed hourly.</div>'
        '</details>'
    )


def render_card(game: dict) -> str:
    away = escape(game.get("away_team", ""))
    home = escape(game.get("home_team", ""))
    tip = escape(game.get("tipoff_et", "") or "TBD")
    status = escape(game.get("status", ""))
    status_html = f'<span class="game-status">{status}</span>' if status else ""

    picks_html = render_picks_panel(game)
    box_html = render_boxscore(game)
    inj_html = render_injuries(game)

    return (
        '<article class="card">'
        '<header class="card-head">'
        '<div class="matchup">'
        f'<span class="team away">{away}</span>'
        '<span class="at">@</span>'
        f'<span class="team home">{home}</span>'
        '</div>'
        '<div class="meta">'
        f'<span class="time">{tip}</span>'
        f'{status_html}'
        '</div>'
        '</header>'
        f'{inj_html}'
        f'{picks_html}'
        f'{box_html}'
        '</article>'
    )


# ---- Calendar ---------------------------------------------------------------

def calendar_html(available: set[str], selected: str) -> str:
    today = datetime.now(ET).date()
    start = today - timedelta(days=30)
    end = today + timedelta(days=7)
    cur = start
    while cur.weekday() != 6:
        cur -= timedelta(days=1)
    weeks = []
    week = []
    while cur <= end + timedelta(days=6):
        cls = ["cal-day"]
        d_str = cur.strftime("%Y-%m-%d")
        if cur < start or cur > end:
            cls.append("cal-out")
        if d_str in available:
            cls.append("cal-has")
        if d_str == selected:
            cls.append("cal-sel")
        if cur == today:
            cls.append("cal-today")
        week.append(f'<button class="{" ".join(cls)}" data-date="{d_str}">{cur.day}</button>')
        if cur.weekday() == 5:
            weeks.append('<div class="cal-week">' + "".join(week) + '</div>')
            week = []
        cur += timedelta(days=1)
    if week:
        weeks.append('<div class="cal-week">' + "".join(week) + '</div>')
    header = '<div class="cal-week cal-header">' + "".join(
        f'<span>{d}</span>' for d in ["S", "M", "T", "W", "T", "F", "S"]
    ) + '</div>'
    return '<div class="calendar">' + header + "".join(weeks) + '</div>'


# ---- Sorting + dedup --------------------------------------------------------

def _sort_key(g: dict):
    tip = g.get("tipoff_et", "")
    if not tip:
        return (1, "")
    try:
        t = datetime.strptime(tip.replace(" ET", "").strip(), "%I:%M %p")
        return (0, t.hour * 60 + t.minute)
    except ValueError:
        return (1, tip)


def prep_games(pred: dict) -> None:
    games = sorted(pred.get("games", []), key=_sort_key)
    seen: dict[tuple[str, str], dict] = {}
    for g in games:
        k = (g.get("away_team", ""), g.get("home_team", ""))
        if k not in seen or len(g.get("picks") or []) > len(seen[k].get("picks") or []):
            seen[k] = g
    pred["games"] = sorted(seen.values(), key=_sort_key)


LEGEND_HTML = """
<div class="legend-panel">
  <div class="legend-title">LEGEND</div>
  <div class="legend-grp"><b>Pick EV</b>
    <span class="chip ev-strong">≥+8% strong</span>
    <span class="chip ev-mid">+3 to +8%</span>
    <span class="chip ev-thin">0 to +3%</span>
    <span class="chip ev-neg">&lt;0 (fade)</span>
  </div>
  <div class="legend-grp"><b>Prop labels</b>
    <span class="chip">Pts / Reb / Ast / 3PM / Stl / Blk</span>
    <span class="chip">PRA · P+R · P+A · R+A</span>
  </div>
  <div class="legend-grp"><b>Model</b>
    <span class="legend-note">10,000 sims per game. Team scoring: pace × efficiency, home court +2. Player props: per-player minutes-adjusted normal draws scaled by opponent defense.</span>
  </div>
  <div class="legend-grp"><b>Filters applied</b>
    <span class="legend-note">Main lines only (no alts). Requires ≥2 books on the same line. Vig cap 8% per market. Odds shown as posted price.</span>
  </div>
  <div class="legend-grp"><b>Sources</b>
    <span class="legend-note">ESPN (games &amp; odds) · stats.wnba.com (player stats) · ESPN team PPG. All free, no API key. Player-prop odds unavailable free, so props are not offered.</span>
  </div>
</div>
"""


def build_dashboard() -> str:
    preds = load_predictions()
    for pred in preds.values():
        prep_games(pred)

    available = set(preds.keys())
    today = datetime.now(ET).strftime("%Y-%m-%d")
    selected = today if today in available else (max(available) if available else today)

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
    initial = bundle.get(selected, {"cards_html": "<p class=\"empty\">No predictions yet — run wnba_predict.py.</p>",
                                    "n_games": 0, "date": selected, "generated_at": ""})

    pretty = datetime.strptime(selected, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
    return HTML_SHELL.format(
        title=f"WNBA · {selected}",
        selected_date=selected,
        selected_pretty=pretty,
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
    --bg: #F5F3F0; --card: #FFFFFF; --ink: #1A1D23; --muted: #6B7280;
    --rule: #E4E1DA; --accent: #E36722; --good: #16A34A; --mid: #D97706;
    --bad: #B8342E; --chip-bg: #F0EBE3;
    --ev-strong-bg: #DEF5E5; --ev-strong-fg: #14532D;
    --ev-mid-bg: #EAF3D2; --ev-mid-fg: #365314;
    --ev-thin-bg: #F0EEE8; --ev-thin-fg: #4B5563;
    --ev-neg-bg: #F5E7E6; --ev-neg-fg: #7C1F1F;
    color-scheme: light;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #0E1116; --card: #171B22; --ink: #E7EAEE; --muted: #8B94A3;
      --rule: #262C36; --accent: #F5893A; --good: #4ADE80; --mid: #FBBF24;
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
    --rule: #262C36; --accent: #F5893A; --good: #4ADE80; --mid: #FBBF24;
    --bad: #F87171; --chip-bg: #1F252E;
    --ev-strong-bg: #1F3A29; --ev-strong-fg: #86EFAC;
    --ev-mid-bg: #2E3A22; --ev-mid-fg: #BEF264;
    --ev-thin-bg: #1F252E; --ev-thin-fg: #94A3B8;
    --ev-neg-bg: #3A1F1F; --ev-neg-fg: #FCA5A5;
    color-scheme: dark;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    font-size: 14.5px; line-height: 1.45; -webkit-font-smoothing: antialiased; }}

  .app {{ display: grid; grid-template-columns: 300px 1fr; min-height: 100vh; }}
  aside {{ border-right: 1px solid var(--rule); padding: 24px 20px; background: var(--card);
    display: flex; flex-direction: column; gap: 22px;
    position: sticky; top: 0; align-self: start; max-height: 100vh; overflow-y: auto; }}
  main {{ padding: 32px 32px 64px; max-width: 1200px; }}

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
  .cal-header span {{ font-size: 10px; text-align: center; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; padding: 4px 0; }}
  .cal-day {{ all: unset; cursor: pointer; display: flex; align-items: center; justify-content: center;
    aspect-ratio: 1; border-radius: 6px; font-size: 12px; font-variant-numeric: tabular-nums;
    color: var(--ink); background: transparent; border: 1px solid transparent; }}
  .cal-day:hover {{ background: var(--chip-bg); }}
  .cal-out {{ color: var(--muted); opacity: 0.35; }}
  .cal-has {{ background: color-mix(in srgb, var(--accent) 12%, var(--card)); color: var(--accent); font-weight: 600; }}
  .cal-today {{ border-color: var(--accent); }}
  .cal-sel {{ background: var(--accent) !important; color: white !important; font-weight: 700; }}

  /* Model performance panel */
  .perf-panel {{ display: flex; flex-direction: column; gap: 6px; padding: 12px 14px; border-radius: 8px; background: var(--card); border: 1px solid var(--rule); }}
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
  .legend-title {{ font-size: 10px; letter-spacing: 0.16em; color: var(--muted); text-transform: uppercase; font-weight: 700; }}
  .legend-grp {{ display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }}
  .legend-grp b {{ display: block; width: 100%; font-size: 10.5px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 2px; }}
  .legend-note {{ color: var(--muted); font-size: 10.5px; font-style: italic; }}
  .chip {{ display: inline-flex; align-items: center; padding: 2px 7px; border-radius: 999px; font-size: 10px;
    font-family: "SF Mono", Menlo, ui-monospace, monospace; font-weight: 600; letter-spacing: 0.02em;
    background: var(--chip-bg); color: var(--ink); border: 1px solid var(--rule); }}
  .chip.ev-strong {{ background: var(--ev-strong-bg); color: var(--ev-strong-fg); border: none; }}
  .chip.ev-mid    {{ background: var(--ev-mid-bg);    color: var(--ev-mid-fg); border: none; }}
  .chip.ev-thin   {{ background: var(--ev-thin-bg);   color: var(--ev-thin-fg); border: none; }}
  .chip.ev-neg    {{ background: var(--ev-neg-bg);    color: var(--ev-neg-fg); border: none; }}

  .top-info {{ display: flex; justify-content: space-between; align-items: baseline; gap: 20px;
    padding-bottom: 20px; margin-bottom: 24px; border-bottom: 1px solid var(--rule); }}
  .top-info h1 {{ margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -0.02em; }}
  .top-info .meta-right {{ color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }}
  .top-info .meta-right b {{ color: var(--ink); font-weight: 600; margin-right: 2px; }}

  .grid {{ display: grid; grid-template-columns: 1fr; gap: 20px; }}
  @media (min-width: 1000px) {{ .grid {{ grid-template-columns: repeat(2, 1fr); }} }}

  .card {{ background: var(--card); border: 1px solid var(--rule); border-radius: 10px;
    padding: 18px 20px; display: flex; flex-direction: column; gap: 18px; }}
  .card-head {{ display: flex; flex-direction: column; gap: 8px; padding-bottom: 12px; border-bottom: 1px solid var(--rule); }}
  .matchup {{ display: flex; align-items: baseline; gap: 12px; font-weight: 800; letter-spacing: -0.02em; font-size: 19px; text-wrap: balance; }}
  .matchup .at {{ color: var(--muted); font-weight: 500; font-size: 14px; }}
  .meta {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; color: var(--muted); font-size: 12.5px; font-variant-numeric: tabular-nums; }}
  .meta .time {{ color: var(--ink); font-weight: 600; }}
  .game-status {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em; padding: 2px 6px; border-radius: 3px; background: var(--chip-bg); color: var(--muted); }}

  /* Picks (collapsible) */
  .picks-wrap {{ padding: 10px 14px; border-radius: 8px; background: var(--bg); border: 1px solid var(--rule); }}
  .picks-header {{ display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
    cursor: pointer; user-select: none; list-style: none; padding-bottom: 8px; }}
  .picks-header::-webkit-details-marker {{ display: none; }}
  .picks-header::before {{ content: "▾ "; color: var(--muted); font-size: 11px; }}
  .picks-wrap:not([open]) .picks-header::before {{ content: "▸ "; }}
  .picks-wrap[open] .picks-header {{ border-bottom: 1px solid var(--rule); margin-bottom: 8px; }}
  .picks-title {{ font-size: 10px; font-weight: 700; letter-spacing: 0.16em; color: var(--accent); }}
  .model-line {{ font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }}
  .model-line b {{ color: var(--ink); font-weight: 600; }}
  .picks-count {{ margin-left: auto; font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); font-weight: 700; }}
  .pick-list {{ display: flex; flex-direction: column; gap: 6px; }}
  .pick {{ padding: 8px 10px; border-radius: 6px; background: var(--card); border: 1px solid var(--rule);
    display: flex; flex-direction: column; gap: 4px; }}
  .pick-line1 {{ display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }}
  .pick-market {{ color: var(--ink); font-weight: 700; font-size: 12.5px; letter-spacing: -0.005em; flex-shrink: 0; }}
  .pick-side {{ color: var(--muted); font-family: "SF Mono", Menlo, ui-monospace, monospace; font-size: 11.5px; flex: 1; min-width: 0; }}
  .pick-line2 {{ display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; font-size: 10.5px;
    font-family: "SF Mono", Menlo, ui-monospace, monospace; font-variant-numeric: tabular-nums; color: var(--muted); }}
  .pick-book {{ background: var(--chip-bg); color: var(--ink); font-weight: 600; padding: 1px 6px; border-radius: 3px; font-size: 10px; letter-spacing: 0.02em; }}
  .pick-odds-tag {{ color: var(--ink); font-weight: 700; }}
  .pick-prob {{ color: var(--muted); }}
  .pick-prob b {{ color: var(--ink); font-weight: 600; }}
  .pick-ev {{ margin-left: auto; font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-weight: 700; padding: 2px 8px; border-radius: 4px; font-size: 12px; flex-shrink: 0; }}
  .ev-strong {{ background: var(--ev-strong-bg); color: var(--ev-strong-fg); }}
  .ev-mid    {{ background: var(--ev-mid-bg);    color: var(--ev-mid-fg); }}
  .ev-thin   {{ background: var(--ev-thin-bg);   color: var(--ev-thin-fg); }}
  .ev-neg    {{ background: var(--ev-neg-bg);    color: var(--ev-neg-fg); }}
  .pick-empty {{ font-size: 12px; color: var(--muted); font-style: italic; padding: 6px 0; }}
  .empty {{ color: var(--muted); font-style: italic; }}

  /* Projected boxscore */
  .inj-wrap {{ margin: 10px 0; border: 1px solid var(--rule); border-radius: 10px; background: var(--bg); overflow: hidden; }}
  .inj-wrap > summary {{ cursor: pointer; padding: 9px 13px; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--bad); list-style: none; display: flex; align-items: center; gap: 8px; }}
  .inj-wrap > summary::-webkit-details-marker {{ display: none; }}
  .inj-count {{ font-weight: 600; letter-spacing: 0; text-transform: none; color: var(--muted); font-size: 10.5px; }}
  .inj-side {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 6px 13px; border-top: 1px solid var(--rule); }}
  .inj-team {{ font-size: 10px; font-weight: 800; letter-spacing: 0.08em; color: var(--muted); min-width: 34px; }}
  .inj-chip {{ font-size: 11px; padding: 2px 7px; border-radius: 999px; background: var(--chip-bg); color: var(--ink); }}
  .inj-chip.inj-out {{ background: color-mix(in srgb, var(--bad) 15%, var(--card)); }}
  .inj-tag {{ font-size: 8.5px; font-weight: 800; letter-spacing: 0.04em; padding: 1px 4px; border-radius: 4px; vertical-align: middle; }}
  .inj-out .inj-tag {{ background: var(--bad); color: #fff; }}
  .inj-gtd .inj-tag {{ background: var(--mid); color: #fff; }}
  .inj-det {{ color: var(--muted); font-size: 10px; }}
  .inj-note {{ padding: 8px 13px 11px; font-size: 10px; color: var(--muted); font-style: italic; line-height: 1.5; border-top: 1px solid var(--rule); }}

  .box-wrap {{ border-top: 1px solid var(--rule); padding-top: 12px; }}
  .box-wrap summary {{ cursor: pointer; font-size: 10.5px; letter-spacing: 0.14em;
    text-transform: uppercase; font-weight: 700; color: var(--accent); padding: 4px 0;
    list-style: none; user-select: none; }}
  .box-wrap summary::-webkit-details-marker {{ display: none; }}
  .box-wrap summary::before {{ content: "▸ "; display: inline-block; color: var(--muted); }}
  .box-wrap[open] summary::before {{ content: "▾ "; }}
  .box-teams {{ display: flex; flex-direction: column; gap: 16px; margin-top: 10px; }}
  .box-team-label {{ font-size: 10px; letter-spacing: 0.18em; color: var(--muted);
    text-transform: uppercase; font-weight: 700; margin-bottom: 4px; }}
  table.boxscore {{ width: 100%; border-collapse: collapse;
    font-family: "SF Mono", Menlo, ui-monospace, monospace;
    font-size: 10.5px; font-variant-numeric: tabular-nums; }}
  table.boxscore th {{ text-align: right; color: var(--muted); font-weight: 600; padding: 4px 4px;
    border-bottom: 1px solid var(--rule); font-size: 9.5px; letter-spacing: 0.06em; text-transform: uppercase; }}
  table.boxscore th:nth-child(1), table.boxscore th:nth-child(2) {{ text-align: left; }}
  table.boxscore td {{ text-align: right; padding: 3px 4px; border-bottom: 1px solid var(--rule); color: var(--ink); }}
  table.boxscore td.pos {{ color: var(--muted); width: 32px; text-align: left; }}
  table.boxscore td.nm {{ text-align: left; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; font-weight: 600; font-size: 11px; }}
  table.boxscore tr.totals td {{ font-weight: 700; border-top: 1px solid var(--rule); border-bottom: none; padding-top: 6px; }}
  table.boxscore tr.totals td:first-child {{ text-align: left; color: var(--muted); text-transform: uppercase; font-size: 9.5px; letter-spacing: 0.08em; }}
  .box-note {{ margin-top: 10px; font-size: 10.5px; color: var(--muted); font-style: italic; line-height: 1.5; }}

  @media (max-width: 900px) {{
    .app {{ grid-template-columns: 1fr; }}
    aside {{ position: static; max-height: none; }}
    main {{ padding: 24px 16px 48px; }}
  }}
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="brand">
      <span class="brand-mark">WNBA TONIGHT</span>
      <span class="brand-sub">daily model &amp; picks</span>
    </div>
    <nav class="sport-tabs">
      <a class="sport-tab" href="index.html">⚾ MLB</a>
      <a class="sport-tab active" href="wnba.html">🏀 WNBA</a>
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
    if (!BUNDLE[d]) {{
      cardsRoot.innerHTML = '<p class="empty">No predictions for this date. Run: <code>python wnba_predict.py ' + d + '</code></p>';
      gamesCount.textContent = '0';
      genAt.textContent = '—';
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
    OUTPUT.write_text(build_dashboard(), encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
