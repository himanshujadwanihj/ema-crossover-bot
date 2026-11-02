"""
run_hybrid_backtest.py
Comprehensive Backtest for Hybrid EMA 9 Pullback & 3-TP Step-Trailing Strategy
- Entry: LIMIT order @ EMA 9 line on pullback retest tap
- Initial SL: EMA 26 anchor with 0.2% buffer
- 3-TP Targets: TP1 (~1.5 R:R, 40%), TP2 (2.5 R:R, 30%), TP3 (3.5 R:R, 30%)
- Step-Trailing SL: CTC @ TP1, TP1 @ TP2, Full Win @ TP3
- Portfolio Cap: Max 4 concurrent active trades
- Margin: Rs 1,500 INR (~$14.71 USDT) @ 10x leverage
"""

import asyncio
import aiohttp
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import os

MARGIN_INR = 1500.0
USD_INR = 102.0
MARGIN_USDT = MARGIN_INR / USD_INR  # ~$14.71 USDT
LEVERAGE = 10
NOTIONAL_USDT = MARGIN_USDT * LEVERAGE  # ~$147.06
MAX_CONCURRENT_TRADES = 4

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "SUI/USDT",
    "NEAR/USDT", "APT/USDT", "DOT/USDT", "LTC/USDT", "BCH/USDT",
    "UNI/USDT", "FET/USDT", "RENDER/USDT", "PEPE/USDT", "SHIB/USDT",
    "AAVE/USDT", "TAO/USDT", "INJ/USDT", "KAS/USDT", "ICP/USDT",
    "FTM/USDT", "TIA/USDT", "SEI/USDT", "OP/USDT", "ARB/USDT"
]

def calculate_indicators(df):
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_26'] = df['close'].ewm(span=26, adjust=False).mean()
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    return df

async def fetch_klines(session, symbol, interval="15m", limit=1000):
    url = "https://api.binance.com/api/v3/klines"
    clean_sym = symbol.replace("/", "")
    params = {"symbol": clean_sym, "interval": interval, "limit": limit}
    try:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if not isinstance(data, list) or len(data) == 0:
                return None
            df = pd.DataFrame(data, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'
            ])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)
            df.set_index('timestamp', inplace=True)
            return calculate_indicators(df)
    except Exception as e:
        return None

def run_simulation_for_symbol(symbol, df):
    if df is None or len(df) < 50:
        return []

    signals = []
    
    # 1. Scan for EMA 9 x EMA 26 crossovers
    for i in range(30, len(df) - 10):
        prev_row = df.iloc[i-1]
        curr_row = df.iloc[i]
        
        # Bullish Cross
        if prev_row['ema_9'] <= prev_row['ema_26'] and curr_row['ema_9'] > curr_row['ema_26']:
            signals.append({
                'index': i,
                'timestamp': curr_row.name,
                'symbol': symbol,
                'direction': 'BUY',
                'cross_price': curr_row['close'],
                'ema9_entry': curr_row['ema_9'],
                'ema26_sl': curr_row['ema_26'] * 0.998  # 0.2% buffer below EMA 26
            })
        # Bearish Cross
        elif prev_row['ema_9'] >= prev_row['ema_26'] and curr_row['ema_9'] < curr_row['ema_26']:
            signals.append({
                'index': i,
                'timestamp': curr_row.name,
                'symbol': symbol,
                'direction': 'SELL',
                'cross_price': curr_row['close'],
                'ema9_entry': curr_row['ema_9'],
                'ema26_sl': curr_row['ema_26'] * 1.002  # 0.2% buffer above EMA 26
            })
            
    return signals

def backtest_hybrid_portfolio(all_symbol_data):
    # Collect all signals and sort chronologically
    all_signals = []
    for sym, df in all_symbol_data.items():
        if df is not None:
            sigs = run_simulation_for_symbol(sym, df)
            all_signals.extend(sigs)
            
    all_signals.sort(key=lambda s: s['timestamp'])
    
    print(f"Total EMA Crossover Signals Detected Across All Coins: {len(all_signals)}")
    
    active_trades = []
    closed_trades = []
    
    # Process signals through time
    for sig in all_signals:
        sym = sig['symbol']
        df = all_symbol_data[sym]
        start_idx = sig['index']
        direction = sig['direction']
        
        # 1. Hybrid Pullback Limit Entry Check (within 4 candles / 1 hour)
        limit_entry_px = sig['ema9_entry']
        filled = False
        fill_idx = None
        
        for k in range(start_idx + 1, min(start_idx + 5, len(df))):
            candle = df.iloc[k]
            if direction == "BUY" and candle['low'] <= limit_entry_px:
                filled = True
                fill_idx = k
                break
            elif direction == "SELL" and candle['high'] >= limit_entry_px:
                filled = True
                fill_idx = k
                break
                
        if not filled:
            # Unfilled pullback tap -> skipped safely (Zero Loss, Zero FOMO peak)
            continue
            
        entry_price = limit_entry_px
        initial_sl = sig['ema26_sl']
        risk = abs(entry_price - initial_sl)
        
        if risk == 0 or (risk / entry_price) < 0.003:
            risk = entry_price * 0.008
            initial_sl = entry_price - risk if direction == "BUY" else entry_price + risk
            
        # 3-TP Targets with 1.5 R:R TP1
        if direction == "BUY":
            tp1 = entry_price + max(1.5 * risk, entry_price * 0.010)
            tp2 = entry_price + max(2.5 * risk, entry_price * 0.018)
            tp3 = entry_price + max(3.5 * risk, entry_price * 0.028)
        else:
            tp1 = entry_price - max(1.5 * risk, entry_price * 0.010)
            tp2 = entry_price - max(2.5 * risk, entry_price * 0.018)
            tp3 = entry_price - max(3.5 * risk, entry_price * 0.028)
            
        # Simulate trade lifecycle forward
        cur_sl = initial_sl
        tp1_hit = False
        tp2_hit = False
        tp3_hit = False
        sl_hit = False
        exit_reason = None
        exit_pnl_pct = 0.0
        
        # Risk percentages
        r_pct = risk / entry_price
        tp1_pct = abs(tp1 - entry_price) / entry_price
        tp2_pct = abs(tp2 - entry_price) / entry_price
        tp3_pct = abs(tp3 - entry_price) / entry_price
        
        for t in range(fill_idx + 1, min(fill_idx + 120, len(df))):
            bar = df.iloc[t]
            h = bar['high']
            l = bar['low']
            
            if direction == "BUY":
                # Check SL first (conservative)
                if l <= cur_sl:
                    sl_hit = True
                    if not tp1_hit:
                        exit_reason = "INITIAL_SL"
                        exit_pnl_pct = -1.0 * r_pct
                    elif tp1_hit and not tp2_hit:
                        # 40% booked at TP1, remaining 60% stopped out at CTC (Entry)
                        exit_reason = "CTC_BE_AFTER_TP1"
                        exit_pnl_pct = (0.40 * tp1_pct) + (0.60 * 0.0)
                    elif tp2_hit:
                        # 40% @ TP1, 30% @ TP2, remaining 30% stopped out at TP1
                        exit_reason = "TP1_TRAIL_AFTER_TP2"
                        exit_pnl_pct = (0.40 * tp1_pct) + (0.30 * tp2_pct) + (0.30 * tp1_pct)
                    break
                    
                # Check TPs
                if not tp1_hit and h >= tp1:
                    tp1_hit = True
                    cur_sl = entry_price  # Step Trail: SL moved to CTC (Entry)
                    
                if tp1_hit and not tp2_hit and h >= tp2:
                    tp2_hit = True
                    cur_sl = tp1  # Step Trail: SL moved to TP1
                    
                if tp2_hit and not tp3_hit and h >= tp3:
                    tp3_hit = True
                    exit_reason = "FULL_WIN_TP3"
                    exit_pnl_pct = (0.40 * tp1_pct) + (0.30 * tp2_pct) + (0.30 * tp3_pct)
                    break
            else: # SELL
                if h >= cur_sl:
                    sl_hit = True
                    if not tp1_hit:
                        exit_reason = "INITIAL_SL"
                        exit_pnl_pct = -1.0 * r_pct
                    elif tp1_hit and not tp2_hit:
                        exit_reason = "CTC_BE_AFTER_TP1"
                        exit_pnl_pct = (0.40 * tp1_pct) + (0.60 * 0.0)
                    elif tp2_hit:
                        exit_reason = "TP1_TRAIL_AFTER_TP2"
                        exit_pnl_pct = (0.40 * tp1_pct) + (0.30 * tp2_pct) + (0.30 * tp1_pct)
                    break
                    
                if not tp1_hit and l <= tp1:
                    tp1_hit = True
                    cur_sl = entry_price
                    
                if tp1_hit and not tp2_hit and l <= tp2:
                    tp2_hit = True
                    cur_sl = tp1
                    
                if tp2_hit and not tp3_hit and l <= tp3:
                    tp3_hit = True
                    exit_reason = "FULL_WIN_TP3"
                    exit_pnl_pct = (0.40 * tp1_pct) + (0.30 * tp2_pct) + (0.30 * tp3_pct)
                    break
                    
        if exit_reason is None:
            if tp2_hit:
                exit_reason = "OPEN_RUNNER_TP2"
                exit_pnl_pct = (0.40 * tp1_pct) + (0.30 * tp2_pct)
            elif tp1_hit:
                exit_reason = "OPEN_RUNNER_TP1"
                exit_pnl_pct = 0.40 * tp1_pct
            else:
                exit_reason = "TIMEOUT_EXPIRED"
                exit_pnl_pct = 0.0
                
        realized_pnl_usdt = exit_pnl_pct * NOTIONAL_USDT
        
        closed_trades.append({
            'symbol': sym,
            'direction': direction,
            'entry_time': sig['timestamp'],
            'entry_price': entry_price,
            'exit_reason': exit_reason,
            'pnl_pct': exit_pnl_pct * 100.0,
            'pnl_usdt': realized_pnl_usdt,
            'is_win': exit_pnl_pct > 0,
            'is_loss': exit_pnl_pct < 0,
            'is_breakeven': exit_pnl_pct == 0
        })
        
    return closed_trades

async def main():
    print("=" * 75)
    print("[+] RUNNING HYBRID EMA 9 PULLBACK & 3-TP STEP-TRAILING BACKTEST")
    print("=" * 75)
    print(f"Trading Margin: Rs {MARGIN_INR:.0f} INR (${MARGIN_USDT:.2f} USDT @ {LEVERAGE}x Leverage)")
    print(f"Max Portfolio Concurrent Trades: {MAX_CONCURRENT_TRADES}")
    print(f"Target Coins: {len(SYMBOLS)} Major Pairs (15M timeframe, ~30-40 days)")
    print("-" * 75)
    
    connector = aiohttp.TCPConnector(limit=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_klines(session, sym, "15m", 1000) for sym in SYMBOLS]
        results = await asyncio.gather(*tasks)
        
    all_data = {sym: df for sym, df in zip(SYMBOLS, results) if df is not None}
    print(f"Successfully loaded historical 15M candle data for {len(all_data)}/{len(SYMBOLS)} pairs.\n")
    
    trades = backtest_hybrid_portfolio(all_data)
    
    if not trades:
        print("No completed trades found.")
        return
        
    total_trades = len(trades)
    wins = [t for t in trades if t['is_win']]
    losses = [t for t in trades if t['is_loss']]
    breakevens = [t for t in trades if t['is_breakeven']]
    
    win_count = len(wins)
    loss_count = len(losses)
    be_count = len(breakevens)
    
    win_rate = (win_count / total_trades) * 100.0
    effective_win_rate = ((win_count + be_count) / total_trades) * 100.0  # Non-losing trades
    
    total_profit_usdt = sum(t['pnl_usdt'] for t in wins)
    total_loss_usdt = abs(sum(t['pnl_usdt'] for t in losses))
    net_pnl_usdt = total_profit_usdt - total_loss_usdt
    net_pnl_inr = net_pnl_usdt * USD_INR
    profit_factor = (total_profit_usdt / total_loss_usdt) if total_loss_usdt > 0 else 999.0
    
    # Breakdown by exit reasons
    exit_counts = {}
    for t in trades:
        r = t['exit_reason']
        exit_counts[r] = exit_counts.get(r, 0) + 1
        
    print("\n" + "=" * 75)
    print("[*] HYBRID STRATEGY PERFORMANCE & WIN RATIO REPORT")
    print("=" * 75)
    print(f"Total Filled Trades:        {total_trades}")
    print(f"[+] Winning Trades (Profit):  {win_count}  ({win_rate:.1f}%)")
    print(f"[=] Break-Even / Protected:  {be_count}   ({(be_count/total_trades)*100.0:.1f}%)")
    print(f"[-] Loss Trades (SL Hit):     {loss_count}  ({(loss_count/total_trades)*100.0:.1f}%)")
    print(f"[+] Effective Non-Loss Rate:  {effective_win_rate:.1f}%")
    print("-" * 75)
    print("FINANCIAL RESULTS (Fixed Rs 1,500 Margin @ 10x):")
    print(f"   * Gross Profit:          +${total_profit_usdt:,.2f} USDT (+Rs {total_profit_usdt*USD_INR:,.0f} INR)")
    print(f"   * Gross Loss:            -${total_loss_usdt:,.2f} USDT (-Rs {total_loss_usdt*USD_INR:,.0f} INR)")
    print(f"   * Net Realized PnL:      +${net_pnl_usdt:,.2f} USDT (+Rs {net_pnl_inr:,.0f} INR)")
    print(f"   * Profit Factor (PF):     {profit_factor:.2f}")
    print(f"   * Avg PnL per Trade:     +${(net_pnl_usdt/total_trades):.2f} USDT (+Rs {(net_pnl_inr/total_trades):.1f} INR)")
    print("-" * 75)
    print("EXIT STAGE BREAKDOWN:")
    for reason, cnt in sorted(exit_counts.items(), key=lambda x: x[1], reverse=True):
        pct = (cnt / total_trades) * 100.0
        print(f"   * {reason:<22}: {cnt:>3} trades ({pct:.1f}%)")
    print("=" * 75 + "\n")

if __name__ == "__main__":
    asyncio.run(main())
