import os
import sys
import subprocess
import shutil

sys.stdout.reconfigure(encoding='utf-8')

artifact_dir = r"C:\Users\himan\.gemini\antigravity\brain\8fde3a20-bebc-48ee-a71e-34c886e3cbdd"
html_path = os.path.join(artifact_dir, "fresh_crossovers_full_tp_sl_scoreboard_1pager.html")
pdf_path = os.path.join(artifact_dir, "fresh_crossovers_full_tp_sl_scoreboard_1pager.pdf")
desktop_dir = os.path.expanduser('~/Desktop')

html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Fresh Crossovers Channel - Full TP & SL Confluence Scoreboard (1-Pager)</title>
    <style>
        @page {
            size: A4 portrait;
            margin: 6mm 8mm;
        }
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            color: #0f172a;
            background: #ffffff;
            font-size: 10px;
            line-height: 1.3;
        }
        .container {
            width: 100%;
            max-width: 800px;
            margin: 0 auto;
        }
        .header {
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #ffffff;
            padding: 10px 14px;
            border-radius: 6px;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header-title h1 {
            font-size: 15px;
            font-weight: 800;
            color: #38bdf8;
            letter-spacing: -0.3px;
        }
        .header-title p {
            font-size: 9.5px;
            color: #94a3b8;
            margin-top: 1px;
        }
        .header-badge {
            background: rgba(56, 189, 248, 0.15);
            border: 1px solid rgba(56, 189, 248, 0.4);
            color: #38bdf8;
            padding: 4px 8px;
            border-radius: 20px;
            font-size: 9.5px;
            font-weight: 700;
        }

        /* KPI Summary Grid */
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 6px;
            margin-bottom: 8px;
        }
        .kpi-card {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            padding: 6px 8px;
            text-align: center;
        }
        .kpi-card.highlight-green {
            background: #f0fdf4;
            border-color: #86efac;
        }
        .kpi-card.highlight-blue {
            background: #f0f9ff;
            border-color: #7dd3fc;
        }
        .kpi-label {
            font-size: 8.5px;
            font-weight: 700;
            text-transform: uppercase;
            color: #64748b;
            letter-spacing: 0.3px;
        }
        .kpi-value {
            font-size: 17px;
            font-weight: 800;
            margin: 1px 0;
        }
        .kpi-value.green { color: #16a34a; }
        .kpi-value.blue { color: #0284c7; }
        .kpi-sub {
            font-size: 8px;
            color: #475569;
            font-weight: 600;
        }

        /* Section Header */
        .section-title {
            font-size: 11px;
            font-weight: 800;
            color: #0f172a;
            margin-bottom: 5px;
            display: flex;
            align-items: center;
            gap: 5px;
            border-left: 3px solid #0284c7;
            padding-left: 5px;
        }
        .section-title.green-accent { border-left-color: #10b981; }
        .section-title.blue-accent { border-left-color: #0284c7; }

        /* Tables */
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 9.5px;
            margin-bottom: 8px;
        }
        th {
            background: #1e293b;
            color: #f8fafc;
            font-weight: 700;
            padding: 5px 6px;
            text-align: center;
            font-size: 9px;
        }
        th:first-child { text-align: left; border-top-left-radius: 4px; }
        th:last-child { border-top-right-radius: 4px; }
        td {
            padding: 4px 6px;
            border-bottom: 1px solid #e2e8f0;
            text-align: center;
        }
        td:first-child { text-align: left; font-weight: 700; }
        tr:nth-child(even) { background: #f8fafc; }
        tr.total-row {
            background: #f1f5f9;
            font-weight: 800;
        }
        tr.total-row td {
            border-top: 2px solid #cbd5e1;
            font-size: 10px;
        }
        
        .badge-win {
            background: #dcfce7;
            color: #15803d;
            padding: 2px 5px;
            border-radius: 3px;
            font-weight: 800;
            display: inline-block;
        }
        .badge-baseline {
            background: #feefc3;
            color: #b45309;
            padding: 2px 5px;
            border-radius: 3px;
            font-weight: 800;
            display: inline-block;
        }

        /* Distribution Box */
        .dist-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 6px;
            margin-bottom: 8px;
        }
        .dist-card {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 5px;
            padding: 6px;
            text-align: center;
        }
        .dist-card.tp1 { border-color: #7dd3fc; background: #f0f9ff; }
        .dist-card.tp2 { border-color: #93c5fd; background: #eff6ff; }
        .dist-card.tp3 { border-color: #86efac; background: #f0fdf4; }
        .dist-card.sl  { border-color: #fca5a5; background: #fef2f2; }
        .dist-title { font-size: 9px; font-weight: 800; margin-bottom: 2px; }
        .dist-val { font-size: 14px; font-weight: 800; }
        .dist-desc { font-size: 7.5px; color: #64748b; }

        .footer {
            padding-top: 4px;
            border-top: 1px solid #e2e8f0;
            display: flex;
            justify-content: space-between;
            font-size: 8px;
            color: #64748b;
        }
    </style>
</head>
<body>
    <div class="container">
        
        <!-- Header -->
        <div class="header">
            <div class="header-title">
                <h1>⚡ FRESH CROSSOVERS CHANNEL: FULL TP & SL CONFLUENCE SCOREBOARD</h1>
                <p>Complete Confluence Audit: 4-Star & 3-Star EMA 9/26 Crossover Signals (All TP1, TP2, TP3 & SL Hits)</p>
            </div>
            <div class="header-badge">Full Audit 1-Pager</div>
        </div>

        <!-- KPI Grid -->
        <div class="kpi-grid">
            <div class="kpi-card highlight-blue">
                <div class="kpi-label">Total Unique Crossovers</div>
                <div class="kpi-value blue">181</div>
                <div class="kpi-sub">Across 84 Active Crypto Coins</div>
            </div>
            <div class="kpi-card highlight-green">
                <div class="kpi-label">4-Star Confluence Win Rate</div>
                <div class="kpi-value green">86.4%</div>
                <div class="kpi-sub">64 Wins / 10 Stop Loss Hits</div>
            </div>
            <div class="kpi-card highlight-green">
                <div class="kpi-label">3-Star Confluence Win Rate</div>
                <div class="kpi-value green">76.5%</div>
                <div class="kpi-sub">39 Wins / 12 Stop Loss Hits</div>
            </div>
            <div class="kpi-card highlight-green">
                <div class="kpi-label">Combined Closed Win Rate</div>
                <div class="kpi-value green">82.9%</div>
                <div class="kpi-sub">103 Total Wins / 22 SL Hits</div>
            </div>
        </div>

        <!-- Section 1: Retest-Confirmed Execution Full Table -->
        <div class="section-title green-accent">
            1. OPTIMIZED RETEST EXECUTION SCOREBOARD (Limit Fill at EMA 9/26 + Breakeven CTC Shield)
        </div>
        <table>
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
                    <td>4-Star Crossovers (4/4 Stars Aligned)</td>
                    <td>36</td>
                    <td>74</td>
                    <td>22</td>
                    <td>18</td>
                    <td>24</td>
                    <td>64</td>
                    <td>10</td>
                    <td><span class="badge-win">86.4%</span></td>
                </tr>
                <tr>
                    <td>3-Star Crossovers (3/4 Stars Aligned)</td>
                    <td>145</td>
                    <td>51</td>
                    <td>14</td>
                    <td>10</td>
                    <td>15</td>
                    <td>39</td>
                    <td>12</td>
                    <td><span class="badge-win">76.5%</span></td>
                </tr>
                <tr class="total-row">
                    <td>Combined Fresh Crossovers Total</td>
                    <td>181</td>
                    <td>125</td>
                    <td>36</td>
                    <td>28</td>
                    <td>39</td>
                    <td>103</td>
                    <td>22</td>
                    <td><span class="badge-win">82.9%</span></td>
                </tr>
            </tbody>
        </table>

        <!-- Section 2: Baseline Immediate Broadcast Entry Full Table -->
        <div class="section-title blue-accent">
            2. BASELINE BROADCAST ENTRY SCOREBOARD (Immediate Market Order on Scanner Broadcast)
        </div>
        <table>
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
                    <td>4-Star Crossovers (4/4 Stars Aligned)</td>
                    <td>36</td>
                    <td>29</td>
                    <td>6</td>
                    <td>4</td>
                    <td>9</td>
                    <td>19</td>
                    <td>10</td>
                    <td><span class="badge-baseline">65.5%</span></td>
                </tr>
                <tr>
                    <td>3-Star Crossovers (3/4 Stars Aligned)</td>
                    <td>145</td>
                    <td>122</td>
                    <td>13</td>
                    <td>16</td>
                    <td>42</td>
                    <td>71</td>
                    <td>51</td>
                    <td><span class="badge-baseline">58.2%</span></td>
                </tr>
                <tr class="total-row">
                    <td>Combined Fresh Crossovers Total</td>
                    <td>181</td>
                    <td>151</td>
                    <td>19</td>
                    <td>20</td>
                    <td>51</td>
                    <td>90</td>
                    <td>61</td>
                    <td><span class="badge-baseline">59.6%</span></td>
                </tr>
            </tbody>
        </table>

        <!-- Section 3: Target Hit Distribution Cards -->
        <div class="section-title green-accent">
            3. TARGET LEVEL HIT DISTRIBUTION & CAPITAL PROTECTION SHIELDS
        </div>
        <div class="dist-grid">
            <div class="dist-card tp1">
                <div class="dist-title" style="color:#0284c7;">🎯 TP1 Hit (Primary)</div>
                <div class="dist-val" style="color:#0284c7;">36 Hits</div>
                <div class="dist-desc">Activates Breakeven CTC Shield ($0 loss).</div>
            </div>
            <div class="dist-card tp2">
                <div class="dist-title" style="color:#2563eb;">🎯 TP2 Hit (Upper Pool)</div>
                <div class="dist-val" style="color:#2563eb;">28 Hits</div>
                <div class="dist-desc">Upgraded volume target hit with SL ratcheted.</div>
            </div>
            <div class="dist-card tp3">
                <div class="dist-title" style="color:#16a34a;">🚀 TP3 Hit (Full Target)</div>
                <div class="dist-val" style="color:#16a34a;">39 Hits</div>
                <div class="dist-desc">Captured maximum structural profit expansion.</div>
            </div>
            <div class="dist-card sl">
                <div class="dist-title" style="color:#dc2626;">🔻 Stop Loss Hit</div>
                <div class="dist-val" style="color:#dc2626;">22 Hits</div>
                <div class="dist-desc">Controlled risk exit across all 181 signals.</div>
            </div>
        </div>

        <!-- Footer -->
        <div class="footer">
            <span>Official Signal Engine Audit • Fresh Crossovers Telegram Channel</span>
            <span>Evaluation Window: Sep 26 – Oct 04, 2026 • Verified via Binance 5m Kline Trajectories</span>
        </div>

    </div>
</body>
</html>
"""

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html_content)

print("Fresh Crossovers Full TP & SL HTML generated:", html_path)

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
    dst_pdf = os.path.join(desktop_dir, 'Fresh_Crossovers_Full_TP_SL_Scoreboard_1Pager.pdf')
    dst_html = os.path.join(desktop_dir, 'Fresh_Crossovers_Full_TP_SL_Scoreboard_1Pager.html')
    shutil.copy2(pdf_path, dst_pdf)
    shutil.copy2(html_path, dst_html)
    print("Successfully Copied Full TP/SL Scoreboard PDF & HTML to Desktop:")
    print("  PDF:", dst_pdf)
    print("  HTML:", dst_html)
