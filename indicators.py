import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import asyncio
import aiohttp
import logging
import warnings
# Suppress pandas/yfinance FutureWarning spam in Railway logs (Bug #12 fix)
warnings.filterwarnings("ignore", category=FutureWarning)
from dynamic_config import get_dyn_config

logger = logging.getLogger("indicators")

def check_amd_killzone(symbol: str, candle_ts) -> bool:
    """
    Checks if a BTC or ETH trade is allowed based on ICT Killzones.
    Only allows trades during Asian (00:00-04:00 UTC) and London Close (14:00-16:00 UTC).
    For all other coins, always returns True.
    """
    if symbol not in ["BTC/USDT", "ETH/USDT"]:
        return True
    
    try:
        import dateutil.parser
        from datetime import timezone
        dt = dateutil.parser.parse(str(candle_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
            
        time_val = dt.hour + dt.minute / 60.0
        
        # Asian Killzone: 00:00 to 04:00 UTC
        if 0.0 <= time_val <= 4.0:
            return True
            
        # London Close Killzone: 14:00 to 16:00 UTC
        if 14.0 <= time_val <= 16.0:
            return True
            
        return False
    except Exception:
        return True

def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df is None: return None
    if 'ema_9' in df.columns:
        return df
    df = df.copy()
    
    # Standard EMAs
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema_26'] = df['close'].ewm(span=26, adjust=False).mean()
    df['ema_34'] = df['close'].ewm(span=34, adjust=False).mean()
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema_89'] = df['close'].ewm(span=89, adjust=False).mean()
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    
    # Standard SMAs
    df['sma_50'] = df['close'].rolling(window=50).mean()
    df['sma_200'] = df['close'].rolling(window=200).mean()
    
    # Volume 20 SMA
    df['vol_sma_20'] = df['volume'].rolling(20).mean()

    # Relative Strength Index (RSI 14)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    df['rsi'] = 100 - (100 / (1 + rs))

    # ADX 14 Trend Strength Indicator
    df['tr0'] = abs(df['high'] - df['low'])
    df['tr1'] = abs(df['high'] - df['close'].shift(1))
    df['tr2'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
    df['+dm'] = df['high'].diff().clip(lower=0)
    df['-dm'] = (-df['low'].diff()).clip(lower=0)
    df['+dm'] = df['+dm'].where(df['+dm'] > df['-dm'], 0)
    df['-dm'] = df['-dm'].where(df['-dm'] > df['+dm'], 0)
    df['tr_14'] = df['tr'].rolling(14).sum()
    df['+di_14'] = 100 * (df['+dm'].rolling(14).sum() / df['tr_14'].replace(0, 1))
    df['-di_14'] = 100 * (df['-dm'].rolling(14).sum() / df['tr_14'].replace(0, 1))
    df['dx'] = 100 * abs(df['+di_14'] - df['-di_14']) / (df['+di_14'] + df['-di_14']).replace(0, 1)
    df['adx'] = df['dx'].rolling(14).mean()
    df['atr'] = df['tr'].rolling(14).mean()
    
    # MACD
    macd_line = df['close'].ewm(span=12, adjust=False).mean() - df['close'].ewm(span=26, adjust=False).mean()
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    df['macd_line'] = macd_line
    df['macd_signal'] = macd_signal
    
    return df

def get_sl_cap_percent(symbol: str) -> float:
    if not symbol:
        return 0.0220
    sym = symbol.upper().replace("/", "").replace("-", "")
    if "XAU" in sym or "GOLD" in sym:
        return 0.0080 # 0.80% max SL for Gold ($32 max at $4000)
    elif "XAG" in sym or "SILVER" in sym:
        return 0.0150 # 1.50% max SL for Silver
    elif any(c in sym for c in ("CL", "OIL", "WTI", "BRENT", "NATGAS")):
        return 0.0180 # 1.80% max SL for Crude Oil / Gas
    elif any(c in sym for c in ("COPPER", "BRONZE", "HG")):
        return 0.0180 # 1.80% max SL for Copper / Bronze
    elif "BTC" in sym:
        return 0.0120 # 1.20% max SL for BTC
    elif "ETH" in sym:
        return 0.0160 # 1.60% max SL for ETH
    elif "SOL" in sym:
        return 0.0200 # 2.00% max SL for SOL
    else:
        return 0.0220 # 2.20% max SL for Altcoins (Strictly prevents large SL!)

def get_min_sl_percent(symbol: str) -> float:
    if not symbol:
        return 0.0080
    sym = symbol.upper().replace("/", "").replace("-", "")
    if "XAU" in sym or "GOLD" in sym:
        return 0.0035 # 0.35% min SL for Gold (~$14 buffer at $4000)
    elif "XAG" in sym or "SILVER" in sym:
        return 0.0060 # 0.60% min SL for Silver
    elif any(c in sym for c in ("CL", "OIL", "WTI", "BRENT", "NATGAS")):
        return 0.0060 # 0.60% min SL for Crude Oil / Gas
    elif any(c in sym for c in ("COPPER", "BRONZE", "HG")):
        return 0.0060 # 0.60% min SL for Copper / Bronze
    elif "BTC" in sym:
        return 0.0050 # 0.50% min SL for BTC
    elif "ETH" in sym:
        return 0.0070 # 0.70% min SL for ETH
    elif "SOL" in sym:
        return 0.0080 # 0.80% min SL for SOL
    else:
        return 0.0080 # 0.80% min SL for Altcoins

def find_liquidity_pool_targets(df: pd.DataFrame, signal: str, entry: float, default_sl: float, symbol: str = "ALT/USDT", tf_data: dict = None, target_r: float = 1.5) -> tuple:
    """
    Finds dynamic Take-Profit Targets (TP1: target_r, TP2: 3.2R) and SL using 26 EMA structural invalidation and swing extremes.
    USER DIRECTIVE:
    - EMA Retests & Crossovers: exact 1.5R Target
    - 1H Breakouts & Breakdowns: exact 2.2R Target
    Returns (sl, (tp1, tp2), (rr1, rr2), (lbl1, lbl2))
    """
    import numpy as np
    
    sl_cap_pct = get_sl_cap_percent(symbol)
    min_sl_pct = get_min_sl_percent(symbol)

    max_sl_dist = entry * sl_cap_pct
    min_sl_floor = entry - max_sl_dist

    if df is None or len(df) < 50:
        sl = max(default_sl, min_sl_floor) if signal == "BUY" else min(default_sl, entry + max_sl_dist)
        risk = abs(entry - sl)
        if risk == 0: risk = entry * 0.01
        from executor import round_price
        if signal == "BUY":
            tp1 = round_price(symbol, entry + (target_r * risk))
        else:
            tp1 = round_price(symbol, entry - (target_r * risk))
        tp2 = tp1
        return sl, (tp1, tp2), (target_r, target_r), (f"{target_r}R Target (100% Book)", f"{target_r}R Target (100% Book)")

    sub = df.iloc[-100:-1] if len(df) >= 100 else df.iloc[:-1]
    highs = sub['high'].values
    lows = sub['low'].values
    
    bsl_levels = []
    ssl_levels = []
    
    for k in range(2, len(sub) - 2):
        if highs[k] > highs[k-1] and highs[k] > highs[k-2] and highs[k] > highs[k+1] and highs[k] > highs[k+2]:
            bsl_levels.append(highs[k])
        if lows[k] < lows[k-1] and lows[k] < lows[k-2] and lows[k] < lows[k+1] and lows[k] < lows[k+2]:
            ssl_levels.append(lows[k])
            
    if symbol in ["BTC/USDT", "ETH/USDT"]:
        try:
            t_df = sub.copy()
            if not isinstance(t_df.index, pd.DatetimeIndex):
                t_df.index = pd.to_datetime(t_df.index)
            t_df['hour'] = t_df.index.hour
            t_df['date'] = t_df.index.date
            
            asian = t_df[(t_df['hour'] >= 0) & (t_df['hour'] < 4)]
            if not asian.empty:
                bsl_levels.extend(asian.groupby('date')['high'].max().values)
                ssl_levels.extend(asian.groupby('date')['low'].min().values)
                
            london = t_df[(t_df['hour'] >= 7) & (t_df['hour'] < 11)]
            if not london.empty:
                bsl_levels.extend(london.groupby('date')['high'].max().values)
                ssl_levels.extend(london.groupby('date')['low'].min().values)
        except Exception:
            pass
            
    refine_ssl = []
    refine_bsl = []
    internal_bsl = []
    internal_ssl = []

    # Extract Internal Range Liquidity (IRL) & External Range Liquidity (ERL) across 5M, 15M, 30M, 1H
    if tf_data is not None:
        for t_key in ["5M", "15M", "30M", "1H"]:
            if t_key in tf_data and tf_data[t_key] is not None:
                r_df = tf_data[t_key]
                if len(r_df) >= 20:
                    r_sub = r_df.iloc[-80:-1] if len(r_df) >= 80 else r_df.iloc[:-1]
                    r_highs = r_sub['high'].values
                    r_lows = r_sub['low'].values
                    
                    # 1-bar Internal Fractals (IRL)
                    for k in range(1, len(r_sub) - 1):
                        if r_highs[k] > r_highs[k-1] and r_highs[k] > r_highs[k+1]:
                            internal_bsl.append(r_highs[k])
                        if r_lows[k] < r_lows[k-1] and r_lows[k] < r_lows[k+1]:
                            internal_ssl.append(r_lows[k])

                    # 2-bar External Fractals (ERL)
                    for k in range(2, len(r_sub) - 2):
                        if r_highs[k] > r_highs[k-1] and r_highs[k] > r_highs[k-2] and r_highs[k] > r_highs[k+1] and r_highs[k] > r_highs[k+2]:
                            refine_bsl.append(r_highs[k])
                        if r_lows[k] < r_lows[k-1] and r_lows[k] < r_lows[k-2] and r_lows[k] < r_lows[k+1] and r_lows[k] < r_lows[k+2]:
                            refine_ssl.append(r_lows[k])

    if signal == "BUY":
        # ATR-Based Dynamic Stop-Loss (1.5x ATR below entry)
        atr_val = 0.0
        if df is not None and 'atr' in df.columns:
            atr_val = float(df['atr'].iloc[-1]) if not pd.isna(df['atr'].iloc[-1]) else 0.0
        elif df is not None and len(df) >= 14:
            tr0 = abs(df['high'] - df['low'])
            tr1 = abs(df['high'] - df['close'].shift(1))
            tr2 = abs(df['low'] - df['close'].shift(1))
            tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
            atr_val = float(tr.rolling(14).mean().iloc[-1]) if not pd.isna(tr.rolling(14).mean().iloc[-1]) else (entry * 0.005)
        
        # ── 26 EMA DYNAMIC BASELINE + STRUCTURAL SWEEP SL WITH 0.25*ATR BUFFER ──
        recent_lows = lows[-5:] if len(lows) >= 5 else [entry * 0.99]
        sweep_low = float(np.min(recent_lows))
        sweep_sl = sweep_low - (0.25 * atr_val) if atr_val > 0 else (sweep_low * 0.9975)

        # 26 EMA Protection: SL must sit strictly BELOW 26 EMA with 0.25*ATR buffer
        ema_26_val = None
        if df is not None and 'ema_26' in df.columns and not pd.isna(df['ema_26'].iloc[-1]):
            ema_26_val = float(df['ema_26'].iloc[-1])
        elif default_sl and default_sl < entry:
            ema_26_val = default_sl

        if ema_26_val:
            ema_sl = ema_26_val - (0.25 * atr_val) if atr_val > 0 else (ema_26_val * 0.9975)
            # Guarantee SL is below both recent swing and 26 EMA baseline
            candidate_sl = min(sweep_sl, ema_sl)
        else:
            candidate_sl = sweep_sl

        # Enforce Minimum 0.80% Stop-Loss Floor (protects against micro-wick noise / spread stopouts)
        min_sl_dist_floor = entry * (1.0 - min_sl_pct)
        sl = min(candidate_sl, min_sl_dist_floor)

        if sl >= entry: sl = np.min(lows[-5:]) * 0.998
        
        # STRUCTURAL INVALIDATION GUARANTEE:
        # A LONG SL must sit strictly BELOW 26 EMA with 0.25*ATR buffer,
        # BUT MUST NEVER exceed max account risk cap (min_sl_floor)!
        if ema_26_val:
            ema_sl_bound = ema_26_val - (0.25 * atr_val if atr_val > 0 else ema_26_val * 0.0025)
            sl = min(sl, ema_sl_bound)
        # HARD RISK CAP: Cap SL to max permissible risk (e.g. 2.2% max)
        sl = max(sl, min_sl_floor)
        if sl >= entry:
            sl = entry * 0.990

        # Post-rounding strict SL direction & Liquidation Shield guarantee
        from executor import round_price, enforce_sl_above_liquidation
        sl = enforce_sl_above_liquidation(symbol, entry, sl, "BUY", 10.0)
        clean_e = round_price(symbol, entry)
        if round_price(symbol, sl) >= clean_e:
            sl = entry * 0.990
        
        risk = abs(entry - sl)
        if risk == 0: risk = entry * 0.01
        
        # USER DIRECTIVE: Exact target_r Take Profit as per SL (1.5R for EMA Retest/Crossover, 2.2R for Breakout/Breakdown)
        from executor import round_price
        clean_tp1 = round_price(symbol, entry + (target_r * risk))
        clean_tp2 = clean_tp1
        
        lbl1 = f"{target_r} R:R Target (100% Book)"
        lbl2 = lbl1

        rr1 = round(abs(clean_tp1 - entry) / risk, 2)
        rr2 = rr1

        return sl, (clean_tp1, clean_tp2), (rr1, rr2), (lbl1, lbl2)

    else: # SELL
        # ATR-Based Dynamic Stop-Loss (1.5x ATR above entry)
        atr_val = 0.0
        if df is not None and 'atr' in df.columns:
            atr_val = float(df['atr'].iloc[-1]) if not pd.isna(df['atr'].iloc[-1]) else 0.0
        elif df is not None and len(df) >= 14:
            tr0 = abs(df['high'] - df['low'])
            tr1 = abs(df['high'] - df['close'].shift(1))
            tr2 = abs(df['low'] - df['close'].shift(1))
            tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
            atr_val = float(tr.rolling(14).mean().iloc[-1]) if not pd.isna(tr.rolling(14).mean().iloc[-1]) else (entry * 0.005)

        # ── 26 EMA DYNAMIC BASELINE + STRUCTURAL SWEEP SL WITH 0.25*ATR BUFFER ──
        recent_highs = highs[-5:] if len(highs) >= 5 else [entry * 1.01]
        sweep_high = float(np.max(recent_highs))
        sweep_sl = sweep_high + (0.25 * atr_val) if atr_val > 0 else (sweep_high * 1.0025)

        # 26 EMA Protection: SL must sit strictly ABOVE 26 EMA with 0.25*ATR buffer
        ema_26_val = None
        if df is not None and 'ema_26' in df.columns and not pd.isna(df['ema_26'].iloc[-1]):
            ema_26_val = float(df['ema_26'].iloc[-1])
        elif default_sl and default_sl > entry:
            ema_26_val = default_sl

        if ema_26_val:
            ema_sl = ema_26_val + (0.25 * atr_val) if atr_val > 0 else (ema_26_val * 1.0025)
            # Guarantee SL is above both recent swing and 26 EMA baseline
            candidate_sl = max(sweep_sl, ema_sl)
        else:
            candidate_sl = sweep_sl

        # Enforce Minimum 0.80% Stop-Loss Ceiling (protects against micro-wick noise / spread stopouts)
        max_sl_dist_ceiling = entry * (1.0 + min_sl_pct)
        sl = max(candidate_sl, max_sl_dist_ceiling)

        if sl <= entry: sl = np.max(highs[-5:]) * 1.002

        # STRUCTURAL INVALIDATION GUARANTEE:
        # A SHORT SL must sit strictly ABOVE 26 EMA with 0.25*ATR buffer,
        # BUT MUST NEVER exceed max account risk cap (sl_cap_pct)!
        if ema_26_val:
            ema_sl_bound = ema_26_val + (0.25 * atr_val if atr_val > 0 else ema_26_val * 0.0025)
            sl = max(sl, ema_sl_bound)
        # HARD RISK CAP: Cap SL to max permissible risk (e.g. 2.2% max)
        sl = min(sl, entry * (1 + sl_cap_pct))
        if sl <= entry:
            sl = entry * 1.010

        # Post-rounding strict SL direction & Liquidation Shield guarantee
        from executor import round_price, enforce_sl_above_liquidation
        sl = enforce_sl_above_liquidation(symbol, entry, sl, "SELL", 10.0)
        clean_e = round_price(symbol, entry)
        if round_price(symbol, sl) <= clean_e:
            sl = entry * 1.010
        
        risk = abs(entry - sl)
        if risk == 0: risk = entry * 0.01
        
        # USER DIRECTIVE: Exact target_r Take Profit as per SL (1.5R for EMA Retest/Crossover, 2.2R for Breakout/Breakdown)
        from executor import round_price
        clean_tp1 = round_price(symbol, entry - (target_r * risk))
        clean_tp2 = clean_tp1
        
        lbl1 = f"{target_r} R:R Target (100% Book)"
        lbl2 = lbl1

        rr1 = round(abs(entry - clean_tp1) / risk, 2)
        rr2 = rr1

        return sl, (clean_tp1, clean_tp2), (rr1, rr2), (lbl1, lbl2)

def analyze_crossovers(df: pd.DataFrame, symbol: str = "ALT/USDT", tf_data: dict = None) -> list:
    """
    Scans for various famous crossovers on the last completed candle.
    Returns a list of signal dicts with dynamic Liquidity Pool TP & SL.
    """
    if df is None or len(df) < 50:
        return []
    
    df = _compute_indicators(df)
    row = df.iloc[-2]
    prev = df.iloc[-3]
    
    signals = []
    
    # Base SL uses EMA 26 (placed slightly below for BUY, above for SELL)
    buy_sl = row['ema_26'] * 0.998
    sell_sl = row['ema_26'] * 1.002
    
    # Validation: SL must be logical (BUY SL < Price, SELL SL > Price)
    if buy_sl >= row['close']:
        buy_sl = min(df.iloc[-4]['low'], df.iloc[-3]['low'], df.iloc[-2]['low']) * 0.998
        if buy_sl >= row['close']:
            buy_sl = row['close'] * 0.96
            
    if sell_sl <= row['close']:
        sell_sl = max(df.iloc[-4]['high'], df.iloc[-3]['high'], df.iloc[-2]['high']) * 1.002
        if sell_sl <= row['close']:
            sell_sl = row['close'] * 1.04
    
    # 0. Core Strategy Filters (Volume + Trend Strength - Dynamically tuned)
    cfg = get_dyn_config()
    vol_thresh = cfg.get('VOLUME_MULTIPLIER', 1.2)
    adx_thresh = cfg.get('ADX_THRESHOLD', 15.0)

    vol_sma = row.get('vol_sma_20', 0) if not pd.isna(row.get('vol_sma_20')) else 0
    prev_vol_sma = prev.get('vol_sma_20', 0) if not pd.isna(prev.get('vol_sma_20')) else 0

    vol_valid = True
    if vol_sma > 0:
        vol_valid = (row['volume'] > vol_sma * vol_thresh) or (prev_vol_sma > 0 and prev['volume'] > prev_vol_sma * vol_thresh)

    adx_val = row.get('adx', 25.0)
    adx_valid = True if pd.isna(adx_val) else (adx_val > adx_thresh)

    # Candle Expansion Check (Reject Dojis/High Wicks - Body must be >= X% of total candle range)
    candle_range = max(row['high'] - row['low'], 1e-9)
    candle_body = abs(row['close'] - row['open'])
    body_ratio_thresh = cfg.get('BODY_RATIO', 0.25)
    expansion_valid = (candle_body / candle_range) >= body_ratio_thresh

    # RSI Precision Momentum Check (Prevents entering at exhausted overbought peaks or oversold bottoms)
    rsi_val = row.get('rsi', 50.0)
    rsi_min = cfg.get('RSI_MIN', 42.0)
    rsi_max = cfg.get('RSI_MAX', 62.0)
    rsi_bullish = True if pd.isna(rsi_val) else (42.0 <= rsi_val <= 62.0)
    rsi_bearish = True if pd.isna(rsi_val) else (38.0 <= rsi_val <= 58.0)

    # 200 SMA Baseline Filter (Price must be above 200 SMA for BUY, below for SELL)
    sma_200 = row.get('sma_200')
    sma_200_bullish = True if pd.isna(sma_200) else (row['close'] > sma_200)
    sma_200_bearish = True if pd.isna(sma_200) else (row['close'] < sma_200)

    # 2.5% EMA 26 Proximity Filter (reject buying/selling overextended vertical spikes)
    ema_26 = row.get('ema_26')
    proximity_valid = True if pd.isna(ema_26) else (abs(row['close'] - ema_26) / row['close'] <= 0.025)

    # Anti-Chop Filter: Adaptive EMA 9 & EMA 26 spread check by timeframe
    ema_9 = row.get('ema_9')
    anti_chop_valid = True
    if not pd.isna(ema_9) and not pd.isna(ema_26) and row['close'] > 0:
        bar_mins = 60
        if len(df) >= 2:
            try:
                bar_mins = int(round((df.index[-1] - df.index[-2]).total_seconds() / 60.0))
            except Exception:
                bar_mins = 60
        if bar_mins <= 15:
            min_spread = 0.00012  # 0.012% for 15M (allows high-value assets: Gold, BTC, ETH, US Stocks)
        elif bar_mins <= 30:
            min_spread = 0.00030  # 0.030% for 30M
        else:
            min_spread = 0.00080  # 0.080% for 1H, 4H, 1D (strictly blocks flat horizontal chop like LINK)
        anti_chop_valid = (abs(ema_9 - ema_26) / row['close']) >= min_spread

    # Enforce ADX > 15 and candle body >= 25% alongside anti-chop & proximity to reject flat-chop/doji signals.
    # Volume & 200 SMA remain weighted downstream by signal_analyser so early crosses & CFD commodities aren't starved.
    valid_setup = anti_chop_valid and proximity_valid and adx_valid and expansion_valid

    # Helper function to append signal with dynamic Liquidity TPs and EMA 9 Pullback Limit Entry
    def add_signal(cross_name, sig_type):
        # Optimal Pullback Tap Entry: Place Entry LIMIT order exactly at the 9 EMA line to catch the retest tap
        ema9_px = float(row.get('ema_9', row['close']))
        opt_entry = float(ema9_px)
        def_sl = buy_sl if sig_type == "BUY" else sell_sl

        dyn_sl, (tp1, tp2), (rr1, rr2), (lbl1, lbl2) = find_liquidity_pool_targets(df, sig_type, opt_entry, def_sl, symbol=symbol, tf_data=tf_data)
        c_open = float(row.get('open', 0))
        c_close = float(row.get('close', 0))
        c_high = float(row.get('high', 0))
        c_low = float(row.get('low', 0))
        c_range = max(c_high - c_low, 1e-8)
        body_ratio = abs(c_close - c_open) / c_range
        prev_vol_sma_val = prev.get('vol_sma_20')
        vol_sma_out = (
            float(prev_vol_sma_val)
            if (prev_vol_sma_val is not None and not pd.isna(prev_vol_sma_val))
            else (float(row.get('vol_sma_20', 0)) if not pd.isna(row.get('vol_sma_20', 0)) else 0.0)
        )

        signals.append({
            "type": cross_name,
            "signal": sig_type,
            "price": opt_entry,
            "sl": dyn_sl,
            "tp1": tp1,
            "tp2": tp2,
            "rr1": rr1,
            "rr2": rr2,
            "lbl1": lbl1,
            "lbl2": lbl2,
            "rsi": float(rsi_val) if not pd.isna(rsi_val) else None,
            "adx": float(adx_val) if not pd.isna(adx_val) else 25.0,
            "volume": float(row.get('volume', 0)),
            "volume_sma": vol_sma_out,
            "macd_line": float(row.get('macd_line', 0)),
            "macd_signal": float(row.get('macd_signal', 0)),
            "body_ratio": body_ratio,
            # Backward compatibility single fields
            "tp": tp1,
            "rr": rr1,
            "tp_label": lbl1
        })

    # 1. Base Strategy: 9/26 EMA Cross
    # Emit whenever structural quality holds; RSI [42..62] / [38..58] & AI Score > 77 bypass
    # are enforced downstream in telegram_bot.queue_alert and main._check_auto_trade_eligibility.
    if valid_setup:
        if prev['ema_9'] <= prev['ema_26'] and row['ema_9'] > row['ema_26']:
            add_signal("9/26 EMA Cross", "BUY")
        elif prev['ema_9'] >= prev['ema_26'] and row['ema_9'] < row['ema_26']:
            add_signal("9/26 EMA Cross", "SELL")
        
    return signals

def get_trend_state(df: pd.DataFrame) -> str:
    if df is None or len(df) < 50: return "neutral"
    df = _compute_indicators(df)
    row = df.iloc[-2] if len(df) >= 2 else df.iloc[-1] # Check last completed candle
    if row['ema_9'] > row['ema_26']: return "bullish"
    elif row['ema_9'] < row['ema_26']: return "bearish"
    return "neutral"

def find_last_crossover_time(df: pd.DataFrame, signal: str, cross_type: str = "9/26 EMA Cross") -> str:
    """Finds the most recent crossover time of the specified type and direction."""
    if df is None or len(df) < 201: return "Unknown"
    df = _compute_indicators(df)
    
    # Iterate backwards starting from the most recently completed candle
    for i in range(len(df)-2, 1, -1):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        
        if cross_type == "9/26 EMA Cross":
            if signal == "BUY" and prev['ema_9'] <= prev['ema_26'] and row['ema_9'] > row['ema_26']: return str(df.index[i])
            if signal == "SELL" and prev['ema_9'] >= prev['ema_26'] and row['ema_9'] < row['ema_26']: return str(df.index[i])
            
    return "Not recently"

def get_macro_structure(df: pd.DataFrame) -> str:
    if df is None or len(df) < 20: return "Unknown"
    df = _compute_indicators(df)
    row = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
    
    c = row['close']
    e9 = row.get('ema_9')
    e26 = row.get('ema_26')
    e34 = row.get('ema_34')
    e89 = row.get('ema_89')
    e200 = row.get('ema_200')
    
    # 1. Primary Check (Coins with >= 200 daily candles)
    if e200 is not None and not pd.isna(e200):
        if c > e34 and e34 > e89 and e89 > e200:
            return "Strong Bullish"
        elif c < e34 and e34 < e89 and e89 < e200:
            return "Strong Bearish"
        elif c > e200:
            return "Bullish Bias"
        elif c < e200:
            return "Bearish Bias"
            
    # 2. Secondary Fallback (Newer Altcoins with 50 to 199 daily candles)
    if e89 is not None and not pd.isna(e89):
        if c > e26 and e26 > e89:
            return "Bullish Trend"
        elif c < e26 and e26 < e89:
            return "Bearish Trend"
            
    if e34 is not None and not pd.isna(e34):
        if c > e9 and e9 > e34:
            return "Bullish Trend"
        elif c < e9 and e9 < e34:
            return "Bearish Trend"
            
    return "Ranging / Mixed"


import zoneinfo
from datetime import datetime, timezone

def _fmt_p(p: float) -> str:
    if p is None: return "0.00"
    if p >= 1000: return f"{p:,.2f}"
    elif p >= 1: return f"{p:.4f}"
    elif p >= 0.0001: return f"{p:.6f}"
    else: return f"{p:.8f}"

def build_consensus_timeline(tf_data: dict, signal: str = "BUY") -> str:
    """
    Builds an explicit Multi-Timeframe Crossover Timeline in IST for 1H, 4H, 8H, 1D.
    For each timeframe:
      - Trend state (Bullish / Bearish)
      - Exact IST crossover timestamp
      - Elapsed time (hours / days ago)
      - Price at crossover & % price move since cross
    """
    if not tf_data:
        return ""
        
    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    lines = ["<b>Multi-Timeframe Crossover Timeline (IST):</b>"]
    
    tf_labels = ["1H", "4H", "8H", "1D", "1W"]
    tf_hours = {"1H": 1, "4H": 4, "8H": 8, "1D": 24, "1W": 168}
    
    for tf in tf_labels:
        df = tf_data.get(tf)
        if df is None or len(df) < 15:
            lines.append(f"• ⚪ <b>{tf}: Neutral</b>")
            continue
            
        df = _compute_indicators(df)
        row = df.iloc[-2]
        current_close = row['close']
        ema9 = row['ema_9']
        ema26 = row['ema_26']
        
        trend = "Bullish" if ema9 > ema26 else "Bearish"
        icon = "🟢" if trend == "Bullish" else "🔴"
        
        bars_ago = -1
        cross_price = None
        cross_dt = None
        
        for i in range(len(df)-2, 1, -1):
            cur = df.iloc[i]
            prev = df.iloc[i-1]
            if trend == "Bullish" and prev['ema_9'] <= prev['ema_26'] and cur['ema_9'] > cur['ema_26']:
                bars_ago = (len(df) - 2) - i
                cross_price = cur['close']
                cross_dt = df.index[i]
                break
            elif trend == "Bearish" and prev['ema_9'] >= prev['ema_26'] and cur['ema_9'] < cur['ema_26']:
                bars_ago = (len(df) - 2) - i
                cross_price = cur['close']
                cross_dt = df.index[i]
                break
                
        if cross_dt is not None and cross_price is not None and bars_ago >= 0:
            if isinstance(cross_dt, str):
                dt = datetime.fromisoformat(cross_dt.replace("Z", "+00:00"))
            else:
                dt = cross_dt
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ist_dt = dt.astimezone(ist_tz)
            
            elapsed_hrs = bars_ago * tf_hours.get(tf, 1)
            pct_move = ((current_close - cross_price) / cross_price) * 100 if trend == "Bullish" else ((cross_price - current_close) / cross_price) * 100
            
            now_ist = datetime.now(timezone.utc).astimezone(ist_tz)
            if ist_dt.date() == now_ist.date():
                time_fmt = ist_dt.strftime("%H:%M IST")
            else:
                time_fmt = ist_dt.strftime("%d %b %H:%M IST")
                
            if bars_ago == 0:
                detail_str = f"<b>Fresh Cross</b> @ {_fmt_p(cross_price)} ({time_fmt})"
            else:
                if elapsed_hrs < 48:
                    time_ago_str = f"{elapsed_hrs}h ago"
                elif elapsed_hrs < 168:
                    time_ago_str = f"{elapsed_hrs // 24}d ago"
                else:
                    time_ago_str = f"{elapsed_hrs // 168}w ago"
                detail_str = f"Crossed {time_fmt} ({time_ago_str} @ {_fmt_p(cross_price)}, {pct_move:+.2f}%)"
                
            lines.append(f"• {icon} <b>{tf}: {trend}</b> — {detail_str}")
        else:
            lines.append(f"• {icon} <b>{tf}: {trend}</b>")
            
    return "\n".join(lines)

_btc_shock_until = None
_btc_shock_state = {
    "until": None,
    "direction": None,
    "is_clean_cascade": False,
    "body_ratio": 0.0,
    "range_pct": 0.0,
    "body_pct": 0.0,
}


def get_btc_shock_context() -> dict:
    """Return active BTC 15M Volatility Shock metadata (direction & cascade quality)."""
    global _btc_shock_until
    now_utc = datetime.now(timezone.utc)
    until = _btc_shock_state.get("until") or _btc_shock_until
    if until is not None and now_utc < until:
        rem = (until - now_utc).total_seconds() / 60.0
        return {
            "active": True,
            "remaining_min": rem,
            "direction": _btc_shock_state.get("direction"),
            "is_clean_cascade": bool(_btc_shock_state.get("is_clean_cascade", False)),
            "body_ratio": float(_btc_shock_state.get("body_ratio", 0.0)),
            "range_pct": float(_btc_shock_state.get("range_pct", 0.0)),
            "body_pct": float(_btc_shock_state.get("body_pct", 0.0)),
        }
    _btc_shock_until = None
    _btc_shock_state["until"] = None
    _btc_shock_state["direction"] = None
    _btc_shock_state["is_clean_cascade"] = False
    return {
        "active": False,
        "remaining_min": 0.0,
        "direction": None,
        "is_clean_cascade": False,
        "body_ratio": 0.0,
        "range_pct": 0.0,
        "body_pct": 0.0,
    }


async def check_btc_volatility_shock(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> tuple[bool, float]:
    """
    Checks BTC/USDT 15M candles for sharp upside/downside price moves (whipsaws/liquidation wicks).
    If candle range % >= 1.5% or abs(open - close) % >= 1.2%:
        Activates 30-minute Volatility Shock mode:
        - Clean Cascade (body_ratio >= 0.45): pauses standard altcoin trades; allows shock-aligned 15M Runaway Chaser only.
        - Two-Sided Whipsaw (body_ratio < 0.45): pauses all crypto entries completely.
    Returns (is_shock_active, remaining_minutes)
    """
    global _btc_shock_until
    now_utc = datetime.now(timezone.utc)

    ctx = get_btc_shock_context()
    if ctx["active"]:
        return True, ctx["remaining_min"]

    try:
        from fetcher import fetch_ohlcv_async
        df_btc = await fetch_ohlcv_async(session, "BTC/USDT", "15m", 10, semaphore)
        if df_btc is None or len(df_btc) < 2:
            return False, 0.0

        row = df_btc.iloc[-2]  # Check ONLY the latest completed 15M candle to prevent continuous pause loops
        o = float(row['open'])
        h = float(row['high'])
        l = float(row['low'])
        c = float(row['close'])

        if o > 0 and l > 0:
            candle_range_pct = ((h - l) / o) * 100.0
            body_move_pct = (abs(c - o) / o) * 100.0
            body_ratio = abs(c - o) / max(h - l, 1e-8)

            if body_move_pct >= 1.2 or candle_range_pct >= 1.5:
                new_until = now_utc + timedelta(minutes=30)
                shock_dir = "BUY" if c > o else "SELL"
                is_clean_cascade = bool(body_ratio >= 0.45)

                if _btc_shock_until is None or new_until > _btc_shock_until:
                    _btc_shock_until = new_until
                    _btc_shock_state.update({
                        "until": new_until,
                        "direction": shock_dir,
                        "is_clean_cascade": is_clean_cascade,
                        "body_ratio": body_ratio,
                        "range_pct": candle_range_pct,
                        "body_pct": body_move_pct,
                    })

                    try:
                        from telegram_bot import send_text_to_chat
                        from config import TELEGRAM_CHAT_ID
                        try:
                            import zoneinfo
                            ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
                        except Exception:
                            import dateutil.tz
                            ist_tz = dateutil.tz.gettz("Asia/Kolkata")
                        until_ist = new_until.astimezone(ist_tz).strftime("%H:%M:%S IST")

                        if is_clean_cascade:
                            action_txt = (
                                f"Standard Altcoins PAUSED 30m; Clean {shock_dir} Cascade ({body_ratio:.0%} body) — "
                                f"aligned 15M Runaway Chaser ONLY (max 1/cycle, max 2 risk)."
                            )
                        else:
                            action_txt = (
                                f"Two-sided Liquidation Whipsaw ({body_ratio:.0%} body) — "
                                f"ALL new crypto entries PAUSED for 30 minutes."
                            )

                        alert_msg = (
                            f"⚡ <b>BTC VOLATILITY SHOCK SHIELD ACTIVATED</b> 🛡️\n\n"
                            f"• <b>BTC 15M Move:</b> {candle_range_pct:.2f}% Range (Body: {body_move_pct:.2f}% | {shock_dir})\n"
                            f"• <b>Reason:</b> Extreme volatility detected. Protecting altcoins from simultaneous SLs.\n"
                            f"• <b>Action:</b> {action_txt}\n"
                            f"• <b>Resumes:</b> {until_ist}\n\n"
                            f"<i>ℹ️ Active position TP/SL guards & Anti-Shock BE Shield remain FULLY ARMED.</i>"
                        )
                        if TELEGRAM_CHAT_ID:
                            asyncio.create_task(send_text_to_chat(str(TELEGRAM_CHAT_ID), alert_msg))
                    except Exception as tel_err:
                        logger.error(f"Failed to send BTC shock Telegram alert: {tel_err}")

                    logger.warning(
                        f"⚡ BTC Volatility Shock triggered! Range={candle_range_pct:.2f}%, Move={body_move_pct:.2f}%, "
                        f"Dir={shock_dir}, Cascade={is_clean_cascade} ({body_ratio:.0%}). Until {new_until.isoformat()}"
                    )

                rem = (_btc_shock_until - now_utc).total_seconds() / 60.0
                return True, rem
    except Exception as e:
        logger.error(f"Error in check_btc_volatility_shock: {e}")

    ctx_after = get_btc_shock_context()
    if ctx_after["active"]:
        return True, ctx_after["remaining_min"]

    return False, 0.0

def is_btc_shock_paused() -> tuple[bool, float]:
    """Synchronous helper to check if BTC shock pause is currently active."""
    ctx = get_btc_shock_context()
    return ctx["active"], ctx["remaining_min"]



def detect_pre_squeeze_coiling(df: pd.DataFrame, symbol: str = "") -> dict:
    """
    Evaluates whether price is tightly compressed/coiling (TTM Squeeze / Bollinger Band coiling).
    Used by Stage 1: Pre-Squeeze Radar.
    """
    if df is None or len(df) < 25:
        return {"is_coiled": False}

    try:
        import pandas as pd
        close = df['close']
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = bb_mid + (2.0 * bb_std)
        bb_lower = bb_mid - (2.0 * bb_std)
        bb_width = (bb_upper - bb_lower) / bb_mid.replace(0, 1e-9)

        # Keltner Channels (20, 1.5 ATR)
        kc_mid = close.ewm(span=20, adjust=False).mean()
        high_low = df['high'] - df['low']
        high_close = (df['high'] - close.shift(1)).abs()
        low_close = (df['low'] - close.shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr_20 = tr.rolling(20).mean()
        kc_upper = kc_mid + (1.5 * atr_20)
        kc_lower = kc_mid - (1.5 * atr_20)

        # TTM Squeeze ON: Bollinger Bands inside Keltner Channels
        ttm_squeeze = bool((bb_lower.iloc[-1] >= kc_lower.iloc[-1]) and (bb_upper.iloc[-1] <= kc_upper.iloc[-1]))

        # Recent 12-candle consolidation range (last 3 hours on 15M)
        recent_12 = df.iloc[-12:]
        range_high = float(recent_12['high'].max())
        range_low = float(recent_12['low'].min())
        curr_price = float(close.iloc[-1])
        compression_pct = ((range_high - range_low) / curr_price) * 100.0 if curr_price > 0 else 5.0
        latest_bb_width = float(bb_width.iloc[-1]) if not pd.isna(bb_width.iloc[-1]) else 0.05

        sym_clean = symbol.upper() if symbol else ""
        is_major = any(m in sym_clean for m in ["BTC", "ETH", "XAU", "GOLD", "SOL"])
        thresh_pct = 1.6 if is_major else 3.0
        thresh_bb = 0.020 if is_major else 0.035

        is_coiled = (compression_pct <= thresh_pct) or (latest_bb_width <= thresh_bb) or ttm_squeeze

        return {
            "is_coiled": bool(is_coiled),
            "compression_pct": round(compression_pct, 2),
            "range_high": range_high,
            "range_low": range_low,
            "bb_width": round(latest_bb_width * 100.0, 2),
            "ttm_squeeze": bool(ttm_squeeze),
            "curr_price": curr_price
        }
    except Exception as e:
        logger.warning(f"Error detecting squeeze coiling: {e}")
        return {"is_coiled": False}


def evaluate_watchlist_coiling_candidate(symbol: str, df_tf, micro: dict = None, tf_states: dict = None, tf_label: str = "1H", tf_data: dict = None) -> dict:
    """
    Performs institutional 6-point verification for Stage 1: Watchlist Coiling / Pre-Squeeze Radar (1H TF & Above).
    Uses completed closed candles, 1H+4H trend / meaningful funding alignment, BTC Micro-Gate pre-check,
    and ATR-buffered invalidation SL so normal wicks do not falsely kill valid squeezes.
    """
    if df_tf is None or len(df_tf) < 26:
        return {"eligible": False, "reason": "Insufficient candles"}

    try:
        import zoneinfo
        from datetime import datetime, timezone
        from config import US_STOCK_TOKENS, is_btc_or_eth_symbol, is_weekend_analysis_blocked
        import pandas as pd

        if is_weekend_analysis_blocked(symbol):
            return {"eligible": False, "reason": "Commodity/US equity market closed on weekend"}

        # Always evaluate on COMPLETED closed candles (exclude live forming bar iloc[-1])
        df_eval = df_tf.iloc[:-1] if len(df_tf) >= 26 else df_tf
        is_btc_eth = is_btc_or_eth_symbol(symbol)

        # ── TICK 1: ACTIVE MARKET & LIQUIDITY CHECK ──
        sym_clean = symbol.upper().strip()
        sym_base = sym_clean.split("/")[0].replace("USDT", "")

        stock_tokens = set(US_STOCK_TOKENS)
        stock_tokens.update({"INTC", "AMD", "BABA", "DIS", "PYPL", "BA", "XOM", "SOXL", "SNDK", "SPY", "QQQ"})
        if sym_base in stock_tokens:
            tz_ist = zoneinfo.ZoneInfo("Asia/Kolkata")
            now_ist = datetime.now(timezone.utc).astimezone(tz_ist)
            if now_ist.weekday() >= 5:
                return {"eligible": False, "reason": "US stock closed on weekend"}

        n_bars = 24 if tf_label.upper() == "1H" else 96
        vol_slice = df_eval['volume'].iloc[-n_bars:] if len(df_eval) >= n_bars else df_eval['volume']
        price_slice = df_eval['close'].iloc[-n_bars:] if len(df_eval) >= n_bars else df_eval['close']
        vol_24h_usd = float((vol_slice * price_slice).sum())

        is_gold = "XAU" in sym_clean or "GOLD" in sym_clean
        is_gold_or_stock = is_gold or (sym_base in stock_tokens)
        if not is_gold_or_stock and vol_24h_usd < 2_500_000.0:
            return {"eligible": False, "reason": f"24h volume ${vol_24h_usd/1e6:.1f}M below $2.5M threshold"}

        # ── TICK 2: VOLATILITY SQUEEZE PINCH (TTM Squeeze or BB Width Pinch <= 4.5%) ──
        close = df_eval['close']
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = bb_mid + (2.0 * bb_std)
        bb_lower = bb_mid - (2.0 * bb_std)
        bb_width_pct = float((bb_upper.iloc[-1] - bb_lower.iloc[-1]) / bb_mid.iloc[-1]) * 100.0

        kc_mid = close.ewm(span=20, adjust=False).mean()
        high_low = df_eval['high'] - df_eval['low']
        high_close = (df_eval['high'] - close.shift(1)).abs()
        low_close = (df_eval['low'] - close.shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr_20 = tr.rolling(20).mean()
        atr_14_ser = tr.rolling(14).mean()
        kc_upper = kc_mid + (1.5 * atr_20)
        kc_lower = kc_mid - (1.5 * atr_20)

        ttm_squeeze = bool((bb_lower.iloc[-1] >= kc_lower.iloc[-1]) and (bb_upper.iloc[-1] <= kc_upper.iloc[-1]))
        max_bb_width = 4.50 if tf_label.upper() == "1H" else 2.50
        is_bb_pinched = bb_width_pct <= max_bb_width

        if not (ttm_squeeze or is_bb_pinched):
            return {"eligible": False, "reason": f"No squeeze pinch (TTM: {ttm_squeeze}, BB width: {bb_width_pct:.2f}%)"}

        # ── TICK 3: GENUINE RANGE COMPRESSION (12 closed 1H bars) ──
        recent_12 = df_eval.iloc[-12:]
        range_high = float(recent_12['high'].max())
        range_low = float(recent_12['low'].min())
        curr_price = float(df_eval['close'].iloc[-1])
        if curr_price <= 0:
            return {"eligible": False, "reason": "Invalid price"}

        compression_pct = ((range_high - range_low) / curr_price) * 100.0
        if is_btc_eth and tf_label.upper() == "1H":
            # Backtest-verified tight compression band for BTC/ETH 1H squeezes (0.60% - 4.50%)
            min_comp = 0.60
            max_comp = 4.50
        else:
            min_comp = 0.40 if tf_label.upper() == "1H" else 0.30
            max_comp = (6.00 if ttm_squeeze else 5.00) if tf_label.upper() == "1H" else 2.50

        if compression_pct < min_comp:
            return {"eligible": False, "reason": f"Dead flatline (compression {compression_pct:.2f}% < {min_comp}%)"}
        if compression_pct > max_comp:
            return {"eligible": False, "reason": f"Not coiled (compression {compression_pct:.2f}% > {max_comp}%)"}

        # ── TICK 4: SMART MONEY ACCUMULATION (Neutral/Positive OI; reject only heavy dumping < -1.50%) ──
        micro = micro or {}
        oi_change_1h = float(micro.get("oi_change_1h_pct", 0.0))
        if not is_gold_or_stock and oi_change_1h < -1.50:
            return {"eligible": False, "reason": f"Heavy OI outflow {oi_change_1h:+.2f}% < -1.50%"}

        # ── TICK 5: TREND & LEVERAGE ALIGNMENT ──
        # BTC & ETH require strict 1H+4H+1D triple HTF alignment; Altcoins/Commodities keep 1H+4H or funding dislocation.
        funding_pct = float(micro.get("funding_pct", 0.0))
        tf_states = tf_states or {}
        h1_trend = str(tf_states.get("1H", "neutral")).lower()
        h4_trend = str(tf_states.get("4H", "neutral")).lower()
        d1_trend = str(tf_states.get("1D", "neutral")).lower()

        if is_btc_eth:
            if h1_trend == "bullish" and h4_trend == "bullish" and d1_trend == "bullish":
                direction = "BUY"
                squeeze_type = "🚀 SHORT SQUEEZE (1H+4H+1D Bullish Base)"
            elif h1_trend == "bearish" and h4_trend == "bearish" and d1_trend == "bearish":
                direction = "SELL"
                squeeze_type = "🩸 LONG SQUEEZE (1H+4H+1D Bearish Breakdown)"
            else:
                return {"eligible": False, "reason": f"BTC/ETH require 1H+4H+1D alignment (1H={h1_trend}, 4H={h4_trend}, 1D={d1_trend})"}
        elif h1_trend == "bullish" and h4_trend == "bullish":
            direction = "BUY"
            squeeze_type = "🚀 SHORT SQUEEZE (Bullish Trend Base)"
        elif h1_trend == "bearish" and h4_trend == "bearish":
            direction = "SELL"
            squeeze_type = "🩸 LONG SQUEEZE (Bearish Trend Breakdown)"
        elif funding_pct <= -0.0250:
            direction = "BUY"
            squeeze_type = "🚀 SHORT SQUEEZE (Heavy Trapped Shorts)"
        elif funding_pct >= 0.0250:
            direction = "SELL"
            squeeze_type = "🩸 LONG SQUEEZE (Heavy Trapped Longs)"
        else:
            return {"eligible": False, "reason": f"No 1H+4H alignment (1H={h1_trend}, 4H={h4_trend}) or extreme funding"}

        # Pre-check Stage 1 with BTC Micro-Gate so Stage 1 only tracks setups Stage 2 can actually take
        try:
            from market_cache import is_btc_safe_for_altcoins
            btc_ok, btc_reason = is_btc_safe_for_altcoins(direction, symbol, tf_data)
            if not btc_ok:
                return {"eligible": False, "reason": f"BTC Micro-Gate mismatch: {btc_reason}"}
        except Exception:
            pass

        # ── TICK 6: OUTER BREAKOUT TRIGGER & ATR/STRUCTURE-SAFE INVALIDATION ──
        atr_val = float(atr_14_ser.iloc[-1]) if not pd.isna(atr_14_ser.iloc[-1]) else (curr_price * 0.01)
        inv_buffer = max(curr_price * 0.008, 0.60 * atr_val)
        if direction == "BUY":
            trigger_entry = range_high * 1.001
            invalidation_sl = range_low - inv_buffer
        else:
            trigger_entry = range_low * 0.999
            invalidation_sl = range_high + inv_buffer

        # Coiling Quality Score (Positive OI gets bonus, flat OI remains eligible)
        coiling_score = (max_comp - compression_pct) * 2.0 + min(max(oi_change_1h, 0.0), 5.0) * 1.5 + (2.0 if ttm_squeeze else 0.0)

        return {
            "eligible": True,
            "symbol": symbol,
            "tf_label": tf_label.upper(),
            "direction": direction,
            "squeeze_type": squeeze_type,
            "funding_pct": funding_pct,
            "oi_change_1h": oi_change_1h,
            "compression_pct": round(compression_pct, 2),
            "vol_24h_usd": vol_24h_usd,
            "ttm_squeeze": ttm_squeeze,
            "bb_width": round(bb_width_pct, 2),
            "range_high": range_high,
            "range_low": range_low,
            "trigger_entry": trigger_entry,
            "invalidation_sl": invalidation_sl,
            "curr_price": curr_price,
            "tf_states": tf_states,
            "coiling_score": round(coiling_score, 2),
            "session_info": micro.get("session_info")
        }
    except Exception as e:
        logger.warning(f"Error evaluating watchlist coiling for {symbol}: {e}")
        return {"eligible": False, "reason": str(e)}
