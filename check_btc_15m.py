import ccxt
import pandas as pd
from indicators import _compute_indicators, analyze_crossovers, get_trend_state

exchange = ccxt.binance()
ohlcv = exchange.fetch_ohlcv('BTC/USDT', '15m', limit=30)
df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
df.set_index('timestamp', inplace=True)
df = _compute_indicators(df)

print("=== BTC 15M LAST 20 CANDLES DETAILED AUDIT ===")
for i in range(len(df)-20, len(df)):
    row = df.iloc[i]
    prev = df.iloc[i-1]
    cross = ""
    if prev['ema_9'] <= prev['ema_26'] and row['ema_9'] > row['ema_26']:
        cross = "*** BULLISH CROSSOVER (BUY) ***"
    elif prev['ema_9'] >= prev['ema_26'] and row['ema_9'] < row['ema_26']:
        cross = "*** BEARISH CROSSOVER (SELL) ***"
    ts_str = str(df.index[i])
    print(f"{ts_str} UTC | Close={row['close']:.2f} | EMA9={row['ema_9']:.2f} | EMA26={row['ema_26']:.2f} | Vol={row['volume']:.1f} | {cross}")

print("\n=== HTF TREND STATES AT THIS MOMENT ===")
for tf in ['30m', '1h', '4h', '1d']:
    o = exchange.fetch_ohlcv('BTC/USDT', tf, limit=50)
    d = pd.DataFrame(o, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    d['timestamp'] = pd.to_datetime(d['timestamp'], unit='ms')
    d.set_index('timestamp', inplace=True)
    d = _compute_indicators(d)
    print(f"TF {tf.upper()}: Trend = {get_trend_state(d)}")
