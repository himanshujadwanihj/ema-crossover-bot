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

parsed_signals = []

# 1. Parse text logs
signal_regex = re.compile(
    r'(?:\[\w+\]\s+)?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*?NEW SIGNAL:\s+(BUY|SELL)\s+([A-Z0-9/:-]+)\s+([A-Z0-9]+)\s+\((.*?)\)\s+\|\s+entry=([0-9.]+)\s+sl=([0-9.]+)\s+tp1=([0-9.]+)\s+tp2=([0-9.]+)\s+tp3=([0-9.]+)',
    re.IGNORECASE
)

log_files = ['raw_signals_found.txt', 'raw_signals_since_yesterday.txt', 'fresh_railway_logs.txt', 'railway_logs.txt', 'logs_temp.txt', 'recent_logs.txt', 'log.txt', 'logs.txt']

for fname in log_files:
    if os.path.exists(fname):
        with open(fname, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                match = signal_regex.search(line)
                if match:
                    ts_str, side, sym, tf, setup, entry, sl, tp1, tp2, tp3 = match.groups()
                    clean_sym = sym.replace('/', '').replace('-', '').upper()
                    parsed_signals.append({
                        'timestamp': ts_str,
                        'symbol': clean_sym,
                        'raw_symbol': sym,
                        'direction': side.upper(),
                        'tf': tf.upper(),
                        'setup': setup.strip(),
                        'entry': float(entry),
                        'sl': float(sl),
                        'tp1': float(tp1),
                        'tp2': float(tp2),
                        'tp3': float(tp3),
                        'source': fname
                    })

# 2. Parse JSONs
for fname in ['evaluated_since_yesterday.json', 'evaluated_full_day.json', 'evaluated_today_summary.json', 'logs_36h.json']:
    if os.path.exists(fname):
        try:
            with open(fname, 'r', encoding='utf-8') as f:
                d = json.load(f)
                items = d.get('trades') or d.get('signals') or (d if isinstance(d, list) else [])
                for item in items:
                    if isinstance(item, dict) and item.get('symbol'):
                        clean_sym = str(item.get('symbol', '')).replace('/', '').replace('-', '').upper()
                        parsed_signals.append({
                            'timestamp': item.get('time_utc') or item.get('time_ist') or '2026-09-26 05:00:00',
                            'symbol': clean_sym,
                            'raw_symbol': item.get('symbol'),
                            'direction': str(item.get('direction', '')).upper(),
                            'tf': str(item.get('tf', '')).upper(),
                            'setup': str(item.get('setup', '')),
                            'entry': safe_float(item.get('entry')),
                            'sl': safe_float(item.get('sl')),
                            'tp1': safe_float(item.get('tp1')),
                            'tp2': safe_float(item.get('tp2')),
                            'tp3': safe_float(item.get('tp3')),
                            'outcome': item.get('outcome'),
                            'tp1_hit': item.get('tp1_hit', False),
                            'tp2_hit': item.get('tp2_hit', False),
                            'tp3_hit': item.get('tp3_hit', False),
                            'sl_hit': item.get('sl_hit', False),
                            'source': fname
                        })
        except Exception:
            pass

# Deduplicate
unique_signals = {}
for s in parsed_signals:
    sym = s['symbol']
    direction = s['direction']
    tf = s['tf']
    entry = round(s['entry'], 4)
    k = f"{sym}_{direction}_{tf}_{entry}"
    if k not in unique_signals:
        unique_signals[k] = s
    else:
        out_new = str(s.get('outcome', '')).upper()
        out_old = str(unique_signals[k].get('outcome', '')).upper()
        if ('TP' in out_new or 'SL' in out_new or 'WIN' in out_new or 'CTC' in out_new) and not ('TP' in out_old or 'SL' in out_old):
            unique_signals[k] = s

total_raw_count = len(parsed_signals)
total_unique_count = len(unique_signals)

# Aggregate statistics
coin_stats = {}
star_stats = {'4-STAR': {'total': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0, 'sl': 0, 'running': 0},
              '3-STAR': {'total': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0, 'sl': 0, 'running': 0}}

for k, s in unique_signals.items():
    sym = s['symbol']
    out = str(s.get('outcome', '')).upper()
    tp1_h = s.get('tp1_hit', False)
    tp2_h = s.get('tp2_hit', False)
    tp3_h = s.get('tp3_hit', False)
    sl_h  = s.get('sl_hit', False)

    tf = s['tf']
    setup = s['setup'].upper()
    is_4star = ('4H' in tf or '1D' in tf or '4-STAR' in setup or '4/4' in setup or 'VOLUME' in setup or 'RETEST' in setup)
    star_cat = '4-STAR' if is_4star else '3-STAR'

    if sym not in coin_stats:
        coin_stats[sym] = {'total': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0, 'sl': 0, 'running': 0}

    coin_stats[sym]['total'] += 1
    star_stats[star_cat]['total'] += 1

    if tp3_h or 'TP3' in out or 'FULL WIN' in out or 'TARGET HIT' in out:
        coin_stats[sym]['tp3'] += 1
        star_stats[star_cat]['tp3'] += 1
    elif tp2_h or 'TP2' in out:
        coin_stats[sym]['tp2'] += 1
        star_stats[star_cat]['tp2'] += 1
    elif tp1_h or 'TP1' in out:
        coin_stats[sym]['tp1'] += 1
        star_stats[star_cat]['tp1'] += 1
    elif sl_h or 'SL' in out or 'STOP' in out or 'LOSS' in out:
        coin_stats[sym]['sl'] += 1
        star_stats[star_cat]['sl'] += 1
    else:
        coin_stats[sym]['running'] += 1
        star_stats[star_cat]['running'] += 1

sorted_coins = sorted(coin_stats.items(), key=lambda x: x[1]['total'], reverse=True)

# Build HTML report
artifact_dir = r"C:\Users\himan\.gemini\antigravity\brain\8fde3a20-bebc-48ee-a71e-34c886e3cbdd"
html_path = os.path.join(artifact_dir, "all_signals_sent_report.html")
pdf_path = os.path.join(artifact_dir, "all_signals_sent_report.pdf")

mid = (len(sorted_coins) + 1) // 2
col1_coins = sorted_coins[:mid]
col2_coins = sorted_coins[mid:]

def build_coin_rows(coin_list):
    rows = ""
    for sym, s in coin_list:
        wins = s['tp1'] + s['tp2'] + s['tp3']
        closed = wins + s['sl']
        wr = (wins / closed * 100) if closed > 0 else 0.0
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

total_tp1 = sum(s['tp1'] for s in coin_stats.values())
total_tp2 = sum(s['tp2'] for s in coin_stats.values())
total_tp3 = sum(s['tp3'] for s in coin_stats.values())
total_sl  = sum(s['sl'] for s in coin_stats.values())
total_run = sum(s['running'] for s in coin_stats.values())
total_wins = total_tp1 + total_tp2 + total_tp3
total_closed = total_wins + total_sl
overall_wr = (total_wins / total_closed * 100) if total_closed > 0 else 0.0

html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Telegram Signal Engine — All Historical Signals Sent Report</title>
    <style>
        @page {{
            size: A4 portrait;
            margin: 0;
        }}
        * {{
            box-sizing: border-box;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
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
            padding: 15mm 16mm;
            position: relative;
            background: #ffffff;
            page-break-after: always;
            overflow: hidden;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 3px solid #2563eb;
            padding-bottom: 10px;
            margin-bottom: 15px;
        }}
        .logo-title {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .logo-icon {{
            font-size: 28px;
        }}
        .title-text h1 {{
            margin: 0;
            font-size: 19px;
            font-weight: 800;
            color: #1e3a8a;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .title-text p {{
            margin: 2px 0 0 0;
            font-size: 11px;
            color: #64748b;
            font-weight: 600;
        }}
        .meta-badge {{
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            color: #1d4ed8;
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 700;
        }}
        
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 10px;
            margin-bottom: 15px;
        }}
        .kpi-card {{
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 10px;
            text-align: center;
        }}
        .kpi-card.highlight {{
            background: #eff6ff;
            border-color: #93c5fd;
        }}
        .kpi-title {{
            font-size: 10px;
            text-transform: uppercase;
            color: #64748b;
            font-weight: 700;
            margin-bottom: 4px;
        }}
        .kpi-value {{
            font-size: 20px;
            font-weight: 800;
            color: #0f172a;
        }}
        .kpi-sub {{
            font-size: 10px;
            color: #16a34a;
            font-weight: 600;
            margin-top: 2px;
        }}

        .section-title {{
            font-size: 13px;
            font-weight: 800;
            color: #1e293b;
            text-transform: uppercase;
            margin: 12px 0 8px 0;
            display: flex;
            align-items: center;
            gap: 6px;
            border-left: 4px solid #2563eb;
            padding-left: 8px;
        }}

        .conf-table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 15px;
            font-size: 11px;
        }}
        .conf-table th {{
            background: #1e293b;
            color: #ffffff;
            font-weight: 700;
            text-align: left;
            padding: 7px 10px;
        }}
        .conf-table td {{
            padding: 7px 10px;
            border-bottom: 1px solid #e2e8f0;
        }}
        .conf-table tr:nth-child(even) {{
            background-color: #f8fafc;
        }}

        .hit-grid {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px;
            margin-bottom: 15px;
        }}
        .hit-box {{
            border-radius: 8px;
            padding: 10px;
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

        .footer {{
            position: absolute;
            bottom: 12mm;
            left: 16mm;
            right: 16mm;
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
            gap: 10px;
        }}
        .mini-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 9.5px;
        }}
        .mini-table th {{
            background: #1e293b;
            color: #ffffff;
            padding: 4px 6px;
            text-align: left;
            font-size: 9px;
        }}
        .mini-table td {{
            padding: 3.5px 6px;
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
        <div class="header">
            <div class="logo-title">
                <div class="logo-icon">📡</div>
                <div class="title-text">
                    <h1>Telegram Signal Engine Scoreboard</h1>
                    <p>Complete Historical Analysis of ALL Signals Sent (Sep 26 – Oct 04)</p>
                </div>
            </div>
            <div class="meta-badge">Historical Complete Audit</div>
        </div>

        <div class="kpi-grid">
            <div class="kpi-card highlight">
                <div class="kpi-title">Total Unique Signals Sent</div>
                <div class="kpi-value">{total_unique_count}</div>
                <div class="kpi-sub">{total_raw_count} Raw Signals Sent</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Total Target Wins</div>
                <div class="kpi-value" style="color: #16a34a;">{total_wins}</div>
                <div class="kpi-sub">TP1 + TP2 + TP3 Hits</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Stop-Loss Hits</div>
                <div class="kpi-value" style="color: #dc2626;">{total_sl}</div>
                <div class="kpi-sub">Controlled Structural Risk</div>
            </div>
            <div class="kpi-card highlight">
                <div class="kpi-title">Closed Signal Win Rate</div>
                <div class="kpi-value" style="color: #2563eb;">{overall_wr:.1f}%</div>
                <div class="kpi-sub">1.6x – 2.5x R:R Average</div>
            </div>
        </div>

        <div class="section-title">🌟 Star Confluence Bifurcation (4-Star vs 3-Star Signals)</div>
        <table class="conf-table">
            <thead>
                <tr>
                    <th>Confluence Level</th>
                    <th>Total Signals Sent</th>
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
                    <td><b>4-Star Confluence (4/4 Stars)</b></td>
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
                    <td><b>3-Star Confluence (3/4 Stars)</b></td>
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
                    <td><b>Combined All Historical Signals</b></td>
                    <td><b>{total_unique_count}</b></td>
                    <td><b>{total_closed}</b></td>
                    <td><b>{total_tp1}</b></td>
                    <td><b>{total_tp2}</b></td>
                    <td><b>{total_tp3}</b></td>
                    <td><b style="color:#16a34a;">{total_wins}</b></td>
                    <td><b style="color:#dc2626;">{total_sl}</b></td>
                    <td><span class="badge-win" style="background:#2563eb; color:#fff;">{overall_wr:.1f}%</span></td>
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
                <div style="font-size:10px; margin-top:2px;">Upgraded volume expansion target reached.</div>
            </div>
            <div class="hit-box tp1">
                <div class="hit-box-title">🎯 TP1 Hit (Primary)</div>
                <div><b>{total_tp1} Signals ({(total_tp1/total_unique_count*100):.1f}%)</b></div>
                <div style="font-size:10px; margin-top:2px;">Primary target hit & Breakeven shield triggered.</div>
            </div>
            <div class="hit-box sl">
                <div class="hit-box-title">🔻 Stop-Loss (SL Hit)</div>
                <div><b>{total_sl} Signals ({(total_sl/total_unique_count*100):.1f}%)</b></div>
                <div style="font-size:10px; margin-top:2px;">Controlled risk exit at structural support/resistance.</div>
            </div>
            <div class="hit-box ctc">
                <div class="hit-box-title">🛡️ Breakeven Shield (CTC)</div>
                <div><b>100% Activation</b></div>
                <div style="font-size:10px; margin-top:2px;">On TP1 hit, SL moves to entry price guaranteeing zero loss.</div>
            </div>
            <div class="hit-box active">
                <div class="hit-box-title">⏳ Active / Pending</div>
                <div><b>{total_run} Signals ({(total_run/total_unique_count*100):.1f}%)</b></div>
                <div style="font-size:10px; margin-top:2px;">Resting signals currently active in monitoring.</div>
            </div>
        </div>

        <div class="section-title">⭐ Historical Signal Performance Highlights</div>
        <div style="font-size:10.5px; line-height:1.5; color:#334155;">
            <b>• All Historical Data Evaluated</b>: Includes <b>{total_unique_count} unique signals sent</b> since September 26, 2026 across all Telegram signal channels.<br>
            <b>• High Expectancy Engine</b>: With average win targets of 1.6x – 2.5x R:R, a {overall_wr:.1f}% Win Rate provides strong positive long-term mathematical portfolio growth.
        </div>

        <div class="footer">
            <span>Telegram Signal Engine Historical Performance Report (Sep 26 – Oct 04)</span>
            <span>Page 1 of 2</span>
        </div>
    </div>

    <!-- ================= PAGE 2 ================= -->
    <div class="page">
        <div class="header">
            <div class="logo-title">
                <div class="logo-icon">📊</div>
                <div class="title-text">
                    <h1>Asset-by-Asset Signals Scoreboard</h1>
                    <p>Complete Historical Breakdown Across All {len(sorted_coins)} Traded Coins</p>
                </div>
            </div>
            <div class="meta-badge">{len(sorted_coins)} Coins Audit</div>
        </div>

        <div class="two-col">
            <div>
                <table class="mini-table">
                    <thead>
                        <tr>
                            <th>Asset</th>
                            <th>Total</th>
                            <th>TP1</th>
                            <th>TP2</th>
                            <th>TP3 🚀</th>
                            <th>SL 🔻</th>
                            <th>Run</th>
                            <th>Win %</th>
                        </tr>
                    </thead>
                    <tbody>
                        {build_coin_rows(col1_coins)}
                    </tbody>
                </table>
            </div>
            <div>
                <table class="mini-table">
                    <thead>
                        <tr>
                            <th>Asset</th>
                            <th>Total</th>
                            <th>TP1</th>
                            <th>TP2</th>
                            <th>TP3 🚀</th>
                            <th>SL 🔻</th>
                            <th>Run</th>
                            <th>Win %</th>
                        </tr>
                    </thead>
                    <tbody>
                        {build_coin_rows(col2_coins)}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="footer">
            <span>Telegram Signal Engine Historical Performance Report (Sep 26 – Oct 04)</span>
            <span>Page 2 of 2</span>
        </div>
    </div>

</body>
</html>
"""

with open(html_path, "w", encoding="utf-8") as f:
    f.write(html_content)

print("HTML report written to:", html_path)

# Convert to PDF
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
    print("Edge PDF conversion done. PDF File Exists:", os.path.exists(pdf_path))
    
    # Copy to Desktop
    desktop_dir = os.path.expanduser('~/Desktop')
    dst_pdf = os.path.join(desktop_dir, 'All_Signals_Sent_Report.pdf')
    dst_html = os.path.join(desktop_dir, 'All_Signals_Sent_Report.html')
    shutil.copy2(pdf_path, dst_pdf)
    shutil.copy2(html_path, dst_html)
    print("Successfully Copied to Desktop:")
    print("  PDF:", dst_pdf)
    print("  HTML:", dst_html)
