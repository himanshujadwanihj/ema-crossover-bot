import os
import sys
import subprocess
import shutil

sys.stdout.reconfigure(encoding='utf-8')

artifact_dir = r"C:\Users\himan\.gemini\antigravity\brain\8fde3a20-bebc-48ee-a71e-34c886e3cbdd"
html_path = os.path.join(artifact_dir, "master_signal_vs_autotrader_1pager.html")
pdf_path = os.path.join(artifact_dir, "master_signal_vs_autotrader_1pager.pdf")
desktop_dir = os.path.expanduser('~/Desktop')

html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Master Performance Audit: Shark Auto-Trader vs. Signal Engine Retest Execution</title>
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

        /* Middle Delta Box */
        .delta-banner {
            background: linear-gradient(90deg, #38bdf8 0%, #10b981 100%);
            color: #ffffff;
            padding: 6px 12px;
            border-radius: 6px;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-weight: 800;
            font-size: 10.5px;
        }

        /* Takeaways Grid */
        .takeaway-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 6px;
            margin-bottom: 6px;
        }
        .takeaway-card {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-left: 3px solid #0284c7;
            border-radius: 4px;
            padding: 6px 8px;
        }
        .takeaway-card strong {
            font-size: 9.5px;
            color: #0f172a;
            display: block;
            margin-bottom: 2px;
        }
        .takeaway-card p {
            font-size: 8.5px;
            color: #475569;
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
                <h1>⚡ MASTER EXECUTIVE COMPARISON: AUTO-TRADER VS SIGNAL ENGINE</h1>
                <p>Side-by-Side Performance Study: Shark Raw Auto-Trader Fills vs. Signal Engine Retest Execution</p>
            </div>
            <div class="header-badge">Master 1-Pager Audit</div>
        </div>

        <!-- Performance Delta Banner -->
        <div class="delta-banner">
            <span>🚀 PERFORMANCE SHIFT: Raw Market Fills (46.8% WR)  ➔  Optimized Retest Execution (84.3% WR)</span>
            <span>+37.5% Win Rate Gain | -76.7% SL Hit Reduction</span>
        </div>

        <!-- 2 Column Comparison Grid -->
        <div class="comparison-grid">
            
            <!-- LEFT COLUMN: SHARK AUTO-TRADER (RAW MARKET EXECUTION) -->
            <div class="side-card autotrader">
                <div class="side-header">
                    <span>🔴 SHARK AUTO-TRADER (RAW EXECUTION)</span>
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
                            <th>Closed</th>
                            <th>Wins</th>
                            <th>SL Hit</th>
                            <th>Win Rate</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>4-Star Confluence (4/4)</td>
                            <td>46</td>
                            <td>19</td>
                            <td>27</td>
                            <td><span class="badge-red">41.3%</span></td>
                        </tr>
                        <tr>
                            <td>3-Star Confluence (3/4)</td>
                            <td>253</td>
                            <td>121</td>
                            <td>132</td>
                            <td><span class="badge-red">47.8%</span></td>
                        </tr>
                        <tr class="total-row">
                            <td>Combined Auto-Trader Total</td>
                            <td>299</td>
                            <td>140</td>
                            <td>159</td>
                            <td><span class="badge-red">46.8%</span></td>
                        </tr>
                    </tbody>
                </table>
                <p style="font-size: 8px; color: #64748b; margin-top: 4px; font-style: italic;">
                    *Raw exchange market entries executed immediately upon signal generation without waiting for pullback touch.
                </p>
            </div>

            <!-- RIGHT COLUMN: SIGNAL ENGINE (OPTIMIZED RETEST EXECUTION) -->
            <div class="side-card signal-engine">
                <div class="side-header">
                    <span>🟢 SIGNAL ENGINE (RETEST EXECUTION)</span>
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
                            <th>Closed</th>
                            <th>Wins</th>
                            <th>SL Hit</th>
                            <th>Win Rate</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>4-Star Confluence (4/4)</td>
                            <td>148</td>
                            <td>130</td>
                            <td>18</td>
                            <td><span class="badge-green">87.8%</span></td>
                        </tr>
                        <tr>
                            <td>3-Star Confluence (3/4)</td>
                            <td>87</td>
                            <td>68</td>
                            <td>19</td>
                            <td><span class="badge-green">78.2%</span></td>
                        </tr>
                        <tr class="total-row">
                            <td>Combined Signal Engine Total</td>
                            <td>235</td>
                            <td>198</td>
                            <td>37</td>
                            <td><span class="badge-green">84.3%</span></td>
                        </tr>
                    </tbody>
                </table>
                <p style="font-size: 8px; color: #15803d; margin-top: 4px; font-style: italic;">
                    *Optimized limit retest entry fill at EMA 9/26 zone + Breakeven CTC Shield ratcheting SL to entry on TP1.
                </p>
            </div>

        </div>

        <!-- Core Takeaways Grid -->
        <div class="takeaway-grid">
            <div class="takeaway-card">
                <strong>1. Elimination of Market Chasing</strong>
                <p>Entering immediately on alert generation exposes trades to immediate pullback noise. Waiting for EMA limit price touch ensures optimal R:R entries.</p>
            </div>
            <div class="takeaway-card">
                <strong>2. Breakeven CTC Protection Shield</strong>
                <p>Ratcheting Stop Loss to Entry ($0 risk) upon reaching TP1 turns potential pullback losses into zero-loss breakeven exits.</p>
            </div>
            <div class="takeaway-card">
                <strong>3. 4-Star High Confluence Edge</strong>
                <p>4-Star multi-timeframe signals achieve an elite <b>87.8% Win Rate</b> under retest execution, with 70 full TP3 target expansions.</p>
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

print("Master 1-Pager HTML generated:", html_path)

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
    dst_pdf = os.path.join(desktop_dir, 'Master_Signal_Vs_AutoTrader_1Pager.pdf')
    dst_html = os.path.join(desktop_dir, 'Master_Signal_Vs_AutoTrader_1Pager.html')
    shutil.copy2(pdf_path, dst_pdf)
    shutil.copy2(html_path, dst_html)
    print("Successfully Copied Master 1-Pager PDF & HTML to Desktop:")
    print("  PDF:", dst_pdf)
    print("  HTML:", dst_html)
