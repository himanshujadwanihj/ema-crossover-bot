import sys
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from fetcher import get_top_pairs_by_volume
from indicators import _compute_indicators, get_trend_state, analyze_crossovers

sys.stdout.reconfigure(encoding='utf-8')

exchange = ccxt.binance()
symbols = get_top_pairs_by_volume(60)

print("======================================================================")
print("FILTERED STRATEGY CROSSOVER AUDIT -- LAST 48 HOURS")
print("======================================================================")

results = []

for symbol in symbols:
    tf_data = {}
    for tf in ["15m", "30m", "1h", "4h", "1d"]:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=120)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df = _compute_indicators(df)
            tf_data[tf.upper()] = df
        except Exception:
            pass

    for label in ["30M", "1H", "4H"]:
        df = tf_data.get(label)
        if df is None or len(df) < 50: continue

        for i in range(len(df) - 48, len(df) - 2):
            if i < 3: continue
            row = df.iloc[i]
            prev = df.iloc[i-1]

            is_buy = (prev['ema_9'] <= prev['ema_26']) and (row['ema_9'] > row['ema_26'])
            is_sell = (prev['ema_9'] >= prev['ema_26']) and (row['ema_9'] < row['ema_26'])
            if not (is_buy or is_sell): continue

            sig_type = "BUY" if is_buy else "SELL"
            target_trend = "bullish" if sig_type == "BUY" else "bearish"

            # 1. Check HTF Trend States
            tf_states = {t: get_trend_state(tf_data.get(t)) for t in ["1H", "4H", "1D", "1W"] if t in tf_data}
            aligned_count = sum(1 for t, st in tf_states.items() if st == target_trend)
            is_1h_aligned = tf_states.get("1H") == target_trend

            # Filter Rule: Must have at least 2-Star HTF alignment + 1H aligned
            if not (is_1h_aligned and aligned_count >= 2):
                continue

            # 2. Check Technical Quality Filters
            vol_sma = row.get('vol_sma_20', 0)
            adx_val = row.get('adx', 0)
            candle_body = abs(row['close'] - row['open'])
            candle_range = max(row['high'] - row['low'], 1e-9)
            body_ratio = candle_body / candle_range
            proximity = abs(row['close'] - row['ema_26']) / row['close']
            anti_chop = abs(row['ema_9'] - row['ema_26']) / row['close']

            passes_vol = (row['volume'] > vol_sma * 1.2) if vol_sma > 0 else True
            passes_adx = (adx_val > 15.0) if not pd.isna(adx_val) else True
            passes_body = body_ratio >= 0.25
            passes_prox = proximity <= 0.025
            passes_antichop = anti_chop >= 0.0008

            if not (passes_vol and passes_adx and passes_body and passes_prox and passes_antichop):
                continue

            entry = row['close']
            ema26 = row['ema_26']
            sl = ema26 * 0.998 if sig_type == "BUY" else ema26 * 1.002
            risk = abs(entry - sl)
            if risk == 0: continue

            tp1 = entry + (1.5 * risk) if sig_type == "BUY" else entry - (1.5 * risk)
            tp2 = entry + (2.5 * risk) if sig_type == "BUY" else entry - (2.5 * risk)
            tp3 = entry + (3.5 * risk) if sig_type == "BUY" else entry - (3.5 * risk)

            # Check outcome
            subsequent = df.iloc[i+1:]
            tp1_hit = False
            tp2_hit = False
            tp3_hit = False
            sl_hit = False
            max_fav = 0.0

            for _, sub in subsequent.iterrows():
                if sig_type == "BUY":
                    gain = ((sub['high'] - entry) / entry) * 100
                    if gain > max_fav: max_fav = gain
                    if sub['high'] >= tp1: tp1_hit = True
                    if sub['high'] >= tp2: tp2_hit = True
                    if sub['high'] >= tp3: tp3_hit = True
                    if sub['low'] <= sl and not tp1_hit:
                        sl_hit = True
                        break
                else:
                    gain = ((entry - sub['low']) / entry) * 100
                    if gain > max_fav: max_fav = gain
                    if sub['low'] <= tp1: tp1_hit = True
                    if sub['low'] <= tp2: tp2_hit = True
                    if sub['low'] <= tp3: tp3_hit = True
                    if sub['high'] >= sl and not tp1_hit:
                        sl_hit = True
                        break

            if tp3_hit: outcome = "TP3 HIT (FULL WIN 🔥)"
            elif tp2_hit: outcome = "TP2 HIT (GREAT WIN ✅)"
            elif tp1_hit: outcome = "TP1 HIT (WIN ✅)"
            elif sl_hit: outcome = "SL HIT (LOSS ❌)"
            else: outcome = f"RUNNING (Max Gain: {max_fav:+.2f}%)"

            cross_time = str(df.index[i])
            results.append({
                "symbol": symbol,
                "tf": label,
                "signal": sig_type,
                "time": cross_time,
                "stars": aligned_count,
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "outcome": outcome,
                "tp1_hit": tp1_hit,
                "tp2_hit": tp2_hit,
                "tp3_hit": tp3_hit,
                "sl_hit": sl_hit,
                "max_fav": max_fav
            })

print(f"\nTOTAL QUALIFIED STRATEGY CROSSOVERS IN LAST 48 HOURS: {len(results)}")

wins = [r for r in results if r['tp1_hit']]
tp2_wins = [r for r in results if r['tp2_hit']]
tp3_wins = [r for r in results if r['tp3_hit']]
losses = [r for r in results if r['sl_hit']]
pending = [r for r in results if not r['tp1_hit'] and not r['sl_hit']]

print(f"  - TP1 HIT (WINS): {len(wins)}")
print(f"  - TP2 HIT (GREAT WINS): {len(tp2_wins)}")
print(f"  - TP3 HIT (FULL WINS): {len(tp3_wins)}")
print(f"  - SL HIT (LOSSES): {len(losses)}")
print(f"  - IN PROGRESS / RUNNING: {len(pending)}")

if (len(wins) + len(losses)) > 0:
    win_rate = (len(wins) / (len(wins) + len(losses))) * 100
    print(f"\n  🎯 STRATEGY WIN RATE (TP1+ vs SL): {win_rate:.1f}%")

print("\n--- ALL QUALIFIED CROSSOVERS & EXACT TARGET OUTCOMES ---")
for r in results:
    print(f"[{r['time']} UTC] {r['symbol']:<10} {r['tf']:<4} {r['signal']:<4} ({r['stars']} Stars) Entry={r['entry']:<10.4f} -> {r['outcome']}")
