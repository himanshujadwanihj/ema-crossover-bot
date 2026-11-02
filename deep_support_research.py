import requests
import pandas as pd
import numpy as np
import sys

sys.stdout.reconfigure(encoding='utf-8')

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT",
    "NEARUSDT", "DOTUSDT", "PEPEUSDT", "APTUSDT", "TAOUSDT",
    "LTCUSDT", "FETUSDT", "ENAUSDT", "AAVEUSDT", "WIFUSDT",
    "RENDERUSDT", "OPUSDT", "ARBUSDT", "INJUSDT", "TIAUSDT"
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
    df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["vol_sma"] = df["volume"].rolling(20).mean()
    
    # RSI 14
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))

    # EMA Slope (change over 3 candles in %)
    df["ema26_slope"] = (df["ema_26"] - df["ema_26"].shift(3)) / df["ema_26"].shift(3) * 100

    # ATR
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df

print("Fetching and analyzing 1H + 4H klines across top 25 symbols...")
all_events = []

for sym in SYMBOLS:
    df_1h = fetch_klines(sym, "1h", limit=1000)
    df_4h = fetch_klines(sym, "4h", limit=500)
    if df_1h is None or df_4h is None or len(df_1h) < 100 or len(df_4h) < 50:
        continue

    df_1h = compute_indicators(df_1h)
    df_4h = compute_indicators(df_4h)

    # Track crossovers and retest count
    retest_count = 0
    last_trend = None

    for i in range(35, len(df_1h) - 25):
        row = df_1h.iloc[i]
        prev_row = df_1h.iloc[i-1]
        t = df_1h.index[i]

        ema9 = row["ema_9"]
        ema26 = row["ema_26"]
        trend = "BULLISH" if ema9 > ema26 else "BEARISH"

        # Check for crossover
        if trend != last_trend:
            last_trend = trend
            retest_count = 0

        # Touch condition
        touch_th = 0.0075
        tapped_26 = (row["low"] <= ema26 <= row["high"]) and (abs(row["close"] - ema26) / ema26 <= touch_th)
        tapped_9 = (row["low"] <= ema9 <= row["high"]) and (abs(row["close"] - ema9) / ema9 <= touch_th)

        if not (tapped_26 or tapped_9):
            continue

        retest_count += 1
        target_ema = ema26 if tapped_26 else ema9

        # Basic baseline direction
        if trend == "BULLISH" and prev_row["close"] >= target_ema * 0.998:
            direction = "BUY"
        elif trend == "BEARISH" and prev_row["close"] <= target_ema * 1.002:
            direction = "SELL"
        else:
            continue

        entry = row["close"]
        atr = row["atr"] if not np.isnan(row["atr"]) else entry * 0.015

        if direction == "BUY":
            sl = min(row["low"] * 0.998, entry - 1.5 * atr)
            risk = entry - sl
            tp1 = entry + 1.2 * risk
            tp2 = entry + 2.0 * risk
        else:
            sl = max(row["high"] * 1.002, entry + 1.5 * atr)
            risk = sl - entry
            tp1 = entry - 1.2 * risk
            tp2 = entry - 2.0 * risk

        if risk <= 0: continue

        # Outcome over next 24 candles
        window = df_1h.iloc[i+1 : i+25]
        tp1_hit, sl_hit = False, False

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

        is_win = tp1_hit and not sl_hit

        # Feature 1: Retest sequence (1st/2nd retest vs 3rd+ retest)
        is_fresh_retest = (retest_count <= 2)

        # Feature 2: EMA 26 Slope (Is EMA trending in trade direction?)
        slope = row["ema26_slope"]
        is_slope_aligned = (slope > 0.05) if direction == "BUY" else (slope < -0.05)

        # Feature 3: 4H Higher Timeframe Alignment
        past_4h = df_4h[df_4h.index <= t]
        htf_aligned = False
        if len(past_4h) > 0:
            row_4h = past_4h.iloc[-1]
            if direction == "BUY":
                htf_aligned = (row_4h["ema_9"] > row_4h["ema_26"]) and (row_4h["close"] > row_4h["ema_50"])
            else:
                htf_aligned = (row_4h["ema_9"] < row_4h["ema_26"]) and (row_4h["close"] < row_4h["ema_50"])

        # Feature 4: RSI Pullback Zone (Is RSI in healthy zone 40-55 for BUY, 45-60 for SELL?)
        rsi = row["rsi"]
        healthy_rsi = (40 <= rsi <= 58) if direction == "BUY" else (42 <= rsi <= 60)

        # Feature 5: Candle 2 Confirmation (Did the NEXT candle break the high/low of retest candle?)
        candle2_confirmed = False
        if len(window) > 0:
            next_cand = window.iloc[0]
            if direction == "BUY":
                candle2_confirmed = (next_cand["high"] > row["high"])
            else:
                candle2_confirmed = (next_cand["low"] < row["low"])

        all_events.append({
            "symbol": sym,
            "direction": direction,
            "is_win": is_win,
            "retest_count": retest_count,
            "is_fresh_retest": is_fresh_retest,
            "is_slope_aligned": is_slope_aligned,
            "htf_aligned": htf_aligned,
            "healthy_rsi": healthy_rsi,
            "candle2_confirmed": candle2_confirmed
        })

print(f"Total Events Processed: {len(all_events)}")

# Save to disk for fast audit
import json

def clean_val(v):
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer, int)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return float(v)
    return v

cleaned_events = [
    {k: clean_val(v) for k, v in ev.items()}
    for ev in all_events
]

with open("deep_support_events.json", "w", encoding="utf-8") as f:
    json.dump(cleaned_events, f, indent=2)

print("Saved deep_support_events.json successfully.")

# Compute statistics immediately
def get_stats(ev_list, label):
    if not ev_list:
        return
    wins = sum(1 for e in ev_list if e["is_win"])
    total = len(ev_list)
    wr = (wins / total) * 100
    print(f"{label:<45} | Trades: {total:<6} | Wins: {wins:<5} | WinRate: {wr:5.1f}%")

print("\n" + "=" * 80)
print("DEEP FACTOR ANALYSIS: WHAT ACTUALLY PREDICTS EMA SUPPORT?")
print("=" * 80)

get_stats(cleaned_events, "ALL EMA RETEST EVENTS (BASELINE)")

# 1. Retest sequence
get_stats([e for e in cleaned_events if e["retest_count"] == 1], "1st Retest after Crossover (Fresh)")
get_stats([e for e in cleaned_events if e["retest_count"] == 2], "2nd Retest after Crossover")
get_stats([e for e in cleaned_events if e["retest_count"] >= 4], "4th+ Retest after Crossover (Exhausted)")

# 2. EMA Slope
get_stats([e for e in cleaned_events if e["is_slope_aligned"]], "EMA 26 Sloping in Trade Direction")
get_stats([e for e in cleaned_events if not e["is_slope_aligned"]], "EMA 26 Flat / Counter-Sloping")

# 3. 4H HTF Alignment
get_stats([e for e in cleaned_events if e["htf_aligned"]], "4H Higher Timeframe Aligned")
get_stats([e for e in cleaned_events if not e["htf_aligned"]], "4H Higher Timeframe NOT Aligned (Counter-Trend)")

# 4. RSI Health
get_stats([e for e in cleaned_events if e["healthy_rsi"]], "RSI in Healthy Zone (40-58)")
get_stats([e for e in cleaned_events if not e["healthy_rsi"]], "RSI Outside Healthy Zone")

# 5. Next Candle Confirmation (Candle 2)
get_stats([e for e in cleaned_events if e["candle2_confirmed"]], "Candle 2 Breaks Retest Candle High/Low")
get_stats([e for e in cleaned_events if not e["candle2_confirmed"]], "Candle 2 Fails to Break High/Low")

# Combined: High Quality Synergy
combo1 = [e for e in cleaned_events if e["is_fresh_retest"] and e["is_slope_aligned"]]
get_stats(combo1, "Combo: Fresh Retest (1st/2nd) + Sloping EMA")

combo2 = [e for e in cleaned_events if e["is_fresh_retest"] and e["htf_aligned"]]
get_stats(combo2, "Combo: Fresh Retest (1st/2nd) + 4H HTF Aligned")

combo3 = [e for e in cleaned_events if e["candle2_confirmed"] and e["htf_aligned"]]
get_stats(combo3, "Combo: Candle 2 Confirmation + 4H HTF Aligned")

combo_ultimate = [e for e in cleaned_events if e["is_fresh_retest"] and e["htf_aligned"] and e["is_slope_aligned"] and e["candle2_confirmed"]]
get_stats(combo_ultimate, "ULTIMATE: Fresh + 4H HTF + Slope + Candle 2")

