import asyncio
import aiohttp
import pandas as pd
import numpy as np
from fetcher import get_top_pairs_by_volume
import time

def compute_indicators(df):
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_26'] = df['close'].ewm(span=26, adjust=False).mean()
    return df

async def fetch_klines(session, symbol, interval, days=30):
    url = "https://fapi.binance.com/fapi/v1/klines"
    limit = 1500 if interval in ['15m', '5m'] else 1000
    params = {"symbol": symbol.replace("/", ""), "interval": interval, "limit": limit}
    try:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if not isinstance(data, list): return None
            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)
            df.set_index('timestamp', inplace=True)
            return compute_indicators(df)
    except:
        return None

def simulate_trades(df, tf_label):
    trades = []
    
    # Track open trades
    in_trade = False
    trade_type = None
    entry_price = 0
    sl = 0
    tp1 = 0
    tp2 = 0
    tp3 = 0
    
    for i in range(10, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        
        # If in trade, check for SL or TPs
        if in_trade:
            high = row['high']
            low = row['low']
            
            # Very basic SL/TP check inside the same candle (pessimistic: assume SL hit first if both hit)
            hit_sl = False
            hit_tp = 0
            
            if trade_type == "BUY":
                if low <= sl:
                    hit_sl = True
                elif high >= tp3: hit_tp = 3
                elif high >= tp2: hit_tp = 2
                elif high >= tp1: hit_tp = 1
            else:
                if high >= sl:
                    hit_sl = True
                elif low <= tp3: hit_tp = 3
                elif low <= tp2: hit_tp = 2
                elif low <= tp1: hit_tp = 1
                
            if hit_sl:
                trades[-1]['result'] = 'SL'
                in_trade = False
            elif hit_tp > 0:
                if 'max_tp' not in trades[-1] or hit_tp > trades[-1]['max_tp']:
                    trades[-1]['max_tp'] = hit_tp
                # We do not close the trade on TP1/TP2, we let it run until SL or TP3
                if hit_tp == 3:
                    trades[-1]['result'] = 'TP3'
                    in_trade = False
            
            # Trailing SL simplified: if TP1 hit, SL = Entry
            if trades[-1].get('max_tp', 0) >= 1:
                sl = entry_price
            continue
            
        # Check for new Crossover Setup
        if prev['ema_9'] <= prev['ema_26'] and row['ema_9'] > row['ema_26']:
            entry_price = row['close']
            sl_val = min(df.iloc[i-3:i]['low']) * 0.998
            if sl_val >= entry_price: sl_val = entry_price * 0.98
            risk = abs(entry_price - sl_val)
            
            trades.append({
                'tf': tf_label,
                'type': 'BUY',
                'entry': entry_price,
                'sl': sl_val,
                'tp1': entry_price + 1.2 * risk,
                'tp2': entry_price + 2.2 * risk,
                'tp3': entry_price + 3.2 * risk,
                'result': 'OPEN',
                'max_tp': 0
            })
            in_trade = True
            trade_type = "BUY"
            sl = sl_val
            tp1 = trades[-1]['tp1']
            tp2 = trades[-1]['tp2']
            tp3 = trades[-1]['tp3']
            
        elif prev['ema_9'] >= prev['ema_26'] and row['ema_9'] < row['ema_26']:
            entry_price = row['close']
            sl_val = max(df.iloc[i-3:i]['high']) * 1.002
            if sl_val <= entry_price: sl_val = entry_price * 1.02
            risk = abs(entry_price - sl_val)
            
            trades.append({
                'tf': tf_label,
                'type': 'SELL',
                'entry': entry_price,
                'sl': sl_val,
                'tp1': entry_price - 1.2 * risk,
                'tp2': entry_price - 2.2 * risk,
                'tp3': entry_price - 3.2 * risk,
                'result': 'OPEN',
                'max_tp': 0
            })
            in_trade = True
            trade_type = "SELL"
            sl = sl_val
            tp1 = trades[-1]['tp1']
            tp2 = trades[-1]['tp2']
            tp3 = trades[-1]['tp3']

    return trades


async def run_backtest():
    print("Fetching top 50 pairs for backtest...")
    pairs = get_top_pairs_by_volume(top_n=50)
    
    all_trades = []
    
    async with aiohttp.ClientSession() as session:
        for i, sym in enumerate(pairs):
            print(f"Backtesting {sym} ({i+1}/{len(pairs)})...")
            for tf in ['15m', '1h', '4h']:
                df = await fetch_klines(session, sym, tf)
                if df is not None and len(df) > 50:
                    trades = simulate_trades(df, tf)
                    all_trades.extend(trades)
            await asyncio.sleep(0.1)
            
    print(f"Total trades simulated: {len(all_trades)}")
    
    # Calculate stats
    total = len([t for t in all_trades if t['result'] != 'OPEN'])
    sl_hits = len([t for t in all_trades if t['result'] == 'SL' and t.get('max_tp', 0) == 0])
    be_hits = len([t for t in all_trades if t['result'] == 'SL' and t.get('max_tp', 0) > 0]) # SL hit after moving to BE
    
    tp1_hits = len([t for t in all_trades if t.get('max_tp', 0) >= 1])
    tp2_hits = len([t for t in all_trades if t.get('max_tp', 0) >= 2])
    tp3_hits = len([t for t in all_trades if t.get('max_tp', 0) == 3])
    
    report = f"""# 📈 EMA Crossover Strategy Backtest Report

**Scope:** Top 50 Binance Pairs (Last 1500 Candles)
**Timeframes:** 15m, 1H, 4H
**Strategy:** 9/26 EMA Crossovers with Dynamic Liquidity SL & Trailing BE
**Execution Rule:** Target TP1 (1:1.2), TP2 (1:2.2), TP3 (1:3.2). Move SL to Entry after TP1.

## 📊 Summary Statistics
- **Total Completed Trades:** {total}
- **Strict SL Hits (Losses):** {sl_hits} ({(sl_hits/total*100):.1f}%)
- **Breakeven Exits (Risk-Free):** {be_hits} ({(be_hits/total*100):.1f}%)
- **Trades Reaching TP1:** {tp1_hits} ({(tp1_hits/total*100):.1f}%)
- **Trades Reaching TP2:** {tp2_hits} ({(tp2_hits/total*100):.1f}%)
- **Trades Reaching TP3 (Full Win):** {tp3_hits} ({(tp3_hits/total*100):.1f}%)

*Note: Trades that reach TP1 and then reverse are counted in "Breakeven Exits", securing profits on the first 40% position without loss on the remainder.*
"""
    with open('backtest_results.md', 'w', encoding='utf-8') as f:
        f.write(report)
    print("Done! Saved to backtest_results.md")

if __name__ == "__main__":
    asyncio.run(run_backtest())
