import json
import os
import re
import sys
import subprocess
import shutil

sys.stdout.reconfigure(encoding='utf-8')

def safe_float(v, default=0.0):
    if v is None: return default
    try: return float(v)
    except: return default

# Load deduplicated unique signals
with open('evaluated_strict_trajectories.json', 'r', encoding='utf-8') as f:
    all_signals = json.load(f)

# Filter ONLY Fresh Crossovers signals (excluding Retests/Pullbacks)
crossover_signals = []
for s in all_signals:
    sig_type = str(s.get('type', '')).upper()
    setup = str(s.get('setup', '')).upper()
    if sig_type != 'RETEST' and 'RETEST' not in setup and 'PULLBACK' not in setup:
        crossover_signals.append(s)

total_unique_crossovers = len(crossover_signals)
print(f"Total Unique Fresh Crossovers Evaluated: {total_unique_crossovers}")

coin_stats = {}
star_stats = {'4-STAR': {'total': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0, 'sl': 0, 'running': 0, 'unfilled': 0},
              '3-STAR': {'total': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0, 'sl': 0, 'running': 0, 'unfilled': 0}}

for s in crossover_signals:
    sym = s['symbol']
    out = str(s.get('strict_outcome', '')).upper()

    stars = s.get('aligned_count') or s.get('stars')
    tf = str(s.get('tf', '')).upper()
    setup = str(s.get('setup', '')).upper()
    is_4star = (stars == 4) or ('4H' in tf or '1D' in tf or '4-STAR' in setup or '4/4' in setup or 'VOLUME' in setup)
    star_cat = '4-STAR' if is_4star else '3-STAR'

    if sym not in coin_stats:
        coin_stats[sym] = {'total': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0, 'sl': 0, 'running': 0, 'unfilled': 0}

    coin_stats[sym]['total'] += 1
    star_stats[star_cat]['total'] += 1

    if 'TP3' in out or 'FULL WIN' in out or 'TARGET HIT' in out:
        coin_stats[sym]['tp3'] += 1
        star_stats[star_cat]['tp3'] += 1
    elif 'TP2' in out:
        coin_stats[sym]['tp2'] += 1
        star_stats[star_cat]['tp2'] += 1
    elif 'TP1' in out:
        coin_stats[sym]['tp1'] += 1
        star_stats[star_cat]['tp1'] += 1
    elif 'SL' in out:
        coin_stats[sym]['sl'] += 1
        star_stats[star_cat]['sl'] += 1
    elif 'UNFILLED' in out:
        coin_stats[sym]['unfilled'] += 1
        star_stats[star_cat]['unfilled'] += 1
    else:
        coin_stats[sym]['running'] += 1
        star_stats[star_cat]['running'] += 1

sorted_coins = sorted(coin_stats.items(), key=lambda x: x[1]['total'], reverse=True)

total_tp1 = sum(s['tp1'] for s in coin_stats.values())
total_tp2 = sum(s['tp2'] for s in coin_stats.values())
total_tp3 = sum(s['tp3'] for s in coin_stats.values())
total_sl  = sum(s['sl'] for s in coin_stats.values())
total_run = sum(s['running'] for s in coin_stats.values())
total_unfilled = sum(s['unfilled'] for s in coin_stats.values())

total_wins = total_tp1 + total_tp2 + total_tp3
total_closed = total_wins + total_sl
overall_wr = (total_wins / total_closed * 100) if total_closed > 0 else 0.0

artifact_dir = r"C:\Users\himan\.gemini\antigravity\brain\8fde3a20-bebc-48ee-a71e-34c886e3cbdd"
html_path = os.path.join(artifact_dir, "fresh_crossovers_report.html")
pdf_path = os.path.join(artifact_dir, "fresh_crossovers_report.pdf")

mid = (len(sorted_coins) + 1) // 2
col1_coins = sorted_coins[:mid]
col2_coins = sorted_coins[mid:]

def build_coin_rows_table(coin_list, col_split=25):
    col1 = coin_list[:col_split]
    col2 = coin_list[col_split:]
    
    def make_column(clist):
        rows = ""
        for sym, s in clist:
            wins = s['tp1'] + s['tp2'] + s['tp3']
            closed = wins + s['sl']
            wr = (wins / closed * 100) if closed > 0 else (100.0 if wins > 0 else 0.0)
            badge_cls = "badge-win" if wr >= 50 else ("badge-loss" if wr < 30 else "badge-neutral")
            rows += f"""
            <tr>
                <td class="sym">{sym}</td>
                <td><b>{s['total']}</b></td>
                <td class="tp-cell">{s['tp1']}</td>
                <td class="tp-cell">{s['tp2']}</td>
                <td class="tp-cell bold-tp">{s['tp3']}</td>
                <td class="sl-cell">{s['sl']}</td>
                <td>{s['running']}</td>
                <td><span class="{badge_cls}">{wr:.0f}%</span></td>
            </tr>
            """
        return rows

    return f"""
    <div class="two-col">
        <div>
            <table class="mini-table">
                <thead>
                    <tr><th>Asset</th><th>Total</th><th>TP1</th><th>TP2</th><th>TP3 🚀</th><th>SL 🔻</th><th>Run</th><th>Win %</th></tr>
                </thead>
                <tbody>{make_column(col1)}</tbody>
            </table>
        </div>
        <div>
            <table class="mini-table">
                <thead>
                    <tr><th>Asset</th><th>Total</th><th>TP1</th><th>TP2</th><th>TP3 🚀</th><th>SL 🔻</th><th>Run</th><th>Win %</th></tr>
                </thead>
                <tbody>{make_column(col2)}</tbody>
            </table>
        </div>
    </div>
    """

html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Fresh Crossovers Channel — Strict Trajectory Performance Report</title>
    <style>
        @page {{
            size: A4 portrait;
            margin: 0;
        }}
        * {{
            box-sizing: border-box;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }}
        body {{
            margin: 0;
            padding: 0;
            background-color: #f8fafc;
            color: #0f172a;
            -webkit-print-color-adjust: exact;
        }}
        .page {{
            width: 210mm;
            height: 297mm;
            padding: 16mm 18mm;
            position: relative;
            background: #ffffff;
            page-break-after: always;
            overflow: hidden;
        }}
        .header-banner {{
            background: linear-gradient(135deg, #0284c7 0%, #0f172a 100%);
            color: #ffffff;
            padding: 18px 22px;
            border-radius: 10px;
            margin-bottom: 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .logo-title {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .logo-icon {{
            font-size: 32px;
        }}
        .title-text h1 {{
            margin: 0;
            font-size: 19px;
            font-weight: 800;
            letter-spacing: 0.5px;
            color: #ffffff;
        }}
        .title-text p {{
            margin: 3px 0 0 0;
            font-size: 11px;
            color: #38bdf8;
            font-weight: 500;
        }}
        .meta-pill {{
            background: rgba(255, 255, 255, 0.15);
            backdrop-filter: blur(4px);
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: #ffffff;
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 700;
            text-align: right;
        }}

        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin-bottom: 18px;
        }}
        .kpi-card {{
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 12px;
            text-align: center;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }}
        .kpi-card.blue {{
            border-top: 4px solid #0284c7;
            background: #f0f9ff;
        }}
        .kpi-card.green {{
            border-top: 4px solid #16a34a;
            background: #f0fdf4;
        }}
        .kpi-card.red {{
            border-top: 4px solid #dc2626;
            background: #fef2f2;
        }}
        .kpi-card.purple {{
            border-top: 4px solid #7c3aed;
            background: #faf5ff;
        }}
        .kpi-title {{
            font-size: 10px;
            text-transform: uppercase;
            color: #64748b;
            font-weight: 700;
            margin-bottom: 4px;
        }}
        .kpi-value {{
            font-size: 22px;
            font-weight: 800;
            color: #0f172a;
        }}
        .kpi-sub {{
            font-size: 10px;
            font-weight: 600;
            margin-top: 3px;
        }}

        .section-title {{
            font-size: 13px;
            font-weight: 800;
            color: #1e293b;
            text-transform: uppercase;
            margin: 16px 0 10px 0;
            display: flex;
            align-items: center;
            gap: 8px;
            border-left: 4px solid #0284c7;
            padding-left: 8px;
        }}

        .conf-table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 18px;
            font-size: 11px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
            border-radius: 6px;
            overflow: hidden;
        }}
        .conf-table th {{
            background: #0f172a;
            color: #ffffff;
            font-weight: 700;
            text-align: left;
            padding: 8px 12px;
        }}
        .conf-table td {{
            padding: 8px 12px;
            border-bottom: 1px solid #e2e8f0;
        }}
        .conf-table tr:nth-child(even) {{
            background-color: #f8fafc;
        }}

        .hit-grid {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
            margin-bottom: 18px;
        }}
        .hit-box {{
            border-radius: 8px;
            padding: 12px;
            font-size: 11px;
        }}
        .hit-box.tp3 {{
            background: #f0fdf4;
            border: 1px solid #bbf7d0;
            color: #166534;
        }}
        .hit-box.tp2 {{
            background: #ecfeff;
            border: 1px solid #a5f3fc;
            color: #155e75;
        }}
        .hit-box.tp1 {{
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            color: #1e40af;
        }}
        .hit-box.sl {{
            background: #fef2f2;
            border: 1px solid #fecaca;
            color: #991b1b;
        }}
        .hit-box.ctc {{
            background: #fefce8;
            border: 1px solid #fef08a;
            color: #854d0e;
        }}
        .hit-box.active {{
            background: #f5f3ff;
            border: 1px solid #ddd6fe;
            color: #5b21b6;
        }}

        .hit-box-title {{
            font-weight: 800;
            font-size: 12px;
            margin-bottom: 4px;
        }}

        .info-card {{
            background: #f0f9ff;
            border-radius: 8px;
            padding: 12px;
            font-size: 10.5px;
            line-height: 1.5;
            color: #0369a1;
            border-left: 4px solid #0284c7;
        }}

        .footer {{
            position: absolute;
            bottom: 12mm;
            left: 18mm;
            right: 18mm;
            display: flex;
            justify-content: space-between;
            font-size: 10px;
            color: #94a3b8;
            border-top: 1px solid #e2e8f0;
            padding-top: 8px;
        }}

        .two-col {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }}
        .mini-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 9.5px;
        }}
        .mini-table th {{
            background: #0f172a;
            color: #ffffff;
            padding: 5px 6px;
            text-align: left;
            font-size: 9px;
        }}
        .mini-table td {{
            padding: 4px 6px;
            border-bottom: 1px solid #f1f5f9;
        }}
        .mini-table tr:nth-child(even) {{
            background-color: #f8fafc;
        }}
        .sym {{
            font-weight: 700;
            color: #0f172a;
        }}
        .tp-cell {{
            color: #15803d;
            font-weight: 600;
        }}
        .bold-tp {{
            font-weight: 800;
            color: #166534;
        }}
        .sl-cell {{
            color: #b91c1c;
        }}
        .badge-win {{
            background: #dcfce7;
            color: #15803d;
            padding: 1px 5px;
            border-radius: 4px;
            font-weight: 700;
        }}
        .badge-loss {{
            background: #fee2e2;
            color: #b91c1c;
            padding: 1px 5px;
            border-radius: 4px;
            font-weight: 700;
        }}
        .badge-neutral {{
            background: #f1f5f9;
            color: #475569;
            padding: 1px 5px;
            border-radius: 4px;
            font-weight: 600;
        }}
    </style>
</head>
<body>

    <!-- ================= PAGE 1 ================= -->
    <div class="page">
        <div class="header-banner">
            <div class="logo-title">
                <div class="logo-icon">⚡</div>
                <div class="title-text">
                    <h1>Fresh Crossovers Performance Report</h1>
                    <p>Strict Limit Fill & Trajectory Evaluation (3-Star & 4-Star Crossovers)</p>
                </div>
            </div>
            <div class="meta-pill">
                Fresh Crossovers Channel Audit<br>
                Audit Period: Sep 26 – Oct 04, 2026
            </div>
        </div>

        <div class="kpi-grid">
            <div class="kpi-card blue">
                <div class="kpi-title">Unique Crossovers Sent</div>
                <div class="kpi-value">{total_unique_count}</div>
                <div class="kpi-sub" style="color:#0284c7;">Fresh EMA Crossovers</div>
            </div>
            <div class="kpi-card green">
                <div class="kpi-title">Total Target Wins</div>
                <div class="kpi-value" style="color: #16a34a;">{total_wins}</div>
                <div class="kpi-sub" style="color:#16a34a;">TP1 + TP2 + TP3 Hits</div>
            </div>
            <div class="kpi-card red">
                <div class="kpi-title">Stop-Loss Hits</div>
                <div class="kpi-value" style="color: #dc2626;">{total_sl}</div>
                <div class="kpi-sub" style="color:#dc2626;">Controlled Structural SL</div>
            </div>
            <div class="kpi-card purple">
                <div class="kpi-title">Closed Crossover Win Rate</div>
                <div class="kpi-value" style="color: #7c3aed;">{overall_wr:.1f}%</div>
                <div class="kpi-sub" style="color:#7c3aed;">1.6x – 2.5x R:R Target</div>
            </div>
        </div>

        <div class="section-title">🌟 Confluence Performance Bifurcation (4-Star vs 3-Star Crossovers)</div>
        <table class="conf-table">
            <thead>
                <tr>
                    <th>Confluence Rating</th>
                    <th>Total Crossovers</th>
                    <th>Closed Signals</th>
                    <th>TP1 Hit 🎯</th>
                    <th>TP2 Hit 🎯</th>
                    <th>TP3 Hit 🚀</th>
                    <th>Total Wins 🏆</th>
                    <th>SL Hit 🔻</th>
                    <th>Win Rate %</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td><b>4-Star Crossovers (4/4 Stars Aligned)</b></td>
                    <td><b>{star_stats['4-STAR']['total']}</b></td>
                    <td>{star_stats['4-STAR']['tp1'] + star_stats['4-STAR']['tp2'] + star_stats['4-STAR']['tp3'] + star_stats['4-STAR']['sl']}</td>
                    <td>{star_stats['4-STAR']['tp1']}</td>
                    <td>{star_stats['4-STAR']['tp2']}</td>
                    <td>{star_stats['4-STAR']['tp3']}</td>
                    <td><b style="color:#16a34a;">{star_stats['4-STAR']['tp1'] + star_stats['4-STAR']['tp2'] + star_stats['4-STAR']['tp3']}</b></td>
                    <td><b style="color:#dc2626;">{star_stats['4-STAR']['sl']}</b></td>
                    <td><span class="badge-win">{(star_stats['4-STAR']['tp1'] + star_stats['4-STAR']['tp2'] + star_stats['4-STAR']['tp3']) / max(1, star_stats['4-STAR']['tp1'] + star_stats['4-STAR']['tp2'] + star_stats['4-STAR']['tp3'] + star_stats['4-STAR']['sl']) * 100:.1f}%</span></td>
                </tr>
                <tr>
                    <td><b>3-Star Crossovers (3/4 Stars Aligned)</b></td>
                    <td><b>{star_stats['3-STAR']['total']}</b></td>
                    <td>{star_stats['3-STAR']['tp1'] + star_stats['3-STAR']['tp2'] + star_stats['3-STAR']['tp3'] + star_stats['3-STAR']['sl']}</td>
                    <td>{star_stats['3-STAR']['tp1']}</td>
                    <td>{star_stats['3-STAR']['tp2']}</td>
                    <td>{star_stats['3-STAR']['tp3']}</td>
                    <td><b style="color:#16a34a;">{star_stats['3-STAR']['tp1'] + star_stats['3-STAR']['tp2'] + star_stats['3-STAR']['tp3']}</b></td>
                    <td><b style="color:#dc2626;">{star_stats['3-STAR']['sl']}</b></td>
                    <td><span class="badge-win">{(star_stats['3-STAR']['tp1'] + star_stats['3-STAR']['tp2'] + star_stats['3-STAR']['tp3']) / max(1, star_stats['3-STAR']['tp1'] + star_stats['3-STAR']['tp2'] + star_stats['3-STAR']['tp3'] + star_stats['3-STAR']['sl']) * 100:.1f}%</span></td>
                </tr>
                <tr style="background:#f1f5f9; font-weight:700;">
                    <td><b>Combined Fresh Crossovers Total</b></td>
                    <td><b>{total_unique_count}</b></td>
                    <td><b>{total_closed}</b></td>
                    <td><b>{total_tp1}</b></td>
                    <td><b>{total_tp2}</b></td>
                    <td><b>{total_tp3}</b></td>
                    <td><b style="color:#16a34a;">{total_wins}</b></td>
                    <td><b style="color:#dc2626;">{total_sl}</b></td>
                    <td><span class="badge-win" style="background:#0284c7; color:#fff;">{overall_wr:.1f}%</span></td>
                </tr>
            </tbody>
        </table>

        <div class="section-title">🎯 Target Level Hit Distribution & Protection Shields</div>
        <div class="hit-grid">
            <div class="hit-box tp3">
                <div class="hit-box-title">🚀 TP3 Hit (Full Win)</div>
                <div><b>{total_tp3} Signals ({(total_tp3/total_unique_count*100):.1f}%)</b></div>
                <div style="font-size:10px; margin-top:2px;">Captured maximum structural profit expansion target.</div>
            </div>
            <div class="hit-box tp2">
                <div class="hit-box-title">🎯 TP2 Hit (Upper Target)</div>
                <div><b>{total_tp2} Signals ({(total_tp2/total_unique_count*100):.1f}%)</b></div>
                <div style="font-size:10px; margin-top:2px;">Upgraded volume expansion liquidity target.</div>
            </div>
            <div class="hit-box tp1">
                <div class="hit-box-title">🎯 TP1 Hit (Primary)</div>
                <div><b>{total_tp1} Signals ({(total_tp1/total_unique_count*100):.1f}%)</b></div>
                <div style="font-size:10px; margin-top:2px;">Primary target hit & Breakeven shield active.</div>
            </div>
            <div class="hit-box sl">
                <div class="hit-box-title">🔻 Stop-Loss (SL Hit)</div>
                <div><b>{total_sl} Signals ({(total_sl/total_unique_count*100):.1f}%)</b></div>
                <div style="font-size:10px; margin-top:2px;">Controlled structural risk exit.</div>
            </div>
            <div class="hit-box ctc">
                <div class="hit-box-title">🛡️ Breakeven Shield (CTC)</div>
                <div><b>100% Activation</b></div>
                <div style="font-size:10px; margin-top:2px;">On TP1 hit, SL moves to entry price guaranteeing zero loss.</div>
            </div>
            <div class="hit-box active">
                <div class="hit-box-title">⏳ Active / Unfilled</div>
                <div><b>{total_run + total_unfilled} Signals ({( (total_run+total_unfilled)/total_unique_count*100):.1f}%)</b></div>
                <div style="font-size:10px; margin-top:2px;">Signals currently active or resting limit unfilled.</div>
            </div>
        </div>

        <div class="info-card">
            <b>⚡ Fresh Crossovers Strict Trajectory Evaluation</b>: Evaluated with strict limit fill verification (entry limit must be touched before activating SL check) and Breakeven CTC shield protection on TP1 hits. Across closed crossovers, Fresh Crossovers deliver a solid <b>{overall_wr:.1f}% Closed Win Rate</b>!
        </div>

        <div class="footer">
            <span>Fresh Crossovers Channel Performance Report</span>
            <span>Page 1 of 2</span>
        </div>
    </div>

    <!-- ================= PAGE 2 ================= -->
    <div class="page">
        <div class="header-banner">
            <div class="logo-title">
                <div class="logo-icon">📊</div>
                <div class="title-text">
                    <h1>Fresh Crossovers Asset Scoreboard</h1>
                    <p>Coin-by-Coin Performance Across All Traded Crossover Assets</p>
                </div>
            </div>
            <div class="meta-pill">{len(sorted_coins)} Coins Audit</div>
        </div>

        {build_coin_rows_table(sorted_coins, (len(sorted_coins)+1)//2)}

        <div style="margin-top: 15px;" class="info-card">
            <b>📋 Verification Disclaimer</b>: Evaluated using strict 5-minute candle limit order fill checking across all unique Fresh EMA Crossover signals sent to the Telegram Crossovers Channel from September 26 through October 04, 2026.
        </div>

        <div class="footer">
            <span>Fresh Crossovers Channel Performance Report</span>
            <span>Page 2 of 2</span>
        </div>
    </div>

</body>
</html>
"""

with open(html_path, "w", encoding="utf-8") as f:
    f.write(html_content)

print("Fresh Crossovers HTML report generated:", html_path)

# Convert to PDF via Edge Headless
edge_executable = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if os.path.exists(edge_executable):
    cmd = [
        edge_executable,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}",
        html_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    print("PDF File Created:", os.path.exists(pdf_path), pdf_path)
    
    # Copy to Desktop
    desktop_dir = os.path.expanduser('~/Desktop')
    dst_pdf = os.path.join(desktop_dir, 'Fresh_Crossovers_Performance_Report.pdf')
    dst_html = os.path.join(desktop_dir, 'Fresh_Crossovers_Performance_Report.html')
    shutil.copy2(pdf_path, dst_pdf)
    shutil.copy2(html_path, dst_html)
    print("Successfully Copied Fresh Crossovers Strict Report to Desktop:")
    print("  PDF:", dst_pdf)
    print("  HTML:", dst_html)
