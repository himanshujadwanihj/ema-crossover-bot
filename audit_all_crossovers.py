import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from fetcher import get_top_pairs_by_volume
from indicators import _compute_indicators, analyze_crossovers, get_trend_state

exchange = ccxt.binance()
symbols = get_top_pairs_by_volume(60)

print(f"=== FULL MARKET CROSSOVER AUDIT ACROSS {len(symbols)} PAIRS ===")

crossovers_found = 0
rejections = []

for symbol in symbols:
    tf_data = {}
    for tf in ["15m", "30m", "1h", "4h", "1d"]:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df = _compute_indicators(df)
            tf_data[tf.upper()] = df
        except Exception as e:
            pass
            
    for label in ["15M", "30M", "1H", "4H", "1D"]:
        df = tf_data.get(label)
        if df is None or len(df) < 50: continue
        
        row = df.iloc[-2]
        prev = df.iloc[-3]
        
        is_buy_cross = (prev['ema_9'] <= prev['ema_26']) and (row['ema_9'] > row['ema_26'])
        is_sell_cross = (prev['ema_9'] >= prev['ema_26']) and (row['ema_9'] < row['ema_26'])
        
        if is_buy_cross or is_sell_cross:
            crossovers_found += 1
            sig_type = "BUY" if is_buy_cross else "SELL"
            candle_time = df.index[-2]
            
            # Check 180s freshness
            now_utc = datetime.now(timezone.utc)
            if candle_time.tzinfo is None:
                candle_time = candle_time.replace(tzinfo=timezone.utc)
            tf_sec = {"15M": 900, "30M": 1800, "1H": 3600, "4H": 14400, "1D": 86400}.get(label, 3600)
            candle_close = candle_time + timedelta(seconds=tf_sec)
            sec_since_close = (now_utc - candle_close).total_seconds()
            
            # Check indicators filters
            vol_sma = row.get('vol_sma_20', 0)
            adx_val = row.get('adx', 0)
            body_ratio = abs(row['close'] - row['open']) / max(row['high'] - row['low'], 1e-9)
            proximity = abs(row['close'] - row['ema_26']) / row['close']
            anti_chop = abs(row['ema_9'] - row['ema_26']) / row['close']
            
            passes_freshness = 0 <= sec_since_close <= 180
            passes_vol = row['volume'] > (vol_sma * 1.2) if vol_sma > 0 else True
            passes_adx = adx_val > 15.0 if not pd.isna(adx_val) else True
            passes_body = body_ratio >= 0.25
            passes_proximity = proximity <= 0.025
            passes_antichop = anti_chop >= 0.0008
            
            tf_states = {t: get_trend_state(tf_data.get(t)) for t in ["1H", "4H", "1D", "1W"] if t in tf_data}
            target_trend = "bullish" if sig_type == "BUY" else "bearish"
            htf_aligned = sum(1 for t, st in tf_states.items() if st == target_trend)
            
            print(f"\n[CROSSOVER DETECTED] {symbol} {label} {sig_type}")
            print(f"   Candle Close (UTC): {candle_close} | Secs Since Close: {sec_since_close:.1f}s | Freshness Pass (<=180s): {passes_freshness}")
            print(f"   Vol Surge Pass (1.2x): {passes_vol} ({row['volume']:.1f} vs {vol_sma*1.2:.1f})")
            print(f"   ADX Pass (>15.0): {passes_adx} (ADX={adx_val:.1f})")
            print(f"   Body Ratio Pass (>=25%): {passes_body} ({body_ratio*100:.1f}%)")
            print(f"   Proximity Pass (<=2.5%): {passes_proximity} ({proximity*100:.2f}%)")
            print(f"   Anti-Chop Pass (>=0.08%): {passes_antichop} ({anti_chop*100:.3f}%)")
            print(f"   HTF Alignment: {htf_aligned}/4 aligned with {sig_type} ({tf_states})")

if crossovers_found == 0:
    print("\nNo crossovers occurred on the last completed candle across all 60 pairs.")
