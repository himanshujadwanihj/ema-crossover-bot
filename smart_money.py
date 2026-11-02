import pandas as pd
import numpy as np
from datetime import datetime, timezone
import logging

logger = logging.getLogger("smart_money")

def check_asian_sweep(df_15m, symbol):
    """
    Checks for an ICT Asian Session Liquidity Sweep.
    Asian Session: 00:00 to 04:00 UTC.
    Sweep: Price breaks the Asian High/Low during London/NY (04:00-16:00 UTC) 
    but closes back inside the range, indicating a trap/reversal.
    """
    try:
        if len(df_15m) < 100:
            return None
            
        now_utc = datetime.now(timezone.utc)
        current_hour = now_utc.hour + now_utc.minute / 60.0
        
        # Only valid to trade the sweep after Asian session closes and before late NY (04:00 - 16:00 UTC)
        if not (4.0 <= current_hour <= 17.0):
            return None
            
        df = df_15m.copy()
        
        # Convert index to UTC datetime if not already
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            
        today = now_utc.date()
        
        # Extract Asian Session for today
        asian_session = df[(df.index.date == today) & (df.index.hour >= 0) & (df.index.hour < 4)]
        if len(asian_session) < 12: # Need enough 15m candles
            return None
            
        asian_high = asian_session['high'].max()
        asian_low = asian_session['low'].min()
        
        # Check the last closed 15m candle
        last_closed = df.iloc[-2]
        prev_closed = df.iloc[-3]
        
        # Make sure the current candle is outside the Asian session
        if last_closed.name.hour < 4:
            return None
            
        signal = None
        setup_name = ""
        entry = last_closed['close']
        sl = 0.0
        
        # BEARISH SWEEP (Sweeps Asian High, closes below it)
        if last_closed['high'] > asian_high and last_closed['close'] < asian_high:
            # Must be a bearish candle or a strong wick rejection
            if last_closed['close'] < last_closed['open'] or (last_closed['high'] - max(last_closed['open'], last_closed['close'])) > abs(last_closed['close'] - last_closed['open']) * 1.5:
                signal = "SELL"
                setup_name = "Asian High Sweep (BSL Grab)"
                sl = last_closed['high'] * 1.002
                
        # BULLISH SWEEP (Sweeps Asian Low, closes above it)
        elif last_closed['low'] < asian_low and last_closed['close'] > asian_low:
            # Must be a bullish candle or a strong wick rejection
            if last_closed['close'] > last_closed['open'] or (min(last_closed['open'], last_closed['close']) - last_closed['low']) > abs(last_closed['close'] - last_closed['open']) * 1.5:
                signal = "BUY"
                setup_name = "Asian Low Sweep (SSL Grab)"
                sl = last_closed['low'] * 0.998
                
        if signal:
            risk = abs(entry - sl)
            tp1 = entry + (1.5 * risk) if signal == "BUY" else entry - (1.5 * risk)
            tp2 = entry + (2.5 * risk) if signal == "BUY" else entry - (2.5 * risk)
            tp3 = entry + (4.0 * risk) if signal == "BUY" else entry - (4.0 * risk)
            
            return {
                "symbol": symbol,
                "tf_label": "15M",
                "signal": signal,
                "setup": setup_name,
                "entry": entry,
                "sl": sl,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "candle_ts": str(last_closed.name),
                "unique_key": f"ASIAN_SWEEP_{today}_{signal}"
            }
            
    except Exception as e:
        logger.error(f"Error checking Asian Sweep for {symbol}: {e}")
        
    return None


def check_htf_order_block(df_4h, df_15m, symbol):
    """
    Identifies 4H Order Blocks (unmitigated supply/demand zones)
    and checks if the 15m chart is currently tapping them and reversing.
    """
    try:
        if len(df_4h) < 50 or len(df_15m) < 10:
            return None
            
        # 1. Find 4H Order Blocks
        bullish_obs = []
        bearish_obs = []
        
        for i in range(1, len(df_4h) - 3):
            c1 = df_4h.iloc[i]
            c2 = df_4h.iloc[i+1]
            c3 = df_4h.iloc[i+2]
            
            # Bullish OB: Bearish candle followed by strong bullish engulfing/impulse
            if c1['close'] < c1['open'] and c2['close'] > c2['open'] and c2['close'] > c1['high']:
                # Ensure the impulse has follow-through
                if c3['close'] > c2['close']:
                    ob_zone = (c1['low'], c1['high'])
                    # Check if mitigated (any future candle went below OB high? 
                    # Strictly, mitigation is touching the OB high/open).
                    mitigated = False
                    for j in range(i+2, len(df_4h) - 1):
                        if df_4h.iloc[j]['low'] <= ob_zone[1]:
                            mitigated = True
                            break
                    if not mitigated:
                        bullish_obs.append(ob_zone)
                        
            # Bearish OB: Bullish candle followed by strong bearish engulfing/impulse
            if c1['close'] > c1['open'] and c2['close'] < c2['open'] and c2['close'] < c1['low']:
                if c3['close'] < c2['close']:
                    ob_zone = (c1['low'], c1['high'])
                    mitigated = False
                    for j in range(i+2, len(df_4h) - 1):
                        if df_4h.iloc[j]['high'] >= ob_zone[0]:
                            mitigated = True
                            break
                    if not mitigated:
                        bearish_obs.append(ob_zone)
                        
        if not bullish_obs and not bearish_obs:
            return None
            
        # 2. Check 15m chart against unmitigated 4H OBs
        last_15 = df_15m.iloc[-2]
        
        signal = None
        setup_name = ""
        entry = last_15['close']
        sl = 0.0
        
        # Check Bullish OB tap
        for ob in bullish_obs:
            # If 15m low taps the zone (between ob low and ob high)
            if last_15['low'] <= ob[1] and last_15['close'] >= ob[0]:
                # Must be a bullish reversal on 15m (closed green)
                if last_15['close'] > last_15['open']:
                    signal = "BUY"
                    setup_name = "4H Bullish Order Block Tap"
                    sl = ob[0] * 0.998 # SL just below the 4H OB Low
                    break
                    
        # Check Bearish OB tap
        if not signal:
            for ob in bearish_obs:
                if last_15['high'] >= ob[0] and last_15['close'] <= ob[1]:
                    if last_15['close'] < last_15['open']:
                        signal = "SELL"
                        setup_name = "4H Bearish Order Block Tap"
                        sl = ob[1] * 1.002 # SL just above the 4H OB High
                        break
                        
        if signal:
            risk = abs(entry - sl)
            # If risk is massive (e.g. OB is huge), cap it.
            if risk / entry > 0.05: return None 
            
            if risk == 0: risk = entry * 0.01
            
            tp1 = entry + (1.5 * risk) if signal == "BUY" else entry - (1.5 * risk)
            tp2 = entry + (2.5 * risk) if signal == "BUY" else entry - (2.5 * risk)
            tp3 = entry + (4.0 * risk) if signal == "BUY" else entry - (4.0 * risk)
            
            return {
                "symbol": symbol,
                "tf_label": "15M",
                "signal": signal,
                "setup": setup_name,
                "entry": entry,
                "sl": sl,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "candle_ts": str(last_15.name),
                "unique_key": f"OB_TAP_{setup_name}_{last_15.name}"
            }
            
    except Exception as e:
        logger.error(f"Error checking Order Blocks for {symbol}: {e}")
        
    return None
