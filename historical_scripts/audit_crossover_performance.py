import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from fetcher import get_top_pairs_by_volume
from indicators import _compute_indicators, get_trend_state

exchange = ccxt.binance()
symbols = get_top_pairs_by_volume(60)

print("======================================================================")
print("EMA CROSSOVER PERFORMANCE AUDIT -- LAST 24 HOURS (OCT 1)")
print("======================================================================")

results = []

for symbol in symbols:
    for tf in ["30m", "1h", "4h"]:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=150)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df = _compute_indicators(df)
            
            # Look for crossovers in the last 48 candles (~24h for 30m, ~48h for 1h)
            for i in range(len(df) - 48, len(df) - 2):
                if i < 2: continue
                row = df.iloc[i]
                prev = df.iloc[i-1]
                
                is_buy = (prev['ema_9'] <= prev['ema_26']) and (row['ema_9'] > row['ema_26'])
                is_sell = (prev['ema_9'] >= prev['ema_26']) and (row['ema_9'] < row['ema_26'])
                
                if not (is_buy or is_sell): continue
                
                sig_type = "BUY" if is_buy else "SELL"
                entry = row['close']
                ema26 = row['ema_26']
                sl = ema26 * 0.998 if sig_type == "BUY" else ema26 * 1.002
                risk = abs(entry - sl)
                if risk == 0: continue
                
                tp1 = entry + (1.5 * risk) if sig_type == "BUY" else entry - (1.5 * risk)
                tp2 = entry + (2.5 * risk) if sig_type == "BUY" else entry - (2.5 * risk)
                tp3 = entry + (3.5 * risk) if sig_type == "BUY" else entry - (3.5 * risk)
                
                # Check outcome on subsequent candles
                subsequent = df.iloc[i+1:]
                outcome = "ACTIVE / PENDING"
                tp1_hit = False
                tp2_hit = False
                tp3_hit = False
                sl_hit = False
                max_favorable_pct = 0.0
                
                for _, sub_row in subsequent.iterrows():
                    if sig_type == "BUY":
                        gain = ((sub_row['high'] - entry) / entry) * 100
                        if gain > max_favorable_pct: max_favorable_pct = gain
                        if sub_row['high'] >= tp1: tp1_hit = True
                        if sub_row['high'] >= tp2: tp2_hit = True
                        if sub_row['high'] >= tp3: tp3_hit = True
                        if sub_row['low'] <= sl and not tp1_hit:
                            sl_hit = True
                            break
                    else:
                        gain = ((entry - sub_row['low']) / entry) * 100
                        if gain > max_favorable_pct: max_favorable_pct = gain
                        if sub_row['low'] <= tp1: tp1_hit = True
                        if sub_row['low'] <= tp2: tp2_hit = True
                        if sub_row['low'] <= tp3: tp3_hit = True
                        if sub_row['high'] >= sl and not tp1_hit:
                            sl_hit = True
                            break
                            
                if tp3_hit: outcome = "TP3 HIT (FULL WIN 🔥)"
                elif tp2_hit: outcome = "TP2 HIT (GREAT WIN ✅)"
                elif tp1_hit: outcome = "TP1 HIT (WIN ✅)"
                elif sl_hit: outcome = "SL HIT (LOSS ❌)"
                else: outcome = f"IN PROGRESS (Max Move: {max_favorable_pct:+.2f}%)"
                
                cross_time = df.index[i]
                results.append({
                    "symbol": symbol,
                    "tf": tf.upper(),
                    "signal": sig_type,
                    "time": str(cross_time),
                    "entry": entry,
                    "sl": sl,
                    "tp1": tp1,
                    "outcome": outcome,
                    "tp1_hit": tp1_hit,
                    "sl_hit": sl_hit,
                    "max_fav": max_favorable_pct
                })
        except Exception:
            pass

print(f"\nTOTAL CROSSOVERS FOUND IN LAST 24 HOURS ACROSS TOP 60 COINS: {len(results)}")

wins = [r for r in results if r['tp1_hit']]
losses = [r for r in results if r['sl_hit']]
pending = [r for r in results if not r['tp1_hit'] and not r['sl_hit']]

print(f"  - TP1 HIT (WINS): {len(wins)}")
print(f"  - SL HIT (LOSSES): {len(losses)}")
print(f"  - IN PROGRESS / PENDING: {len(pending)}")

if (len(wins) + len(losses)) > 0:
    win_rate = (len(wins) / (len(wins) + len(losses))) * 100
    print(f"  - WIN RATE (TP1+ vs SL): {win_rate:.1f}%")

print("\n--- DETAILED LIST OF RECENT CROSSOVERS & OUTCOMES ---")
for r in results[-30:]:
    print(f"[{r['time']}] {r['symbol']} {r['tf']} {r['signal']} @ {r['entry']:.4f} -> {r['outcome']}")
