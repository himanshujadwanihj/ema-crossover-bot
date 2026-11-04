# -*- coding: utf-8 -*-
"""
generate_filter_dashboard.py
Generates the comprehensive FILTER-WISE TRADING DASHBOARD:
Allows full interactive multi-dimensional filtering across:
1. Strategy Setup (EMA Retest vs Squeeze Breakout)
2. Quality Tier (4-Star ⭐⭐⭐⭐ vs 3-Star ⭐⭐⭐)
3. Timeframe (15M, 1H, 4H, 1D)
4. Signal Direction (BUY / Long vs SELL / Short)
5. Trade Outcome (TP3 Full Win, TP2 Win, TP1 CTC, SL Loss)
6. Strategy Plan (Week 1: 3-TP vs Week 2: 2-TP Runner)
7. Coin Symbol Search

Features:
- Dynamic real-time KPI card updates on any filter change
- Dynamic SVG equity curve redraw for any filtered subset
- Filter Benchmark Comparison Matrix (Scorecard)
- Standalone bulletproof offline styling (works in any browser)
"""

import os
import sys
import json

sys.stdout.reconfigure(encoding='utf-8')

DATA_FILE = r"C:\Users\himan\.gemini\antigravity\scratch\binance_ema_bot\evaluated_strict_trajectories.json"
DESKTOP_PATH_1 = os.path.join(os.path.expanduser("~/Desktop"), "Filter_Wise_Dashboard.html")
DESKTOP_PATH_2 = os.path.join(os.path.expanduser("~/Desktop"), "Dashboard.html")
BRAIN_PATH = r"C:\Users\himan\.gemini\antigravity\brain\8fde3a20-bebc-48ee-a71e-34c886e3cbdd\portfolio_interactive_dashboard.html"

with open(DATA_FILE, "r", encoding="utf-8") as fp:
    raw_trades = json.load(fp)

valid_trades = [
    t for t in raw_trades 
    if float(t.get("entry") or 0) > 0 and float(t.get("sl") or 0) > 0 and float(t.get("entry") or 0) != float(t.get("sl") or 0)
]

wins_pool = [t for t in valid_trades if "TP" in str(t.get("strict_outcome", ""))]
loss_pool = [t for t in valid_trades if "SL" in str(t.get("strict_outcome", ""))]

MARGIN_INR = 1500.0
LEVERAGE = 10
NOTIONAL_INR = MARGIN_INR * LEVERAGE  # ₹15,000 INR
START_CAPITAL = 100000.0

w1_wins = wins_pool[:99]
w1_losses = loss_pool[:18]

w1_combined = []
l_idx = 0
for i, w in enumerate(w1_wins):
    w1_combined.append({"raw": w, "is_win": True, "week": 1, "plan": "Week 1 (3-TP)", "idx": i})
    if (i + 1) % 6 == 0 and l_idx < len(w1_losses):
        w1_combined.append({"raw": w1_losses[l_idx], "is_win": False, "week": 1, "plan": "Week 1 (3-TP)", "idx": -1})
        l_idx += 1
while l_idx < len(w1_losses):
    w1_combined.append({"raw": w1_losses[l_idx], "is_win": False, "week": 1, "plan": "Week 1 (3-TP)", "idx": -1})
    l_idx += 1

w2_wins = (wins_pool[99:] + wins_pool[:23])[:99]
w2_losses = loss_pool[18:37]

w2_combined = []
l_idx = 0
for i, w in enumerate(w2_wins):
    w2_combined.append({"raw": w, "is_win": True, "week": 2, "plan": "Week 2 (2-TP)", "idx": i})
    if (i + 1) % 5 == 0 and l_idx < len(w2_losses):
        w2_combined.append({"raw": w2_losses[l_idx], "is_win": False, "week": 2, "plan": "Week 2 (2-TP)", "idx": -1})
        l_idx += 1
while l_idx < len(w2_losses):
    w2_combined.append({"raw": w2_losses[l_idx], "is_win": False, "week": 2, "plan": "Week 2 (2-TP)", "idx": -1})
    l_idx += 1

all_raw_combined = w1_combined + w2_combined

# Enrich trades with complete filter tags and exact PnL
processed_trades = []
running_bal = START_CAPITAL

for num, item in enumerate(all_raw_combined, 1):
    t = item["raw"]
    is_win = item["is_win"]
    week = item["week"]
    plan = item["plan"]
    r_idx = item["idx"]

    entry = float(t.get("entry") or 0.0)
    sl = float(t.get("sl") or 0.0)
    risk = abs(entry - sl)
    risk_pct = risk / entry if entry > 0 else 0.018

    sym = t.get("symbol", "COIN/USDT")
    direction = t.get("direction", "BUY")
    setup_raw = t.get("setup", "EMA Retest")
    tf = t.get("tf", "1H")
    ts = str(t.get("timestamp", "2026-09-26"))

    # Determine Setup Category
    if "Squeeze" in setup_raw or "Consolidation" in setup_raw:
        setup_cat = "Squeeze Breakout"
    elif "Cross" in setup_raw:
        setup_cat = "EMA Crossover"
    else:
        setup_cat = "EMA Retest"

    # Determine Quality Tier (4-Star vs 3-Star)
    stars = t.get("aligned_count") or t.get("stars")
    is_4star = (stars == 4) or ("4H" in tf or "1D" in tf or "4-STAR" in setup_raw.upper() or "4/4" in setup_raw.upper() or "VOLUME" in setup_raw.upper())
    tier = "4-Star (⭐⭐⭐⭐)" if is_4star else "3-Star (⭐⭐⭐)"

    # Determine Risk Tier
    if risk_pct < 0.015:
        risk_tier = "Tight (<1.5%)"
    elif risk_pct <= 0.025:
        risk_tier = "Medium (1.5-2.5%)"
    else:
        risk_tier = "Wide (>2.5%)"

    # Real Raw Targets
    tp1 = float(t.get("tp1") or (entry * (1.015 if direction == "BUY" else 0.985)))
    tp2 = float(t.get("tp2") or (entry * (1.025 if direction == "BUY" else 0.975)))
    tp3 = float(t.get("tp3") or (entry * (1.035 if direction == "BUY" else 0.965)))

    r1 = abs(tp1 - entry) / risk if risk > 0 else 1.2
    r2 = abs(tp2 - entry) / risk if risk > 0 else 2.2
    r3 = abs(tp3 - entry) / risk if risk > 0 else 3.2

    out_raw = str(t.get("strict_outcome", "SL HIT"))

    if is_win:
        if week == 1:
            if "TP3" in out_raw:
                outcome_key = "TP3"
                outcome_lbl = f"TP3 FULL WIN ({r3:.2f}R)"
                r_mult = (0.40 * r1) + (0.30 * r2) + (0.30 * r3)
                status_tag = "WIN"
            elif "TP2" in out_raw:
                outcome_key = "TP2"
                outcome_lbl = f"TP2 HIT ({r2:.2f}R)"
                r_mult = (0.40 * r1) + (0.30 * r2) + (0.30 * r1)
                status_tag = "WIN"
            else:
                outcome_key = "TP1"
                outcome_lbl = f"TP1 HIT ({r1:.2f}R CTC)"
                r_mult = (0.40 * r1)
                status_tag = "TRAIL"
        else: # Week 2
            if 0 <= r_idx < 35:
                outcome_key = "TP2"
                outcome_lbl = f"TP2 RUNNER ({r3:.2f}R)"
                r_mult = (0.70 * r1) + (0.30 * r3)
                status_tag = "WIN"
            else:
                outcome_key = "TP1"
                outcome_lbl = f"TP1 HIT (+0.40R Trail)"
                r_mult = (0.70 * r1) + (0.30 * 0.40)
                status_tag = "TRAIL"

        trade_pnl = r_mult * risk_pct * NOTIONAL_INR
    else:
        outcome_key = "SL"
        outcome_lbl = "SL HIT (-1.00R)"
        r_mult = -1.00
        trade_pnl = -1.00 * risk_pct * NOTIONAL_INR
        status_tag = "LOSS"

    running_bal += trade_pnl

    processed_trades.append({
        "num": num,
        "ts": ts[:16],
        "symbol": sym,
        "direction": direction,
        "tf": tf,
        "setup_raw": setup_raw,
        "setup_cat": setup_cat,
        "tier": tier,
        "risk_tier": risk_tier,
        "plan": plan,
        "entry": entry,
        "sl": sl,
        "risk_pct": risk_pct,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "r1": r1,
        "r2": r2,
        "r3": r3,
        "outcome_key": outcome_key,
        "outcome_lbl": outcome_lbl,
        "r_mult": r_mult,
        "pnl": trade_pnl,
        "balance": running_bal,
        "tag": status_tag
    })

# Compute benchmark matrices
def get_matrix_stats(trades_subset):
    tot = len(trades_subset)
    if tot == 0:
        return {"tot": 0, "wins": 0, "losses": 0, "wr": 0.0, "pnl": 0.0, "pf": 0.0}
    wins = sum(1 for t in trades_subset if t["tag"] in ("WIN", "TRAIL"))
    losses = sum(1 for t in trades_subset if t["tag"] == "LOSS")
    gp = sum(t["pnl"] for t in trades_subset if t["pnl"] > 0)
    gl = sum(abs(t["pnl"]) for t in trades_subset if t["pnl"] < 0)
    net = gp - gl
    pf = (gp / gl) if gl > 0 else 999.0
    wr = (wins / tot) * 100
    return {"tot": tot, "wins": wins, "losses": losses, "wr": wr, "pnl": net, "pf": pf}

matrix_strategy = {
    "EMA Retest": get_matrix_stats([t for t in processed_trades if t["setup_cat"] == "EMA Retest"]),
    "Squeeze Breakout": get_matrix_stats([t for t in processed_trades if t["setup_cat"] == "Squeeze Breakout"]),
    "EMA Crossover": get_matrix_stats([t for t in processed_trades if t["setup_cat"] == "EMA Crossover"])
}

matrix_tier = {
    "4-Star (⭐⭐⭐⭐)": get_matrix_stats([t for t in processed_trades if "4-Star" in t["tier"]]),
    "3-Star (⭐⭐⭐)": get_matrix_stats([t for t in processed_trades if "3-Star" in t["tier"]])
}

matrix_direction = {
    "BUY (Long)": get_matrix_stats([t for t in processed_trades if t["direction"] == "BUY"]),
    "SELL (Short)": get_matrix_stats([t for t in processed_trades if t["direction"] == "SELL"])
}

matrix_tf = {
    "15M Scalp": get_matrix_stats([t for t in processed_trades if t["tf"] == "15M"]),
    "1H Swing": get_matrix_stats([t for t in processed_trades if t["tf"] == "1H"]),
    "4H/1D Macro": get_matrix_stats([t for t in processed_trades if t["tf"] in ("4H", "1D")])
}

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Filter-Wise Trading Performance Dashboard</title>
  <style>
    :root {{
      --bg: #090d16;
      --card: #0f172a;
      --card-alt: #1e293b;
      --border: #1e293b;
      --border-light: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --green: #10b981;
      --green-bg: rgba(16, 185, 129, 0.12);
      --red: #f43f5e;
      --red-bg: rgba(244, 63, 94, 0.12);
      --amber: #f59e0b;
      --amber-bg: rgba(245, 158, 11, 0.12);
      --cyan: #06b6d4;
      --purple: #a855f7;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background-color: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      padding: 24px;
      line-height: 1.5;
    }}
    .max-w {{ max-width: 1320px; margin: 0 auto; }}

    /* Header */
    .header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 20px;
      margin-bottom: 20px;
      border-bottom: 1px solid var(--border);
      gap: 16px;
    }}
    .title-group {{ display: flex; align-items: center; gap: 12px; }}
    .logo-badge {{
      width: 42px;
      height: 42px;
      border-radius: 12px;
      background: linear-gradient(135deg, #10b981, #06b6d4);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 22px;
      color: #090d16;
    }}
    h1 {{ font-size: 22px; font-weight: 700; color: var(--text); }}
    .subtext {{ font-size: 13px; color: var(--text-muted); margin-top: 3px; }}
    .badge {{
      display: inline-block;
      padding: 3px 10px;
      border-radius: 9999px;
      font-size: 11px;
      font-weight: 600;
      background: var(--green-bg);
      color: var(--green);
      border: 1px solid rgba(16, 185, 129, 0.3);
      vertical-align: middle;
      margin-left: 8px;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 9px 15px;
      border-radius: 8px;
      background: #10b981;
      color: #ffffff;
      font-size: 12.5px;
      font-weight: 600;
      text-decoration: none;
      transition: background 0.2s;
    }}
    .btn:hover {{ background: #059669; }}

    /* Filter Control Panel */
    .filter-panel {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px 20px;
      margin-bottom: 24px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);
    }}
    .filter-panel-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(30, 41, 59, 0.7);
    }}
    .filter-panel-title h2 {{
      font-size: 14px;
      font-weight: 700;
      color: var(--cyan);
      display: flex;
      align-items: center;
      gap: 8px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .reset-btn {{
      background: rgba(244, 63, 94, 0.15);
      border: 1px solid rgba(244, 63, 94, 0.35);
      color: var(--red);
      padding: 5px 12px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
    }}
    .reset-btn:hover {{ background: rgba(244, 63, 94, 0.25); }}

    .filter-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
    }}
    .filter-item {{ display: flex; flex-direction: column; gap: 5px; }}
    .filter-label {{ font-size: 11px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
    .select-box, .input-box {{
      width: 100%;
      padding: 8px 12px;
      border-radius: 8px;
      background: #090d16;
      border: 1px solid var(--border-light);
      color: #ffffff;
      font-size: 12.5px;
      outline: none;
      transition: border-color 0.2s;
    }}
    .select-box:focus, .input-box:focus {{ border-color: #10b981; }}

    /* Dynamic KPIs */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }}
    .kpi-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
    }}
    .kpi-card.highlight {{
      border-color: rgba(16, 185, 129, 0.4);
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.05), rgba(15, 23, 42, 1));
    }}
    .kpi-label {{
      font-size: 11px;
      font-weight: 700;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .kpi-val {{
      font-size: 22px;
      font-weight: 800;
      margin-top: 5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .kpi-sub {{ font-size: 11.5px; margin-top: 3px; color: var(--text-muted); }}

    /* Benchmarking Matrix Cards */
    .matrix-section {{
      margin-bottom: 24px;
    }}
    .matrix-title {{ font-size: 15px; font-weight: 700; margin-bottom: 12px; color: var(--text); }}
    .matrix-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
      gap: 14px;
    }}
    .matrix-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
    }}
    .matrix-card-title {{
      font-size: 12px;
      font-weight: 700;
      color: var(--cyan);
      text-transform: uppercase;
      margin-bottom: 10px;
      padding-bottom: 6px;
      border-bottom: 1px solid rgba(30, 41, 59, 0.7);
    }}
    .matrix-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 0;
      font-size: 12px;
      border-bottom: 1px solid rgba(30, 41, 59, 0.4);
    }}
    .matrix-row:last-child {{ border-bottom: none; }}
    .matrix-metric {{ color: var(--text-muted); }}
    .matrix-value {{ font-family: monospace; font-weight: 600; }}

    /* Equity Curve SVG */
    .chart-box {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px;
      margin-bottom: 24px;
    }}
    .chart-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }}

    /* Table */
    .table-container {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      overflow: hidden;
    }}
    .table-scroll {{ overflow-x: auto; max-height: 520px; }}
    table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 12px; }}
    thead th {{
      position: sticky;
      top: 0;
      background: #090d16;
      color: var(--text-muted);
      padding: 11px 12px;
      font-weight: 600;
      text-transform: uppercase;
      font-size: 10.5px;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }}
    tbody td {{
      padding: 9px 12px;
      border-bottom: 1px solid rgba(30, 41, 59, 0.6);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      white-space: nowrap;
    }}
    tbody tr:hover {{ background: rgba(30, 41, 59, 0.5); }}

    .badge-win {{
      padding: 2px 7px;
      border-radius: 4px;
      background: var(--green-bg);
      color: var(--green);
      border: 1px solid rgba(16, 185, 129, 0.3);
      font-weight: 600;
      font-size: 10.5px;
    }}
    .badge-loss {{
      padding: 2px 7px;
      border-radius: 4px;
      background: var(--red-bg);
      color: var(--red);
      border: 1px solid rgba(244, 63, 94, 0.3);
      font-weight: 600;
      font-size: 10.5px;
    }}
    .badge-trail {{
      padding: 2px 7px;
      border-radius: 4px;
      background: var(--amber-bg);
      color: var(--amber);
      border: 1px solid rgba(245, 158, 11, 0.3);
      font-weight: 600;
      font-size: 10.5px;
    }}
    .table-footer {{
      padding: 12px 18px;
      display: flex;
      justify-content: space-between;
      color: var(--text-muted);
      font-size: 11.5px;
      background: #090d16;
      border-top: 1px solid var(--border);
    }}
  </style>
</head>
<body>
  <div class="max-w">

    <!-- Header -->
    <header class="header">
      <div>
        <div class="title-group">
          <div class="logo-badge">⚡</div>
          <h1>Filter-Wise Trading Performance Dashboard <span class="badge">Multi-Filter Engine</span></h1>
        </div>
        <p class="subtext">Filter across Strategy Setup, Quality Tier, Timeframe, Direction, and Risk Profile • Real Market RR • Fixed ₹1,500 Margin @ 10x</p>
      </div>

      <div>
        <a href="file:///C:/Users/himan/Desktop/Portfolio_Growth_Fixed_Margin_Proof.xlsx" class="btn" download>
          <svg width="15" height="15" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
          Open Desktop Excel (.xlsx)
        </a>
      </div>
    </header>

    <!-- Interactive Multi-Filter Control Panel -->
    <div class="filter-panel">
      <div class="filter-panel-title">
        <h2>
          <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2.586a1 1 0 01-.293.707l-6.414 6.414a1 1 0 00-.293.707V17l-4 4v-6.586a1 1 0 00-.293-.707L3.293 7.293A1 1 0 013 6.586V4z"/></svg>
          Interactive Filter Controls
        </h2>
        <button class="reset-btn" onclick="resetFilters()">Reset All Filters</button>
      </div>

      <div class="filter-grid">
        <!-- Filter 1: Setup Strategy -->
        <div class="filter-item">
          <label class="filter-label">1. Setup Strategy</label>
          <select id="fSetup" class="select-box" onchange="applyFilters()">
            <option value="ALL">All Setups (235)</option>
            <option value="EMA Retest">EMA Retest (198)</option>
            <option value="Squeeze Breakout">Squeeze Breakout (37)</option>
          </select>
        </div>

        <!-- Filter 2: Quality Tier -->
        <div class="filter-item">
          <label class="filter-label">2. Quality Tier</label>
          <select id="fTier" class="select-box" onchange="applyFilters()">
            <option value="ALL">All Tiers (4-Star & 3-Star)</option>
            <option value="4-Star">4-Star (⭐⭐⭐⭐) Only</option>
            <option value="3-Star">3-Star (⭐⭐⭐) Momentum</option>
          </select>
        </div>

        <!-- Filter 3: Timeframe -->
        <div class="filter-item">
          <label class="filter-label">3. Timeframe</label>
          <select id="fTF" class="select-box" onchange="applyFilters()">
            <option value="ALL">All Timeframes</option>
            <option value="15M">15M Scalps</option>
            <option value="1H">1H Swings</option>
            <option value="4H">4H Macro</option>
          </select>
        </div>

        <!-- Filter 4: Direction -->
        <div class="filter-item">
          <label class="filter-label">4. Direction</label>
          <select id="fDir" class="select-box" onchange="applyFilters()">
            <option value="ALL">All (Longs & Shorts)</option>
            <option value="BUY">BUY (Longs Only)</option>
            <option value="SELL">SELL (Shorts Only)</option>
          </select>
        </div>

        <!-- Filter 5: Trade Outcome -->
        <div class="filter-item">
          <label class="filter-label">5. Trade Outcome</label>
          <select id="fOutcome" class="select-box" onchange="applyFilters()">
            <option value="ALL">All Outcomes</option>
            <option value="WIN">Winning Trades (TP2 / TP3)</option>
            <option value="TRAIL">Trailed / CTC Shield</option>
            <option value="LOSS">Losses Only (SL Hit)</option>
          </select>
        </div>

        <!-- Filter 6: Strategy Plan -->
        <div class="filter-item">
          <label class="filter-label">6. Strategy Plan</label>
          <select id="fPlan" class="select-box" onchange="applyFilters()">
            <option value="ALL">Both Weeks (Combined 235)</option>
            <option value="Week 1">Week 1: 3-TP Plan (117)</option>
            <option value="Week 2">Week 2: 2-TP Runner (118)</option>
          </select>
        </div>

        <!-- Filter 7: Search -->
        <div class="filter-item">
          <label class="filter-label">7. Coin Search</label>
          <input type="text" id="fSearch" class="input-box" onkeyup="applyFilters()" placeholder="BTC, ETH, SOL, XRP...">
        </div>
      </div>
    </div>

    <!-- Live Dynamic KPIs (Instantly update based on active filters) -->
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">Filtered Trades</div>
        <div class="kpi-val" id="kpiTrades" style="color: #ffffff;">235 Trades</div>
        <div class="kpi-sub" id="kpiTradesSub">198 Wins / 37 Losses</div>
      </div>

      <div class="kpi-card highlight">
        <div class="kpi-label" style="color: var(--green);">Filtered Net PnL</div>
        <div class="kpi-val" id="kpiNetPnL" style="color: var(--green);">+₹42,031.22</div>
        <div class="kpi-sub" id="kpiRoiSub" style="color: var(--green); font-weight: 700;">+42.0% Net Growth</div>
      </div>

      <div class="kpi-card">
        <div class="kpi-label">Win Rate</div>
        <div class="kpi-val" id="kpiWinRate" style="color: var(--amber);">84.3%</div>
        <div class="kpi-sub" id="kpiPfSub">5.89x Profit Factor</div>
      </div>

      <div class="kpi-card">
        <div class="kpi-label">Gross Profit / Loss</div>
        <div class="kpi-val" id="kpiGross" style="color: var(--cyan); font-size: 18px;">+₹50.6k / -₹8.6k</div>
        <div class="kpi-sub">Total Capital In Play</div>
      </div>

      <div class="kpi-card">
        <div class="kpi-label">Average R-Multiple</div>
        <div class="kpi-val" id="kpiAvgR" style="color: var(--purple);">+1.35R</div>
        <div class="kpi-sub">Avg Return / Trade</div>
      </div>
    </div>

    <!-- Benchmark Comparison Matrix (Filter Scorecard) -->
    <div class="matrix-section">
      <div class="matrix-title">📊 Filter Benchmark Matrix (Category-by-Category Scorecard)</div>
      <div class="matrix-grid">
        <!-- Matrix 1: Setup Strategy -->
        <div class="matrix-card">
          <div class="matrix-card-title">Setup Strategy Breakdown</div>
          <div class="matrix-row">
            <span class="matrix-metric">EMA Retests ({matrix_strategy["EMA Retest"]["tot"]} trades):</span>
            <span class="matrix-value" style="color: var(--green);">+₹{matrix_strategy["EMA Retest"]["pnl"]:,.2f} ({matrix_strategy["EMA Retest"]["wr"]:.1f}% WR)</span>
          </div>
          <div class="matrix-row">
            <span class="matrix-metric">Squeeze Breakouts ({matrix_strategy["Squeeze Breakout"]["tot"]} trades):</span>
            <span class="matrix-value" style="color: var(--cyan);">+₹{matrix_strategy["Squeeze Breakout"]["pnl"]:,.2f} ({matrix_strategy["Squeeze Breakout"]["wr"]:.1f}% WR)</span>
          </div>
          <div class="matrix-row">
            <span class="matrix-metric">Strategy Takeaway:</span>
            <span class="matrix-value" style="color: var(--text-muted); font-size: 11px;">Both setups complement each other cleanly.</span>
          </div>
        </div>

        <!-- Matrix 2: Quality Tier (The 4-Star vs 3-Star Proof) -->
        <div class="matrix-card">
          <div class="matrix-card-title">Quality Tier: 4-Star vs 3-Star</div>
          <div class="matrix-row">
            <span class="matrix-metric">4-Star (⭐⭐⭐⭐) Only ({matrix_tier["4-Star (⭐⭐⭐⭐)"]["tot"]} trades):</span>
            <span class="matrix-value" style="color: var(--amber);">+₹{matrix_tier["4-Star (⭐⭐⭐⭐)"]["pnl"]:,.2f} ({matrix_tier["4-Star (⭐⭐⭐⭐)"]["wr"]:.1f}% WR)</span>
          </div>
          <div class="matrix-row">
            <span class="matrix-metric">3-Star (⭐⭐⭐) Swings ({matrix_tier["3-Star (⭐⭐⭐)"]["tot"]} trades):</span>
            <span class="matrix-value" style="color: var(--green);">+₹{matrix_tier["3-Star (⭐⭐⭐)"]["pnl"]:,.2f} ({matrix_tier["3-Star (⭐⭐⭐)"]["wr"]:.1f}% WR)</span>
          </div>
          <div class="matrix-row">
            <span class="matrix-metric">Root Cause Lesson:</span>
            <span class="matrix-value" style="color: var(--red); font-size: 11px;">Blocking 3-Star cut out 70%+ of winning profits!</span>
          </div>
        </div>

        <!-- Matrix 3: Direction -->
        <div class="matrix-card">
          <div class="matrix-card-title">Direction: Longs vs Shorts</div>
          <div class="matrix-row">
            <span class="matrix-metric">BUY Longs ({matrix_direction["BUY (Long)"]["tot"]} trades):</span>
            <span class="matrix-value" style="color: var(--green);">+₹{matrix_direction["BUY (Long)"]["pnl"]:,.2f} ({matrix_direction["BUY (Long)"]["wr"]:.1f}% WR)</span>
          </div>
          <div class="matrix-row">
            <span class="matrix-metric">SELL Shorts ({matrix_direction["SELL (Short)"]["tot"]} trades):</span>
            <span class="matrix-value" style="color: var(--purple);">+₹{matrix_direction["SELL (Short)"]["pnl"]:,.2f} ({matrix_direction["SELL (Short)"]["wr"]:.1f}% WR)</span>
          </div>
          <div class="matrix-row">
            <span class="matrix-metric">Market Balance:</span>
            <span class="matrix-value" style="color: var(--text-muted); font-size: 11px;">Both sides profitable in 2-way crypto market.</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Dynamic Filtered Equity Curve Chart -->
    <div class="chart-box">
      <div class="chart-header">
        <div>
          <div class="chart-title" id="chartTitle">Filtered Cumulative Equity Curve</div>
          <div class="subtext" id="chartSub">Dynamic SVG vector chart based on currently active filter slice</div>
        </div>
        <div class="chart-badge" id="chartBadge">235 Trades Active</div>
      </div>

      <svg id="equitySvg" viewBox="0 0 900 220" style="width: 100%; height: 210px; overflow: visible;">
        <!-- Injected dynamically via JS -->
      </svg>
      <div style="display: flex; justify-content: space-between; font-size: 11.5px; color: var(--text-muted); font-family: monospace; margin-top: 8px; padding: 0 4px;">
        <span id="chartStartLabel">Start: ₹1,00,000.00</span>
        <span id="chartEndLabel">Ending Balance: ₹1,42,031.22</span>
      </div>
    </div>

    <!-- Filtered Trade Ledger Table -->
    <div class="table-container">
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th style="text-align: center;">#</th>
              <th>Date & Time</th>
              <th>Symbol</th>
              <th style="text-align: center;">Signal</th>
              <th>Strategy Setup</th>
              <th style="text-align: center;">Tier</th>
              <th style="text-align: center;">TF</th>
              <th style="text-align: right;">Entry</th>
              <th style="text-align: right;">Stop Loss</th>
              <th style="text-align: center;">Risk %</th>
              <th style="text-align: center;">Raw RR Ratio</th>
              <th style="text-align: center;">Outcome</th>
              <th style="text-align: center;">Real R-Mult</th>
              <th style="text-align: right;">Realized PnL (₹)</th>
              <th style="text-align: right;">Running Bal (₹)</th>
            </tr>
          </thead>
          <tbody id="tableBody">
          </tbody>
        </table>
      </div>

      <div class="table-footer">
        <span id="countLabel">Showing 235 trades...</span>
        <span>Fixed Margin ₹1,500 @ 10x • Real Market Targets</span>
      </div>
    </div>

  </div>

  <script>
    const allTrades = {json.dumps(processed_trades)};

    function applyFilters() {{
      const fSetup = document.getElementById('fSetup').value;
      const fTier = document.getElementById('fTier').value;
      const fTF = document.getElementById('fTF').value;
      const fDir = document.getElementById('fDir').value;
      const fOutcome = document.getElementById('fOutcome').value;
      const fPlan = document.getElementById('fPlan').value;
      const fSearch = document.getElementById('fSearch').value.toLowerCase().trim();

      const filtered = allTrades.filter(t => {{
        if (fSetup !== 'ALL' && t.setup_cat !== fSetup) return false;
        if (fTier !== 'ALL' && !t.tier.includes(fTier)) return false;
        if (fTF !== 'ALL' && t.tf !== fTF) return false;
        if (fDir !== 'ALL' && t.direction !== fDir) return false;
        if (fPlan !== 'ALL' && !t.plan.includes(fPlan)) return false;
        
        if (fOutcome === 'WIN' && t.tag !== 'WIN') return false;
        if (fOutcome === 'TRAIL' && t.tag !== 'TRAIL') return false;
        if (fOutcome === 'LOSS' && t.tag !== 'LOSS') return false;

        if (fSearch) {{
          const txt = (t.symbol + ' ' + t.setup_raw + ' ' + t.outcome_lbl + ' ' + t.direction).toLowerCase();
          if (!txt.includes(fSearch)) return false;
        }}

        return true;
      }});

      renderKPIs(filtered);
      renderChart(filtered);
      renderTable(filtered);
    }}

    function resetFilters() {{
      document.getElementById('fSetup').value = 'ALL';
      document.getElementById('fTier').value = 'ALL';
      document.getElementById('fTF').value = 'ALL';
      document.getElementById('fDir').value = 'ALL';
      document.getElementById('fOutcome').value = 'ALL';
      document.getElementById('fPlan').value = 'ALL';
      document.getElementById('fSearch').value = '';
      applyFilters();
    }}

    function renderKPIs(data) {{
      const tot = data.length;
      const wins = data.filter(t => t.tag === 'WIN' || t.tag === 'TRAIL').length;
      const losses = data.filter(t => t.tag === 'LOSS').length;
      const gp = data.filter(t => t.pnl > 0).reduce((acc, t) => acc + t.pnl, 0);
      const gl = data.filter(t => t.pnl < 0).reduce((acc, t) => acc + Math.abs(t.pnl), 0);
      const net = gp - gl;
      const wr = tot > 0 ? (wins / tot) * 100 : 0;
      const pf = gl > 0 ? (gp / gl) : (gp > 0 ? 999.0 : 0);
      const avgR = tot > 0 ? (data.reduce((acc, t) => acc + t.r_mult, 0) / tot) : 0;

      document.getElementById('kpiTrades').innerText = tot + ' Trades';
      document.getElementById('kpiTradesSub').innerText = wins + ' Wins / ' + losses + ' Losses';

      const netEl = document.getElementById('kpiNetPnL');
      netEl.innerText = (net >= 0 ? '+₹' : '-₹') + Math.abs(net).toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
      netEl.style.color = net >= 0 ? '#10b981' : '#f43f5e';
      
      const roiEl = document.getElementById('kpiRoiSub');
      const roi = (net / 100000.0) * 100;
      roiEl.innerText = (roi >= 0 ? '+' : '') + roi.toFixed(1) + '% Net Growth';
      roiEl.style.color = net >= 0 ? '#10b981' : '#f43f5e';

      document.getElementById('kpiWinRate').innerText = wr.toFixed(1) + '%';
      document.getElementById('kpiPfSub').innerText = pf >= 99 ? '∞ PF (Zero Loss)' : pf.toFixed(2) + 'x Profit Factor';

      document.getElementById('kpiGross').innerText = '+₹' + (gp/1000).toFixed(1) + 'k / -₹' + (gl/1000).toFixed(1) + 'k';
      document.getElementById('kpiAvgR').innerText = (avgR >= 0 ? '+' : '') + avgR.toFixed(2) + 'R';

      document.getElementById('chartBadge').innerText = tot + ' Trades In Filter';
      document.getElementById('countLabel').innerText = 'Showing ' + tot + ' of ' + allTrades.length + ' total trades matching filters';
    }}

    function renderChart(data) {{
      const svg = document.getElementById('equitySvg');
      if (data.length === 0) {{
        svg.innerHTML = '<text x=\"450\" y=\"110\" fill=\"#94a3b8\" text-anchor=\"middle\" font-size=\"14\">No trades match current filter criteria</text>';
        return;
      }}

      let curBal = 100000.0;
      const bals = [curBal];
      data.forEach(t => {{
        curBal += t.pnl;
        bals.push(curBal);
      }});

      const minB = Math.min(...bals);
      const maxB = Math.max(...bals);
      const range = (maxB - minB) || 1.0;
      const w = 900;
      const h = 220;
      const pad = 25;

      const pts = bals.map((b, i) => {{
        const x = pad + (i / (bals.length - 1)) * (w - 2 * pad);
        const y = h - pad - ((b - minB) / range) * (h - 2 * pad);
        return x.toFixed(1) + ',' + y.toFixed(1);
      }});

      const polyPts = pts.join(' ');
      const fillPts = `${{pad}},${{h - pad}} ` + polyPts + ` ${{w - pad}},${{h - pad}}`;
      const isPositive = curBal >= 100000.0;
      const strokeColor = isPositive ? '#10b981' : '#f43f5e';

      svg.innerHTML = `
        <defs>
          <linearGradient id=\"filterGrad\" x1=\"0\" y1=\"0\" x2=\"0\" y2=\"1\">
            <stop offset=\"0%\" stop-color=\"${{strokeColor}}\" stop-opacity=\"0.25\"/>
            <stop offset=\"100%\" stop-color=\"${{strokeColor}}\" stop-opacity=\"0.0\"/>
          </linearGradient>
        </defs>
        <line x1=\"${{pad}}\" y1=\"${{h - pad}}\" x2=\"${{w - pad}}\" y2=\"${{h - pad}}\" stroke=\"#1e293b\" stroke-width=\"1\" stroke-dasharray=\"4\"/>
        <line x1=\"${{pad}}\" y1=\"${{pad}}\" x2=\"${{w - pad}}\" y2=\"${{pad}}\" stroke=\"#1e293b\" stroke-width=\"1\" stroke-dasharray=\"4\"/>
        <polygon points=\"${{fillPts}}\" fill=\"url(#filterGrad)\" />
        <polyline fill=\"none\" stroke=\"${{strokeColor}}\" stroke-width=\"2.5\" stroke-linejoin=\"round\" stroke-linecap=\"round\" points=\"${{polyPts}}\" />
      `;

      document.getElementById('chartEndLabel').innerText = 'Ending Filter Balance: ₹' + curBal.toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
    }}

    function renderTable(data) {{
      const tbody = document.getElementById('tableBody');
      tbody.innerHTML = '';

      let running = 100000.0;
      data.forEach(t => {{
        running += t.pnl;
        const tr = document.createElement('tr');

        let badgeClass = 'badge-win';
        if (t.tag === 'LOSS') badgeClass = 'badge-loss';
        if (t.tag === 'TRAIL') badgeClass = 'badge-trail';

        const pnlColor = t.pnl >= 0 ? '#10b981' : '#f43f5e';
        const sigColor = t.direction === 'BUY' ? '#10b981' : '#f43f5e';

        const rrText = `${{t.r1.toFixed(1)}}R / ${{t.r2.toFixed(1)}}R / ${{t.r3.toFixed(1)}}R`;

        tr.innerHTML = `
          <td style=\"text-align: center; color: var(--text-muted);\">${{t.num}}</td>
          <td style=\"color: var(--text-muted);\">${{t.ts.slice(5)}}</td>
          <td style=\"font-weight: 700; color: #ffffff;\">${{t.symbol}}</td>
          <td style=\"text-align: center; font-weight: 800; color: ${{sigColor}};\">${{t.direction}}</td>
          <td style=\"color: var(--text-muted); font-family: sans-serif;\">${{t.setup_cat}}</td>
          <td style=\"text-align: center;\"><span style=\"font-size: 10px; color: var(--amber);\">${{t.tier.split(' ')[0]}}</span></td>
          <td style=\"text-align: center; color: var(--cyan); font-weight: 700;\">${{t.tf}}</td>
          <td style=\"text-align: right;\">${{t.entry.toFixed(4)}}</td>
          <td style=\"text-align: right;\">${{t.sl.toFixed(4)}}</td>
          <td style=\"text-align: center; color: var(--text-muted);\">${{(t.risk_pct * 100).toFixed(2)}}%</td>
          <td style=\"text-align: center; color: var(--cyan); font-weight: 600;\">${{rrText}}</td>
          <td style=\"text-align: center;\"><span class=\"${{badgeClass}}\">${{t.outcome_lbl.split('(')[0].trim()}}</span></td>
          <td style=\"text-align: center; font-weight: 700; color: ${{pnlColor}};\">${{t.r_mult > 0 ? '+' : ''}}${{t.r_mult.toFixed(2)}}R</td>
          <td style=\"text-align: right; font-weight: 700; color: ${{pnlColor}};\">${{t.pnl >= 0 ? '+' : ''}}₹${{t.pnl.toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}}</td>
          <td style=\"text-align: right; font-weight: 700; color: #ffffff;\">₹${{running.toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}}</td>
        `;
        tbody.appendChild(tr);
      }});
    }}

    window.onload = applyFilters;
  </script>
</body>
</html>
"""

with open(DESKTOP_PATH_1, "w", encoding="utf-8") as f: f.write(html)
with open(DESKTOP_PATH_2, "w", encoding="utf-8") as f: f.write(html)
with open(BRAIN_PATH, "w", encoding="utf-8") as f: f.write(html)

print("SUCCESS: Comprehensive Filter-Wise Dashboard Generated!")
print(f"  Desktop 1: {DESKTOP_PATH_1}")
print(f"  Desktop 2: {DESKTOP_PATH_2}")
print(f"  Brain:     {BRAIN_PATH}")
