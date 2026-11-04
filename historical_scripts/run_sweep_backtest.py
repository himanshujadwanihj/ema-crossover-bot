import os
import json
import ccxt
import pandas as pd
import numpy as np

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "SUI/USDT",
    "NEAR/USDT", "DOT/USDT", "LTC/USDT", "UNI/USDT", "AAVE/USDT",
    "APT/USDT", "TAO/USDT", "ENA/USDT", "RENDER/USDT", "OP/USDT",
    "XAU/USDT:USDT" # Spot Gold
]

def compute_indicators(df):
    df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_26"] = df["close"].ewm(span=26, adjust=False).mean()
    df["vol_sma"] = df["volume"].rolling(20).mean()
    
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df

def run_backtest():
    ex = ccxt.bybit({'enableRateLimit': True, 'timeout': 10000})
    
    all_signals = []
    print("Fetching 1H candle history across 20+ pairs (including Gold)...")
    
    for sym in SYMBOLS:
        try:
            raw_candles = ex.fetch_ohlcv(sym, "1h", limit=1000)
            if not raw_candles or len(raw_candles) < 100:
                continue
            df = pd.DataFrame(raw_candles, columns=["time", "open", "high", "low", "close", "volume"])
            df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
            df.set_index("time", inplace=True)
            df = compute_indicators(df)
            
            # Detect valid pullback / crossover entries
            last_bar = -999
            for i in range(35, len(df) - 30):
                if i - last_bar < 5:  # cooldown
                    continue
                
                row = df.iloc[i]
                prev = df.iloc[i-1]
                
                bullish = row["ema_9"] > row["ema_26"]
                bearish = row["ema_9"] < row["ema_26"]
                
                touch_thresh = 0.0075
                # Pullback touch check
                tapped_buy = bullish and (row["low"] <= row["ema_26"] <= row["high"] or row["low"] <= row["ema_9"] <= row["high"]) and (abs(row["close"] - row["ema_9"]) / row["ema_9"] <= touch_thresh)
                tapped_sell = bearish and (row["low"] <= row["ema_26"] <= row["high"] or row["low"] <= row["ema_9"] <= row["high"]) and (abs(row["close"] - row["ema_9"]) / row["ema_9"] <= touch_thresh)
                
                # Fresh crossover check
                cross_buy = (prev["ema_9"] <= prev["ema_26"]) and (row["ema_9"] > row["ema_26"])
                cross_sell = (prev["ema_9"] >= prev["ema_26"]) and (row["ema_9"] < row["ema_26"])
                
                direction = None
                if tapped_buy or cross_buy:
                    direction = "BUY"
                elif tapped_sell or cross_sell:
                    direction = "SELL"
                    
                if not direction:
                    continue
                    
                entry = float(row["close"])
                atr = float(row["atr"]) if not np.isnan(row["atr"]) else entry * 0.015
                
                # Recent 5-candle sweep low/high
                recent_5 = df.iloc[i-4:i+1]
                sweep_low = float(recent_5["low"].min())
                sweep_high = float(recent_5["high"].max())
                
                # Subsequent 30 candles trajectory for simulation
                forward_candles = df.iloc[i+1:i+31]
                
                all_signals.append({
                    "symbol": sym,
                    "index": i,
                    "direction": direction,
                    "entry": entry,
                    "atr": atr,
                    "sweep_low": sweep_low,
                    "sweep_high": sweep_high,
                    "forward": forward_candles
                })
                last_bar = i
        except Exception as e:
            print(f"Error fetching {sym}: {e}")
            
    print(f"Total historical trade setups collected: {len(all_signals)}")
    
    # ── SIMULATE 3 MODELS ──────────────────────────────────────────────────────────
    # Model 1: Baseline ATR SL (1.5x ATR)
    # Model 2: Exact Sweep Wick Tip (0 Buffer)
    # Model 3: Human-Eye Sweep Wick + 0.25*ATR Buffer
    
    def simulate_model(signals, sl_mode):
        wins = 0
        losses = 0
        full_moon_wins = 0
        trailed_sl_wins = 0
        total_pnl_r = 0.0
        r_multiples = []
        
        for s in signals:
            entry = s["entry"]
            atr = s["atr"]
            direction = s["direction"]
            fw = s["forward"]
            
            if direction == "BUY":
                if sl_mode == "baseline":
                    sl = entry - (1.5 * atr)
                elif sl_mode == "sweep_exact":
                    sl = s["sweep_low"] * 0.9995  # exact wick
                elif sl_mode == "sweep_buffer":
                    sl = s["sweep_low"] - (0.25 * atr)  # sweep + buffer
                
                if sl >= entry:
                    sl = entry - (0.01 * entry)
                    
                risk = entry - sl
                tp1 = entry + (1.6 * risk)
                tp2 = entry + (3.2 * risk)
                profit_lock_sl = entry + (0.4 * risk) # +0.4R lock
                
                tp1_hit = False
                tp2_hit = False
                sl_hit = False
                cur_sl = sl
                
                for _, bar in fw.iterrows():
                    h = bar["high"]
                    l = bar["low"]
                    
                    if not tp1_hit:
                        # Check SL before TP1
                        if l <= cur_sl:
                            sl_hit = True
                            break
                        if h >= tp1:
                            tp1_hit = True
                            cur_sl = profit_lock_sl # Trail to +0.4R
                    else:
                        # After TP1: check TP2 or trailed SL
                        if h >= tp2:
                            tp2_hit = True
                            break
                        if l <= cur_sl:
                            # Trailed SL hit
                            break
                            
                # Calculate trade R-multiple
                if not tp1_hit and sl_hit:
                    # Pure loss
                    losses += 1
                    trade_r = -1.0
                elif tp1_hit and tp2_hit:
                    # Full Win (70% @ 1.6R + 30% @ 3.2R)
                    wins += 1
                    full_moon_wins += 1
                    trade_r = (0.70 * 1.6) + (0.30 * 3.2) # +2.08R
                elif tp1_hit and not tp2_hit:
                    # Trailed SL Winner (70% @ 1.6R + 30% @ +0.4R)
                    wins += 1
                    trailed_sl_wins += 1
                    trade_r = (0.70 * 1.6) + (0.30 * 0.4) # +1.24R
                else:
                    # Timeout / neither hit in 30 bars (exit at last close)
                    last_c = fw.iloc[-1]["close"] if len(fw) > 0 else entry
                    r_exit = (last_c - entry) / risk
                    trade_r = max(-1.0, min(1.6, r_exit))
                    if trade_r > 0: wins += 1
                    else: losses += 1
                    
            else: # SELL
                if sl_mode == "baseline":
                    sl = entry + (1.5 * atr)
                elif sl_mode == "sweep_exact":
                    sl = s["sweep_high"] * 1.0005
                elif sl_mode == "sweep_buffer":
                    sl = s["sweep_high"] + (0.25 * atr)
                    
                if sl <= entry:
                    sl = entry + (0.01 * entry)
                    
                risk = sl - entry
                tp1 = entry - (1.6 * risk)
                tp2 = entry - (3.2 * risk)
                profit_lock_sl = entry - (0.4 * risk)
                
                tp1_hit = False
                tp2_hit = False
                sl_hit = False
                cur_sl = sl
                
                for _, bar in fw.iterrows():
                    h = bar["high"]
                    l = bar["low"]
                    
                    if not tp1_hit:
                        if h >= cur_sl:
                            sl_hit = True
                            break
                        if l <= tp1:
                            tp1_hit = True
                            cur_sl = profit_lock_sl
                    else:
                        if l <= tp2:
                            tp2_hit = True
                            break
                        if h >= cur_sl:
                            break
                            
                if not tp1_hit and sl_hit:
                    losses += 1
                    trade_r = -1.0
                elif tp1_hit and tp2_hit:
                    wins += 1
                    full_moon_wins += 1
                    trade_r = (0.70 * 1.6) + (0.30 * 3.2)
                elif tp1_hit and not tp2_hit:
                    wins += 1
                    trailed_sl_wins += 1
                    trade_r = (0.70 * 1.6) + (0.30 * 0.4)
                else:
                    last_c = fw.iloc[-1]["close"] if len(fw) > 0 else entry
                    r_exit = (entry - last_c) / risk
                    trade_r = max(-1.0, min(1.6, r_exit))
                    if trade_r > 0: wins += 1
                    else: losses += 1
                    
            total_pnl_r += trade_r
            r_multiples.append(trade_r)
            
        total_trades = wins + losses
        win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
        avg_r = total_pnl_r / total_trades if total_trades > 0 else 0
        profit_factor = (sum(r for r in r_multiples if r > 0) / abs(sum(r for r in r_multiples if r < 0))) if any(r < 0 for r in r_multiples) else 999.0
        
        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "full_moon_wins": full_moon_wins,
            "trailed_sl_wins": trailed_sl_wins,
            "total_pnl_r": total_pnl_r,
            "avg_r_per_trade": avg_r,
            "profit_factor": profit_factor
        }

    res_baseline = simulate_model(all_signals, "baseline")
    res_exact = simulate_model(all_signals, "sweep_exact")
    res_buffer = simulate_model(all_signals, "sweep_buffer")
    
    print("\n" + "="*70)
    print("BACKTEST COMPARISON: BASELINE vs SWEEP EXACT vs SWEEP + BUFFER")
    print("="*70)
    
    for name, r in [("1. Baseline (1.5x ATR SL)", res_baseline), 
                    ("2. Sweep Wick Exact Tip (0 Buffer)", res_exact), 
                    ("3. Human-Eye Sweep + 0.25*ATR Buffer", res_buffer)]:
        print(f"\n--- {name} ---")
        print(f"Total Trades:       {r['total_trades']}")
        print(f"Win Rate:           {r['win_rate']:.2f}% (Wins: {r['wins']}, Losses: {r['losses']})")
        print(f"TP2 Full Moon Wins: {r['full_moon_wins']}")
        print(f"Trailed SL Wins:    {r['trailed_sl_wins']}")
        print(f"Total Return:       +{r['total_pnl_r']:.2f} R")
        print(f"Avg R Per Trade:    +{r['avg_r_per_trade']:.3f} R")
        print(f"Profit Factor:      {r['profit_factor']:.2f}")

    # Output JSON summary for artifact
    summary = {
        "baseline": res_baseline,
        "sweep_exact": res_exact,
        "sweep_buffer": res_buffer
    }
    with open("C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/backtest_sweep_results.json", "w") as f:
        json.dump(summary, f, indent=2)

if __name__ == "__main__":
    run_backtest()
