import re
import json
import requests
from datetime import datetime, timezone, timedelta

raw_file = "raw_signals_since_yesterday.txt"
with open(raw_file, "r", encoding="utf-8") as f:
    lines = f.readlines()

p_signal = re.compile(
    r"\[([\w-]+)\]\s+([\d\-]+\s+[\d\:]+),\d+\s+\|\s+\w+\s+\|\s+\w+\s+\|\s+NEW SIGNAL:\s+(BUY|SELL)\s+([^\s]+)\s+(\w+)\s+\((.+)\)\s+\|\s+entry=([e\d\.\-]+)\s+sl=([e\d\.\-]+)\s+tp1=([e\d\.\-]+)\s+tp2=([e\d\.\-]+)\s+tp3=([e\d\.\-]+)"
)

p_retest = re.compile(
    r"\[([\w-]+)\]\s+([\d\-]+\s+[\d\:]+),\d+\s+\|\s+\w+\s+\|\s+pullback_tracker\s+\|\s+Sent\s+(EMA\s+\d+\s+(?:Support|Resistance)\s+Retest)\s+Alert\s+for\s+([A-Za-z0-9_\u4e00-\u9fff/]+)\s+(\w+)\s+(BULLISH|BEARISH)"
)

signals = []

for line in lines:
    m = p_signal.search(line)
    if m:
        dep, time_str, direction, symbol, tf, setup, entry, sl, tp1, tp2, tp3 = m.groups()
        signals.append({
            "type": "SIGNAL",
            "time_utc": time_str,
            "direction": direction,
            "symbol": symbol,
            "tf": tf,
            "setup": setup,
            "entry": float(entry),
            "sl": float(sl),
            "tp1": float(tp1),
            "tp2": float(tp2),
            "tp3": float(tp3),
        })
        continue
    
    m2 = p_retest.search(line)
    if m2:
        dep, time_str, setup, symbol, tf, bias = m2.groups()
        direction = "BUY" if bias == "BULLISH" else "SELL"
        signals.append({
            "type": "RETEST",
            "time_utc": time_str,
            "direction": direction,
            "symbol": symbol,
            "tf": tf,
            "setup": setup,
            "entry": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "tp3": None,
        })

print(f"Total raw signals parsed: {len(signals)}")

# Deduplicate identical setups within 60 minutes
unique_signals = []
for s in signals:
    s_dt = datetime.strptime(s["time_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    s["dt"] = s_dt
    
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    s["time_ist"] = s_dt.astimezone(ist_tz).strftime("%d-%b %H:%M IST")
    
    is_dup = False
    for u in unique_signals:
        if u["symbol"] == s["symbol"] and u["tf"] == s["tf"] and u["direction"] == s["direction"] and u["setup"] == s["setup"]:
            time_diff = abs((s_dt - u["dt"]).total_seconds())
            if time_diff < 3600:
                is_dup = True
                break
    if not is_dup:
        unique_signals.append(s)

print(f"Total unique trades: {len(unique_signals)}")

unique_signals.sort(key=lambda x: x["dt"])

session = requests.Session()

def fetch_klines(symbol, start_dt):
    clean_sym = symbol.replace("/", "")
    start_ms = int(start_dt.timestamp() * 1000)
    url = f"https://api.binance.com/api/v3/klines?symbol={clean_sym}&interval=5m&startTime={start_ms}&limit=1000"
    try:
        r = session.get(url, timeout=3.5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []

evaluated = []

for s in unique_signals:
    klines = fetch_klines(s["symbol"], s["dt"])
    entry = s["entry"]
    sl = s["sl"]
    tp1 = s["tp1"]
    tp2 = s["tp2"]
    tp3 = s["tp3"]
    direction = s["direction"]
    disp_name = s["symbol"]
            
    if not klines:
        evaluated.append({
            "time_ist": s["time_ist"],
            "symbol": disp_name,
            "tf": s["tf"],
            "direction": direction,
            "setup": s["setup"],
            "entry": entry or 0.0,
            "outcome": "UNAVAILABLE ON SPOT",
            "max_pnl": 0.0,
        })
        continue

    # If Retest had no entry, use first candle close/open
    if not entry:
        entry = float(klines[0][1])
        risk_pct = 0.012
        sl = entry * (1 - risk_pct) if direction == "BUY" else entry * (1 + risk_pct)
        tp1 = entry * (1 + 1.2 * risk_pct) if direction == "BUY" else entry * (1 - 1.2 * risk_pct)
        tp2 = entry * (1 + 2.2 * risk_pct) if direction == "BUY" else entry * (1 - 2.2 * risk_pct)
        tp3 = entry * (1 + 3.2 * risk_pct) if direction == "BUY" else entry * (1 - 3.2 * risk_pct)

    highest_price = entry
    lowest_price = entry
    cur_price = float(klines[-1][4])
    
    tp1_hit = False
    tp2_hit = False
    tp3_hit = False
    sl_hit = False
    
    for k in klines:
        k_high = float(k[2])
        k_low = float(k[3])
        if k_high > highest_price: highest_price = k_high
        if k_low < lowest_price: lowest_price = k_low
        
        if direction == "BUY":
            if k_low <= sl:
                sl_hit = True
                break
            if k_high >= tp1: tp1_hit = True
            if k_high >= tp2: tp2_hit = True
            if k_high >= tp3: tp3_hit = True
        else: # SELL
            if k_high >= sl:
                sl_hit = True
                break
            if k_low <= tp1: tp1_hit = True
            if k_low <= tp2: tp2_hit = True
            if k_low <= tp3: tp3_hit = True

    if direction == "BUY":
        max_pnl = ((highest_price - entry) / entry) * 100
        cur_pnl = ((cur_price - entry) / entry) * 100
    else:
        max_pnl = ((entry - lowest_price) / entry) * 100
        cur_pnl = ((entry - cur_price) / entry) * 100

    if tp3_hit:
        outcome = "TP3 HIT"
    elif tp2_hit:
        outcome = "TP2 HIT"
    elif tp1_hit:
        outcome = "TP1 HIT"
    elif sl_hit:
        outcome = "SL HIT"
    else:
        outcome = f"RUNNING (+{cur_pnl:.2f}%)" if cur_pnl > 0 else f"RUNNING ({cur_pnl:.2f}%)"

    evaluated.append({
        "time_ist": s["time_ist"],
        "symbol": disp_name,
        "tf": s["tf"],
        "direction": direction,
        "setup": s["setup"],
        "entry": round(entry, 6) if entry < 1 else round(entry, 2),
        "current_price": round(cur_price, 6) if cur_price < 1 else round(cur_price, 2),
        "max_pnl": round(max_pnl, 2),
        "cur_pnl": round(cur_pnl, 2),
        "outcome": outcome,
        "tp1": round(tp1, 6) if tp1 < 1 else round(tp1, 2),
        "tp2": round(tp2, 6) if tp2 < 1 else round(tp2, 2),
        "tp3": round(tp3, 6) if tp3 < 1 else round(tp3, 2),
        "sl": round(sl, 6) if sl < 1 else round(sl, 2)
    })

tp1_w = sum(1 for e in evaluated if e["outcome"] == "TP1 HIT")
tp2_w = sum(1 for e in evaluated if e["outcome"] == "TP2 HIT")
tp3_w = sum(1 for e in evaluated if e["outcome"] == "TP3 HIT")
wins = tp1_w + tp2_w + tp3_w
losses = sum(1 for e in evaluated if e["outcome"] == "SL HIT")
running = sum(1 for e in evaluated if "RUNNING" in e["outcome"])
closed = wins + losses
win_rate = (wins / closed * 100) if closed > 0 else 0.0

summary = {
    "total_signals": len(evaluated),
    "wins": wins,
    "tp1": tp1_w,
    "tp2": tp2_w,
    "tp3": tp3_w,
    "losses": losses,
    "running": running,
    "win_rate": round(win_rate, 1),
    "trades": evaluated
}

with open("evaluated_since_yesterday.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2)

print("\n================== PERFORMANCE SUMMARY ==================")
print(f"Total Unique Trades Evaluated: {len(evaluated)}")
print(f"Total Wins: {wins}")
print(f"  - TP1 Hits: {tp1_w}")
print(f"  - TP2 Hits: {tp2_w}")
print(f"  - TP3 Hits: {tp3_w}")
print(f"Stop Loss Hits: {losses}")
print(f"Running / Open / CTC: {running}")
print(f"Win Rate on Closed Trades: {win_rate:.1f}%")
print("==========================================================")
