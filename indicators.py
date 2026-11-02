import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import asyncio
import aiohttp
import logging
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
    elif "BTC" in sym:
        return 0.0050 # 0.50% min SL for BTC
    elif "ETH" in sym:
        return 0.0070 # 0.70% min SL for ETH
    elif "SOL" in sym:
        return 0.0080 # 0.80% min SL for SOL
    else:
        return 0.0080 # 0.80% min SL for Altcoins

def find_liquidity_pool_targets(df: pd.DataFrame, signal: str, entry: float, default_sl: float, symbol: str = "ALT/USDT", tf_data: dict = None) -> tuple:
    """
    Finds dynamic 3-Tier Take-Profit Targets (TP1, TP2, TP3) and SL using ascending Liquidity Pools (BSL/SSL) and Swing Highs/Lows.
    Enforces Asset-Specific Hard Stop-Loss Caps (BTC <= 0.48%, ETH <= 0.80%, SOL <= 2.20%, Altcoins <= 2.50%).
    Enforces Minimum Stop-Loss Floor (Altcoins >= 0.80%) to prevent razor-thin noise stops.
    Returns (sl, (tp1, tp2, tp3), (rr1, rr2, rr3), (lbl1, lbl2, lbl3))
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
        if signal == "BUY":
            tp1, tp2, tp3 = entry + (1.2 * risk), entry + (2.2 * risk), entry + (3.2 * risk)
        else:
            tp1, tp2, tp3 = entry - (1.2 * risk), entry - (2.2 * risk), entry - (3.2 * risk)
        return sl, (tp1, tp2, tp3), (1.2, 2.2, 3.2), ("Near Swing Target", "Mid Swing Target", "Extended Expansion Target")

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
        
        # ── HUMAN-EYE LIQUIDITY SWEEP SL + 0.25*ATR BUFFER + 0.8% SAFETY FLOOR ──
        recent_lows = lows[-5:] if len(lows) >= 5 else [entry * 0.99]
        sweep_low = float(np.min(recent_lows))
        sweep_sl = sweep_low - (0.25 * atr_val) if atr_val > 0 else (sweep_low * 0.9975)

        valid_ssl = [p for p in refine_ssl if p < entry] if refine_ssl else [p for p in ssl_levels if p < entry]
        ssl_low = max(valid_ssl) if valid_ssl else sweep_low
        struct_sl = ssl_low * 0.998

        # Hybrid: Tighter structural sweep level for higher R:R
        candidate_sl = max(sweep_sl, struct_sl)

        # Enforce Minimum 0.80% Stop-Loss Floor (protects against micro-wick noise / spread stopouts)
        min_sl_dist_floor = entry * (1.0 - min_sl_pct)
        sl = min(candidate_sl, min_sl_dist_floor)

        if sl >= entry: sl = np.min(lows[-5:]) * 0.998
        if sl < min_sl_floor: sl = min_sl_floor
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
        
        # High-Probability Single TP Target (>1.5x R:R to 1.8x R:R):
        min_tp1 = entry + max(1.6 * risk, entry * 0.015)
        
        irl_bsl = sorted(list(set([p for p in internal_bsl if p >= min_tp1])))
        erl_bsl = sorted(list(set([p for p in refine_bsl if p >= min_tp1])))
        all_bsl = sorted(list(set(irl_bsl + erl_bsl)))
        
        if len(all_bsl) >= 1:
            raw_tp1 = max(all_bsl[0], min_tp1)
            tp1 = max(raw_tp1 * 0.998, min_tp1)  # 0.20% Front-running buffer
            lbl1 = "Liquidity Pool Target 1 (0.2% Front-Run)"
        else:
            tp1 = entry + (1.6 * risk)
            lbl1 = "1.6 R:R Expansion Target 1"

        higher_bsl = [p for p in all_bsl if p > tp1 * 1.003]
        if len(higher_bsl) >= 1:
            tp2 = higher_bsl[0]
            lbl2 = "Liquidity Pool Target 2"
        else:
            tp2 = max(tp1 * 1.005, entry + (2.5 * risk))
            lbl2 = "2.5 R:R Expansion Target 2"

        even_higher_bsl = [p for p in higher_bsl if p > tp2 * 1.003]
        if len(even_higher_bsl) >= 1:
            tp3 = even_higher_bsl[0]
            lbl3 = "Liquidity Pool Target 3"
        else:
            tp3 = max(tp2 * 1.005, entry + (3.5 * risk))
            lbl3 = "3.5 R:R Expansion Target 3"

        rr1 = (tp1 - entry) / risk
        rr2 = (tp2 - entry) / risk
        rr3 = (tp3 - entry) / risk

        # High Volume Expansion Rule (>= 1.5x 20-SMA Volume): Upgrade primary target (tp1) to upper liquidity pool (tp2)
        vol_expansion = 1.0
        if df is not None and 'volume' in df.columns and len(df) >= 20:
            vol_sma = df['volume'].rolling(20).mean().iloc[-1]
            if vol_sma > 0:
                vol_expansion = float(df['volume'].iloc[-1] / vol_sma)

        if vol_expansion >= 1.5 and tp2 > tp1:
            tp1 = tp2
            lbl1 = f"High Volume Expansion ({vol_expansion:.1f}x) Upper Liquidity Target"
            rr1 = rr2

        return sl, (tp1, tp2, tp3), (round(rr1, 2), round(rr2, 2), round(rr3, 2)), (lbl1, lbl2, lbl3)

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

        # ── HUMAN-EYE LIQUIDITY SWEEP SL + 0.25*ATR BUFFER + 0.8% SAFETY CEILING ──
        recent_highs = highs[-5:] if len(highs) >= 5 else [entry * 1.01]
        sweep_high = float(np.max(recent_highs))
        sweep_sl = sweep_high + (0.25 * atr_val) if atr_val > 0 else (sweep_high * 1.0025)

        valid_bsl = [p for p in refine_bsl if p > entry] if refine_bsl else [p for p in bsl_levels if p > entry]
        bsl_high = min(valid_bsl) if valid_bsl else sweep_high
        struct_sl = bsl_high * 1.002

        # Hybrid: Tighter structural sweep level for higher R:R
        candidate_sl = min(sweep_sl, struct_sl)

        # Enforce Minimum 0.80% Stop-Loss Ceiling (protects against micro-wick noise / spread stopouts)
        max_sl_dist_ceiling = entry * (1.0 + min_sl_pct)
        sl = max(candidate_sl, max_sl_dist_ceiling)

        if sl <= entry: sl = np.max(highs[-5:]) * 1.002
        max_sl_cap = entry * (1 + sl_cap_pct)
        if sl > max_sl_cap: sl = max_sl_cap
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
        
        # High-Probability TP Targets (TP1, TP2, TP3):
        max_tp1 = entry - max(1.6 * risk, entry * 0.015)
        
        irl_ssl = sorted(list(set([p for p in internal_ssl if p <= max_tp1])), reverse=True)
        erl_ssl = sorted(list(set([p for p in refine_ssl if p <= max_tp1])), reverse=True)
        all_ssl = sorted(list(set(irl_ssl + erl_ssl)), reverse=True)
        
        if len(all_ssl) >= 1:
            raw_tp1 = min(all_ssl[0], max_tp1)
            tp1 = min(raw_tp1 * 1.002, max_tp1)  # 0.20% Front-running buffer
            lbl1 = "Liquidity Pool Target 1 (0.2% Front-Run)"
        else:
            tp1 = entry - (1.6 * risk)
            lbl1 = "1.6 R:R Expansion Target 1"

        lower_ssl = [p for p in all_ssl if p < tp1 * 0.997]
        if len(lower_ssl) >= 1:
            tp2 = lower_ssl[0]
            lbl2 = "Liquidity Pool Target 2"
        else:
            tp2 = min(tp1 * 0.995, entry - (2.5 * risk))
            lbl2 = "2.5 R:R Expansion Target 2"

        even_lower_ssl = [p for p in lower_ssl if p < tp2 * 0.997]
        if len(even_lower_ssl) >= 1:
            tp3 = even_lower_ssl[0]
            lbl3 = "Liquidity Pool Target 3"
        else:
            tp3 = min(tp2 * 0.995, entry - (3.5 * risk))
            lbl3 = "3.5 R:R Expansion Target 3"

        rr1 = (entry - tp1) / risk
        rr2 = (entry - tp2) / risk
        rr3 = (entry - tp3) / risk

        # High Volume Expansion Rule (>= 1.5x 20-SMA Volume): Upgrade primary target (tp1) to lower liquidity pool (tp2)
        vol_expansion = 1.0
        if df is not None and 'volume' in df.columns and len(df) >= 20:
            vol_sma = df['volume'].rolling(20).mean().iloc[-1]
            if vol_sma > 0:
                vol_expansion = float(df['volume'].iloc[-1] / vol_sma)

        if vol_expansion >= 1.5 and tp2 < tp1:
            tp1 = tp2
            lbl1 = f"High Volume Expansion ({vol_expansion:.1f}x) Lower Liquidity Target"
            rr1 = rr2

        return sl, (tp1, tp2, tp3), (round(rr1, 2), round(rr2, 2), round(rr3, 2)), (lbl1, lbl2, lbl3)

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

    # RSI Precision Momentum Check
    rsi_val = row.get('rsi', 50.0)
    rsi_min = cfg.get('RSI_MIN', 45.0)
    rsi_max = cfg.get('RSI_MAX', 75.0)
    rsi_bullish = True if pd.isna(rsi_val) else (rsi_min <= rsi_val <= rsi_max)
    rsi_bearish = True if pd.isna(rsi_val) else ((100 - rsi_max) <= rsi_val <= (100 - rsi_min))

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

    # Strategy Setup Validation:
    # All coins (Majors & Altcoins) require clear EMA separation (anti-chop) and proximity to prevent vertical chase.
    # Confluence filtering (3★ and 4★ HTF alignment) serves as the primary master filter in queue_alert/flush_alerts.
    valid_setup = anti_chop_valid and proximity_valid

    # Helper function to append signal with dynamic Liquidity TPs and EMA 9 Pullback Limit Entry
    def add_signal(cross_name, sig_type):
        # Optimal Pullback Tap Entry: Place Entry LIMIT order exactly at the 9 EMA line to catch the retest tap
        ema9_px = float(row.get('ema_9', row['close']))
        opt_entry = float(ema9_px)
        def_sl = buy_sl if sig_type == "BUY" else sell_sl

        dyn_sl, (tp1, tp2, tp3), (rr1, rr2, rr3), (lbl1, lbl2, lbl3) = find_liquidity_pool_targets(df, sig_type, opt_entry, def_sl, symbol=symbol, tf_data=tf_data)
        signals.append({
            "type": cross_name,
            "signal": sig_type,
            "price": opt_entry,
            "sl": dyn_sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "rr1": rr1,
            "rr2": rr2,
            "rr3": rr3,
            "lbl1": lbl1,
            "lbl2": lbl2,
            "lbl3": lbl3,
            "rsi": float(rsi_val) if not pd.isna(rsi_val) else None,
            "adx": float(adx_val) if not pd.isna(adx_val) else 25.0,
            # Backward compatibility single fields
            "tp": tp1,
            "rr": rr1,
            "tp_label": lbl1
        })

    # 1. Base Strategy: 9/26 EMA Cross (Enforcing Volume, ADX, Expansion, Proximity, Anti-Chop, and Strict RSI Filters)
    if valid_setup:
        if prev['ema_9'] <= prev['ema_26'] and row['ema_9'] > row['ema_26']:
            if rsi_bullish:
                add_signal("9/26 EMA Cross", "BUY")
            else:
                logger.debug(f"BUY 9/26 cross for {symbol} skipped: RSI {rsi_val:.1f} outside [{rsi_min}, {rsi_max}]")
        elif prev['ema_9'] >= prev['ema_26'] and row['ema_9'] < row['ema_26']:
            if rsi_bearish:
                add_signal("9/26 EMA Cross", "SELL")
            else:
                logger.debug(f"SELL 9/26 cross for {symbol} skipped: RSI {rsi_val:.1f} outside [{100-rsi_max}, {100-rsi_min}]")
        
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

async def check_btc_volatility_shock(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> tuple[bool, float]:
    """
    Checks BTC/USDT 15M candles for sharp upside/downside price moves (whipsaws/liquidation wicks).
    If candle range % >= 1.5% or abs(open - close) % >= 1.2%:
        Activates 30-minute auto-trading pause lock for new candidate signals.
    Returns (is_shock_active, remaining_minutes)
    """
    global _btc_shock_until
    now_utc = datetime.now(timezone.utc)
    
    if _btc_shock_until is not None:
        if now_utc < _btc_shock_until:
            rem = (_btc_shock_until - now_utc).total_seconds() / 60.0
            return True, rem
        else:
            _btc_shock_until = None
            
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
            
            if body_move_pct >= 1.8 or candle_range_pct >= 2.2:
                new_until = now_utc + timedelta(minutes=15)
                if _btc_shock_until is None or new_until > _btc_shock_until:
                    _btc_shock_until = new_until
                    
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
                        
                        alert_msg = (
                            f"⚡ <b>BTC VOLATILITY SHOCK SHIELD ACTIVATED</b> 🛡️\n\n"
                            f"• <b>BTC 15M Move:</b> {candle_range_pct:.2f}% Range (Body: {body_move_pct:.2f}%)\n"
                            f"• <b>Reason:</b> Extreme market volatility detected. Pausing new entries to prevent buying spike peaks/falling knives.\n"
                            f"• <b>Action:</b> New signal scanning PAUSED for 15 minutes.\n"
                            f"• <b>Resumes:</b> {until_ist}\n\n"
                            f"<i>ℹ️ Active position TP/SL guards, step-trailing SL, and auto-healing remain 100% FULLY ARMED.</i>"
                        )
                        if TELEGRAM_CHAT_ID:
                            asyncio.create_task(send_text_to_chat(str(TELEGRAM_CHAT_ID), alert_msg))
                    except Exception as tel_err:
                        logger.error(f"Failed to send BTC shock Telegram alert: {tel_err}")
                        
                    logger.warning(f"⚡ BTC Volatility Shock Shield triggered! Range: {candle_range_pct:.2f}%, Move: {body_move_pct:.2f}%. Scanner paused for 15m until {new_until.isoformat()}")
                
                rem = (_btc_shock_until - now_utc).total_seconds() / 60.0
                return True, rem
    except Exception as e:
        logger.error(f"Error in check_btc_volatility_shock: {e}")
        
    if _btc_shock_until is not None and now_utc < _btc_shock_until:
        rem = (_btc_shock_until - now_utc).total_seconds() / 60.0
        return True, rem
        
    return False, 0.0

def is_btc_shock_paused() -> tuple[bool, float]:
    """
    Synchronous helper to check if BTC shock pause is currently active.
    """
    global _btc_shock_until
    if _btc_shock_until is None:
        return False, 0.0
    now_utc = datetime.now(timezone.utc)
    if now_utc < _btc_shock_until:
        rem = (_btc_shock_until - now_utc).total_seconds() / 60.0
        return True, rem
    else:
        _btc_shock_until = None
        return False, 0.0



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


def evaluate_watchlist_coiling_candidate(symbol: str, df_tf, micro: dict = None, tf_states: dict = None, tf_label: str = "1H") -> dict:
    """
    Performs institutional 6-point verification for Stage 1: Watchlist Coiling / Pre-Squeeze Radar (1H TF & Above).
    All criteria MUST pass (proper ticks):
    1. Active Market & Liquidity (US Stock hours check, 24h Vol >= $10M)
    2. Range Compression (coiling bounds tailored for timeframe)
    3. Volatility Pinch (TTM Squeeze ON or BB width pinch)
    4. Smart Money OI Accumulation (1H OI change >= +0.50%)
    5. Leverage / Directional Bias (Dislocation funding or aligned trend)
    6. Action Levels Defined (Trigger entry + Invalidation SL)
    """
    if df_tf is None or len(df_tf) < 25:
        return {"eligible": False, "reason": "Insufficient candles"}

    try:
        import zoneinfo
        from datetime import datetime, timezone
        from config import US_STOCK_TOKENS
        import pandas as pd

        # ── TICK 1: ACTIVE MARKET & LIQUIDITY CHECK ──
        sym_clean = symbol.upper().strip()
        sym_base = sym_clean.split("/")[0].replace("USDT", "")

        stock_tokens = set(US_STOCK_TOKENS)
        stock_tokens.update({"INTC", "AMD", "BABA", "DIS", "PYPL", "BA", "XOM", "SOXL", "SNDK", "SPY", "QQQ"})
        if sym_base in stock_tokens:
            tz_ist = zoneinfo.ZoneInfo("Asia/Kolkata")
            now_ist = datetime.now(timezone.utc).astimezone(tz_ist)
            # US regular market hours: Mon-Fri 19:00 - 01:30 IST
            if now_ist.weekday() >= 5:
                return {"eligible": False, "reason": "US stock closed on weekend"}
            in_hours = (now_ist.hour >= 19) or (now_ist.hour == 0) or (now_ist.hour == 1 and now_ist.minute <= 30)
            if not in_hours:
                return {"eligible": False, "reason": "US stock closed outside market hours (19:00 - 01:30 IST)"}

        # Calculate 24h USD volume: 24 bars for 1H, 96 bars for 15M
        n_bars = 24 if tf_label.upper() == "1H" else 96
        vol_slice = df_tf['volume'].iloc[-n_bars:] if len(df_tf) >= n_bars else df_tf['volume']
        price_slice = df_tf['close'].iloc[-n_bars:] if len(df_tf) >= n_bars else df_tf['close']
        vol_24h_usd = float((vol_slice * price_slice).sum())

        # For commodities like Gold (XAU/USD), volume on CFD is tick-based, so bypass dollar threshold
        is_gold = "XAU" in sym_clean or "GOLD" in sym_clean
        if not is_gold and vol_24h_usd < 10_000_000.0:
            return {"eligible": False, "reason": f"24h volume ${vol_24h_usd/1e6:.1f}M below $10M threshold"}

        # ── TICK 2: GENUINE RANGE COMPRESSION (Tailored for 1H TF: 0.60% <= comp <= 3.50%) ──
        recent_12 = df_tf.iloc[-12:]
        range_high = float(recent_12['high'].max())
        range_low = float(recent_12['low'].min())
        curr_price = float(df_tf['close'].iloc[-1])
        if curr_price <= 0:
            return {"eligible": False, "reason": "Invalid price"}

        compression_pct = ((range_high - range_low) / curr_price) * 100.0
        min_comp = 0.50 if tf_label.upper() == "1H" else 0.40
        max_comp = 3.50 if tf_label.upper() == "1H" else 2.20

        if compression_pct < min_comp:
            return {"eligible": False, "reason": f"Dead flatline (compression {compression_pct:.2f}% < {min_comp}%)"}
        if compression_pct > max_comp:
            return {"eligible": False, "reason": f"Not coiled (compression {compression_pct:.2f}% > {max_comp}%)"}

        # ── TICK 3: VOLATILITY SQUEEZE PINCH (TTM Squeeze or BB Width Pinch) ──
        close = df_tf['close']
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = bb_mid + (2.0 * bb_std)
        bb_lower = bb_mid - (2.0 * bb_std)
        bb_width_pct = float((bb_upper.iloc[-1] - bb_lower.iloc[-1]) / bb_mid.iloc[-1]) * 100.0

        kc_mid = close.ewm(span=20, adjust=False).mean()
        high_low = df_tf['high'] - df_tf['low']
        high_close = (df_tf['high'] - close.shift(1)).abs()
        low_close = (df_tf['low'] - close.shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr_20 = tr.rolling(20).mean()
        kc_upper = kc_mid + (1.5 * atr_20)
        kc_lower = kc_mid - (1.5 * atr_20)

        ttm_squeeze = bool((bb_lower.iloc[-1] >= kc_lower.iloc[-1]) and (bb_upper.iloc[-1] <= kc_upper.iloc[-1]))
        max_bb_width = 3.50 if tf_label.upper() == "1H" else 2.20
        is_bb_pinched = bb_width_pct <= max_bb_width

        if not (ttm_squeeze or is_bb_pinched):
            return {"eligible": False, "reason": f"No squeeze pinch (TTM: {ttm_squeeze}, BB width: {bb_width_pct:.2f}%)"}

        # ── TICK 4: SMART MONEY ACCUMULATION (OI 1H >= +0.50%) ──
        micro = micro or {}
        oi_change_1h = float(micro.get("oi_change_1h_pct", 0.0))
        if not is_gold and oi_change_1h < 0.50:
            return {"eligible": False, "reason": f"OI 1H change {oi_change_1h:+.2f}% < +0.50%"}

        # ── TICK 5: DIRECTIONAL & LEVERAGE BIAS ──
        funding_pct = float(micro.get("funding_pct", 0.0))
        tf_states = tf_states or {}
        h1_trend = str(tf_states.get("1H", "neutral")).lower()
        h4_trend = str(tf_states.get("4H", "neutral")).lower()

        if funding_pct <= -0.0050:
            direction = "BUY"
            squeeze_type = "SHORT SQUEEZE (Trapped Shorts)"
        elif funding_pct >= 0.0200:
            direction = "SELL"
            squeeze_type = "LONG SQUEEZE (Trapped Longs)"
        elif h1_trend == "bullish" or h4_trend == "bullish":
            direction = "BUY"
            squeeze_type = "SHORT SQUEEZE (Bullish Base)"
        elif h1_trend == "bearish" or h4_trend == "bearish":
            direction = "SELL"
            squeeze_type = "LONG SQUEEZE (Bearish Breakdown)"
        else:
            mid_price = (range_high + range_low) / 2.0
            direction = "BUY" if curr_price >= mid_price else "SELL"
            squeeze_type = "SHORT SQUEEZE (Bullish Base)" if direction == "BUY" else "LONG SQUEEZE (Bearish Base)"

        # ── TICK 6: ACTION LEVELS DEFINED ──
        trigger_entry = range_high * 1.001 if direction == "BUY" else range_low * 0.999
        invalidation_sl = range_low * 0.998 if direction == "BUY" else range_high * 1.002

        # Coiling Quality Score
        coiling_score = (max_comp - compression_pct) * 2.0 + min(max(oi_change_1h, 0.0), 5.0) + (2.0 if ttm_squeeze else 0.0)

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
