import json
import os

data_path = "evaluated_since_yesterday.json"
if not os.path.exists(data_path):
    print("Data file not found!")
    exit(1)

with open(data_path, "r", encoding="utf-8") as f:
    data = json.load(f)

trades = data.get("trades", [])

margin_inr = 1500.0
usd_inr = 102.0
margin_usdt = margin_inr / usd_inr  # ~$14.71 USDT per trade @ 10x lev ($147.06 notional)
leverage = 10

pnl_3tp_usdt = 0.0
pnl_2tp_usdt = 0.0

tp1_hits = 0
tp2_hits = 0
tp3_hits = 0
sl_hits = 0

count_2tp_tp1_only = 0
count_2tp_tp2_full = 0
count_2tp_sl_loss = 0

for t in trades:
    outcome = t.get("outcome", "")
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp1 = t.get("tp1", 0.0)
    tp2 = t.get("tp2", 0.0)
    tp3 = t.get("tp3", 0.0)
    direction = t.get("direction", "BUY")

    if not entry or entry <= 0 or not sl or sl == entry:
        continue

    # Risk % and Reward %
    risk_pct = abs(entry - sl) / entry
    tp1_pct = abs(tp1 - entry) / entry if tp1 else risk_pct * 1.2
    tp2_pct = abs(tp2 - entry) / entry if tp2 else risk_pct * 2.0
    tp3_pct = abs(tp3 - entry) / entry if tp3 else risk_pct * 3.0

    notional = margin_usdt * leverage

    # --- 3-TP Plan (40% @ TP1, 30% @ TP2, 30% @ TP3) ---
    if outcome == "TP3 HIT":
        trade_3tp_pnl = (0.40 * tp1_pct + 0.30 * tp2_pct + 0.30 * tp3_pct) * notional
    elif outcome == "TP2 HIT":
        # TP1 hit (40%), TP2 hit (30%), SL trailed to TP1 for remaining 30%
        trade_3tp_pnl = (0.40 * tp1_pct + 0.30 * tp2_pct + 0.30 * tp1_pct) * notional
    elif outcome == "TP1 HIT":
        # TP1 hit (40%), remaining 60% trailed to CTC (break-even = $0)
        trade_3tp_pnl = (0.40 * tp1_pct + 0.60 * 0.0) * notional
    elif outcome == "SL HIT":
        trade_3tp_pnl = -1.0 * risk_pct * notional
    else:
        # Running / unconfirmed
        cur_pnl = t.get("cur_pnl", 0.0) / 100.0
        trade_3tp_pnl = cur_pnl * notional

    pnl_3tp_usdt += trade_3tp_pnl

    # --- 2-TP Plan (70% @ TP1, 30% @ TP2) ---
    if outcome in ("TP3 HIT", "TP2 HIT"):
        # Both TP1 (70%) and TP2 (30%) were hit -> Full Win 100% closed!
        trade_2tp_pnl = (0.70 * tp1_pct + 0.30 * tp2_pct) * notional
        count_2tp_tp2_full += 1
    elif outcome == "TP1 HIT":
        # TP1 hit (70%), remaining 30% trailed to CTC (break-even = $0)
        trade_2tp_pnl = (0.70 * tp1_pct + 0.30 * 0.0) * notional
        count_2tp_tp1_only += 1
    elif outcome == "SL HIT":
        trade_2tp_pnl = -1.0 * risk_pct * notional
        count_2tp_sl_loss += 1
    else:
        cur_pnl = t.get("cur_pnl", 0.0) / 100.0
        trade_2tp_pnl = cur_pnl * notional

    pnl_2tp_usdt += trade_2tp_pnl

total_evaluated = len([t for t in trades if t.get("entry")])
diff_usdt = pnl_2tp_usdt - pnl_3tp_usdt
diff_inr = diff_usdt * usd_inr

print("\n" + "=" * 75)
print("[BACKTEST COMPARISON REPORT: 3-TP PLAN VS 2-TP PLAN (70% / 30%)]")
print("=" * 75)
print(f"Total Evaluated Signals: {total_evaluated}")
print(f"Fixed Margin per Trade: Rs {margin_inr:.0f} INR (${margin_usdt:.2f} USDT @ {leverage}x Leverage)")
print("-" * 75)
print(f"[+] 3-TP PLAN (40% / 30% / 30% Split):")
print(f"   * Total Realized Profit:  ${pnl_3tp_usdt:,.2f} USDT  (Rs {pnl_3tp_usdt*usd_inr:,.0f} INR)")
print(f"   * Avg Return per Trade:  ${(pnl_3tp_usdt/total_evaluated):.2f} USDT")

print("-" * 75)
print(f"[+] NEW 2-TP PLAN (70% / 30% Split):")
print(f"   * Total Realized Profit:  ${pnl_2tp_usdt:,.2f} USDT  (Rs {pnl_2tp_usdt*usd_inr:,.0f} INR)")
print(f"   * Avg Return per Trade:  ${(pnl_2tp_usdt/total_evaluated):.2f} USDT")
print(f"   * TP1 Only Hits (70% Profit + CTC Shield): {count_2tp_tp1_only}")
print(f"   * Full Win (100% Closed at TP2):            {count_2tp_tp2_full}")

print("-" * 75)
print(f"[RESULT] PERFORMANCE COMPARISON:")
if diff_usdt >= 0:
    print(f"   >>> NEW 2-TP PLAN OUTPERFORMS BY: +${diff_usdt:,.2f} USDT (+Rs {diff_inr:,.0f} INR)")
else:
    print(f"   >>> 3-TP Plan vs 2-TP Plan Difference: ${diff_usdt:,.2f} USDT (Rs {diff_inr:,.0f} INR)")

print("=" * 75)
