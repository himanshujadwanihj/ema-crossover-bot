"""
analyze_hybrid_performance.py
Comprehensive analysis of the Hybrid Strategy with:
- 3-TP Step-Trailing SL: (40% @ TP1, 30% @ TP2, 30% @ TP3)
- Margin: Rs 1,500 INR (~$14.71 USDT) @ 10x leverage ($147.06 notional)
- Step Trail Rules:
  * TP1 Hit: 40% booked, SL moved to Entry (CTC) -> remaining 60% exits at $0 if pulled back
  * TP2 Hit: 30% booked, SL moved to TP1 -> remaining 30% exits at TP1
  * TP3 Hit: 30% booked -> Full Win
"""

import json
import os

MARGIN_INR = 1500.0
USD_INR = 102.0
MARGIN_USDT = MARGIN_INR / USD_INR  # ~$14.71
LEVERAGE = 10
NOTIONAL_USDT = MARGIN_USDT * LEVERAGE  # ~$147.06

data_file = "evaluated_since_yesterday.json"
if not os.path.exists(data_file):
    print("Data file not found.")
    exit(1)

with open(data_file, "r", encoding="utf-8") as f:
    data = json.load(f)

trades = data.get("trades", [])

total_trades = 0
tp1_only = 0
tp2_trail = 0
tp3_full_wins = 0
sl_losses = 0
running_trades = 0

total_profit_usdt = 0.0
total_loss_usdt = 0.0

trade_results = []

for t in trades:
    outcome = t.get("outcome", "")
    entry = float(t.get("entry") or 0.0)
    sl = float(t.get("sl") or 0.0)
    tp1 = float(t.get("tp1") or 0.0)
    tp2 = float(t.get("tp2") or 0.0)
    tp3 = float(t.get("tp3") or 0.0)
    direction = t.get("direction", "BUY")

    if entry <= 0 or sl <= 0 or sl == entry:
        continue

    total_trades += 1
    risk_pct = abs(entry - sl) / entry
    tp1_pct = abs(tp1 - entry) / entry if tp1 else risk_pct * 1.5
    tp2_pct = abs(tp2 - entry) / entry if tp2 else risk_pct * 2.5
    tp3_pct = abs(tp3 - entry) / entry if tp3 else risk_pct * 3.5

    if outcome == "TP3 HIT":
        tp3_full_wins += 1
        # 40% @ TP1, 30% @ TP2, 30% @ TP3
        pnl_pct = (0.40 * tp1_pct) + (0.30 * tp2_pct) + (0.30 * tp3_pct)
        pnl_usdt = pnl_pct * NOTIONAL_USDT
        total_profit_usdt += pnl_usdt
        trade_results.append(("WIN_TP3", pnl_usdt, pnl_pct * 100))

    elif outcome == "TP2 HIT":
        tp2_trail += 1
        # 40% @ TP1, 30% @ TP2, remaining 30% stopped at TP1 (Step Trailing SL)
        pnl_pct = (0.40 * tp1_pct) + (0.30 * tp2_pct) + (0.30 * tp1_pct)
        pnl_usdt = pnl_pct * NOTIONAL_USDT
        total_profit_usdt += pnl_usdt
        trade_results.append(("WIN_TP2", pnl_usdt, pnl_pct * 100))

    elif outcome == "TP1 HIT":
        tp1_only += 1
        # 40% @ TP1, remaining 60% stopped at CTC (Break-Even = $0)
        pnl_pct = (0.40 * tp1_pct) + (0.60 * 0.0)
        pnl_usdt = pnl_pct * NOTIONAL_USDT
        total_profit_usdt += pnl_usdt
        trade_results.append(("WIN_TP1_BE", pnl_usdt, pnl_pct * 100))

    elif outcome == "SL HIT":
        sl_losses += 1
        pnl_pct = -1.0 * risk_pct
        pnl_usdt = pnl_pct * NOTIONAL_USDT
        total_loss_usdt += abs(pnl_usdt)
        trade_results.append(("LOSS_SL", pnl_usdt, pnl_pct * 100))

    else:
        running_trades += 1
        cur_pnl_pct = float(t.get("cur_pnl") or 0.0) / 100.0
        pnl_usdt = cur_pnl_pct * NOTIONAL_USDT
        if pnl_usdt > 0:
            total_profit_usdt += pnl_usdt
        else:
            total_loss_usdt += abs(pnl_usdt)
        trade_results.append(("RUNNING", pnl_usdt, cur_pnl_pct * 100))

winning_trades_count = tp1_only + tp2_trail + tp3_full_wins
completed_trades = winning_trades_count + sl_losses

win_rate = (winning_trades_count / completed_trades) * 100.0 if completed_trades > 0 else 0.0
net_pnl_usdt = total_profit_usdt - total_loss_usdt
net_pnl_inr = net_pnl_usdt * USD_INR
profit_factor = (total_profit_usdt / total_loss_usdt) if total_loss_usdt > 0 else 999.0

print("=" * 75)
print("[*] HYBRID STRATEGY (3-TP STEP-TRAILING SL) PERFORMANCE REPORT")
print("=" * 75)
print(f"Total Evaluated Signals:    {total_trades}")
print(f"Completed Trades:           {completed_trades} (Active/Running: {running_trades})")
print("-" * 75)
print(f"[+] Total Winning Trades:   {winning_trades_count} ({win_rate:.1f}% Win Rate)")
print(f"   * TP1 Hit + CTC Protected: {tp1_only} trades (40% profit booked, 0 risk on rest)")
print(f"   * TP2 Hit + Trailed:       {tp2_trail} trades (70% profit booked + locked)")
print(f"   * TP3 Full Moonbag Win:    {tp3_full_wins} trades (100% full targets achieved)")
print(f"[-] Loss Trades (SL Hit):    {sl_losses} trades ({(sl_losses/completed_trades)*100.0:.1f}%)")
print("-" * 75)
print(f"FINANCIAL OUTCOME (Rs {MARGIN_INR:.0f} Margin @ {LEVERAGE}x Leverage per trade):")
print(f"   * Gross Profit:          +${total_profit_usdt:,.2f} USDT (+Rs {total_profit_usdt*USD_INR:,.0f} INR)")
print(f"   * Gross Loss:            -${total_loss_usdt:,.2f} USDT (-Rs {total_loss_usdt*USD_INR:,.0f} INR)")
print(f"   * Net Realized Profit:   +${net_pnl_usdt:,.2f} USDT (+Rs {net_pnl_inr:,.0f} INR)")
print(f"   * Profit Factor (PF):     {profit_factor:.2f}")
print(f"   * Avg Profit per Trade:  +${(net_pnl_usdt/completed_trades):.2f} USDT (+Rs {(net_pnl_inr/completed_trades):.1f} INR)")
print("=" * 75)
