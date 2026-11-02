import json

with open("evaluated_since_yesterday.json", "r", encoding="utf-8") as f:
    data = json.load(f)

trades = data.get("trades", [])
margin_usdt = 1500.0 / 102.0
leverage = 10
notional = margin_usdt * leverage
usd_inr = 102.0

tp1_hitting_trades = [t for t in trades if t.get("outcome") in ("TP1 HIT", "TP2 HIT", "TP3 HIT")]

pnl_tp1_old_3tp = 0.0
pnl_tp1_new_2tp = 0.0

for t in tp1_hitting_trades:
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp1 = t.get("tp1", 0.0)
    if not entry or entry <= 0: continue
    risk = abs(entry - sl) / entry
    t1 = abs(tp1 - entry) / entry if tp1 else risk * 1.2

    # Old 3-TP (40% at TP1)
    pnl_tp1_old_3tp += (0.40 * t1) * notional

    # New 2-TP (70% at TP1)
    pnl_tp1_new_2tp += (0.70 * t1) * notional

diff_usdt = pnl_tp1_new_2tp - pnl_tp1_old_3tp

print(f"Total Trades Hitting TP1: {len(tp1_hitting_trades)}")
print(f"Old 3-TP Plan (40% at TP1):  ${pnl_tp1_old_3tp:.2f} USDT (Rs {pnl_tp1_old_3tp*usd_inr:,.0f} INR)")
print(f"New 2-TP Plan (70% at TP1):  ${pnl_tp1_new_2tp:.2f} USDT (Rs {pnl_tp1_new_2tp*usd_inr:,.0f} INR)")
print(f"Difference (2-TP Extra at TP1): +${diff_usdt:.2f} USDT (+Rs {diff_usdt*usd_inr:,.0f} INR)")
