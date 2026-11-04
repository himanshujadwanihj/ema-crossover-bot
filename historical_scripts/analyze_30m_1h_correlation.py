import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# List of major representative coins across crypto
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
            data = r.json()
            df = pd.DataFrame(data, columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "q_vol", "trades", "tb_vol", "tb_q_vol", "ignore"
            ])
            df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df.set_index("time", inplace=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df
    except Exception as e:
        print(f"Error fetching {symbol} {interval}: {e}")
    return None

def compute_emas(df):
    df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_26"] = df["close"].ewm(span=26, adjust=False).mean()
    return df

results = {
    "bullish": {
        "total_30m_crosses": 0,
        "followed_by_1h_cross": 0,
        "failed_fakeouts": 0,
        "lags_hours": [],
        "max_gain_pct_when_1h_crosses": [],
        "max_gain_pct_when_fails": []
    },
    "bearish": {
        "total_30m_crosses": 0,
        "followed_by_1h_cross": 0,
        "failed_fakeouts": 0,
        "lags_hours": [],
        "max_drop_pct_when_1h_crosses": [],
        "max_drop_pct_when_fails": []
    }
}

for sym in SYMBOLS:
    print(f"Analyzing {sym}...")
    df_30m = fetch_klines(sym, "30m", limit=1000)
    df_1h = fetch_klines(sym, "1h", limit=1000)
    
    if df_30m is None or df_1h is None:
        continue
        
    df_30m = compute_emas(df_30m)
    df_1h = compute_emas(df_1h)
    
    # Identify 1H crosses
    df_1h["prev_e9"] = df_1h["ema_9"].shift(1)
    df_1h["prev_e26"] = df_1h["ema_26"].shift(1)
    
    h1_bull_crosses = df_1h[(df_1h["prev_e9"] <= df_1h["prev_e26"]) & (df_1h["ema_9"] > df_1h["ema_26"])].index
    h1_bear_crosses = df_1h[(df_1h["prev_e9"] >= df_1h["prev_e26"]) & (df_1h["ema_9"] < df_1h["ema_26"])].index

    # Identify 30m crosses
    df_30m["prev_e9"] = df_30m["ema_9"].shift(1)
    df_30m["prev_e26"] = df_30m["ema_26"].shift(1)
    
    # Iterate over 30m candles
    for i in range(1, len(df_30m) - 48): # Leave room to observe future
        t30 = df_30m.index[i]
        row = df_30m.iloc[i]
        prev = df_30m.iloc[i-1]
        
        # Bullish 30m Cross
        if prev["ema_9"] <= prev["ema_26"] and row["ema_9"] > row["ema_26"]:
            results["bullish"]["total_30m_crosses"] += 1
            entry_price = row["close"]
            
            # Look up to 24 hours (48 30m candles) ahead
            window = df_30m.iloc[i+1 : i+49]
            # Check if 30m reversed back into a bearish cross before 1h crossed
            reversed_30m = window[(window["prev_e9"] >= window["prev_e26"]) & (window["ema_9"] < window["ema_26"])]
            reversal_time = reversed_30m.index[0] if len(reversed_30m) > 0 else None
            
            # Find next 1H bull cross
            subsequent_1h = [t for t in h1_bull_crosses if t >= t30 and (t - t30).total_seconds() <= 24 * 3600]
            
            if subsequent_1h:
                t1h = subsequent_1h[0]
                # Did 30m reverse before 1H even crossed?
                if reversal_time and reversal_time < t1h:
                    results["bullish"]["failed_fakeouts"] += 1
                    max_gain = (window.loc[:reversal_time]["high"].max() - entry_price) / entry_price * 100
                    results["bullish"]["max_gain_pct_when_fails"].append(max_gain)
                else:
                    results["bullish"]["followed_by_1h_cross"] += 1
                    lag_hours = (t1h - t30).total_seconds() / 3600.0
                    results["bullish"]["lags_hours"].append(lag_hours)
                    max_gain = (window["high"].max() - entry_price) / entry_price * 100
                    results["bullish"]["max_gain_pct_when_1h_crosses"].append(max_gain)
            else:
                results["bullish"]["failed_fakeouts"] += 1
                max_gain = (window["high"].max() - entry_price) / entry_price * 100
                results["bullish"]["max_gain_pct_when_fails"].append(max_gain)
                
        # Bearish 30m Cross
        elif prev["ema_9"] >= prev["ema_26"] and row["ema_9"] < row["ema_26"]:
            results["bearish"]["total_30m_crosses"] += 1
            entry_price = row["close"]
            
            window = df_30m.iloc[i+1 : i+49]
            reversed_30m = window[(window["prev_e9"] <= window["prev_e26"]) & (window["ema_9"] > window["ema_26"])]
            reversal_time = reversed_30m.index[0] if len(reversed_30m) > 0 else None
            
            subsequent_1h = [t for t in h1_bear_crosses if t >= t30 and (t - t30).total_seconds() <= 24 * 3600]
            
            if subsequent_1h:
                t1h = subsequent_1h[0]
                if reversal_time and reversal_time < t1h:
                    results["bearish"]["failed_fakeouts"] += 1
                    max_drop = (entry_price - window.loc[:reversal_time]["low"].min()) / entry_price * 100
                    results["bearish"]["max_drop_pct_when_fails"].append(max_drop)
                else:
                    results["bearish"]["followed_by_1h_cross"] += 1
                    lag_hours = (t1h - t30).total_seconds() / 3600.0
                    results["bearish"]["lags_hours"].append(lag_hours)
                    max_drop = (entry_price - window["low"].min()) / entry_price * 100
                    results["bearish"]["max_drop_pct_when_1h_crosses"].append(max_drop)
            else:
                results["bearish"]["failed_fakeouts"] += 1
                max_drop = (entry_price - window["low"].min()) / entry_price * 100
                results["bearish"]["max_drop_pct_when_fails"].append(max_drop)

# Summary calculations
print("\n" + "="*50)
print("=== 30-MIN vs 1-HOUR CROSSOVER CORRELATION REPORT ===")
print("="*50)

for side in ["bullish", "bearish"]:
    tot = results[side]["total_30m_crosses"]
    succ = results[side]["followed_by_1h_cross"]
    fail = results[side]["failed_fakeouts"]
    rate = (succ / tot * 100) if tot > 0 else 0
    lags = results[side]["lags_hours"]
    avg_lag = np.mean(lags) if lags else 0
    median_lag = np.median(lags) if lags else 0
    
    print(f"\n--- {side.upper()} CROSSOVERS ---")
    print(f"Total 30M Crosses Examined: {tot}")
    print(f"Led to a 1H {side.capitalize()} Cross: {succ} ({rate:.1f}%)")
    print(f"Failed (30M reversed before 1H crossed): {fail} ({100 - rate:.1f}%)")
    print(f"Average Lead Time (30M before 1H): {avg_lag:.1f} hours ({avg_lag*60:.0f} mins)")
    print(f"Median Lead Time: {median_lag:.1f} hours ({median_lag*60:.0f} mins)")
    
    if side == "bullish":
        avg_gain_succ = np.mean(results[side]["max_gain_pct_when_1h_crosses"]) if results[side]["max_gain_pct_when_1h_crosses"] else 0
        avg_gain_fail = np.mean(results[side]["max_gain_pct_when_fails"]) if results[side]["max_gain_pct_when_fails"] else 0
        print(f"Average Max Move when 1H follows: +{avg_gain_succ:.2f}%")
        print(f"Average Max Move when 1H fails: +{avg_gain_fail:.2f}%")
    else:
        avg_drop_succ = np.mean(results[side]["max_drop_pct_when_1h_crosses"]) if results[side]["max_drop_pct_when_1h_crosses"] else 0
        avg_drop_fail = np.mean(results[side]["max_drop_pct_when_fails"]) if results[side]["max_drop_pct_when_fails"] else 0
        print(f"Average Max Move when 1H follows: -{avg_drop_succ:.2f}%")
        print(f"Average Max Move when 1H fails: -{avg_drop_fail:.2f}%")

# Distribution of lead time
for side in ["bullish", "bearish"]:
    lags = results[side]["lags_hours"]
    print(f"\nLag distribution for {side.upper()}:")
    print(f"  Within 1 Hour: {sum(1 for l in lags if l <= 1.0)} ({sum(1 for l in lags if l <= 1.0)/len(lags)*100:.1f}%)")
    print(f"  1 - 3 Hours:   {sum(1 for l in lags if 1.0 < l <= 3.0)} ({sum(1 for l in lags if 1.0 < l <= 3.0)/len(lags)*100:.1f}%)")
    print(f"  3 - 6 Hours:   {sum(1 for l in lags if 3.0 < l <= 6.0)} ({sum(1 for l in lags if 3.0 < l <= 6.0)/len(lags)*100:.1f}%)")
    print(f"  > 6 Hours:     {sum(1 for l in lags if l > 6.0)} ({sum(1 for l in lags if l > 6.0)/len(lags)*100:.1f}%)")
