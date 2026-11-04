import ccxt
import pandas as pd
import sys
from indicators import _compute_indicators, get_trend_state

exchange = ccxt.binance()
symbol = 'BTC/USDT'
print('=== BTC/USDT LIVE EMA CROSSOVER & STATUS AUDIT ===')

tf_data = {}
for tf in ['15m', '30m', '1h', '4h', '1d']:
    ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df = _compute_indicators(df)
    tf_data[tf.upper()] = df
    
    last_closed = df.iloc[-2]
    trend = get_trend_state(df)
    
    last_cross_bars_ago = -1
    last_cross_type = None
    last_cross_time = None
    for i in range(len(df)-2, 1, -1):
        r = df.iloc[i]
        pr = df.iloc[i-1]
        if pr['ema_9'] <= pr['ema_26'] and r['ema_9'] > r['ema_26']:
            last_cross_bars_ago = (len(df) - 2) - i
            last_cross_type = 'BULLISH (BUY)'
            last_cross_time = df.index[i]
            break
        elif pr['ema_9'] >= pr['ema_26'] and r['ema_9'] < r['ema_26']:
            last_cross_bars_ago = (len(df) - 2) - i
            last_cross_type = 'BEARISH (SELL)'
            last_cross_time = df.index[i]
            break
            
    print(f"TF {tf.upper()}: Trend={trend} | Last Closed Price={last_closed['close']:.2f} | EMA9={last_closed['ema_9']:.2f} | EMA26={last_closed['ema_26']:.2f}")
    if last_cross_bars_ago >= 0:
        print(f"   -> Last Crossover was {last_cross_bars_ago} candles ago on {last_cross_time} UTC ({last_cross_type})")
    else:
        print("   -> No crossover found in last 100 candles")

print("\n=== SUMMARY OF WHY NO FRESH CROSSOVER RIGHT NOW ===")
