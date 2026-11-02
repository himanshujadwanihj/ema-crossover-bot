import requests
import pandas as pd
import numpy as np
import sys

sys.stdout.reconfigure(encoding='utf-8')

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT",
    "NEARUSDT", "DOTUSDT", "PEPEUSDT", "APTUSDT", "TAOUSDT"
]

session = requests.Session()

def fetch_klines(symbol, interval, limit=1000):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        r = session.get(url, timeout=12)
        if r.status_code == 200:
            df = pd.DataFrame(r.json(), columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "q_vol", "trades", "tb_vol", "tb_q_vol", "ignore"
            ])
            df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df.set_index("time", inplace=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df
    except Exception:
        pass
    return None

def compute_indicators(df):
    df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_26"] = df["close"].ewm(span=26, adjust=False).mean()
    df["vol_sma"] = df["volume"].rolling(20).mean()
    
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df

print("Analyzing 1H Retests alongside sub-candle 30M & 15M behavior across 15 symbols...")

records = []

for sym in SYMBOLS:
    df_1h = fetch_klines(sym, "1h", limit=1000)
    df_30m = fetch_klines(sym, "30m", limit=1000)
    df_15m = fetch_klines(sym, "15m", limit=1000)

    if df_1h is None or df_30m is None or df_15m is None:
        continue

    df_1h = compute_indicators(df_1h)
    df_30m = compute_indicators(df_30m)
    df_15m = compute_indicators(df_15m)

    touch_threshold = 0.0075

    for i in range(30, len(df_1h) - 24):
        row_1h = df_1h.iloc[i]
        prev_1h = df_1h.iloc[i-1]
        t_1h = df_1h.index[i]

        ema9_1h = row_1h["ema_9"]
        ema26_1h = row_1h["ema_26"]
        trend = "BULLISH" if ema9_1h > ema26_1h else "BEARISH"

        tapped_26 = (row_1h["low"] <= ema26_1h <= row_1h["high"]) and (abs(row_1h["close"] - ema26_1h) / ema26_1h <= touch_threshold)
        tapped_9 = (row_1h["low"] <= ema9_1h <= row_1h["high"]) and (abs(row_1h["close"] - ema9_1h) / ema9_1h <= touch_threshold)

        if not (tapped_26 or tapped_9):
            continue

        target_ema = ema26_1h if tapped_26 else ema9_1h

        if trend == "BULLISH" and prev_1h["close"] >= target_ema * 0.998:
            direction = "BUY"
        elif trend == "BEARISH" and prev_1h["close"] <= target_ema * 1.002:
            direction = "SELL"
        else:
            continue

        entry = row_1h["close"]
        atr = row_1h["atr"] if not np.isnan(row_1h["atr"]) else entry * 0.015

        if direction == "BUY":
            sl = min(row_1h["low"] * 0.998, entry - 1.5 * atr)
            risk = entry - sl
            tp1 = entry + 1.2 * risk
        else:
            sl = max(row_1h["high"] * 1.002, entry + 1.5 * atr)
            risk = sl - entry
            tp1 = entry - 1.2 * risk

        if risk <= 0: continue

        # Forward outcome
        window = df_1h.iloc[i+1 : i+25]
        sl_hit = False
        tp1_hit = False

        for w_idx in range(len(window)):
            w = window.iloc[w_idx]
            if direction == "BUY":
                if w["low"] <= sl:
                    sl_hit = True
                    break
                if w["high"] >= tp1:
                    tp1_hit = True
                    break
            else:
                if w["high"] >= sl:
                    sl_hit = True
                    break
                if w["low"] <= tp1:
                    tp1_hit = True
                    break

        is_fail = sl_hit or (not tp1_hit)
        is_success = tp1_hit and not sl_hit

        # --- SUB-CANDLE TRAITS & SIGNALS (Inside this 1H candle) ---
        # Get 30m candles inside / leading up to this 1h candle
        t_start = t_1h
        t_end = t_1h + pd.Timedelta(hours=1)
        sub_30m = df_30m[(df_30m.index >= t_start) & (df_30m.index < t_end)]
        sub_15m = df_15m[(df_15m.index >= t_start) & (df_15m.index < t_end)]

        # Trait 1: 30M Bearish Cross (Early Death Signal for BUY)
        # Did 30M EMA 9 cross below 30M EMA 26 during or right before this 1H candle?
        prior_30m = df_30m[df_30m.index <= t_end]
        c30_cross_opposing = False
        c30_close_below_ema = False
        c30_rsi_extreme = False
        
        if len(prior_30m) >= 3:
            last_30m = prior_30m.iloc[-1]
            if direction == "BUY":
                c30_cross_opposing = last_30m["ema_9"] < last_30m["ema_26"] # 30M already crossed bearish!
                c30_close_below_ema = last_30m["close"] < target_ema * 0.998 # 30M closed below 1H EMA!
                c30_rsi_extreme = last_30m["rsi"] < 40 # 30M momentum dumping
            else:
                c30_cross_opposing = last_30m["ema_9"] > last_30m["ema_26"]
                c30_close_below_ema = last_30m["close"] > target_ema * 1.002
                c30_rsi_extreme = last_30m["rsi"] > 60

        # Trait 2: 15M Structure Breakdown (15M printed 2 consecutive lower closes below EMA)
        prior_15m = df_15m[df_15m.index <= t_end]
        c15_two_bars_below = False
        c15_high_vol_dump = False
        
        if len(prior_15m) >= 4:
            last2_15m = prior_15m.iloc[-2:]
            if direction == "BUY":
                c15_two_bars_below = (last2_15m["close"].iloc[0] < target_ema) and (last2_15m["close"].iloc[1] < target_ema)
                c15_high_vol_dump = (last2_15m["volume"].iloc[-1] > 1.5 * last2_15m["vol_sma"].iloc[-1]) and (last2_15m["close"].iloc[-1] < last2_15m["open"].iloc[-1])
            else:
                c15_two_bars_below = (last2_15m["close"].iloc[0] > target_ema) and (last2_15m["close"].iloc[1] > target_ema)
                c15_high_vol_dump = (last2_15m["volume"].iloc[-1] > 1.5 * last2_15m["vol_sma"].iloc[-1]) and (last2_15m["close"].iloc[-1] > last2_15m["open"].iloc[-1])

        # Trait 3: Velocity of approach (Did 1H drop into EMA after 3+ consecutive heavy red bars?)
        prior_1h_bars = df_1h.iloc[max(0, i-3) : i]
        if direction == "BUY":
            rapid_dump_into_ema = all(b["close"] < b["open"] for _, b in prior_1h_bars.iterrows()) and len(prior_1h_bars) == 3
        else:
            rapid_dump_into_ema = all(b["close"] > b["open"] for _, b in prior_1h_bars.iterrows()) and len(prior_1h_bars) == 3

        records.append({
            "symbol": sym,
            "direction": direction,
            "is_success": is_success,
            "is_fail": is_fail,
            "c30_cross_opposing": c30_cross_opposing,
            "c30_close_below_ema": c30_close_below_ema,
            "c30_rsi_extreme": c30_rsi_extreme,
            "c15_two_bars_below": c15_two_bars_below,
            "c15_high_vol_dump": c15_high_vol_dump,
            "rapid_dump_into_ema": rapid_dump_into_ema
        })

print(f"Total 1H Retests analyzed with 15M/30M sub-candle telemetry: {len(records)}")

import json
def clean_val(v):
    if isinstance(v, (np.bool_, bool)): return bool(v)
    if isinstance(v, (np.integer, int)): return int(v)
    if isinstance(v, (np.floating, float)): return float(v)
    return v

with open("sub_candle_failure_analysis.json", "w", encoding="utf-8") as f:
    json.dump([{k: clean_val(v) for k, v in r.items()} for r in records], f, indent=2)

print("\n" + "=" * 85)
print("EARLY FAILURE DETECTION: HOW TO KNOW A 1H EMA CANDLE WILL FAIL?")
print("=" * 85)

tot = len(records)
tot_wins = sum(1 for r in records if r["is_success"])
base_fail_rate = (sum(1 for r in records if r["is_fail"]) / tot) * 100
base_wr = (tot_wins / tot) * 100
print(f"BASELINE: Total Trades = {tot} | Win Rate = {base_wr:.1f}% | Failure Rate = {base_fail_rate:.1f}%\n")

def check_sign(name, cond_func):
    flagged = [r for r in records if cond_func(r)]
    if not flagged: return
    n = len(flagged)
    fails = sum(1 for r in flagged if r["is_fail"])
    wins = sum(1 for r in flagged if r["is_success"])
    fail_rate = (fails / n) * 100
    win_rate = (wins / n) * 100
    print(f"{name:<55} | Triggered: {n:<5} | FAIL RATE: {fail_rate:5.1f}% | WinRate: {win_rate:5.1f}%")

check_sign("Sign 1: 30M already crossed opposing direction (Bearish)", lambda r: r["c30_cross_opposing"])
check_sign("Sign 2: 30M candle closed BELOW 1H EMA", lambda r: r["c30_close_below_ema"])
check_sign("Sign 3: 30M RSI broke into extreme (<40 on BUY)", lambda r: r["c30_rsi_extreme"])
check_sign("Sign 4: 15M printed 2 consecutive closes BELOW EMA", lambda r: r["c15_two_bars_below"])
check_sign("Sign 5: 15M High-Volume Red Dump into EMA (>1.5x Vol)", lambda r: r["c15_high_vol_dump"])
check_sign("Sign 6: Rapid Avalanche (3 consecutive dump 1H candles)", lambda r: r["rapid_dump_into_ema"])

# Combined Early Warning Filter: Any 2 warning signs present
def is_doomed_retest(r):
    score = (1 if r["c30_cross_opposing"] else 0) + \
            (1 if r["c30_close_below_ema"] else 0) + \
            (1 if r["c15_two_bars_below"] else 0) + \
            (1 if r["c15_high_vol_dump"] else 0) + \
            (1 if r["c30_rsi_extreme"] else 0)
    return score >= 2

check_sign("COMBO: Early Doomed Retest Filter (>= 2 Warning Signs)", is_doomed_retest)

# What happens if we BLOCK all Doomed Retests?
clean_trades = [r for r in records if not is_doomed_retest(r)]
clean_n = len(clean_trades)
clean_wins = sum(1 for r in clean_trades if r["is_success"])
clean_fails = sum(1 for r in clean_trades if r["is_fail"])
clean_wr = (clean_wins / clean_n) * 100
print("\n" + "=" * 85)
print(f"EFFECT OF BLOCKING EARLY DOOMED RETESTS:")
print(f"• Trades Filtered Out (Avoided): {tot - clean_n} dangerous setups")
print(f"• Win Rate Before:               {base_wr:.1f}%")
print(f"• Win Rate After Filter:         {clean_wr:.1f}% (+{clean_wr - base_wr:.1f}% Boost!)")
print(f"• Losses Blocked:                {sum(1 for r in records if is_doomed_retest(r) and r['is_fail'])} Stop-Losses prevented before entry!")
