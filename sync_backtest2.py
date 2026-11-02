import requests
import pandas as pd
import time

def compute_indicators(df):
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_26'] = df['close'].ewm(span=26, adjust=False).mean()
    
    # ATR 14
    df['tr0'] = abs(df['high'] - df['low'])
    df['tr1'] = abs(df['high'] - df['close'].shift(1))
    df['tr2'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
    df['atr'] = df['tr'].rolling(14).mean()
    
    return df

def fetch_data(symbol, interval, limit=1500):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=5)
        data = r.json()
        if not isinstance(data, list): return None
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return compute_indicators(df)
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return None

def backtest():
    # Hardcoded top 20 list to avoid binance exchangeInfo endpoint hang
    pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT", "NEARUSDT", "DOTUSDT", "LTCUSDT", "SHIBUSDT", "TRXUSDT", "WIFUSDT", "BCHUSDT", "UNIUSDT", "PEPEUSDT", "APTUSDT"]
    
    all_trades = []
    
    for symbol in pairs:
        print(f"Backtesting {symbol}...")
        for tf in ['15m', '1h', '4h']:
            df = fetch_data(symbol, tf, 1500)
            if df is None: continue
            
            in_trade = False
            trade_type = None
            entry = 0
            sl = 0
            tp1, tp2, tp3 = 0, 0, 0
            
            for i in range(15, len(df)):
                row = df.iloc[i]
                prev = df.iloc[i-1]
                
                if in_trade:
                    high = row['high']
                    low = row['low']
                    hit_sl = False
                    hit_tp = 0
                    
                    if trade_type == "BUY":
                        if low <= sl: hit_sl = True
                        elif high >= tp3: hit_tp = 3
                        elif high >= tp2: hit_tp = 2
                        elif high >= tp1: hit_tp = 1
                    else:
                        if high >= sl: hit_sl = True
                        elif low <= tp3: hit_tp = 3
                        elif low <= tp2: hit_tp = 2
                        elif low <= tp1: hit_tp = 1
                        
                    if hit_sl:
                        all_trades[-1]['result'] = 'SL'
                        in_trade = False
                    elif hit_tp > 0:
                        if hit_tp > all_trades[-1]['max_tp']:
                            all_trades[-1]['max_tp'] = hit_tp
                        if hit_tp == 3:
                            all_trades[-1]['result'] = 'TP3'
                            in_trade = False
                        
                        # Trailing SL
                        if hit_tp >= 1:
                            sl = entry
                    continue
                
                # New crossover setup
                if prev['ema_9'] <= prev['ema_26'] and row['ema_9'] > row['ema_26']:
                    entry = row['close']
                    atr_val = row['atr'] if not pd.isna(row['atr']) else entry * 0.005
                    
                    sl = row['ema_26'] - atr_val
                    if sl >= entry:
                        swing_low = min(df.iloc[i-3:i]['low'])
                        sl = swing_low - atr_val
                        if sl >= entry: sl = entry - (atr_val * 2)
                        
                    risk = abs(entry - sl)
                    
                    all_trades.append({
                        'symbol': symbol,
                        'tf': tf,
                        'type': 'BUY',
                        'result': 'OPEN',
                        'max_tp': 0
                    })
                    tp1 = entry + 1.2 * risk
                    tp2 = entry + 2.2 * risk
                    tp3 = entry + 3.2 * risk
                    in_trade = True
                    trade_type = "BUY"
                    
                elif prev['ema_9'] >= prev['ema_26'] and row['ema_9'] < row['ema_26']:
                    entry = row['close']
                    atr_val = row['atr'] if not pd.isna(row['atr']) else entry * 0.005
                    
                    sl = row['ema_26'] + atr_val
                    if sl <= entry:
                        swing_high = max(df.iloc[i-3:i]['high'])
                        sl = swing_high + atr_val
                        if sl <= entry: sl = entry + (atr_val * 2)
                        
                    risk = abs(entry - sl)
                    
                    all_trades.append({
                        'symbol': symbol,
                        'tf': tf,
                        'type': 'SELL',
                        'result': 'OPEN',
                        'max_tp': 0
                    })
                    tp1 = entry - 1.2 * risk
                    tp2 = entry - 2.2 * risk
                    tp3 = entry - 3.2 * risk
                    in_trade = True
                    trade_type = "SELL"
                    
        time.sleep(0.5)

    print("Done! Generating report...")
    total = len([t for t in all_trades if t['result'] != 'OPEN'])
    sl_hits = len([t for t in all_trades if t['result'] == 'SL' and t['max_tp'] == 0])
    be_hits = len([t for t in all_trades if t['result'] == 'SL' and t['max_tp'] > 0])
    tp1_hits = len([t for t in all_trades if t['max_tp'] >= 1])
    tp2_hits = len([t for t in all_trades if t['max_tp'] >= 2])
    tp3_hits = len([t for t in all_trades if t['max_tp'] == 3])

    report = f"""# 📈 EMA Crossover (ATR Edition) Backtest

**Scope:** Top 20 Binance Pairs (Last 1500 Candles)
**Timeframes:** 15m, 1H, 4H
**Strategy:** 9/26 EMA Crossovers with ATR Stop Buffer & Trailing BE

## 📊 Comparison vs Fixed Margin (Previous Run)
- **Previous SL Hit Rate:** 56.5%
- **NEW ATR SL Hit Rate:** {(sl_hits/total*100):.1f}%

## 📊 Summary Statistics
- **Total Completed Trades:** {total}
- **Strict SL Hits (Losses):** {sl_hits} ({(sl_hits/total*100):.1f}%)
- **Breakeven Exits (Risk-Free):** {be_hits} ({(be_hits/total*100):.1f}%)
- **Trades Reaching TP1 (1:1.2):** {tp1_hits} ({(tp1_hits/total*100):.1f}%)
- **Trades Reaching TP2 (1:2.2):** {tp2_hits} ({(tp2_hits/total*100):.1f}%)
- **Trades Reaching TP3 (1:3.2):** {tp3_hits} ({(tp3_hits/total*100):.1f}%)
"""
    with open('backtest_atr_final.md', 'w', encoding='utf-8') as f:
        f.write(report)
        
    print("FINISHED")

if __name__ == "__main__":
    backtest()
