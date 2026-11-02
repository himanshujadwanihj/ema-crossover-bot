import requests
import pandas as pd
import numpy as np

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT",
    "NEARUSDT", "DOTUSDT", "PEPEUSDT", "APTUSDT", "TAOUSDT"
]

session = requests.Session()

def fetch_klines(symbol, interval, limit=1000):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        r = session.get(url, timeout=10)
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
    # RSI 14
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df

records = []

for sym in SYMBOLS:
    df_30m = fetch_klines(sym, "30m", limit=1000)
    df_1h = fetch_klines(sym, "1h", limit=1000)
    df_4h = fetch_klines(sym, "4h", limit=500)
    
    if df_30m is None or df_1h is None or df_4h is None:
        continue
        
    df_30m = compute_indicators(df_30m)
    df_1h = compute_indicators(df_1h)
    df_4h = compute_indicators(df_4h)
    
    df_1h["prev_e9"] = df_1h["ema_9"].shift(1)
    df_1h["prev_e26"] = df_1h["ema_26"].shift(1)
    h1_bull_crosses = df_1h[(df_1h["prev_e9"] <= df_1h["prev_e26"]) & (df_1h["ema_9"] > df_1h["ema_26"])].index
    h1_bear_crosses = df_1h[(df_1h["prev_e9"] >= df_1h["prev_e26"]) & (df_1h["ema_9"] < df_1h["ema_26"])].index

    df_30m["prev_e9"] = df_30m["ema_9"].shift(1)
    df_30m["prev_e26"] = df_30m["ema_26"].shift(1)
    
    for i in range(25, len(df_30m) - 48):
        t30 = df_30m.index[i]
        row30 = df_30m.iloc[i]
        prev30 = df_30m.iloc[i-1]
        
        is_bull = prev30["ema_9"] <= prev30["ema_26"] and row30["ema_9"] > row30["ema_26"]
        is_bear = prev30["ema_9"] >= prev30["ema_26"] and row30["ema_9"] < row30["ema_26"]
        
        if not (is_bull or is_bear):
            continue
            
        direction = "BUY" if is_bull else "SELL"
        entry_price = row30["close"]
        
        # Check if volume was above average
        vol_surge = row30["volume"] >= 1.3 * row30["vol_sma"]
        
        # Check candle body dominance (strong marubozu-like candle vs wick)
        candle_range = abs(row30["high"] - row30["low"])
        body = abs(row30["close"] - row30["open"])
        strong_candle = (body / candle_range >= 0.60) if candle_range > 0 else False
        
        # Get 1H state at that moment
        past_1h = df_1h[df_1h.index <= t30]
        if len(past_1h) < 10: continue
        row1h = past_1h.iloc[-1]
        
        # 1H gap between EMA9 and EMA26 (in %)
        gap_1h = abs(row1h["ema_9"] - row1h["ema_26"]) / row1h["close"] * 100
        tight_1h_gap = gap_1h <= 0.6  # 1H EMAs already close to crossing
        
        # 1H RSI
        rsi_1h_aligned = (row1h["rsi"] >= 50) if is_bull else (row1h["rsi"] <= 50)
        
        # Price crossed 1H EMA 26
        price_past_1h_ema26 = (entry_price > row1h["ema_26"]) if is_bull else (entry_price < row1h["ema_26"])
        
        # 4H trend
        past_4h = df_4h[df_4h.index <= t30]
        if len(past_4h) < 10: continue
        row4h = past_4h.iloc[-1]
        trend_4h_aligned = (row4h["ema_9"] > row4h["ema_26"]) if is_bull else (row4h["ema_9"] < row4h["ema_26"])
        
        # Check outcome within next 24 hours
        window = df_30m.iloc[i+1 : i+49]
        if is_bull:
            reversed_30m = window[(window["prev_e9"] >= window["prev_e26"]) & (window["ema_9"] < window["ema_26"])]
            reversal_time = reversed_30m.index[0] if len(reversed_30m) > 0 else None
            subsequent_1h = [t for t in h1_bull_crosses if t >= t30 and (t - t30).total_seconds() <= 24 * 3600]
        else:
            reversed_30m = window[(window["prev_e9"] <= window["prev_e26"]) & (window["ema_9"] > window["ema_26"])]
            reversal_time = reversed_30m.index[0] if len(reversed_30m) > 0 else None
            subsequent_1h = [t for t in h1_bear_crosses if t >= t30 and (t - t30).total_seconds() <= 24 * 3600]
            
        success = False
        if subsequent_1h:
            t1h = subsequent_1h[0]
            if not reversal_time or reversal_time >= t1h:
                success = True
                
        records.append({
            "direction": direction,
            "success": success,
            "vol_surge": vol_surge,
            "strong_candle": strong_candle,
            "tight_1h_gap": tight_1h_gap,
            "rsi_1h_aligned": rsi_1h_aligned,
            "price_past_1h_ema26": price_past_1h_ema26,
            "trend_4h_aligned": trend_4h_aligned,
        })

df_res = pd.DataFrame(records)
print(f"Total events analyzed: {len(df_res)}")
base_wr = df_res['success'].mean() * 100
print(f"Baseline (Blind 30M Cross) Success Rate: {base_wr:.1f}%\n")

factors = [
    ("1. Price Already Beyond 1H EMA 26", "price_past_1h_ema26"),
    ("2. 1H EMA Gap Already Tight (<= 0.6%)", "tight_1h_gap"),
    ("3. 4H Trend Aligned (Higher TF backing)", "trend_4h_aligned"),
    ("4. Volume Surge on 30M (>= 1.3x 20-SMA)", "vol_surge"),
    ("5. Strong Full-Body Candle (>= 60% Body)", "strong_candle"),
    ("6. 1H RSI Momentum Aligned (>50 bull, <50 bear)", "rsi_1h_aligned"),
]

for label, col in factors:
    subset = df_res[df_res[col] == True]
    wr = subset['success'].mean() * 100
    count = len(subset)
    print(f"{label}:")
    print(f"   Success Rate: {wr:.1f}% ({subset['success'].sum()}/{count}) [vs {base_wr:.1f}% base]")

# Best Combinations
print("\n=== POWER COMBINATIONS ===")
c1 = df_res[(df_res["price_past_1h_ema26"] == True) & (df_res["tight_1h_gap"] == True)]
print(f"Price Beyond 1H EMA 26 + Tight 1H Gap: {c1['success'].mean()*100:.1f}% Success ({c1['success'].sum()}/{len(c1)})")

c2 = df_res[(df_res["price_past_1h_ema26"] == True) & (df_res["trend_4h_aligned"] == True)]
print(f"Price Beyond 1H EMA 26 + 4H Trend Aligned: {c2['success'].mean()*100:.1f}% Success ({c2['success'].sum()}/{len(c2)})")

c3 = df_res[(df_res["price_past_1h_ema26"] == True) & (df_res["vol_surge"] == True)]
print(f"Price Beyond 1H EMA 26 + Volume Surge: {c3['success'].mean()*100:.1f}% Success ({c3['success'].sum()}/{len(c3)})")

c4 = df_res[(df_res["price_past_1h_ema26"] == True) & (df_res["trend_4h_aligned"] == True) & (df_res["tight_1h_gap"] == True)]
print(f"TRIPLE CONFLUENCE (Beyond 1H EMA26 + 4H Trend + Tight 1H Gap): {c4['success'].mean()*100:.1f}% Success ({c4['success'].sum()}/{len(c4)})")
