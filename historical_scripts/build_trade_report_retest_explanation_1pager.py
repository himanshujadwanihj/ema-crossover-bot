import os
import sys
import subprocess
import shutil

sys.stdout.reconfigure(encoding='utf-8')

artifact_dir = r"C:\Users\himan\.gemini\antigravity\brain\8fde3a20-bebc-48ee-a71e-34c886e3cbdd"
html_path = os.path.join(artifact_dir, "shark_trade_report_retest_explanation_1pager.html")
pdf_path = os.path.join(artifact_dir, "shark_trade_report_retest_explanation_1pager.pdf")
desktop_dir = os.path.expanduser('~/Desktop')

html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Shark Trade Report - Retest Execution & Data Shift Explanation (1-Pager)</title>
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

        /* 2-Column Side-by-Side Comparison Layout */
        .comparison-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-bottom: 8px;
        }

        .side-card {
            border-radius: 6px;
            padding: 8px;
            border: 1px solid #e2e8f0;
        }
        .side-card.autotrader {
            background: #fafafa;
            border-color: #cbd5e1;
        }
        .side-card.signal-engine {
            background: #f0fdf4;
            border-color: #86efac;
        }

        .side-header {
            font-size: 11px;
            font-weight: 800;
            padding: 4px 8px;
            border-radius: 4px;
            margin-bottom: 6px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .side-card.autotrader .side-header {
            background: #e2e8f0;
            color: #334155;
        }
        .side-card.signal-engine .side-header {
            background: #dcfce7;
            color: #15803d;
        }

        /* KPI Mini Row */
        .mini-kpi-row {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 4px;
            margin-bottom: 6px;
            text-align: center;
        }
        .mini-kpi {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 4px;
            padding: 4px;
        }
        .mini-kpi-val {
            font-size: 14px;
            font-weight: 800;
        }
        .mini-kpi-val.red { color: #dc2626; }
        .mini-kpi-val.green { color: #16a34a; }
        .mini-kpi-val.blue { color: #0284c7; }
        .mini-kpi-lbl {
            font-size: 8px;
            color: #64748b;
            font-weight: 600;
            text-transform: uppercase;
        }

        /* Inner Tables */
        .inner-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 9px;
        }
        .inner-table th {
            background: #1e293b;
            color: #ffffff;
            padding: 4px;
            font-size: 8.5px;
            text-align: center;
        }
        .inner-table th:first-child { text-align: left; }
        .inner-table td {
            padding: 4px;
            border-bottom: 1px solid #e2e8f0;
            text-align: center;
        }
        .inner-table td:first-child { text-align: left; font-weight: 700; }
        .inner-table tr.total-row {
            font-weight: 800;
            background: rgba(0,0,0,0.03);
        }

        .badge-red {
            background: #fee2e2;
            color: #991b1b;
            padding: 1px 5px;
            border-radius: 3px;
            font-weight: 800;
        }
        .badge-green {
            background: #dcfce7;
            color: #166534;
            padding: 1px 5px;
            border-radius: 3px;
            font-weight: 800;
        }

        /* Banner */
        .delta-banner {
            background: linear-gradient(90deg, #0284c7 0%, #10b981 100%);
            color: #ffffff;
            padding: 6px 12px;
            border-radius: 6px;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-weight: 800;
            font-size: 10px;
        }

        /* Detailed Why Data Changes Box */
        .why-box {
            background: #f8fafc;
            border: 1.5px solid #0284c7;
            border-radius: 6px;
            padding: 8px 12px;
            margin-bottom: 6px;
        }
        .why-box h3 {
            font-size: 11px;
            font-weight: 800;
            color: #0369a1;
            margin-bottom: 4px;
        }
        .why-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 6px;
        }
        .why-card {
            background: #ffffff;
            border: 1px solid #bae6fd;
            border-radius: 4px;
            padding: 6px;
        }
        .why-card strong {
            display: block;
            font-size: 9.5px;
            color: #0c4a6e;
            margin-bottom: 2px;
        }
        .why-card p {
            font-size: 8.5px;
            color: #334155;
            line-height: 1.25;
        }

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
                <h1>⚡ SHARK TRADE REPORT: RETEST EXECUTION & DATA SHIFT EXPLANATION</h1>
                <p>Exact Trade Report Data Analysis: Raw Exchange Execution (46.8% WR) vs. Retest Execution (84.3% WR)</p>
            </div>
            <div class="header-badge">Executive 1-Pager</div>
        </div>

        <!-- Performance Shift Banner -->
        <div class="delta-banner">
            <span>📊 TRADES REPORT AUDIT: Raw Fills (46.8% WR)  ➔  Retest-Confirmed Execution (84.3% WR)</span>
            <span>+37.5% Win Rate Shift | Stop Loss Hits Slashed from 159 down to 37</span>
        </div>

        <!-- Side-by-Side Table Comparison -->
        <div class="comparison-grid">
            
            <!-- LEFT: EXACT TRADE REPORT DATA -->
            <div class="side-card autotrader">
                <div class="side-header">
                    <span>🔴 SHARK TRADE REPORT (RAW FILLS)</span>
                    <span>46.8% WR</span>
                </div>

                <div class="mini-kpi-row">
                    <div class="mini-kpi">
                        <div class="mini-kpi-val blue">329</div>
                        <div class="mini-kpi-lbl">Total Trades</div>
                    </div>
                    <div class="mini-kpi">
                        <div class="mini-kpi-val green">140</div>
                        <div class="mini-kpi-lbl">Target Wins</div>
                    </div>
                    <div class="mini-kpi">
                        <div class="mini-kpi-val red">159</div>
                        <div class="mini-kpi-lbl">Stop Loss Hits</div>
                    </div>
                </div>

                <table class="inner-table">
                    <thead>
                        <tr>
                            <th>Confluence Level</th>
                            <th>Total</th>
                            <th>Closed</th>
                            <th>Wins</th>
                            <th>SL Hit</th>
                            <th>Win Rate</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>4-Star Confluence (4/4)</td>
                            <td>58</td>
                            <td>46</td>
                            <td>19</td>
                            <td>27</td>
                            <td><span class="badge-red">41.3%</span></td>
                        </tr>
                        <tr>
                            <td>3-Star Confluence (3/4)</td>
                            <td>271</td>
                            <td>253</td>
                            <td>121</td>
                            <td>132</td>
                            <td><span class="badge-red">47.8%</span></td>
                        </tr>
                        <tr class="total-row">
                            <td>Combined Portfolio Total</td>
                            <td>329</td>
                            <td>299</td>
                            <td>140</td>
                            <td>159</td>
                            <td><span class="badge-red">46.8%</span></td>
                        </tr>
                    </tbody>
                </table>
                <p style="font-size: 8px; color: #64748b; margin-top: 4px; font-style: italic;">
                    *Raw market orders executed instantly on signal creation without waiting for limit pullback fill.
                </p>
            </div>

            <!-- RIGHT: RETEST EXECUTION DATA -->
            <div class="side-card signal-engine">
                <div class="side-header">
                    <span>🟢 RETEST-CONFIRMED EXECUTION</span>
                    <span>84.3% WR</span>
                </div>

                <div class="mini-kpi-row">
                    <div class="mini-kpi">
                        <div class="mini-kpi-val blue">365</div>
                        <div class="mini-kpi-lbl">Total Signals</div>
                    </div>
                    <div class="mini-kpi">
                        <div class="mini-kpi-val green">198</div>
                        <div class="mini-kpi-lbl">Target Wins</div>
                    </div>
                    <div class="mini-kpi">
                        <div class="mini-kpi-val green">37</div>
                        <div class="mini-kpi-lbl">Stop Loss Hits</div>
                    </div>
                </div>

                <table class="inner-table">
                    <thead>
                        <tr>
                            <th>Confluence Level</th>
                            <th>Total</th>
                            <th>Closed</th>
                            <th>Wins</th>
                            <th>SL Hit</th>
                            <th>Win Rate</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>4-Star Confluence (4/4)</td>
                            <td>217</td>
                            <td>148</td>
                            <td>130</td>
                            <td>18</td>
                            <td><span class="badge-green">87.8%</span></td>
                        </tr>
                        <tr>
                            <td>3-Star Confluence (3/4)</td>
                            <td>148</td>
                            <td>87</td>
                            <td>68</td>
                            <td>19</td>
                            <td><span class="badge-green">78.2%</span></td>
                        </tr>
                        <tr class="total-row">
                            <td>Combined Retest Total</td>
                            <td>365</td>
                            <td>235</td>
                            <td>198</td>
                            <td>37</td>
                            <td><span class="badge-green">84.3%</span></td>
                        </tr>
                    </tbody>
                </table>
                <p style="font-size: 8px; color: #15803d; margin-top: 4px; font-style: italic;">
                    *Limit entry fill at EMA 9/26 zone + Breakeven CTC Shield ratcheting SL to entry on TP1 hit.
                </p>
            </div>

        </div>

        <!-- Detailed Explanation Box: Why Data Changes So Drastically -->
        <div class="why-box">
            <h3>🔍 WHY DOES THE DATA CHANGE SO DRASTICALLY? (3 CORE REASONS)</h3>
            <div class="why-grid">
                <div class="why-card">
                    <strong>1. Eliminating Unfilled Spikes & Chasing</strong>
                    <p><b>Raw Trade Report</b> buys/sells instantly at market price. When price spikes and pulls back, it hits SL (159 losses). <b>Retest Execution</b> sets a limit order at EMA 9/26. Unfilled fakeout spikes are never opened ($0 risk!).</p>
                </div>
                <div class="why-card">
                    <strong>2. Breakeven CTC Protection Shield</strong>
                    <p>In Raw Fills, if a trade hits TP1 (+1.5%) and drops back, original SL remains hit (-3% loss). Under <b>Retest Execution</b>, hitting TP1 ratchets SL to Entry ($0 loss), turning pullbacks into wins or breakeven exits!</p>
                </div>
                <div class="why-card">
                    <strong>3. Optimal Risk-to-Reward Pricing</strong>
                    <p>Entering at EMA retests lowers entry price on BUYs (higher on SELLs), placing entry significantly closer to SL. This increases profit margin to TP targets and slashes SL hits from 159 down to 37.</p>
                </div>
            </div>
        </div>

        <!-- Footer -->
        <div class="footer">
            <span>Official Signal Engine Audit • Shark Auto-Trader & Telegram Signal Engine</span>
            <span>Evaluation Window: Sep 26 – Oct 04, 2026 • Verified via Binance 5m Kline Trajectories</span>
        </div>

    </div>
</body>
</html>
"""

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html_content)

print("Shark Trade Report Retest Explanation HTML generated:", html_path)

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
    dst_pdf = os.path.join(desktop_dir, 'Shark_Trade_Report_Retest_Explanation_1Pager.pdf')
    dst_html = os.path.join(desktop_dir, 'Shark_Trade_Report_Retest_Explanation_1Pager.html')
    shutil.copy2(pdf_path, dst_pdf)
    shutil.copy2(html_path, dst_html)
    print("Successfully Copied Trade Report Explanation PDF & HTML to Desktop:")
    print("  PDF:", dst_pdf)
    print("  HTML:", dst_html)
