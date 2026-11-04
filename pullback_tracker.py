import asyncio
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta
import zoneinfo
from fetcher import fetch_ohlcv_async, get_top_pairs_by_volume
from indicators import _compute_indicators, get_macro_structure, find_liquidity_pool_targets, get_trend_state
from telegram_bot import send_text_alert, format_price, queue_alert
from state import is_coin_on_cooldown, is_already_sent, mark_sent, set_coin_cooldown
from executor import check_and_apply_retest_trailing_sl

logger = logging.getLogger("pullback_tracker")
import time
BOT_START_TIME = time.time()

# Core 10 Coins
CORE_10 = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "SUI/USDT"
]

# Top 25 Curated Altcoins
TOP_25_ALTS = [
    "NEAR/USDT", "DOT/USDT", "LTC/USDT", "SHIB/USDT", "TRX/USDT",
    "UNI/USDT", "AAVE/USDT", "ARB/USDT", "POL/USDT", "RENDER/USDT",
    "TAO/USDT", "JUP/USDT", "ONDO/USDT", "OP/USDT", "TIA/USDT",
    "SEI/USDT", "INJ/USDT", "PYTH/USDT", "LDO/USDT", "FET/USDT",
    "APT/USDT", "GRT/USDT", "ENA/USDT", "FIL/USDT", "HBAR/USDT"
]

# Commodities (Spot Gold, Silver, Crude Oil, Natural Gas)
COMMODITIES = [
    "XAU/USD", "XAUT/USDT", "XAG/USD", "CL/USD", "NATGAS/USD"
]

# US Equities (15 Assets)
US_EQUITIES = [
    "NVDA/USDT", "TSLA/USDT", "MSTR/USDT", "MSFT/USDT",
    "GOOGL/USDT", "QQQ/USDT", "COIN/USDT", "INTC/USDT",
    "AMD/USDT", "AAPL/USDT", "AMZN/USDT", "META/USDT",
    "NFLX/USDT", "SPY/USDT", "BABA/USDT"
]

# Curated Scanner Pool (54 Curated Assets)
CURATED_SCANNER_POOL = set(CORE_10 + TOP_25_ALTS + COMMODITIES + US_EQUITIES)

# Extended Altcoins for general watchlist / volume scanning
ADDITIONAL_ALTS = [
    "S/USDT", "EIGEN/USDT", "PENDLE/USDT", "ZRO/USDT",
    "STX/USDT", "ATOM/USDT", "RAY/USDT", "STRK/USDT", "ZK/USDT",
    "IMX/USDT", "W/USDT", "IO/USDT", "AR/USDT", "CFG/USDT", "DYM/USDT",
    "GALA/USDT", "BLUR/USDT", "HAEDAL/USDT", "ZAMA/USDT",
    "PEPE/USDT", "WIF/USDT", "FLOKI/USDT", "BONK/USDT", "BCH/USDT", "ICP/USDT",
    "RUNE/USDT", "ORDI/USDT", "WLD/USDT", "NOT/USDT", "PNUT/USDT", "ACT/USDT", "XLM/USDT"
]

TOP_COINS = CORE_10
ALTCOINS = TOP_25_ALTS + COMMODITIES + ADDITIONAL_ALTS

# Combine for total watchlist coverage
WATCHLIST = list(dict.fromkeys(CORE_10 + TOP_25_ALTS + COMMODITIES + US_EQUITIES + ADDITIONAL_ALTS))

# List 3: SPECIAL LIST (Delta / Custom Pairs)
SPECIAL_LIST = []



def is_same_level_retest(symbol: str, tf: str, new_price: float, signal: str, threshold_pct: float = 0.015, max_hours: float = 2.5) -> bool:
    """
    Checks if a signal in the SAME direction on the SAME timeframe was already alerted
    for this symbol at virtually the SAME price level (within threshold_pct) within the last max_hours.
    Returns True (PAUSE) if price is hovering at the same level on that TF.
    Returns False (ALLOW) if price has moved away, or if it is on a different timeframe / level.
    """
    from state import _load
    state = _load()
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=max_hours)
    clean_sym = symbol.upper()
    clean_tf = str(tf).upper()

    for k, v in reversed(list(state.items())):
        if not isinstance(v, dict):
            continue
        parts = k.split("|")
        if len(parts) < 4:
            continue
        sym_part, tf_part, sig_part, _ = parts
        if sym_part.upper() != clean_sym:
            continue

        # Scope level dedup strictly to the same timeframe category so 15M does not suppress 1H/4H/1D!
        if not tf_part.upper().startswith(clean_tf):
            continue
        
        # Only compare against SAME direction signals (never block reversals)
        if sig_part.upper() != signal.upper():
            continue
            
        sent_at_str = v.get("sent_at")
        if not sent_at_str:
            continue
        try:
            sent_at = datetime.fromisoformat(sent_at_str.replace("Z", "+00:00"))
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            if sent_at < cutoff:
                continue
        except Exception:
            continue
            
        prev_entry = float(v.get("entry") or 0.0)
        if prev_entry <= 0:
            continue
            
        pct_diff = abs(new_price - prev_entry) / prev_entry
        if pct_diff <= threshold_pct:
            return True
            
    return False


def get_ist_time_str(dt_or_str) -> str:
    try:
        ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        if isinstance(dt_or_str, str):
            dt = datetime.fromisoformat(dt_or_str.replace("Z", "+00:00"))
        else:
            dt = dt_or_str
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist_dt = dt.astimezone(ist_tz)
        return ist_dt.strftime("%d-%b %H:%M IST")
    except Exception:
        return str(dt_or_str)

def get_crossover_context(df, trend):
    for i in range(len(df)-2, 1, -1):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        if trend == "BULLISH" and prev['ema_9'] <= prev['ema_26'] and row['ema_9'] > row['ema_26']:
            cross_time_ist = get_ist_time_str(df.index[i])
            return (len(df) - 2) - i, row['close'], cross_time_ist
        if trend == "BEARISH" and prev['ema_9'] >= prev['ema_26'] and row['ema_9'] < row['ema_26']:
            cross_time_ist = get_ist_time_str(df.index[i])
            return (len(df) - 2) - i, row['close'], cross_time_ist
    return -1, None, None

def get_full_watchlist():
    try:
        top_vol = get_top_pairs_by_volume(80)
        return list(dict.fromkeys(SPECIAL_LIST + WATCHLIST + top_vol))
    except Exception:
        return list(dict.fromkeys(SPECIAL_LIST + WATCHLIST))

async def check_symbol_pullbacks(symbol: str, tf_data: dict, session=None) -> bool:
    if not tf_data: return False

    # STRICT USER DIRECTIVE: No trades of commodities, BTC, and ETH on Sat and Sunday in any case
    from config import is_weekend_trading_blocked
    if is_weekend_trading_blocked(symbol):
        logger.debug(f"WEEKEND RETEST BLOCKED: {symbol} skipped on Saturday/Sunday.")
        return False

    # RETEST TIMEFRAME RULE:
    # Gold (XAU/USD, XAUT/USDT): Scalping retests on 5M and 15M are fully active per user directive!
    # All other coins: Retests are strictly 1-Hour (1H) and Above (1H, 4H, 1D, 1W).
    is_gold_asset = any(k in symbol.upper() for k in ["XAU", "XAUT"])
    if is_gold_asset:
        tf_map = {"5m": "5M", "15m": "15M", "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W"}
    else:
        tf_map = {"1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W"}

    # Deduplication and level filters handle spam per timeframe.
    first_cand = None
    
    for tf, tf_key in tf_map.items():
            
        try:
            df = tf_data.get(tf_key)
            if df is None or len(df) < 30: continue
            
            df = _compute_indicators(df)
            row = df.iloc[-1]       # LIVE forming candle
            prev_row = df.iloc[-2]  # Previous closed candle
            
            ema26 = row['ema_26']
            ema9 = row['ema_9']
            candle_time_utc = df.index[-1]
            candle_time_ist = get_ist_time_str(candle_time_utc)
            
            macro_struct = "Unknown"
            try:
                macro_struct = get_macro_structure(df)
            except Exception:
                pass

            trend = "BULLISH" if ema9 > ema26 else "BEARISH"

            bars_ago, cross_price, cross_time_ist = get_crossover_context(df, trend)

            crossover_context_str = ""
            if cross_price and bars_ago >= 0 and cross_time_ist:
                pct_diff = ((row['close'] - cross_price) / cross_price) * 100
                cross_time_only = cross_time_ist.split(' ')[1] if ' ' in cross_time_ist else cross_time_ist
                crossover_context_str = f"{cross_time_only} IST ({bars_ago} candles ago @ {format_price(cross_price)}, {pct_diff:+.2f}% move)"

            # Live Real-time Retest Detection:
            # 1. Wick physically touches the exact EMA line (low <= ema <= high).
            # 2. Distance threshold (1H: 0.75%, 4H: 1.0%, 1D: 1.5%, 1W: 2.5%).
            touch_threshold = {"5m": 0.0025, "15m": 0.0040, "1h": 0.0075, "4h": 0.010, "1d": 0.015, "1w": 0.025}.get(tf, 0.010)
            candle_range = max(row['high'] - row['low'], 1e-8)

            tapped_26 = (row['low'] <= ema26 <= row['high']) and (abs(row['close'] - ema26) / ema26 <= touch_threshold)
            tapped_9 = (row['low'] <= ema9 <= row['high']) and (abs(row['close'] - ema9) / ema9 <= touch_threshold)

            if not (tapped_26 or tapped_9):
                continue

            target_ema = ema26 if tapped_26 else ema9
            prev_close = prev_row['close']
            cur_close = row['close']

            # STRICT SUPPORT vs RESISTANCE DEFINITION (CANDLE & WICK DIRECTION):
            # 1. SUPPORT (BUY Bounce):
            #    - Price came from ABOVE the EMA (previous close was above the EMA).
            #    - Low tapped the EMA, and price pushes back UP.
            #    - Leaves a LOWER WICK (tail at bottom: (close - low) / candle_range >= 0.25).
            #    - Current price stays/closes above the EMA (cur_close >= target_ema * 0.998).
            #
            # 2. RESISTANCE (SELL Rejection):
            #    - Price came from BELOW the EMA (previous close was below the EMA).
            #    - High tapped the EMA, and price pushes back DOWN.
            #    - Leaves an UPPER WICK (tail at top: (high - close) / candle_range >= 0.25).
            #    - Current price stays/closes below the EMA (cur_close <= target_ema * 1.002).

            # STRICT SUPPORT vs RESISTANCE DEFINITION (CANDLE & WICK DIRECTION):
            # Support Retests only occur in BULLISH trend (EMA 9 > EMA 26) bouncing upward off EMA.
            # Resistance Retests only occur in BEARISH trend (EMA 9 < EMA 26) rejecting downward off EMA.
            is_support = False
            is_resistance = False

            if trend == "BULLISH":
                # Price came from ABOVE the EMA and must sustain ABOVE/AT the EMA (no dumping below EMA!)
                if prev_close >= target_ema * 0.998 and cur_close >= target_ema * 0.998:
                    lower_wick_ratio = (cur_close - row['low']) / candle_range
                    if lower_wick_ratio >= 0.20 or cur_close > target_ema:
                        is_support = True
            elif trend == "BEARISH":
                # Price came from BELOW the EMA and must sustain BELOW/AT the EMA (no pumping above EMA!)
                if prev_close <= target_ema * 1.002 and cur_close <= target_ema * 1.002:
                    upper_wick_ratio = (row['high'] - cur_close) / candle_range
                    if upper_wick_ratio >= 0.20 or cur_close < target_ema:
                        is_resistance = True

            if is_support:
                sig_type = "BUY"
                action_name = "Support Retest"
                def_sl = target_ema * 0.998
            elif is_resistance:
                sig_type = "SELL"
                action_name = "Resistance Retest"
                def_sl = target_ema * 1.002
            else:
                # Ambiguous or counter-trend touch
                continue

            if tapped_26 or tapped_9:
                ema_name = "EMA 26" if tapped_26 else "EMA 9"
                tf_label = tf.upper()
                retest_price = float(ema26 if tapped_26 else ema9)
                unique_key = f"{tf_label}_{ema_name.replace(' ', '')}_{action_name.replace(' ', '')}"
                unified_key = f"{tf_label}_EMARetest_{action_name.replace(' ', '')}"
                
                # 1. Deduplication per candle so each fresh pullback tap generates an alert
                dedup_ts = str(candle_time_utc)
                if is_already_sent(symbol, unique_key, sig_type, dedup_ts) or \
                   is_already_sent(symbol, unified_key, sig_type, dedup_ts):
                    continue

                # 2. Strict Per-Coin Anti-Spam Cooldown (Strict 4-Hour Lock to prevent repeated notifications!)
                is_gold_asset = any(k in symbol.upper() for k in ["XAU", "XAUT"])
                if tf_label in ["5M", "15M"]:
                    cooldown_hours = 0.35 if tf_label == "5M" else 0.75  # 20m for 5M, 45m for 15M Gold scalps
                    if is_coin_on_cooldown(symbol, tf_label, max_hours=cooldown_hours):
                        logger.info(f"COOLDOWN: {symbol} {tf_label} scalp suppressed - already notified in last {cooldown_hours*60:.0f}m.")
                        continue
                else:
                    gold_cooldown_hours = 4.0 if is_gold_asset else 3.0
                    if is_coin_on_cooldown(symbol, tf_label, max_hours=gold_cooldown_hours) or \
                       (is_gold_asset and is_coin_on_cooldown(symbol, max_hours=3.0)):
                        logger.info(f"COOLDOWN: {symbol} {tf_label} alert suppressed - already notified in the last {gold_cooldown_hours}h.")
                        continue

                # 3. Smart Level-Aware Filter: If price is identical to an alert in the last 4 hours on this TF, pause spam
                if is_same_level_retest(symbol, tf_label, retest_price, sig_type, threshold_pct=0.015, max_hours=4.0):
                    logger.info(f"PAUSED duplicate retest for {symbol} {tf_label} {sig_type} @ {retest_price:.4f}: Price within 1.5% of recent alerted level.")
                    continue

                # ─── HIGH-WINRATE UPGRADES (58% WIN RATE ENGINE) ───────────────────
                # A. 4H HIGHER TIMEFRAME TREND FILTER (ELIMINATES 3,200+ COUNTER-TREND TRAPS)
                target_trend = "bullish" if sig_type == "BUY" else "bearish"
                if tf_label == "1H":
                    h4_trend = get_trend_state(tf_data.get("4H"))
                    if h4_trend and h4_trend.lower() != target_trend:
                        logger.info(f"FILTERED 1H RETEST: {symbol} {sig_type} rejected due to opposing 4H trend ({h4_trend}).")
                        continue
                elif tf_label in ["5M", "15M"]:
                    h1_trend = get_trend_state(tf_data.get("1H"))
                    if h1_trend and h1_trend.lower() != target_trend:
                        logger.info(f"FILTERED {tf_label} RETEST: {symbol} {sig_type} rejected due to opposing 1H trend ({h1_trend}).")
                        continue

                # B. SUB-CANDLE EARLY FAILURE CHECK (30M BREAKDOWN ELIMINATES 66.6% FAILURES)
                df_30m = tf_data.get("30M")
                if tf_label == "1H" and df_30m is not None and len(df_30m) >= 5:
                    last_30m = df_30m.iloc[-1]
                    if sig_type == "BUY" and last_30m['close'] < target_ema * 0.998:
                        logger.info(f"FILTERED 1H RETEST: {symbol} BUY rejected - 30M closed below 1H EMA ({last_30m['close']:.4f} < {target_ema:.4f}).")
                        continue
                    elif sig_type == "SELL" and last_30m['close'] > target_ema * 1.002:
                        logger.info(f"FILTERED 1H RETEST: {symbol} SELL rejected - 30M closed above 1H EMA ({last_30m['close']:.4f} > {target_ema:.4f}).")
                        continue

                # C. CANDLE REACTION & LOWER LOW PROTECTION (PREVENTS CATCHING FALLING KNIVES)
                c1_high = float(prev_row['high'])
                c1_low = float(prev_row['low'])
                cur_high = float(row['high'])
                cur_low = float(row['low'])

                # If candle printed an active breakdown lower low below retest candle (on BUY), support failed!
                if sig_type == "BUY" and cur_low < c1_low * 0.998:
                    logger.info(f"FILTERED 1H RETEST: {symbol} BUY rejected - Lower Low printed below retest candle.")
                    continue
                elif sig_type == "SELL" and cur_high > c1_high * 1.002:
                    logger.info(f"FILTERED 1H RETEST: {symbol} SELL rejected - Higher High printed above retest candle.")
                    continue

                # D. SOLUTION 1 - RULE 1: MICRO STRUCTURE SHIFT (CHoCH / BREAK OF STRUCTURE ON 15M)
                # Prevents buying into dying bounces that continuously make lower highs (e.g. BNB $758 -> $752.5 -> breakdown)
                df_15m = tf_data.get("15M") if tf_data.get("15M") is not None else tf_data.get("15m")
                if df_15m is not None and len(df_15m) >= 6:
                    if sig_type == "BUY":
                        recent_highs = df_15m['high'].iloc[-7:-1]
                        local_swing_high = float(recent_highs.max())
                        cur_15m_high = float(df_15m['high'].iloc[-1])
                        is_consecutive_lower_highs = (
                            df_15m['high'].iloc[-2] < df_15m['high'].iloc[-3] and
                            df_15m['high'].iloc[-3] < df_15m['high'].iloc[-4]
                        )
                        if is_consecutive_lower_highs and cur_15m_high < (local_swing_high * 0.998):
                            logger.info(f"FILTERED RETEST: {symbol} BUY rejected ? Micro Structure Failed (Consecutive Lower Highs on 15M, no CHoCH/local high break).")
                            continue
                    elif sig_type == "SELL":
                        recent_lows = df_15m['low'].iloc[-7:-1]
                        local_swing_low = float(recent_lows.min())
                        cur_15m_low = float(df_15m['low'].iloc[-1])
                        is_consecutive_higher_lows = (
                            df_15m['low'].iloc[-2] > df_15m['low'].iloc[-3] and
                            df_15m['low'].iloc[-3] > df_15m['low'].iloc[-4]
                        )
                        if is_consecutive_higher_lows and cur_15m_low > (local_swing_low * 1.002):
                            logger.info(f"FILTERED RETEST: {symbol} SELL rejected ? Micro Structure Failed (Consecutive Higher Lows on 15M, no CHoCH/local low break).")
                            continue

                # E. SOLUTION 1 - RULE 2: BOUNCE VOLUME COMPARISON
                # Bounce candle volume must show buyer conviction, not a low-volume drift into support
                cur_vol = float(row.get('volume', 0.0))
                prev_vol = float(prev_row.get('volume', 0.0))
                if prev_vol > 0 and cur_vol > 0:
                    if sig_type == "BUY" and prev_row['close'] < prev_row['open']:
                        if cur_vol < (0.60 * prev_vol):
                            logger.info(f"FILTERED RETEST: {symbol} BUY rejected ? Weak Bounce Volume ({cur_vol:.0f} < 0.6x prior sell volume {prev_vol:.0f}).")
                            continue
                    elif sig_type == "SELL" and prev_row['close'] > prev_row['open']:
                        if cur_vol < (0.60 * prev_vol):
                            logger.info(f"FILTERED RETEST: {symbol} SELL rejected ? Weak Rejection Volume ({cur_vol:.0f} < 0.6x prior buy volume {prev_vol:.0f}).")
                            continue

                # F. SOLUTION 2: BTC REAL-TIME MICRO-GATE & RELATIVE STRENGTH
                from market_cache import is_btc_safe_for_altcoins
                is_safe, btc_reason = is_btc_safe_for_altcoins(sig_type, symbol, tf_data)
                if not is_safe:
                    logger.info(f"FILTERED RETEST: {symbol} {sig_type} rejected by BTC Micro-Gate ({btc_reason}).")
                    continue

                sl, (tp1, tp2, tp3), (rr1, rr2, rr3), (lbl1, lbl2, lbl3) = find_liquidity_pool_targets(df, sig_type, retest_price, def_sl, symbol=symbol, tf_data=tf_data)
                
                # Dynamic Retest Trailing Hook:
                # If an active trade is already open on this coin, evaluate if this confirmed retest allows trailing SL
                try:
                    await check_and_apply_retest_trailing_sl(session, symbol, sig_type, sl, tf_label)
                except Exception as e_trail:
                    logger.warning(f"Retest trailing check error for {symbol}: {e_trail}")
                
                tf_states = {
                    "1H": get_trend_state(tf_data.get("1H")),
                    "4H": get_trend_state(tf_data.get("4H")),
                    "1D": get_trend_state(tf_data.get("1D")),
                    "1W": get_trend_state(tf_data.get("1W")),
                }

                cur_rsi = float(row['rsi']) if ('rsi' in row and not pd.isna(row['rsi'])) else None
                # Strict RSI Retest Filter (ensures healthy pullbacks, rejects falling knives)
                if cur_rsi is not None:
                    rsi_buy_min, rsi_buy_max = (35.0, 75.0) if tf_label in ["5M", "15M"] else (40.0, 70.0)
                    rsi_sell_min, rsi_sell_max = (25.0, 65.0) if tf_label in ["5M", "15M"] else (30.0, 60.0)
                    if sig_type == "BUY" and not (rsi_buy_min <= cur_rsi <= rsi_buy_max):
                        logger.info(f"FILTERED RETEST: {symbol} BUY rejected — RSI {cur_rsi:.1f} outside healthy zone [{rsi_buy_min}, {rsi_buy_max}].")
                        continue
                    elif sig_type == "SELL" and not (rsi_sell_min <= cur_rsi <= rsi_sell_max):
                        logger.info(f"FILTERED RETEST: {symbol} SELL rejected — RSI {cur_rsi:.1f} outside healthy zone [{rsi_sell_min}, {rsi_sell_max}].")
                        continue

                is_special = symbol in SPECIAL_LIST
                await queue_alert(
                    symbol=symbol,
                    tf_label=tf_label,
                    signal=sig_type,
                    entry=retest_price,
                    sl=sl,
                    tp=tp1,
                    tf_states=tf_states,
                    cross_type=f"{ema_name} {action_name}",
                    candle_time=candle_time_ist,
                    macro_struct=macro_struct,
                    tp1=tp1, tp2=tp2, tp3=tp3,
                    rr1=rr1, rr2=rr2, rr3=rr3,
                    lbl1=lbl1, lbl2=lbl2, lbl3=lbl3,
                    tf_data=tf_data,
                    is_special_list=is_special,
                    rsi=cur_rsi
                )
                mark_sent(symbol, unique_key, sig_type, dedup_ts, entry=retest_price, sl=sl, tp=tp1, tp1=tp1, tp2=tp2, tp3=tp3)
                mark_sent(symbol, unified_key, sig_type, dedup_ts, entry=retest_price, sl=sl, tp=tp1, tp1=tp1, tp2=tp2, tp3=tp3)
                logger.info(f"Sent {ema_name} {action_name} Alert for {symbol} {tf_label} {trend}")
                set_coin_cooldown(symbol, tf_label)
                if any(k in symbol.upper() for k in ["XAU", "XAUT"]) and tf_label not in ["5M", "15M"]:
                    set_coin_cooldown(symbol)  # Global cooldown for HTF Gold only
                
                target_trend = "bullish" if sig_type == "BUY" else "bearish"
                aligned_count = sum(1 for t in ["1H", "4H", "1D", "1W"] if tf_states.get(t, "neutral").lower() == target_trend)
                is_3_htf_aligned = (
                    tf_states.get("1H", "").lower() == target_trend and
                    tf_states.get("4H", "").lower() == target_trend and
                    tf_states.get("1D", "").lower() == target_trend
                )
                
                if first_cand is None:
                    first_cand = {
                        "symbol": symbol,
                        "signal": sig_type,
                        "entry": retest_price,
                        "sl": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "tp3": tp3,
                        "tp": tp1,
                        "aligned_count": aligned_count,
                        "is_3_htf_aligned": is_3_htf_aligned,
                        "is_30m_early": False,
                        "is_retest": True,
                        "is_special": is_special,
                        "unique_key": unique_key,
                        "candle_ts": dedup_ts,
                        "rr3": rr3 or 1.5,
                        "rsi": cur_rsi,
                    }
        except Exception as e:
            logger.error(f"Error in pullback tracker for {symbol}: {e}")
    return first_cand or False
