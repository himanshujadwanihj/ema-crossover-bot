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
    return df

trades_raw = []
trades_filtered = []

for sym in SYMBOLS:
    df_30m = fetch_klines(sym, "30m", limit=1000)
    df_1h = fetch_klines(sym, "1h", limit=1000)
    
    if df_30m is None or df_1h is None:
        continue
        
    df_30m = compute_indicators(df_30m)
    df_1h = compute_indicators(df_1h)
    
    df_30m["prev_e9"] = df_30m["ema_9"].shift(1)
    df_30m["prev_e26"] = df_30m["ema_26"].shift(1)
    
    for i in range(25, len(df_30m) - 30):
        t30 = df_30m.index[i]
        row30 = df_30m.iloc[i]
        prev30 = df_30m.iloc[i-1]
        
        is_bull = prev30["ema_9"] <= prev30["ema_26"] and row30["ema_9"] > row30["ema_26"]
        is_bear = prev30["ema_9"] >= prev30["ema_26"] and row30["ema_9"] < row30["ema_26"]
        
        if not (is_bull or is_bear):
            continue
            
        direction = "BUY" if is_bull else "SELL"
        entry = row30["close"]
        
        # 1.5% SL and 1.8% / 3.3% / 4.8% TPs (Standard dynamic RR)
        sl_pct = 0.015
        tp1_pct = 0.018
        tp2_pct = 0.033
        tp3_pct = 0.048
        
        if direction == "BUY":
            sl = entry * (1 - sl_pct)
            tp1 = entry * (1 + tp1_pct)
            tp2 = entry * (1 + tp2_pct)
            tp3 = entry * (1 + tp3_pct)
        else:
            sl = entry * (1 + sl_pct)
            tp1 = entry * (1 - tp1_pct)
            tp2 = entry * (1 - tp2_pct)
            tp3 = entry * (1 - tp3_pct)
            
        # Outcome over next 30 candles
        window = df_30m.iloc[i+1 : i+31]
        tp1_hit, tp2_hit, tp3_hit, sl_hit = False, False, False, False
        
        for idx in range(len(window)):
            k = window.iloc[idx]
            if direction == "BUY":
                if k["low"] <= sl:
                    sl_hit = True
                    break
                if k["high"] >= tp1: tp1_hit = True
                if k["high"] >= tp2: tp2_hit = True
                if k["high"] >= tp3: tp3_hit = True
            else:
                if k["high"] >= sl:
                    sl_hit = True
                    break
                if k["low"] <= tp1: tp1_hit = True
                if k["low"] <= tp2: tp2_hit = True
                if k["low"] <= tp3: tp3_hit = True
                
        outcome = "SL" if sl_hit and not tp1_hit else ("TP3" if tp3_hit else ("TP2" if tp2_hit else ("TP1" if tp1_hit else "RUNNING")))
        pnl = -1.0 if outcome == "SL" else (3.2 if outcome == "TP3" else (2.2 if outcome == "TP2" else (1.2 if outcome == "TP1" else 0.0)))
        
        trade_data = {
            "sym": sym, "time": t30, "direction": direction,
            "outcome": outcome, "pnl": pnl, "is_win": outcome.startswith("TP")
        }
        trades_raw.append(trade_data)
        
        # Now evaluate Filter Checklist:
        # Sign 1: 1H gap pinch <= 1.0%
        past_1h = df_1h[df_1h.index <= t30]
        if len(past_1h) < 5: continue
        row1h = past_1h.iloc[-1]
        gap_1h = abs(row1h["ema_9"] - row1h["ema_26"]) / row1h["close"] * 100
        sign1 = gap_1h <= 1.0
        
        # Sign 2: Price cleared 1H EMA 26
        sign2 = (entry > row1h["ema_26"]) if direction == "BUY" else (entry < row1h["ema_26"])
        
        # Sign 3: Decisive candle + volume
        body = abs(row30["close"] - row30["open"])
        crange = abs(row30["high"] - row30["low"])
        sign3 = (body / crange >= 0.55) and (row30["volume"] >= 1.15 * row30["vol_sma"])
        
        # Pass filter if at least Sign 1 & Sign 2 pass (The Structural Confirmation)
        if sign1 and sign2:
            trades_filtered.append(trade_data)

def calc_stats(trades):
    tot = len(trades)
    wins = [t for t in trades if t["is_win"]]
    losses = [t for t in trades if t["outcome"] == "SL"]
    running = [t for t in trades if t["outcome"] == "RUNNING"]
    closed = len(wins) + len(losses)
    wr = len(wins) / closed * 100 if closed > 0 else 0
    tp1 = sum(1 for t in trades if t["outcome"] == "TP1")
    tp2 = sum(1 for t in trades if t["outcome"] == "TP2")
    tp3 = sum(1 for t in trades if t["outcome"] == "TP3")
    total_r = sum(t["pnl"] for t in trades)
    avg_r = total_r / closed if closed > 0 else 0
    profit_factor = sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)) if losses else 999
    
    return {
        "total": tot, "closed": closed, "wins": len(wins), "losses": len(losses),
        "running": len(running), "wr": wr, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "total_r": total_r, "avg_r": avg_r, "profit_factor": profit_factor
    }

raw_stats = calc_stats(trades_raw)
filt_stats = calc_stats(trades_filtered)

print("=== BEFORE (RAW WITHOUT FILTER) ===")
print(f"Total Trades: {raw_stats['total']}")
print(f"Wins: {raw_stats['wins']} | Losses: {raw_stats['losses']}")
print(f"Win Rate: {raw_stats['wr']:.1f}%")
print(f"TP1={raw_stats['tp1']} | TP2={raw_stats['tp2']} | TP3={raw_stats['tp3']}")
print(f"Net Profit (R-multiples): +{raw_stats['total_r']:.1f}R")
print(f"Profit Factor: {raw_stats['profit_factor']:.2f}")

print("\n=== AFTER (WITH THE 1H CONFIRMATION FILTER) ===")
print(f"Total Trades: {filt_stats['total']} (Cut out {raw_stats['total'] - filt_stats['total']} low-quality trades)")
print(f"Wins: {filt_stats['wins']} | Losses: {filt_stats['losses']} (Eliminated {raw_stats['losses'] - filt_stats['losses']} Stop Loss hits!)")
print(f"Win Rate: {filt_stats['wr']:.1f}% (+{filt_stats['wr'] - raw_stats['wr']:.1f}% BOOST!)")
print(f"TP1={filt_stats['tp1']} | TP2={filt_stats['tp2']} | TP3={filt_stats['tp3']}")
print(f"Net Profit (R-multiples): +{filt_stats['total_r']:.1f}R")
print(f"Profit Factor: {filt_stats['profit_factor']:.2f} (Jumped from {raw_stats['profit_factor']:.2f})")
