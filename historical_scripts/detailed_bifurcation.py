import json

with open("evaluated_since_yesterday.json", "r", encoding="utf-8") as f:
    data = json.load(f)

trades = data.get("trades", [])

margin_usdt = 1500.0 / 102.0  # ~$14.71 USDT
leverage = 10
notional = margin_usdt * leverage

cat_tp1_only = [t for t in trades if t.get("outcome") == "TP1 HIT"]
cat_tp2_hit = [t for t in trades if t.get("outcome") == "TP2 HIT"]
cat_tp3_hit = [t for t in trades if t.get("outcome") == "TP3 HIT"]
cat_sl_hit = [t for t in trades if t.get("outcome") == "SL HIT"]

print(f"Total Trades Evaluated: {len(trades)}")
print(f"  • Category 1 (TP1 Only -> CTC): {len(cat_tp1_only)}")
print(f"  • Category 2 (TP2 Hit):          {len(cat_tp2_hit)}")
print(f"  • Category 3 (TP3 Full Runner):  {len(cat_tp3_hit)}")
print(f"  • Category 4 (SL Loss):          {len(cat_sl_hit)}")

# 3-TP PnL calculations per category
def calc_3tp(t):
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp1 = t.get("tp1", 0.0)
    tp2 = t.get("tp2", 0.0)
    tp3 = t.get("tp3", 0.0)
    outcome = t.get("outcome", "")
    if not entry or entry <= 0: return 0.0
    risk = abs(entry - sl) / entry
    t1 = abs(tp1 - entry) / entry if tp1 else risk * 1.2
    t2 = abs(tp2 - entry) / entry if tp2 else risk * 2.0
    t3 = abs(tp3 - entry) / entry if tp3 else risk * 3.0
    if outcome == "TP3 HIT": return (0.40 * t1 + 0.30 * t2 + 0.30 * t3) * notional
    if outcome == "TP2 HIT": return (0.40 * t1 + 0.30 * t2 + 0.30 * t1) * notional
    if outcome == "TP1 HIT": return (0.40 * t1 + 0.60 * 0.0) * notional
    if outcome == "SL HIT": return -1.0 * risk * notional
    return 0.0

# 2-TP PnL calculations per category
def calc_2tp(t):
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp1 = t.get("tp1", 0.0)
    tp2 = t.get("tp2", 0.0)
    outcome = t.get("outcome", "")
    if not entry or entry <= 0: return 0.0
    risk = abs(entry - sl) / entry
    t1 = abs(tp1 - entry) / entry if tp1 else risk * 1.2
    t2 = abs(tp2 - entry) / entry if tp2 else risk * 2.0
    if outcome in ("TP3 HIT", "TP2 HIT"): return (0.70 * t1 + 0.30 * t2) * notional
    if outcome == "TP1 HIT": return (0.70 * t1 + 0.30 * 0.0) * notional
    if outcome == "SL HIT": return -1.0 * risk * notional
    return 0.0

p3_cat1 = sum(calc_3tp(t) for t in cat_tp1_only)
p2_cat1 = sum(calc_2tp(t) for t in cat_tp1_only)

p3_cat2 = sum(calc_3tp(t) for t in cat_tp2_hit)
p2_cat2 = sum(calc_2tp(t) for t in cat_tp2_hit)

p3_cat3 = sum(calc_3tp(t) for t in cat_tp3_hit)
p2_cat3 = sum(calc_2tp(t) for t in cat_tp3_hit)

p3_sl = sum(calc_3tp(t) for t in cat_sl_hit)
p2_sl = sum(calc_2tp(t) for t in cat_sl_hit)

print("\n--- BIFURCATION SUMMARY ---")
print(f"Category 1 (TP1 Only): 3-TP PnL = +${p3_cat1:.2f} USDT | 2-TP PnL = +${p2_cat1:.2f} USDT (2-TP gain: +${p2_cat1 - p3_cat1:.2f})")
print(f"Category 2 (TP2 Hit):  3-TP PnL = +${p3_cat2:.2f} USDT | 2-TP PnL = +${p2_cat2:.2f} USDT (2-TP gain: +${p2_cat2 - p3_cat2:.2f})")
print(f"Category 3 (TP3 Hit):  3-TP PnL = +${p3_cat3:.2f} USDT | 2-TP PnL = +${p2_cat3:.2f} USDT (3-TP gain: +${p3_cat3 - p2_cat3:.2f})")
print(f"Category 4 (SL Loss):  3-TP Loss = -${abs(p3_sl):.2f} USDT | 2-TP Loss = -${abs(p2_sl):.2f} USDT")
