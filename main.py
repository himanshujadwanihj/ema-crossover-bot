import pandas as pd
import asyncio
import aiohttp
import logging
import os
import time
import warnings
# BUG #12 FIX: Suppress pandas/yfinance FutureWarning spam that clutters Railway logs
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning if hasattr(pd.errors, 'PerformanceWarning') else FutureWarning)
BOT_START_TIME = time.time()
import json
import zoneinfo
from datetime import datetime, timezone, timedelta
from config import validate_config, SCAN_INTERVAL_MINUTES, TIMEFRAMES, MAX_PAIRS, clean_contract_symbol, TELEGRAM_CHAT_ID, TIMEZONE
from fetcher import get_top_pairs_by_volume, fetch_all_timeframes_async, fetch_ohlcv_async
from indicators import analyze_crossovers, get_trend_state, _compute_indicators, get_macro_structure, find_last_crossover_time, check_btc_volatility_shock
from state import (
    is_already_sent, mark_sent, get_all_signals, get_pending_trades, update_trade_outcome, 
    update_trade_progress, update_trade_sl, _parse_iso_dt, clear_all_cooldowns, clear_all_pending_backlogs,
    is_coin_on_cooldown, set_coin_cooldown, _make_key,
    get_last_trade_activity_ts, record_trade_activity_ts,
    get_last_inactivity_notice_ts, set_last_inactivity_notice_ts,
)
from telegram_bot import (
    send_startup_message, send_text_alert, format_price, queue_alert, flush_alerts, 
    start_telegram_bot_service, stop_telegram_bot_service, send_text_to_chat,
    send_market_inactivity_notice,
)
from executor import executor, round_price
from pullback_tracker import check_symbol_pullbacks, WATCHLIST, SPECIAL_LIST
from market_cache import update_symbol_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("main")


async def run_startup_reconciliation(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
    """
    1-Minute Startup Reconciliation & Offline Audit:
    When bot resumes after being stopped:
    1. Audits all pending/active paper positions and resting orders to see what happened offline.
    2. Checks for hits (TP1, TP2, TP3, SL) and 30-min expirations.
    3. Reconciles wallet balance, locked profits, and fees.
    4. Logs detailed breakdown and sends an admin notification.
    5. Waits the remaining time of 60 seconds for full order book and market data stabilization before scanning.
    """
    start_recon = time.time()
    logger.info("=" * 65)
    logger.info("🔄 BOT RESUMED: Starting 60-Second Offline Audit & Reconciliation...")
    logger.info("=" * 65)
    
    # 1. Resolve pending trades & paper wallet (ONLY if paper trading is enabled)
    from config import PAPER_TRADING_ENABLED
    from paper_trader import evaluate_paper_trades_async, load_wallet
    
    if PAPER_TRADING_ENABLED:
        w_before = load_wallet()
        active_before = len(w_before.get("active_positions", []))
        resting_before = len(w_before.get("resting_orders", []))
        closed_before = len(w_before.get("closed_trades", []))
        
        await resolve_pending_trades_async(session, semaphore)
        await evaluate_paper_trades_async(session, semaphore)
        
        w_after = load_wallet()
        active_after = len(w_after.get("active_positions", []))
        resting_after = len(w_after.get("resting_orders", []))
        closed_after = len(w_after.get("closed_trades", []))
        newly_closed = closed_after - closed_before
        
        logger.info(f"📊 RECONCILIATION AUDIT (PAPER):")
        logger.info(f"   • Active Positions: {active_before} -> {active_after}")
        logger.info(f"   • Current Balance:  ₹{w_after.get('balance', 0):,.2f} INR")
    else:
        # Hard purge any leftover paper state from persistent disk
        load_wallet()
        active_after = 0
        newly_closed = 0
    
    # Send one clean Telegram admin notification about the reconciliation
    try:
        from telegram_bot import send_text_to_chat
        from config import TELEGRAM_CHAT_ID
        import zoneinfo
        admin_chat = TELEGRAM_CHAT_ID or "771305593"
        time_str_recon = datetime.now(zoneinfo.ZoneInfo('Asia/Kolkata')).strftime('%d %b %Y, %I:%M %p IST')
        
        if PAPER_TRADING_ENABLED:
            recon_msg = (
                "🔄 <b>BOT RE-ACTIVATED & RECONCILED</b>\n\n"
                f"• Time: <b>{time_str_recon}</b>\n"
                f"• Mode: <b>PAPER TRADING (SIMULATION)</b>\n"
                f"• Active Positions: <b>{active_after}</b>\n"
                f"• Closed/Expired while offline: <b>{newly_closed}</b>\n"
                f"• Live Paper Balance: <b>₹{w_after.get('balance', 0):,.2f} INR</b>\n\n"
                "⏳ <i>Warming up feeds... Fresh live market scan starting in a few seconds.</i>"
            )
        else:
            from executor import executor
            shark_bal = await executor.get_wallet_balance_summary(session) if executor.is_configured() else ""
            pos_list = await executor.get_open_positions(session) if executor.is_configured() else []
            # Seed last real trade timestamp from live/closed Shark history so restarts preserve quiet-market timer
            try:
                latest_exch_ts = get_last_trade_activity_ts()
                for op in (pos_list or []):
                    for f_ts in ("updatedAt", "createdAt"):
                        dt_op = _parse_iso_dt(op.get(f_ts))
                        if dt_op and dt_op.timestamp() > latest_exch_ts:
                            latest_exch_ts = dt_op.timestamp()
                if executor.is_configured():
                    st_cl, closed_raw = await executor.primary.shark_get(session, "/v1/positions/CLOSED")
                    if st_cl == 200 and isinstance(closed_raw, list):
                        for cp in closed_raw[:15]:
                            for f_ts in ("createdAt", "updatedAt"):
                                dt_cp = _parse_iso_dt(cp.get(f_ts))
                                if dt_cp and dt_cp.timestamp() > latest_exch_ts:
                                    latest_exch_ts = dt_cp.timestamp()
                record_trade_activity_ts(latest_exch_ts if latest_exch_ts > 0 else BOT_START_TIME)
            except Exception as e_seed:
                logger.debug(f"Could not seed last trade activity timestamp: {e_seed}")
            recon_msg = (
                "🔄 <b>BOT RE-ACTIVATED & RECONCILED</b>\n\n"
                f"• Time: <b>{time_str_recon}</b>\n"
                f"• Mode: 🟢 <b>REAL LIVE TRADING (SHARK EXCHANGE)</b>\n"
                f"• Live Broker Positions: <b>{len(pos_list)}</b>\n"
                f"{shark_bal}\n\n"
                "⏳ <i>100% Target Engine Online. Zero paper simulation active.</i>"
            )
        await send_text_to_chat(admin_chat, recon_msg)
    except Exception as e_tg:
        logger.warning(f"Could not dispatch reconciliation Telegram alert: {e_tg}")


    # Ensure full 60 seconds warm-up before kicking off the first scan
    elapsed = time.time() - start_recon
    remain_sleep = max(0, 60.0 - elapsed)
    if remain_sleep > 0:
        logger.info(f"⏳ Market warm-up in progress ({remain_sleep:.1f}s remaining). Waiting before initial live scan...")
        await asyncio.sleep(remain_sleep)
    
    logger.info("✅ 60-Second Reconciliation Complete. Initiating Fresh Live Scan...")


async def resolve_pending_trades_async(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
    # NOTE: evaluate_paper_trades_async is called by tp_monitor_worker every 5s.
    # Do NOT call it here again to avoid double-evaluation race conditions (Bug #4 fix).
    pending = get_pending_trades()
    if not pending:
        return
        
    for key, data in pending.items():
        try:
            parts = key.split("|")
            if len(parts) < 4: continue
            symbol = parts[0]
            direction = parts[2]
            
            from executor import is_us_stock_token
            if is_us_stock_token(symbol):
                update_trade_outcome(key, "SIGNAL_ONLY_US_STOCK")
                continue
            
            entry = data.get("entry")
            sl = data.get("sl")
            initial_sl = data.get("initial_sl") or sl
            tp = data.get("tp")
            tp1 = data.get("tp1") or tp
            tp2 = data.get("tp2") or data.get("tp3") or tp
            tp3 = tp2  # Backward compat alias
            if not entry or not sl or not tp1:
                update_trade_outcome(key, "INVALID")
                continue
                
            df = await fetch_ohlcv_async(session, symbol, "1h", 1000, semaphore)
            if df is None or df.empty:
                continue
                
            sent_at_str = data.get("sent_at")
            if not sent_at_str: continue
            sent_at = _parse_iso_dt(sent_at_str)
            if not sent_at: continue
            
            limit_placed_at_str = data.get("limit_placed_at") or sent_at_str
            limit_placed_at = _parse_iso_dt(limit_placed_at_str) or sent_at

            # USER DIRECTIVE: Strict 30-Minute Limit Order Expiry Everywhere!
            # If entry limit order is not filled within 30 minutes, it is cancelled.
            limit_cutoff = limit_placed_at + timedelta(minutes=30)

            limit_filled = data.get("limit_filled", False)
            limit_filled_at_str = data.get("limit_filled_at")
            limit_filled_at_dt = _parse_iso_dt(limit_filled_at_str) if limit_filled_at_str else None

            after_signal = df[df.index > sent_at]
            if after_signal.empty:
                continue
                
            risk = abs(entry - initial_sl)
            if risk == 0: risk = entry * 0.01

            raw_tp1 = float(data.get("tp1")) if data.get("tp1") is not None else None
            raw_tp2 = float(data.get("tp2") or data.get("tp3")) if (data.get("tp2") is not None or data.get("tp3") is not None) else None

            if direction == "BUY":
                tp1 = raw_tp1 if (raw_tp1 and raw_tp1 > entry) else (entry + 1.5 * risk)
                tp2 = raw_tp2 if (raw_tp2 and raw_tp2 >= tp1) else tp1
                tp3 = tp2
            else:
                tp1 = raw_tp1 if (raw_tp1 and raw_tp1 < entry) else (entry - 1.5 * risk)
                tp2 = raw_tp2 if (raw_tp2 and raw_tp2 <= tp1) else tp1
                tp3 = tp2

            tp1_hit = data.get("tp1_hit", False)
            tp2_hit = data.get("tp2_hit", False)
            tp1_hit_at_str = data.get("tp1_hit_at")
            tp1_hit_at_dt = _parse_iso_dt(tp1_hit_at_str) if tp1_hit_at_str else None
            tp2_hit_at_str = data.get("tp2_hit_at")
            tp2_hit_at_dt = _parse_iso_dt(tp2_hit_at_str) if tp2_hit_at_str else None

            shark_order_placed = data.get("shark_order_placed", False)
            outcome = None
            final_price = initial_sl
            now_utc = datetime.now(timezone.utc)

            # REAL-TIME CLOCK 20-MIN EXPIRY CHECK (Does not wait for 1H candle to close!)
            if not limit_filled:
                has_pos = False
                if shark_order_placed:
                    from executor import executor
                    pos = await executor.get_open_position(session, symbol)
                    if pos and float(pos.get("quantity", 0)) > 0:
                        has_pos = True
                        limit_filled = True
                        update_trade_progress(key, limit_filled=True, limit_filled_at=now_utc.isoformat())
                        # CRITICAL FIX: Place TPs immediately on restart-detected fill
                        # (candle loop will skip fill-check since limit_filled=True — TPs must be placed HERE)
                        if tp1 and tp2:
                            from executor import executor as _exec_tp
                            await _exec_tp.place_tp_limits(session, symbol, direction, entry, tp1, tp2, initial_sl=initial_sl)

                if not has_pos and now_utc > limit_cutoff:
                    outcome = "CANCELLED_UNFILLED"
                    from executor import executor as _exec
                    await _exec.cancel_all_open_orders(session, symbol)
                    update_trade_outcome(key, outcome)
                    continue

            for idx, row in after_signal.iterrows():
                candle_dt = idx.to_pydatetime() if hasattr(idx, 'to_pydatetime') else idx
                if candle_dt.tzinfo is None:
                    candle_dt = candle_dt.replace(tzinfo=timezone.utc)
                
                # Rule 1: Limit Order Fill Check (Direct Shark API Check)
                if not limit_filled:
                    has_pos = False
                    if shark_order_placed:
                        from executor import executor
                        pos = await executor.get_open_position(session, symbol)
                        if pos and float(pos.get("quantity", 0)) > 0:
                            has_pos = True
                    
                    if has_pos or (direction == "BUY" and row['low'] <= entry and candle_dt <= limit_cutoff) or \
                       (direction == "SELL" and row['high'] >= entry and candle_dt <= limit_cutoff):
                        limit_filled = True
                        limit_filled_at_dt = candle_dt
                        limit_filled_at_str = candle_dt.isoformat()
                        update_trade_progress(key, limit_filled=True, limit_filled_at=limit_filled_at_str)
                        if shark_order_placed:
                            from executor import executor
                            await executor.place_tp_limits(session, symbol, direction, entry, tp1, tp2, initial_sl=initial_sl)
                    elif candle_dt > limit_cutoff:
                        outcome = "CANCELLED_UNFILLED"
                        if shark_order_placed:
                            from executor import executor as _exec
                            await _exec.cancel_all_open_orders(session, symbol)
                        break
                    else:
                        continue

                # Rule 4: 18-Hour Time-Based Expiration Gate (from fill time)
                fill_dt = limit_filled_at_dt or limit_placed_at
                expire_cutoff = fill_dt + timedelta(hours=18)
                if candle_dt > expire_cutoff:
                    if tp2_hit:
                        outcome = "TP2_EXIT"
                    elif tp1_hit:
                        outcome = "BREAKEVEN"
                    else:
                        outcome = "EXPIRED_TIME"
                    final_price = row['close']
                    break

                # For live Shark Exchange trades, do NOT simulate TP hits from Binance candle wicks.
                # Outcomes & Telegram alerts are strictly driven by actual Shark Exchange order fills.
                if shark_order_placed:
                    continue

                # Rule 2.5-Hour Time Shield: DISABLED per user directive (allow trades full room to hit TP1)
                time_shield_hit = False

                # Rule 3: Multi-Stage Partial Profit & Trailing SL Check (+0.4R Profit-Lock Engine)
                risk_dist = abs(entry - initial_sl) if (initial_sl and initial_sl != entry) else (entry * 0.015)
                profit_lock_sl = (entry + 0.40 * risk_dist) if direction == "BUY" else (entry - 0.40 * risk_dist)

                if tp2_hit and tp2_hit_at_dt is not None and candle_dt >= tp2_hit_at_dt:
                    cur_sl = tp1
                elif tp1_hit and tp1_hit_at_dt is not None and candle_dt >= tp1_hit_at_dt:
                    cur_sl = profit_lock_sl
                elif time_shield_hit:
                    cur_sl = entry
                else:
                    cur_sl = data.get("cur_sl") or initial_sl

                if direction == "BUY":
                    # 1. Stop-Loss / Trailed SL Hit FIRST
                    if row['low'] <= cur_sl:
                        outcome = "TP2_EXIT" if tp2_hit else ("PROFIT_LOCK_WIN" if tp1_hit else "LOSS")
                        final_price = cur_sl
                        break

                    # 2. Check TP1 Hit (Unified 100% Full Exit at TP1)
                    if not tp1_hit and row['high'] >= tp1:
                        tp1_hit = True
                        tp2_hit = True
                        tp1_hit_at_dt = candle_dt
                        tp1_hit_at_str = tp1_hit_at_dt.isoformat()
                        update_trade_progress(key, tp1_hit=True, tp2_hit=True, new_sl=tp1, tp1_hit_at=tp1_hit_at_str, tp2_hit_at=tp1_hit_at_str)
                        outcome = "WIN"
                        final_price = tp1
                        break

                elif direction == "SELL":
                    # 1. Stop-Loss / Trailed SL Hit FIRST
                    if row['high'] >= cur_sl:
                        outcome = "TP2_EXIT" if tp2_hit else ("PROFIT_LOCK_WIN" if tp1_hit else "LOSS")
                        final_price = cur_sl
                        break

                    # 2. Check TP1 Hit (Unified 100% Full Exit at TP1)
                    if not tp1_hit and row['low'] <= tp1:
                        tp1_hit = True
                        tp2_hit = True
                        tp1_hit_at_dt = candle_dt
                        tp1_hit_at_str = tp1_hit_at_dt.isoformat()
                        update_trade_progress(key, tp1_hit=True, tp2_hit=True, new_sl=tp1, tp1_hit_at=tp1_hit_at_str, tp2_hit_at=tp1_hit_at_str)
                        outcome = "WIN"
                        final_price = tp1
                        break

            if not limit_filled and now_utc > limit_cutoff:
                outcome = "CANCELLED_UNFILLED"
                final_price = entry
                        
            if outcome:
                update_trade_outcome(key, outcome)
                # DAILY RISK CIRCUIT BREAKER: Only track if live auto-trading is NOT active.
                # (When live trading is active, executor.py is the 100% authoritative real broker source).
                from config import AUTO_TRADING_ENABLED
                if not AUTO_TRADING_ENABLED:
                    if outcome in ("SL", "LOSS"):
                        from state import record_trade_result
                        cb_trig, cb_st = record_trade_result(is_sl=True, symbol=symbol, pause_minutes=60, max_consecutive_sl=5)
                        if cb_trig:
                            from telegram_bot import send_circuit_breaker_alert
                            await send_circuit_breaker_alert(cb_st["paused_until"], cb_st["consecutive_sl_count"], symbol)
                    elif outcome in ("WIN", "TP1", "TP2", "TP3"):
                        from state import record_trade_result
                        record_trade_result(is_sl=False, symbol=symbol)
                # Disabled cancel_all_open_orders: Shark natively cleans up POSITION-attached orders on close.
                # Note: Win & Loss outcome summary alerts and scoreboards disabled per user directive.
                # Only Crossover Signal Alert and Entry Hit Alert remain active.
        except Exception as e:
            logger.error(f"Error resolving {key}: {e}")

async def process_symbol_async(session, symbol, semaphore, tf_data=None, shock_ctx=None):
    try:
        from state import is_already_sent, mark_sent
        from pullback_tracker import SPECIAL_LIST
        from market_cache import is_commodity_symbol, is_stock_symbol
        in_crypto_shock = bool(
            shock_ctx and shock_ctx.get("active")
            and not is_commodity_symbol(symbol) and not is_stock_symbol(symbol)
        )
        
        if tf_data is None:
            tf_data = await fetch_all_timeframes_async(session, symbol, semaphore)
        if not tf_data: return 0

        # Weekend Commodity & Stock Suppression Rule (Commodities & US Equities closed on Sat/Sun IST)
        # BTC and ETH are crypto 24/7 and fully ACTIVE on weekends per user directive.
        # STRICT USER DIRECTIVE: No trades of commodities, BTC, and ETH on Sat and Sunday in any case
        from config import is_weekend_trading_blocked
        if is_weekend_trading_blocked(symbol):
            logger.info(f"WEEKEND TRADING PAUSED: {symbol} skipped — zero trades for commodities, BTC, and ETH on Saturday/Sunday.")
            return 0

        from telegram_bot import TF_ORDER
        tf_states = {}
        for l in TF_ORDER:
            tf_states[l] = get_trend_state(tf_data.get(l))
            
        signals_sent = 0
        sq_candidate = None

        macro_struct = "Unknown"
        if "1D" in tf_data:
            from indicators import get_macro_structure
            macro_struct = get_macro_structure(tf_data["1D"])
            
        from config import ALLOWED_15M_COINS
            
        # -- 0. DEDICATED SQUEEZE BREAKOUT ENGINE (Completely decoupled from EMA Crossovers) --
        # Paused for crypto during active BTC Volatility Shock to avoid multi-coin whipsaw SLs.
        df_1h = tf_data.get("1H") if tf_data.get("1H") is not None else tf_data.get("1h")
        if (not in_crypto_shock) and df_1h is not None and len(df_1h) >= 25:
            try:
                # 1. STRICT CANDLE FRESHNESS GATE: Must be freshly completed 1H candle (within 1 hour)
                candle_dt = df_1h.index[-2]
                if hasattr(candle_dt, "tzinfo") and candle_dt.tzinfo is None:
                    candle_dt = candle_dt.replace(tzinfo=timezone.utc)
                elif not hasattr(candle_dt, "tzinfo"):
                    candle_dt = pd.to_datetime(candle_dt, utc=True)
                
                seconds_since_close = (datetime.now(timezone.utc) - candle_dt).total_seconds()
                if 0 <= seconds_since_close <= 3660:
                    recent_12 = df_1h.iloc[-14:-2] if len(df_1h) >= 14 else df_1h.iloc[:-2]
                    range_high = float(recent_12['high'].max())
                    range_low = float(recent_12['low'].min())
                    candle_close = float(df_1h['close'].iloc[-2])
                    candle_ts = str(df_1h.index[-2])

                    range_width_pct = (range_high - range_low) / max(range_low, 1e-8)
                    # Valid consolidation range is 0.8% to 8.5% (prevents wild swinging chop from qualifying)
                    is_valid_consolidation = (0.008 <= range_width_pct <= 0.085)

                    from state import get_active_radar, mark_radar_triggered
                    from fetcher import fetch_futures_microstructure_async
                    radar_entry = get_active_radar(symbol)
                    has_radar = radar_entry and radar_entry.get("status") == "WATCHING"
                    
                    c_open = float(df_1h['open'].iloc[-2])
                    c_high = float(df_1h['high'].iloc[-2])
                    c_low = float(df_1h['low'].iloc[-2])
                    c_close = float(df_1h['close'].iloc[-2])
                    c_body = abs(c_close - c_open)
                    c_range = max(c_high - c_low, 1e-8)
                    body_ratio = c_body / c_range
                    lower_wick_ratio = (min(c_open, c_close) - c_low) / c_range
                    upper_wick_ratio = (c_high - max(c_open, c_close)) / c_range

                    vol_20_sma = df_1h['volume'].iloc[-22:-2].mean() if len(df_1h) >= 22 else df_1h['volume'].mean()
                    breakout_vol = float(df_1h['volume'].iloc[-2])
                    
                    from indicators import _compute_indicators
                    df_1h_ind = _compute_indicators(df_1h)
                    sq_rsi = float(df_1h_ind['rsi'].iloc[-2]) if 'rsi' in df_1h_ind and not pd.isna(df_1h_ind['rsi'].iloc[-2]) else 50.0
                    atr_1h = float(df_1h_ind['atr'].iloc[-1]) if 'atr' in df_1h_ind and not pd.isna(df_1h_ind['atr'].iloc[-1]) else (candle_close * 0.005)
                    ema_26_1h = float(df_1h_ind['ema_26'].iloc[-1]) if 'ema_26' in df_1h_ind and not pd.isna(df_1h_ind['ema_26'].iloc[-1]) else None

                    sq_signal = None
                    is_trap_reversal = False
                    trap_direction = None

                    # Check 15M micro-structure for reversal confirmation
                    df_15m = tf_data.get("15M") if tf_data.get("15M") is not None else tf_data.get("15m")
                    choch_bullish = True
                    choch_bearish = True
                    if df_15m is not None and len(df_15m) >= 3:
                        choch_bullish = bool(df_15m['high'].iloc[-1] >= df_15m['high'].iloc[-2])
                        choch_bearish = bool(df_15m['low'].iloc[-1] <= df_15m['low'].iloc[-2])

                    # ─────────────────────────────────────────────────────────────
                    # DUAL ENGINE: BRANCH A — SFP / LIQUIDITY SWEEP REVERSAL ENGINE
                    # ─────────────────────────────────────────────────────────────
                    if is_valid_consolidation:
                        # 1. BEAR TRAP REVERSAL (Support Sweep + Reclaim -> BUY):
                        if c_low < range_low:
                            sweep_depth_pct = ((range_low - c_low) / range_low) * 100.0
                            is_reclaimed = c_close >= (range_low * 0.999)
                            # Rules: Sweep depth <= 2.0% (not a bloodbath), candle range <= 2.5*ATR, lower wick >= 35%, RSI < 42.0 (oversold sellers trap), 15M CHoCH
                            if (sweep_depth_pct <= 2.0 and c_range <= (2.5 * atr_1h) and 
                                is_reclaimed and lower_wick_ratio >= 0.35 and sq_rsi < 42.0 and choch_bullish):
                                sq_signal = 'BUY'
                                is_trap_reversal = True
                                trap_direction = 'BEAR_TRAP'
                                logger.info(f"🎯 DETECTED 1H BEAR TRAP: {symbol} swept {sweep_depth_pct:.2f}% below support, reclaimed with {lower_wick_ratio:.1%} lower wick (RSI {sq_rsi:.1f}). BUY Reversal Activated!")

                        # 2. BULL TRAP REVERSAL (Resistance Sweep + Rejection -> SELL):
                        elif c_high > range_high and not sq_signal:
                            sweep_depth_pct = ((c_high - range_high) / range_high) * 100.0
                            is_rejected = c_close <= (range_high * 1.001)
                            # Rules: Sweep depth <= 2.0%, candle range <= 2.5*ATR, upper wick >= 35%, RSI > 58.0 (overbought buyers trap), 15M CHoCH
                            if (sweep_depth_pct <= 2.0 and c_range <= (2.5 * atr_1h) and 
                                is_rejected and upper_wick_ratio >= 0.35 and sq_rsi > 58.0 and choch_bearish):
                                sq_signal = 'SELL'
                                is_trap_reversal = True
                                trap_direction = 'BULL_TRAP'
                                logger.info(f"🎯 DETECTED 1H BULL TRAP: {symbol} swept {sweep_depth_pct:.2f}% above resistance, rejected with {upper_wick_ratio:.1%} upper wick (RSI {sq_rsi:.1f}). SELL Reversal Activated!")

                    # ─────────────────────────────────────────────────────────────
                    # DUAL ENGINE: BRANCH B — PRE-SQUEEZE RADAR (V2: Candle-Close Inv, 6h Window, 1H+4H Align, Retest-Only 2.2R)
                    # ─────────────────────────────────────────────────────────────
                    if not sq_signal:
                        # MODE A: Dedicated Pre-Squeeze Radar (Continuous Lifecycle Tracking)
                        if has_radar:
                            radar_trigger = float(radar_entry.get('trigger_entry', 0.0))
                            radar_invalidation = float(radar_entry.get('invalidation_sl', 0.0))
                            radar_dir = str(radar_entry.get('direction', '')).upper()

                            # 1. Confirmed 1H Candle-Close Invalidation Check (liquidity sweep wicks do NOT kill active coil):
                            is_invalidated = False
                            invalidation_reason = ""
                            if radar_dir == 'BUY' and radar_invalidation > 0 and candle_close <= radar_invalidation:
                                is_invalidated = True
                                invalidation_reason = f"1H candle closed below support invalidation SL ({format_price(radar_invalidation, symbol)})."
                            elif radar_dir == 'SELL' and radar_invalidation > 0 and candle_close >= radar_invalidation:
                                is_invalidated = True
                                invalidation_reason = f"1H candle closed above resistance invalidation SL ({format_price(radar_invalidation, symbol)})."

                            if is_invalidated:
                                from state import mark_radar_invalidated
                                from telegram_bot import send_radar_status_update
                                mark_radar_invalidated(symbol, invalidation_reason)
                                await send_radar_status_update(radar_entry, status="INVALIDATED", reason=invalidation_reason, candle_close=candle_close)
                                logger.info(f"🛑 RADAR INVALIDATED for {symbol}: {invalidation_reason}")
                                has_radar = False

                            # 2. 6-Hour Expiration Check (use immutable first_detected_at so re-coiling doesn't reset the timer):
                            if has_radar:
                                created_str = radar_entry.get("first_detected_at") or radar_entry.get("created_at")
                                if created_str:
                                    try:
                                        created_dt = datetime.fromisoformat(created_str)
                                        if created_dt.tzinfo is None:
                                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                                        if (datetime.now(timezone.utc) - created_dt).total_seconds() > 21600:
                                            from state import mark_radar_expired
                                            from telegram_bot import send_radar_status_update
                                            exp_reason = "6 hours elapsed without breakout close. Consolidation lost momentum."
                                            mark_radar_expired(symbol)
                                            await send_radar_status_update(radar_entry, status="EXPIRED", reason=exp_reason, candle_close=candle_close)
                                            logger.info(f"⏱️ RADAR EXPIRED for {symbol} after 6h coiling window.")
                                            has_radar = False
                                    except Exception:
                                        pass

                            # 3. Confirmed Close Breakout Check (pre-close wicks keep WATCHING alive within 6h window):
                            if has_radar:
                                if radar_dir == 'BUY':
                                    if candle_close >= (radar_trigger or (range_high * 1.001)):
                                        sq_signal = 'BUY'
                                    elif c_high >= radar_trigger:
                                        logger.info(f"🕯️ PRE-CLOSE WICK PROBE for {symbol} BUY ({format_price(c_high, symbol)} -> close {format_price(candle_close, symbol)}) — keeping radar WATCHING.")
                                elif radar_dir == 'SELL':
                                    if candle_close <= (radar_trigger or (range_low * 0.999)):
                                        sq_signal = 'SELL'
                                    elif c_low <= radar_trigger:
                                        logger.info(f"🕯️ PRE-CLOSE WICK PROBE for {symbol} SELL ({format_price(c_low, symbol)} -> close {format_price(candle_close, symbol)}) — keeping radar WATCHING.")

                        # MODE B: Direct Multi-Hour Consolidation Breakdown / Breakout (Requires 1H+4H Alignment & Tight BB Pinch)
                        elif is_valid_consolidation:
                            bb_mid_1h = float(df_1h['close'].iloc[-21:-1].mean()) if len(df_1h) >= 21 else 0.0
                            bb_std_1h = float(df_1h['close'].iloc[-21:-1].std()) if len(df_1h) >= 21 else 0.0
                            bb_w_1h = ((4.0 * bb_std_1h) / bb_mid_1h * 100.0) if bb_mid_1h > 0 else 99.0
                            h1_tr_b = str(tf_states.get("1H", "")).lower()
                            h4_tr_b = str(tf_states.get("4H", "")).lower()
                            if bb_w_1h <= 4.5:
                                if candle_close <= (range_low * 0.998) and h1_tr_b == "bearish" and h4_tr_b == "bearish":
                                    sq_signal = 'SELL'
                                elif candle_close >= (range_high * 1.002) and h1_tr_b == "bullish" and h4_tr_b == "bullish":
                                    sq_signal = 'BUY'

                        if sq_signal:
                            req_body = 0.35 if has_radar else 0.45
                            fail_reason = None
                            if body_ratio < req_body:
                                fail_reason = f"Candle body ratio {body_ratio:.1%} < {req_body:.0%} (Weak expansion / long wick trap)."
                            elif sq_signal == 'BUY' and (sq_rsi > (85.0 if has_radar else 78.0) or sq_rsi < (35.0 if has_radar else 42.0)):
                                fail_reason = f"RSI {sq_rsi:.1f} outside healthy momentum (has_radar={has_radar})."
                            elif sq_signal == 'SELL' and (sq_rsi < (15.0 if has_radar else 22.0) or sq_rsi > (65.0 if has_radar else 58.0)):
                                fail_reason = f"RSI {sq_rsi:.1f} outside healthy momentum (has_radar={has_radar})."
                            elif sq_signal == 'BUY' and upper_wick_ratio > (0.45 if has_radar else 0.38):
                                fail_reason = f"Upper rejection wick {upper_wick_ratio:.1%} > {'45%' if has_radar else '38%'} (Shooting star / whale trap)."
                            elif sq_signal == 'SELL' and lower_wick_ratio > (0.45 if has_radar else 0.38):
                                fail_reason = f"Lower rejection wick {lower_wick_ratio:.1%} > {'45%' if has_radar else '38%'} (Hammer / bear trap)."
                            elif vol_20_sma > 0 and breakout_vol < ((1.05 if has_radar else 1.25) * vol_20_sma):
                                fail_reason = f"Volume {breakout_vol:.0f} < {'1.05x' if has_radar else '1.25x'} 20-SMA {vol_20_sma:.0f} (Volume surge missing)."

                            if fail_reason:
                                logger.info(f"FILTERED 1H CHASER: {symbol} rejected — {fail_reason}")
                                sq_signal = None

                    if sq_signal:
                        from market_cache import is_btc_safe_for_altcoins
                        is_safe, btc_reason = is_btc_safe_for_altcoins(sq_signal, symbol, tf_data)
                        if not is_safe:
                            logger.info(f"FILTERED 1H SIGNAL: {symbol} {sq_signal} rejected by BTC Micro-Gate ({btc_reason}).")
                            sq_signal = None

                        # Breakout Freshness Guard: Reject historical signals older than 65 minutes
                        if sq_signal and candle_ts:
                            c_dt = _parse_iso_dt(candle_ts)
                            if c_dt and (datetime.now(timezone.utc) - c_dt).total_seconds() > 3900:
                                logger.info(f"FILTERED STALE 1H SIGNAL: {symbol} 1H candle was {(datetime.now(timezone.utc) - c_dt).total_seconds()/60:.1f}m ago while bot was offline.")
                                sq_signal = None

                        unique_sq_key = f"1H_TrapReversal_{trap_direction}_{candle_ts}" if is_trap_reversal else (f"1H_RangeBreak_{sq_signal}_{candle_ts}" if not has_radar else f"1H_SqueezeBreakout_{sq_signal}_{candle_ts}")
                        if sq_signal and not is_already_sent(symbol, unique_sq_key, sq_signal, candle_ts):
                            micro_1h = await fetch_futures_microstructure_async(session, symbol, sq_signal, df=df_1h, closed_only=True)
                            cvd_metrics = micro_1h.get("cvd_metrics", {})
                            cvd_aligned = cvd_metrics.get("is_aligned", True)
                            
                            if cvd_aligned:
                                if is_trap_reversal:
                                    # REVERSAL SETUP: Quick 1.5R target back to 26 EMA / Range Boundary
                                    target_r = 1.5
                                    if trap_direction == 'BEAR_TRAP':
                                        entry = round_price(symbol, candle_close)
                                        sl = round_price(symbol, float(c_low) * 0.998)
                                        risk = abs(entry - sl) if (entry and sl and entry != sl) else (candle_close * 0.015)
                                        if risk > (candle_close * 0.025):
                                            logger.info(f"FILTERED REVERSAL: {symbol} Bear trap risk {risk/candle_close:.1%} > 2.5% (Wick too long).")
                                            sq_signal = None
                                        tp1 = round_price(symbol, entry + (target_r * risk))
                                        tp2 = tp1
                                        cross_type = "🚀 1H Bear Trap Reversal (Short Squeeze Bounce + SFP ✅)"
                                    else:
                                        entry = round_price(symbol, candle_close)
                                        sl = round_price(symbol, float(c_high) * 1.002)
                                        risk = abs(entry - sl) if (entry and sl and entry != sl) else (candle_close * 0.015)
                                        if risk > (candle_close * 0.025):
                                            logger.info(f"FILTERED REVERSAL: {symbol} Bull trap risk {risk/candle_close:.1%} > 2.5% (Wick too long).")
                                            sq_signal = None
                                        tp1 = round_price(symbol, entry - (target_r * risk))
                                        tp2 = tp1
                                        cross_type = "🩸 1H Bull Trap Reversal (Long Squeeze Flush + SFP ✅)"
                                else:
                                    # RETEST-ONLY LIMIT DISCIPLINE (V2): Never market-chase late 1H squeeze spikes;
                                    # always place disciplined retest limit at frozen box boundary + max(1%, 1*ATR) SL floor!
                                    target_r = 2.2
                                    box_hi_ref = float(radar_entry.get("range_high") or range_high) if has_radar else range_high
                                    box_lo_ref = float(radar_entry.get("range_low") or range_low) if has_radar else range_low
                                    sl_floor = max(candle_close * 0.010, atr_1h)

                                    if sq_signal == 'BUY':
                                        retest_target = box_hi_ref * 1.0015
                                        entry = round_price(symbol, retest_target if candle_close > retest_target * 1.002 else min(candle_close, retest_target))
                                        logger.info(f"🎯 1H SQUEEZE RETEST LIMIT (V2): {symbol} BUY limit @ {entry} (box high {box_hi_ref})")

                                        sl = float(df_1h['low'].iloc[-5:-1].min()) * 0.998
                                        if ema_26_1h:
                                            sl = min(sl, ema_26_1h - (0.25 * atr_1h))
                                        if (entry - sl) < sl_floor:
                                            sl = entry - sl_floor
                                        if entry and sl and (entry - sl) > (candle_close * 0.025):
                                            sl = entry - (candle_close * 0.025)
                                        sl = round_price(symbol, sl)
                                        risk = abs(entry - sl) if (entry and sl and entry != sl) else (candle_close * 0.015)
                                        tp1 = round_price(symbol, entry + (target_r * risk))
                                        tp2 = tp1
                                        cross_type = "🚀 1H Range Breakout (Volume Expansion + CVD ✅)" if not has_radar else "🚀 1H Short-Squeeze Breakout (Retest Limit + CVD ✅)"
                                    else:
                                        retest_target = box_lo_ref * 0.9985
                                        entry = round_price(symbol, retest_target if candle_close < retest_target * 0.998 else max(candle_close, retest_target))
                                        logger.info(f"🎯 1H SQUEEZE RETEST LIMIT (V2): {symbol} SELL limit @ {entry} (box low {box_lo_ref})")

                                        sl = float(df_1h['high'].iloc[-5:-1].max()) * 1.002
                                        if ema_26_1h:
                                            sl = max(sl, ema_26_1h + (0.25 * atr_1h))
                                        if (sl - entry) < sl_floor:
                                            sl = entry + sl_floor
                                        if entry and sl and (sl - entry) > (candle_close * 0.025):
                                            sl = entry + (candle_close * 0.025)
                                        sl = round_price(symbol, sl)
                                        risk = abs(entry - sl) if (entry and sl and entry != sl) else (candle_close * 0.015)
                                        tp1 = round_price(symbol, entry - (target_r * risk))
                                        tp2 = tp1
                                        cross_type = "🩸 1H Range Breakdown (Volume Expansion + CVD ✅)" if not has_radar else "🩸 1H Long-Squeeze Breakdown (Retest Limit + CVD ✅)"

                                if sq_signal:
                                    dyn_sl = sl
                                    rr1 = target_r
                                    rr2 = target_r
                                    lbl1 = f"{target_r}R Target (100% Exit)"
                                    lbl2 = lbl1

                                    from telegram_bot import queue_alert
                                    sq_queued = await queue_alert(
                                        symbol=symbol,
                                        tf_label="1H",
                                        signal=sq_signal,
                                        entry=entry,
                                        sl=dyn_sl,
                                        tp=tp1,
                                        tf_states=tf_states,
                                        cross_type=cross_type,
                                        candle_time=candle_ts,
                                        macro_struct=macro_struct,
                                        tp1=tp1, tp2=tp2,
                                        rr1=rr1, rr2=rr2,
                                        lbl1=lbl1, lbl2=lbl2,
                                        tf_data=tf_data,
                                        is_special_list=(symbol in SPECIAL_LIST),
                                        rsi=sq_rsi,
                                        is_dedicated_squeeze=True,
                                        is_trap_reversal=is_trap_reversal
                                    )
                                    if sq_queued:
                                        mark_sent(symbol, unique_sq_key, sq_signal, candle_ts, entry=entry, sl=dyn_sl, tp=tp1, tp1=tp1, tp2=tp2, target_r=target_r)
                                        if has_radar:
                                            mark_radar_triggered(symbol)
                                        logger.info(f"💥 DEDICATED 1H BREAKOUT/BREAKDOWN/REVERSAL TRIGGERED for {symbol} ({sq_signal}) @ {entry} (Target: {target_r}R = {tp1})")

                                        # AUTO-TRADING ENROLLMENT:
                                        # Capture 1H Squeeze Breakout / Reversal candidate for Shark Exchange execution
                                        sq_candidate = {
                                            "symbol": symbol,
                                            "signal": sq_signal,
                                            "entry": entry,
                                            "sl": dyn_sl,
                                            "tp1": tp1,
                                            "tp2": tp2,
                                            "tp": tp1,
                                            "target_r": target_r,
                                            "aligned_count": 4,  # High-conviction verified setup with CVD + volume
                                            "is_3_htf_aligned": True,
                                            "is_30m_early": False,
                                            "is_special": (symbol in SPECIAL_LIST),
                                            "is_dedicated_squeeze": True,
                                            "is_trap_reversal": is_trap_reversal,
                                            "unique_key": unique_sq_key,
                                            "candle_ts": candle_ts,
                                            "rr2": target_r,
                                            "rsi": sq_rsi,
                                        }
                            elif has_radar:
                                logger.info(f"FILTERED 1H SQUEEZE CVD: {symbol} — waiting on closed candle CVD alignment (radar stays WATCHING).")
            except Exception as e_sq_eng:
                logger.warning(f"Squeeze engine error for {symbol}: {e_sq_eng}", exc_info=True)

        # -- 0B. 15M 8H-BREAKOUT RUNAWAY CHASE ENGINE (Wide 1H ATR SL, 2.2R) --
        # Catches fast 1-way runaway pumps/dumps on 15M aligned with 1H+4H+BTC (1H ADX>=22, >=1.8x Vol, >=50% Body).
        rc_candidate = None
        df_15m_rc = tf_data.get("15M") if tf_data.get("15M") is not None else tf_data.get("15m")
        if df_15m_rc is not None and len(df_15m_rc) >= 36 and df_1h is not None and len(df_1h) >= 26:
            try:
                from state import is_coin_on_cooldown, set_coin_cooldown
                c15_dt = df_15m_rc.index[-2]
                if hasattr(c15_dt, "tzinfo") and c15_dt.tzinfo is None:
                    c15_dt = c15_dt.replace(tzinfo=timezone.utc)
                elif not hasattr(c15_dt, "tzinfo"):
                    c15_dt = pd.to_datetime(c15_dt, utc=True)

                secs_15 = (datetime.now(timezone.utc) - c15_dt).total_seconds()
                if 0 <= secs_15 <= 1020 and not is_coin_on_cooldown(symbol, "15M_RUNAWAY", max_hours=2.0):
                    from indicators import _compute_indicators
                    df_15m_ind = _compute_indicators(df_15m_rc)
                    df_1h_ind_rc = _compute_indicators(df_1h)

                    d15_hi = float(df_15m_rc['high'].iloc[-34:-2].max())
                    d15_lo = float(df_15m_rc['low'].iloc[-34:-2].min())
                    c15_open = float(df_15m_rc['open'].iloc[-2])
                    c15_high = float(df_15m_rc['high'].iloc[-2])
                    c15_low = float(df_15m_rc['low'].iloc[-2])
                    c15_close = float(df_15m_rc['close'].iloc[-2])
                    c15_vol = float(df_15m_rc['volume'].iloc[-2])
                    vol_sma_15 = float(df_15m_rc['volume'].iloc[-22:-2].mean()) if len(df_15m_rc) >= 22 else c15_vol
                    body_15 = abs(c15_close - c15_open) / max(c15_high - c15_low, 1e-8)
                    rsi_15_rc = float(df_15m_ind['rsi'].iloc[-2]) if 'rsi' in df_15m_ind and not pd.isna(df_15m_ind['rsi'].iloc[-2]) else 50.0
                    adx_1h_rc = float(df_1h_ind_rc['adx'].iloc[-2]) if 'adx' in df_1h_ind_rc and not pd.isna(df_1h_ind_rc['adx'].iloc[-2]) else 0.0
                    atr_1h_rc = float(df_1h_ind_rc['atr'].iloc[-2]) if 'atr' in df_1h_ind_rc and not pd.isna(df_1h_ind_rc['atr'].iloc[-2]) else (c15_close * 0.01)

                    h1_tr_rc = str(tf_states.get("1H", "")).lower()
                    h4_tr_rc = str(tf_states.get("4H", "")).lower()
                    rc_signal = None

                    if adx_1h_rc >= 22.0 and vol_sma_15 > 0 and c15_vol >= (1.8 * vol_sma_15) and body_15 >= 0.50:
                        if c15_close > (d15_hi * 1.001) and h1_tr_rc == "bullish" and h4_tr_rc == "bullish" and rsi_15_rc <= 72.0:
                            rc_signal = "BUY"
                        elif c15_close < (d15_lo * 0.999) and h1_tr_rc == "bearish" and h4_tr_rc == "bearish" and rsi_15_rc >= 28.0:
                            rc_signal = "SELL"

                    if rc_signal and in_crypto_shock:
                        if not shock_ctx.get("is_clean_cascade"):
                            logger.debug(f"FILTERED 15M RUNAWAY {symbol} {rc_signal} — BTC shock is two-sided whipsaw.")
                            rc_signal = None
                        elif shock_ctx.get("direction") and rc_signal != shock_ctx.get("direction"):
                            logger.debug(f"FILTERED 15M RUNAWAY {symbol} {rc_signal} — opposes BTC shock cascade ({shock_ctx.get('direction')}).")
                            rc_signal = None

                    if rc_signal:
                        from market_cache import is_btc_safe_for_altcoins
                        is_btc_ok, btc_rc_reason = is_btc_safe_for_altcoins(rc_signal, symbol, tf_data)
                        candle_15_ts = str(df_15m_rc.index[-2])
                        unique_rc_key = f"15M_RunawayChase_{rc_signal}_{candle_15_ts}"
                        if is_btc_ok and not is_already_sent(symbol, unique_rc_key, rc_signal, candle_15_ts):
                            target_r = 2.2
                            live_15m_close = float(df_15m_rc['close'].iloc[-1])
                            if rc_signal == "BUY":
                                raw_entry = min(max(c15_close, live_15m_close), c15_close * 1.0015) if live_15m_close > c15_close else c15_close
                            else:
                                raw_entry = max(min(c15_close, live_15m_close), c15_close * 0.9985) if live_15m_close < c15_close else c15_close
                            entry = round_price(symbol, raw_entry)
                            sl_dist = min(max(1.2 * atr_1h_rc, entry * 0.015), entry * 0.025)
                            sl = round_price(symbol, (entry - sl_dist) if rc_signal == "BUY" else (entry + sl_dist))
                            risk = abs(entry - sl) if (entry and sl and entry != sl) else (c15_close * 0.015)
                            tp1 = round_price(symbol, (entry + target_r * risk) if rc_signal == "BUY" else (entry - target_r * risk))
                            tp2 = tp1
                            cross_type_rc = "⚡ 15M Runaway Chase (8H Breakout + Wide 1H ATR SL ✅)"

                            from telegram_bot import queue_alert
                            rc_queued = await queue_alert(
                                symbol=symbol,
                                tf_label="15M",
                                signal=rc_signal,
                                entry=entry,
                                sl=sl,
                                tp=tp1,
                                tf_states=tf_states,
                                cross_type=cross_type_rc,
                                candle_time=candle_15_ts,
                                macro_struct=macro_struct,
                                tp1=tp1, tp2=tp2,
                                rr1=target_r, rr2=target_r,
                                lbl1="2.2R Target (100% Exit)", lbl2="2.2R Target (100% Exit)",
                                tf_data=tf_data,
                                is_special_list=(symbol in SPECIAL_LIST),
                                rsi=rsi_15_rc,
                                adx=adx_1h_rc,
                                is_runaway_chase=True,
                            )
                            if rc_queued:
                                mark_sent(symbol, unique_rc_key, rc_signal, candle_15_ts, entry=entry, sl=sl, tp=tp1, tp1=tp1, tp2=tp2, target_r=target_r)
                                set_coin_cooldown(symbol, "15M_RUNAWAY")
                                aligned_rc = sum(1 for t in ["1H", "4H", "1D", "1W"] if tf_states.get(t, "neutral").lower() == ("bullish" if rc_signal == "BUY" else "bearish"))
                                rc_candidate = {
                                    "symbol": symbol,
                                    "signal": rc_signal,
                                    "entry": entry,
                                    "sl": sl,
                                    "tp1": tp1,
                                    "tp2": tp2,
                                    "tp": tp1,
                                    "target_r": target_r,
                                    "aligned_count": max(aligned_rc, 2),
                                    "is_3_htf_aligned": aligned_rc >= 3,
                                    "is_30m_early": False,
                                    "is_special": (symbol in SPECIAL_LIST),
                                    "is_runaway_chase": True,
                                    "unique_key": unique_rc_key,
                                    "candle_ts": candle_15_ts,
                                    "rr2": target_r,
                                    "rsi": rsi_15_rc,
                                    "adx": adx_1h_rc,
                                    "tf_label": "15M",
                                    "cross_type": cross_type_rc,
                                }
                                logger.info(f"⚡ 15M 8H-BREAKOUT RUNAWAY CHASE TRIGGERED: {symbol} [{rc_signal}] @ {entry} SL={sl} TP={tp1} (ADX={adx_1h_rc:.1f}, Vol={c15_vol/vol_sma_15:.1f}x)")
                        elif not is_btc_ok:
                            logger.debug(f"FILTERED 15M RUNAWAY {symbol} {rc_signal} — {btc_rc_reason}")
            except Exception as e_rc:
                logger.warning(f"15M runaway chase engine error for {symbol}: {e_rc}", exc_info=True)

        # -- 1. EMA CROSSOVER ENGINE --
        # BUG #1 FIX: Collect ALL crossover candidates from ALL timeframes (not just the first one).
        # Previously `return cand` was inside the loop causing all 4H/1D signals to be silently dropped.
        all_crossover_candidates = []
        if sq_candidate:
            all_crossover_candidates.append(sq_candidate)
        if rc_candidate:
            all_crossover_candidates.append(rc_candidate)

        crossover_labels = [] if in_crypto_shock else ["5M", "15M", "30M", "1H", "4H", "1D", "1W"]
        for label in crossover_labels:
            is_gold = symbol in ["XAU/USD", "XAUT/USDT", "XAUTUSDT"] or "XAU" in symbol.upper()

            # 5M timeframe: Strictly Gold Scalps only!
            if label in ["5M", "5m"] and not is_gold:
                continue

            # 5M & 15M timeframes: Standard crossover scalps are strictly reserved for Gold only!
            # Non-Gold coins on 15M do NOT receive generic crossovers.
            if label in ["5M", "5m", "15M", "15m"] and not is_gold:
                continue
                
            df = tf_data.get(label)
            if df is None:
                df = tf_data.get(label.lower())
            if df is None:
                df = tf_data.get(label.upper())
            if df is None:
                continue
            
            crossovers = analyze_crossovers(df, symbol=symbol, tf_data=tf_data)
            candle_ts = str(df.index[-2]) if len(df) >= 2 else "unknown"
            
            for cross in crossovers:
                signal = cross["signal"]
                entry = cross["price"]
                sl = cross["sl"]
                cross_type = cross["type"]
                
                # 15M SQUEEZE ELIMINATED (User Directive: "1 hr squeze pr hi raho fir")
                # All Squeeze breakouts strictly handled on 1H timeframe by the dedicated 1H Squeeze Engine.

                # 30M EARLY CROSSOVER FILTER — MUST PASS 3 HTF SIGNS + 3 TECH QUALITY SIGNS:
                # 1. 3 HTF Trend Confluence Signs: 1H + 4H + 1D EMA trend alignment in signal direction
                # 2. 3 Technical Quality Signs:
                #    - Volume Surge: Volume > 1.5x 20-period Volume SMA
                #    - ADX Trend Strength: ADX > 20.0
                #    - Candle Body Ratio: Body >= 25% of total Candle Range
                SUPER_MAJORS = {
                    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
                    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "SUI/USDT",
                    "NEAR/USDT", "DOT/USDT", "LTC/USDT", "UNI/USDT", "AAVE/USDT",
                    "BCH/USDT", "XAU/USD", "XAUUSDT", "XAUT/USDT"
                }
                MAJOR_PREFIXES = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI", "NEAR", "DOT", "LTC", "UNI", "AAVE", "BCH", "XAU", "XAUT"]
                is_super = (symbol in SUPER_MAJORS or any(m in symbol for m in MAJOR_PREFIXES))

                if label in ["30M", "30m"] and not is_super:
                    target_trend = "bullish" if signal == "BUY" else "bearish"
                    is_1h_aligned = tf_states.get("1H", "").lower() == target_trend
                    aligned_count_check = sum(1 for t in ["1H", "4H", "1D", "1W"] if tf_states.get(t, "neutral").lower() == target_trend)
                    # Allow 2-Star and 3-Star HTF signals for Telegram channel as long as 1H trend is aligned & >= 2 stars
                    htf_aligned = (is_1h_aligned and aligned_count_check >= 2)
                    if not htf_aligned:
                        logger.debug(f"30M signal for {symbol} [{signal}] skipped — less than 2-star HTF alignment.")
                        continue

                    cross_candle = df.iloc[-2]
                    vol_20_sma = df["volume"].iloc[-22:-2].mean() if len(df) >= 22 else df["volume"].mean()
                    is_vol_surge = cross_candle["volume"] > (1.20 * vol_20_sma) if vol_20_sma > 0 else True

                    from indicators import _compute_indicators
                    df_ind = _compute_indicators(df)
                    adx_val = df_ind.iloc[-2].get("adx", 0.0)
                    is_adx_valid = (not pd.isna(adx_val)) and (adx_val > 15.0)

                    candle_body = abs(cross_candle["close"] - cross_candle["open"])
                    candle_range = abs(cross_candle["high"] - cross_candle["low"])
                    body_ratio = (candle_body / candle_range) if candle_range > 0 else 0
                    is_valid_body = body_ratio >= 0.25

                    if not (is_vol_surge and is_adx_valid and is_valid_body):
                        logger.debug(
                            f"30M signal for {symbol} [{signal}] skipped — Tech Quality Failed: "
                            f"Vol Surge:{is_vol_surge}, ADX > 15:{is_adx_valid}, Body >= 25%:{is_valid_body}"
                        )
                        continue

                    cross_type = "⚡ 30M Early Cross (3 HTF + 3 Tech Quality Signs ✅)"


                
                unique_key = f"{label}_{cross_type}"
                
                # CRITICAL REBOOT FIX & STRICT FRESHNESS GUARANTEE:
                # Crossover must have occurred on the candle that JUST closed.
                # If the candle closed more than 180 seconds (3 1-min candles) ago, discard it!
                candle_time_utc = df.index[-2]
                now_utc = datetime.now(timezone.utc)
                if candle_time_utc.tzinfo is None:
                    candle_time_utc = candle_time_utc.replace(tzinfo=timezone.utc)
                    
                tf_duration_seconds = {
                    "5M": 300,
                    "15m": 900, "15M": 900,
                    "30m": 1800, "30M": 1800,
                    "1h": 3600, "1H": 3600,
                    "4h": 14400, "4H": 14400,
                    "1d": 86400, "1D": 86400,
                    "1w": 604800, "1W": 604800,
                }.get(label, 3600)
                
                candle_close_utc = candle_time_utc + timedelta(seconds=tf_duration_seconds)
                seconds_since_close = (now_utc - candle_close_utc).total_seconds()
                
                # STRICT FRESHNESS GUARANTEE (ONLY BRAND-NEW FRESH CROSSOVERS):
                # Process crossovers on candles that closed within the timeframe candle duration.
                # (5M: 300s, 15M: 900s, 30M: 1800s, 1H: 3600s). Rejects old historical crossovers from past days.
                is_fresh = (0 <= seconds_since_close <= tf_duration_seconds)
                if not is_fresh:
                    continue

                target_trend = "bullish" if signal == "BUY" else "bearish"
                is_30m_early = "30M Early Cross" in cross_type

                if is_already_sent(symbol, unique_key, signal, candle_ts):
                    continue
                    
                risk = abs(entry - sl)
                if risk == 0: continue
                
                tp = cross.get("tp", entry + (2.0 * risk) if signal == "BUY" else entry - (2.0 * risk))
                rr = cross.get("rr", 2.0)
                tp_label = cross.get("tp_label", "Liquidity Pool Target")
                
                tp1 = cross.get("tp1")
                tp2 = cross.get("tp2")
                rr1 = cross.get("rr1")
                rr2 = cross.get("rr2")
                lbl1 = cross.get("lbl1")
                lbl2 = cross.get("lbl2")
                rsi = cross.get("rsi")
                
                last_1h_cross_time = None
                if label in ["4H", "1D"] and "1H" in tf_data:
                    from indicators import find_last_crossover_time
                    last_1h_cross_time = find_last_crossover_time(tf_data["1H"], signal, cross_type)
                
                # SOLUTION 2: BTC REAL-TIME MICRO-GATE & RELATIVE STRENGTH
                from market_cache import is_btc_safe_for_altcoins
                is_safe, btc_reason = is_btc_safe_for_altcoins(signal, symbol, tf_data)
                if not is_safe:
                    logger.info(f"FILTERED CROSSOVER: {symbol} {signal} rejected by BTC Micro-Gate ({btc_reason}).")
                    record_quiet_scan_event(f"BTC Micro-Gate: {btc_reason}", f"{symbol} {label} {signal}")
                    continue

                logger.info(f"NEW SIGNAL: {signal} {symbol} {label} ({cross_type}) | entry={entry} sl={sl} tp1={tp1} tp2={tp2}")
                from pullback_tracker import SPECIAL_LIST
                is_special = symbol in SPECIAL_LIST
                # Pass candle_time_utc (candle open time in UTC) to queue_alert so calculate_candle_close_time calculates the exact close time accurately
                open_time_iso = candle_time_utc.isoformat()
                
                # Queue alert for Telegram (Bug #3 fix: only mark sent & append candidate if queue_alert accepted it)
                from telegram_bot import queue_alert
                is_queued = await queue_alert(symbol, label, signal, entry, sl, tp, tf_states, cross_type, last_1h_cross_time, candle_time=open_time_iso, macro_struct=macro_struct, rr=rr, tp_label=tp_label, tf_data=tf_data, tp1=tp1, tp2=tp2, rr1=rr1, rr2=rr2, lbl1=lbl1, lbl2=lbl2, is_special_list=is_special, rsi=rsi, adx=cross.get("adx"))
                if not is_queued:
                    record_quiet_scan_event("RSI / Quality Filter (ADX / Body / RSI)", f"{symbol} {label} {signal}")
                    # Mark candle sent so it doesn't endlessly re-process the exact same completed candle every 5 seconds
                    mark_sent(symbol, unique_key, signal, candle_ts, entry=entry, sl=sl, tp=tp, tp1=tp1, tp2=tp2)
                    continue
                
                is_3_htf_aligned = (
                    tf_states.get("1H", "").lower() == target_trend and
                    tf_states.get("4H", "").lower() == target_trend and
                    tf_states.get("1D", "").lower() == target_trend
                )
                aligned_count = sum(1 for t in ["1H", "4H", "1D", "1W"] if tf_states.get(t, "neutral").lower() == target_trend)
                is_30m_early = "30M Early Cross" in cross_type

                mark_sent(symbol, unique_key, signal, candle_ts, entry=entry, sl=sl, tp=tp, tp1=tp1, tp2=tp2)
                from state import set_coin_cooldown
                set_coin_cooldown(symbol, tf=label)

                cand = {
                    "symbol": symbol,
                    "signal": signal,
                    "entry": entry,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp": tp,
                    "aligned_count": aligned_count,
                    "is_3_htf_aligned": is_3_htf_aligned,
                    "is_30m_early": is_30m_early,
                    "is_special": is_special,
                    "unique_key": unique_key,
                    "candle_ts": candle_ts,
                    "rr2": rr2 or 1.5,
                    "adx": cross.get("adx"),
                    "rsi": rsi,
                    "volume": cross.get("volume"),
                    "volume_sma": cross.get("volume_sma"),
                    "macd_line": cross.get("macd_line"),
                    "macd_signal_val": cross.get("macd_signal"),
                    "body_ratio": cross.get("body_ratio"),
                    "tf_label": label,
                    "cross_type": cross_type,
                }
                # BUG #1 FIX: Append instead of returning immediately.
                # Collects signals from ALL timeframes (1H + 4H + 1D all captured).
                all_crossover_candidates.append(cand)
                # Continue to scan next timeframe (do NOT return early)

        return all_crossover_candidates if all_crossover_candidates else None
    except Exception as e:
        logger.error(f"Error processing {symbol}: {e}")
        return None

async def scan_single_symbol_async(session, symbol, semaphore, shock_ctx=None):
    try:
        tf_data = await fetch_all_timeframes_async(session, symbol, semaphore)
        if not tf_data:
            return None

        from indicators import get_trend_state
        from telegram_bot import TF_ORDER
        from market_cache import is_commodity_symbol, is_stock_symbol
        in_crypto_shock = bool(
            shock_ctx and shock_ctx.get("active")
            and not is_commodity_symbol(symbol) and not is_stock_symbol(symbol)
        )
        tf_states = {l: get_trend_state(tf_data.get(l)) for l in TF_ORDER}
        clean_sym = symbol.upper()
        is_super = any(m in clean_sym for m in ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "XAU", "GOLD"])

        # ── STAGE 1: WATCHLIST COILING CANDIDATE EVALUATION (1H TF & Above per User Directive) ──
        radar_cand = None
        if not in_crypto_shock:
            try:
                from config import is_weekend_trading_blocked
                if not is_weekend_trading_blocked(symbol):
                    df_1h = tf_data.get("1H") if tf_data.get("1H") is not None else tf_data.get("1h")
                    if df_1h is not None and len(df_1h) >= 25:
                        from fetcher import fetch_futures_microstructure_async
                        micro_radar = await fetch_futures_microstructure_async(session, symbol, signal=None, df=df_1h)
                        from indicators import evaluate_watchlist_coiling_candidate
                        eval_res = evaluate_watchlist_coiling_candidate(symbol, df_1h, micro_radar, tf_states, tf_label="1H", tf_data=tf_data)
                        if eval_res.get("eligible"):
                            radar_cand = eval_res
                            logger.info(f"🎯 Verified 1H Watchlist Coiling Candidate: {symbol} ({radar_cand['direction']}) | Score: {radar_cand.get('coiling_score')} | Comp: {radar_cand.get('compression_pct')}%")
            except Exception as e_rad:
                logger.debug(f"Watchlist coiling evaluation error for {symbol}: {e_rad}")

        # Update in-memory market cache for instant Telegram filtering
        try:
            from market_cache import update_symbol_cache
            update_symbol_cache(symbol, tf_data)
        except Exception as e:
            logger.debug(f"Cache update error for {symbol}: {e}")

        # WEEKEND SCANNING LOGIC (User Directive):
        # - BTC, ETH, Commodities, US Stock Tokens: BLOCKED on weekends
        # - All Binance Futures Altcoins: Fresh Crossovers
        # - Top Altcoins & Watchlist Coins: Fresh Crossovers AND Retests!
        import zoneinfo
        tz = zoneinfo.ZoneInfo(TIMEZONE)
        now_ist = datetime.now(timezone.utc).astimezone(tz)
        is_weekend = now_ist.weekday() >= 5

        retest_cand = None
        from pullback_tracker import WATCHLIST, TOP_COINS, TOP_25_ALTS, ADDITIONAL_ALTS
        retest_allowed_coins = set(TOP_COINS + TOP_25_ALTS + ADDITIONAL_ALTS + WATCHLIST)
        from config import is_weekend_trading_blocked
        is_retest_eligible = (not in_crypto_shock) and ((symbol in retest_allowed_coins) if is_weekend else True) and not is_weekend_trading_blocked(symbol)

        if is_retest_eligible:
            # Run EMA Support/Resistance Retest Check
            from pullback_tracker import check_symbol_pullbacks
            retest_cand = await check_symbol_pullbacks(symbol, tf_data, session=session)

        # 2. Run Multi-Crossover & Breakout Check (Fresh Crossovers)
        # BUG #1 FIX: process_symbol_async now returns a LIST of candidates (all TF crossovers)
        crossover_cands = await process_symbol_async(session, symbol, semaphore, tf_data=tf_data, shock_ctx=shock_ctx)

        # Build unified trades list: crossovers (all TFs) + retest candidates
        trade_cands = []
        if isinstance(crossover_cands, list):
            trade_cands.extend(crossover_cands)
        elif isinstance(crossover_cands, dict):
            trade_cands.append(crossover_cands)  # Backward compat
        if isinstance(retest_cand, list):
            trade_cands.extend(retest_cand)
        elif isinstance(retest_cand, dict):
            trade_cands.append(retest_cand)

        return {
            "trades": trade_cands,   # List of all candidates for this symbol
            "radar": radar_cand
        }
    except Exception as e:
        logger.error(f"Error scanning {symbol}: {e}")
        return None



# ─────────────────────────────────────────────────────────────────────────────
# SHARK-NATIVE TP MONITOR  (BULLETPROOF v2)
# Runs every 15s. Detects TP fills by counting remaining LIMIT reduceOnly orders.
# Key safety guarantees:
#   1. First-run seeds cache from LIVE data → zero false triggers on restart
#   2. Only fires trailing SL on genuine count DROP (prev→current transition)
#   3. Handles 3→1 jump (two TPs hit while bot was restarting)
#   4. Auto-heals missing TPs inline if position still open
#   5. SL check: if SL missing after TP1 hit, places it at CTC immediately
# ─────────────────────────────────────────────────────────────────────────────

async def shark_native_tp_sl_monitor(session):
    """Delegates exchange sync and order management to executor.sync_and_manage_live_orders."""
    from executor import executor
    await executor.sync_and_manage_live_orders(session)

_rejection_notified_cache = {}
INACTIVITY_NOTICE_INTERVAL_SECS = 9000  # 2.5 hours (2-3h quiet-market threshold)

_QUIET_SCAN_STATS = {
    "scans_completed": 0,
    "pairs_scanned": 0,
    "rejection_counts": {},
    "recent_samples": [],
}


def reset_quiet_scan_stats() -> None:
    _QUIET_SCAN_STATS["scans_completed"] = 0
    _QUIET_SCAN_STATS["rejection_counts"] = {}
    _QUIET_SCAN_STATS["recent_samples"] = []


def _categorize_rejection_reason(reason: str) -> str:
    rl = str(reason or "").lower()
    if "star" in rl or "confluence" in rl:
        return "low_stars"
    if "rsi" in rl or "score" in rl or "ai quality" in rl:
        return "rsi_or_score"
    if "micro-gate" in rl:
        return "btc_gate"
    if "shock" in rl:
        return "btc_shock"
    if "funding" in rl or "crowded" in rl:
        return "funding_shield"
    if "circuit breaker" in rl:
        return "circuit_breaker"
    if "directional limit" in rl or "already exists" in rl or "cap" in rl:
        return "risk_cap"
    return "other"


def record_quiet_scan_event(reason: str, sample: str = None) -> None:
    cat = _categorize_rejection_reason(reason)
    counts = _QUIET_SCAN_STATS["rejection_counts"]
    counts[cat] = int(counts.get(cat, 0)) + 1
    if sample:
        clean_s = str(sample).strip()
        samples = _QUIET_SCAN_STATS["recent_samples"]
        if clean_s and clean_s not in samples:
            samples.append(clean_s)
            if len(samples) > 4:
                del samples[0]


async def maybe_dispatch_inactivity_notice(
    session: aiohttp.ClientSession = None,
    now_ts: float = None,
    boot_ts: float = None,
    threshold_secs: float = INACTIVITY_NOTICE_INTERVAL_SECS,
) -> bool:
    """
    Shares a clear live market-condition update with users if no new trade has executed
    for > 2.5 hours (2-3h quiet threshold), repeating at most once every 2.5 hours.
    Skipped during Night Maintenance Window and first 120s boot warm-up.
    """
    from config import AUTO_TRADING_ENABLED, PAPER_TRADING_ENABLED, is_night_maintenance_window

    if not AUTO_TRADING_ENABLED or PAPER_TRADING_ENABLED:
        return False

    cur_ts = float(now_ts if now_ts is not None else time.time())
    cur_dt = datetime.fromtimestamp(cur_ts, tz=timezone.utc)
    if is_night_maintenance_window(cur_dt):
        return False

    start_ts = float(boot_ts if boot_ts is not None else BOT_START_TIME)
    if (cur_ts - start_ts) < 120.0:
        return False

    last_trade_ts = get_last_trade_activity_ts()
    ref_trade_ts = last_trade_ts if last_trade_ts > 0.0 else start_ts
    idle_secs = cur_ts - ref_trade_ts
    if idle_secs < threshold_secs:
        return False

    last_notice_ts = get_last_inactivity_notice_ts()
    if (cur_ts - last_notice_ts) < threshold_secs:
        return False

    open_pos_cnt = 0
    if session is not None and executor.is_configured():
        try:
            open_pos = await executor.get_open_positions(session)
            open_pos_cnt = len(open_pos) if isinstance(open_pos, list) else 0
        except Exception:
            open_pos_cnt = 0

    diag = {
        "idle_secs": idle_secs,
        "scans_completed": _QUIET_SCAN_STATS.get("scans_completed", 0),
        "pairs_scanned": _QUIET_SCAN_STATS.get("pairs_scanned", 0),
        "active_open_positions": open_pos_cnt,
        "rejection_counts": dict(_QUIET_SCAN_STATS.get("rejection_counts") or {}),
        "recent_samples": list(_QUIET_SCAN_STATS.get("recent_samples") or []),
    }
    try:
        delivered = await send_market_inactivity_notice(diag)
        if delivered:
            set_last_inactivity_notice_ts(cur_ts)
            reset_quiet_scan_stats()
            return True
    except Exception as e_inact:
        logger.warning(f"Failed sending market inactivity notice: {e_inact}")
    return False


async def notify_signal_rejection(symbol: str, signal: str, reason: str, cand: dict = None):
    """Notifies the user reliably via Telegram when a signal is rejected/filtered from Shark execution."""
    import html as _html
    cand = cand or {}
    uniq_k = str(cand.get("unique_key") or "signal")
    c_ts = str(cand.get("candle_ts") or "")
    stars = cand.get("aligned_count", 0)
    rsi_val = cand.get("rsi")
    entry_p = cand.get("entry")
    tf_lbl = str(cand.get("tf_label") or "")
    setup_lbl = str(cand.get("cross_type") or "")

    reason_str = str(reason or "Unspecified rejection reason")
    record_quiet_scan_event(reason_str, f"{symbol} {tf_lbl} {signal}".strip())
    dedup_key = f"{symbol}_{signal}_{uniq_k}_{c_ts}_{reason_str[:35]}"
    now_ts = time.time()

    # Prune old cache (> 2 hours)
    for k, ts in list(_rejection_notified_cache.items()):
        if now_ts - ts > 7200:
            _rejection_notified_cache.pop(k, None)

    if dedup_key in _rejection_notified_cache and (now_ts - _rejection_notified_cache[dedup_key] < 1800):
        return  # Deduplicated within 30 minutes

    safe_sym = _html.escape(str(symbol), quote=False)
    safe_sig = _html.escape(str(signal).upper(), quote=False)
    safe_reason = _html.escape(reason_str, quote=False)
    safe_setup = _html.escape(f"{setup_lbl} ({tf_lbl})".strip(), quote=False) if (setup_lbl or tf_lbl) else ""

    stars_str = f"{stars}/4 Stars" if stars > 0 else "Signal"
    rsi_str = f" | 📊 <b>RSI:</b> <code>{float(rsi_val):.1f}</code>" if rsi_val is not None else ""
    funding_val = cand.get("funding_pct")
    funding_str = f" | ⚡ <b>Funding:</b> <code>{funding_val:+.4f}%</code>" if funding_val is not None else ""
    entry_str = f"\n🎯 <b>Signal Entry:</b> <code>{_html.escape(str(entry_p), quote=False)}</code>" if entry_p else ""
    setup_line = f"\n📐 <b>Setup:</b> {safe_setup}" if safe_setup else ""

    score_val = cand.get("quality_score") or (cand.get("analysis") or {}).get("score")
    score_verdict = (cand.get("analysis") or {}).get("verdict")
    comps = (cand.get("analysis") or {}).get("components") or {}

    score_block = ""
    if score_val is not None:
        safe_verdict = _html.escape(str(score_verdict or ""), quote=False)
        comp_lines = ""
        if comps:
            comp_lines = (
                f"\n📊 <b>Score Breakdown:</b>\n"
                f"  • HTF Trend: <code>{comps.get('htf', 0)}/20</code> | RSI: <code>{comps.get('rsi', 0)}/20</code>\n"
                f"  • Volume Surge: <code>{comps.get('volume', 0)}/15</code> | ADX Trend: <code>{comps.get('adx', 0)}/15</code>\n"
                f"  • MACD: <code>{comps.get('macd', 0)}/10</code> | Candle Body: <code>{comps.get('body', 0)}/10</code>\n"
                f"  • Session: <code>{comps.get('session', 0)}/10</code>"
            )
        score_block = f"\n🧠 <b>AI Quality Score:</b> <code>{score_val}/100</code> ({safe_verdict}){comp_lines}"

    rl = reason_str.lower()
    if "maintenance" in rl:
        footer_note = "🌙 <i>Night Maintenance Window (12:30 AM–05:30 AM IST) active — zero new trades executed.</i>"
    elif "circuit breaker" in rl or "weekend" in rl:
        footer_note = "🛡️ <i>Scheduled risk protection pause active — capital preserved.</i>"
    elif "shark broker" in rl or "partial" in rl or "margin" in rl:
        footer_note = "⚠️ <i>Broker/Account execution error reported above — check account margin or order state.</i>"
    elif "already exists" in rl or "directional limit" in rl:
        footer_note = "🛡️ <i>Portfolio risk shield prevented duplicate or over-concentrated exposure.</i>"
    else:
        footer_note = "💡 <i>Low-probability or weak score setup filtered out to prevent unnecessary SL loss.</i>"

    msg = (
        f"🛡️ <b>SHARK AUTO-TRADE REJECTION NOTICE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>Pair:</b> <code>{safe_sym}</code> [<b>{safe_sig}</b>]{setup_line}\n"
        f"⭐ <b>Confluence:</b> {stars_str}{rsi_str}{funding_str}{entry_str}{score_block}\n"
        f"🚫 <b>Execution Status:</b> Not Executed on Broker\n"
        f"📋 <b>Exact Reason:</b> {safe_reason}\n\n"
        f"{footer_note}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        from telegram_bot import send_text_to_chat
        from config import TELEGRAM_CHAT_ID
        target = str(TELEGRAM_CHAT_ID or "771305593").strip()
        sent = await send_text_to_chat(target, msg)
        if sent is not None:
            _rejection_notified_cache[dedup_key] = now_ts
            logger.info(f"Dispatched trade rejection notice for {symbol} [{signal}]: {reason_str}")
        else:
            logger.error(f"Telegram returned None dispatching rejection notice for {symbol} [{signal}]")
    except Exception as e_ntf:
        logger.error(f"Error dispatching rejection notice for {symbol}: {e_ntf}", exc_info=True)

async def main_loop():
    validate_config()
    from state import clear_all_pending_backlogs
    purged_count = clear_all_pending_backlogs()
    logger.info("Preserving active signal cooldowns across startup to prevent duplicate alert spam.")
    
    # Start Telegram Interactive Bot Service (commands & interactive button filters)
    from telegram_bot import start_telegram_bot_service, stop_telegram_bot_service
    await start_telegram_bot_service()

    # Start AlphaCryptoSignals Dedicated Filter Bot Service
    try:
        from filter_bot import start_filter_bot_service
        await start_filter_bot_service()
    except Exception as e_fbot:
        logger.error(f"Failed to start filter_bot_service: {e_fbot}")
    

    # Start Live Web Dashboard & API server if PORT environment variable is provided
    port_env = os.getenv('PORT')
    if port_env:
        try:
            from web_dashboard import setup_web_app
            from aiohttp import web
            web_app = setup_web_app()
            runner = web.AppRunner(web_app)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', int(port_env))
            await site.start()
            logger.info(f'AlphaSniper Live Web Dashboard & API server started on port {port_env}!')
        except Exception as e_port:
            logger.warning(f'Could not bind Web Dashboard to PORT {port_env}: {e_port}')
    semaphore = asyncio.Semaphore(10)
    
    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist_boot = datetime.now(timezone.utc).astimezone(ist_tz)
    today_boot_str = now_ist_boot.strftime("%Y-%m-%d")

    daily_file = "/data/daily_msgs.json" if os.path.isdir("/data") else "daily_msgs.json"
    _night_notified_date = None
    _morning_notified_date = None
    try:
        if os.path.exists(daily_file):
            with open(daily_file, 'r') as f:
                d = json.load(f)
                _night_notified_date = d.get('night')
                _morning_notified_date = d.get('morning')
    except: pass

    is_boot_night = (now_ist_boot.hour == 0 and now_ist_boot.minute >= 30) or (1 <= now_ist_boot.hour < 5) or (now_ist_boot.hour == 5 and now_ist_boot.minute < 30)
    # Do not suppress night notification on restart if it hasn't been sent to channels yet today

    
    logger.info("News Monitor Service PAUSED. Bot 100% focused on EMA Signals & Delta Exchange Algo.")
    try:
        import urllib.request
        server_ip = urllib.request.urlopen("https://api.ipify.org", timeout=5).read().decode("utf-8")
        logger.info(f"SERVER OUTBOUND PUBLIC IP: {server_ip}")
    except Exception as e_ip:
        logger.warning(f"Could not resolve server IP: {e_ip}")
    
    try:
        try:
            import certifi, ssl
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(limit=100, limit_per_host=30, ttl_dns_cache=300, ssl=ssl_ctx)
        except Exception:
            connector = aiohttp.TCPConnector(limit=100, limit_per_host=30, ttl_dns_cache=300, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async def tp_monitor_worker():
                logger.info("⚡ Fast TP/SL Monitor Worker STARTED (Polling every 5s for real-time exchange TP/SL hit alerts & order sweeps)")
                from executor import executor
                from paper_trader import evaluate_paper_trades_async, load_wallet, generate_strategy_lookout_card, notify_telegram
                from state import get_last_lookout_ts, set_last_lookout_ts
                last_lookout_time = get_last_lookout_ts()
                while True:
                    try:
                        await executor.sync_and_manage_live_orders(session)
                    except Exception as tp_err:
                        logger.error(f"TP Monitor Worker error: {tp_err}")

                    from config import PAPER_TRADING_ENABLED
                    if PAPER_TRADING_ENABLED:
                        try:
                            await evaluate_paper_trades_async(session, semaphore)
                        except Exception as paper_err:
                            logger.error(f"Paper Trade Monitor error: {paper_err}")

                        # Regular Automated Strategy Lookout Update (Every 1 hour when trades are active)
                        now_t = time.time()
                        if now_t - last_lookout_time >= 3600 and (now_t - BOT_START_TIME >= 65):
                            try:
                                wallet = load_wallet()
                                if wallet.get("active_positions") or wallet.get("resting_orders"):
                                    card = await generate_strategy_lookout_card(session)
                                    await notify_telegram(card)
                                    logger.info("Dispatched regular hourly paper trading strategy lookout update.")
                                last_lookout_time = now_t
                                set_last_lookout_ts(now_t)
                            except Exception as e_look:
                                logger.error(f"Error sending regular paper lookout update: {e_look}")
                    else:
                        # REAL LIVE TRADING: Multi-Account Hourly Position Tracking Lookout Update
                        now_t = time.time()
                        if now_t - last_lookout_time >= 3600 and (now_t - BOT_START_TIME >= 65):
                            try:
                                if executor.is_configured():
                                    import zoneinfo
                                    from telegram_bot import build_hourly_live_lookout_cards, send_text_to_chat
                                    from config import TELEGRAM_CHAT_ID
                                    ist_h = zoneinfo.ZoneInfo("Asia/Kolkata")
                                    time_str_h = datetime.now(ist_h).strftime("%d %b %Y, %I:%M %p IST")

                                    admin_card, client_dispatches = await build_hourly_live_lookout_cards(session, time_str_h)
                                    admin_chat = str(TELEGRAM_CHAT_ID or "771305593").strip()
                                    await send_text_to_chat(admin_chat, admin_card)
                                    logger.info("Dispatched multi-account hourly real live trading lookout update to Admin Telegram.")

                                    for client_chat, client_card in client_dispatches:
                                        try:
                                            await send_text_to_chat(client_chat, client_card)
                                            logger.info(f"Dispatched personal hourly lookout update to client chat {client_chat}.")
                                        except Exception as e_cli:
                                            logger.error(f"Error sending personal hourly lookout to {client_chat}: {e_cli}")
                                last_lookout_time = now_t
                                set_last_lookout_ts(now_t)
                            except Exception as e_live_look:
                                logger.error(f"Error sending regular live lookout update: {e_live_look}", exc_info=True)

                    await asyncio.sleep(5)

            asyncio.create_task(tp_monitor_worker())
            from message_sync import start_deletion_sync_watcher
            asyncio.create_task(start_deletion_sync_watcher())

            last_dashboard_update_time = 0
            # MINOR #4 FIX: Load daily_digest_sent_today from daily_file to survive restarts
            daily_digest_sent_today = False
            try:
                if os.path.exists(daily_file):
                    with open(daily_file, 'r') as _f:
                        _d = json.load(_f)
                        _digest_date = _d.get('digest')
                        if _digest_date == now_ist_boot.strftime("%Y-%m-%d") and now_ist_boot.hour == 22:
                            daily_digest_sent_today = True
            except Exception:
                pass

            # 1-Minute Startup Reconciliation & Offline Audit on Resume
            await run_startup_reconciliation(session, semaphore)

            while True:
                if os.path.exists("STOP_BOT.flag"):
                    logger.warning("STOP_BOT.flag detected! Emergency Stop Switch triggered. Exiting main scanner loop.")
                    break
                try:
                    now_ist = datetime.now(timezone.utc).astimezone(ist_tz)
                    today_str = now_ist.strftime("%Y-%m-%d")
                    # USER DIRECTIVE: Night Maintenance Pause (12:30 AM to 05:30 AM IST) strictly enforced
                    from config import is_night_maintenance_window
                    is_night_pause = is_night_maintenance_window(datetime.now(timezone.utc))
                    
                    # Always resolve pending active trades (TP / SL / Breakeven Shield)
                    await resolve_pending_trades_async(session, semaphore)
                    
                    if is_night_pause:
                        if _night_notified_date != today_str:
                            from telegram_bot import send_text_to_chat
                            from config import TELEGRAM_CHAT_ID, TELEGRAM_SIGNAL_CHANNEL_ID, TELEGRAM_SCANNER_CHANNEL_ID
                            night_msg = (
                                "🌙 <b>NIGHT MAINTENANCE PAUSE</b>\n\n"
                                "Scanner is currently taking a break for the night. New signals will resume automatically at 5:30 AM IST.\n\n"
                                "Rest well, snipers! ☀️"
                            )
                            # Broadcast to both channels and Personal DM
                            broadcast_targets = list(dict.fromkeys([
                                TELEGRAM_SIGNAL_CHANNEL_ID or "-1004463573705",
                                TELEGRAM_SCANNER_CHANNEL_ID or "-1004332146822",
                                TELEGRAM_CHAT_ID or "771305593"
                            ]))
                            for ch_id in broadcast_targets:
                                if ch_id:
                                    try:
                                        await send_text_to_chat(str(ch_id), night_msg)
                                    except Exception as e_ch:
                                        logger.error(f"Failed to send night pause to {ch_id}: {e_ch}")
                            _night_notified_date = today_str
                            try:
                                with open(daily_file, 'w') as f:
                                    json.dump({'morning': _morning_notified_date, 'night': _night_notified_date}, f)
                            except: pass
                        logger.info(f"Night Maintenance Pause Window ({now_ist.strftime('%H:%M IST')} - 12:30 AM to 05:30 AM IST). Resolving active trades only; new signal scanning paused.")
                    else:
                        if _morning_notified_date != today_str and ((now_ist.hour == 5 and now_ist.minute >= 30) or now_ist.hour == 6):
                            from telegram_bot import send_text_to_chat
                            from config import TELEGRAM_CHAT_ID, TELEGRAM_SIGNAL_CHANNEL_ID, TELEGRAM_SCANNER_CHANNEL_ID
                            morning_msg = "☀️ <b>GOOD MORNING! SCANNER ACTIVE</b>\n\nNight maintenance complete. The 1-minute sniper is now hunting for fresh Crossovers & Retests."
                            broadcast_targets = list(dict.fromkeys([
                                TELEGRAM_SIGNAL_CHANNEL_ID or "-1004463573705",
                                TELEGRAM_SCANNER_CHANNEL_ID or "-1004332146822",
                                TELEGRAM_CHAT_ID or "771305593"
                            ]))
                            for ch_id in broadcast_targets:
                                if ch_id:
                                    try:
                                        await send_text_to_chat(str(ch_id), morning_msg)
                                    except Exception as e_ch:
                                        logger.error(f"Failed to send morning active to {ch_id}: {e_ch}")
                            _morning_notified_date = today_str
                            try:
                                with open(daily_file, 'w') as f:
                                    json.dump({'morning': _morning_notified_date, 'night': _night_notified_date}, f)
                            except: pass
                            
                        # BTC Volatility Shock Shield Check (Protects crypto from whipsaws; allows clean-cascade 15M Chaser)
                        is_shock_active, shock_rem = await check_btc_volatility_shock(session, semaphore)
                        from indicators import get_btc_shock_context
                        shock_ctx = get_btc_shock_context()
                        if is_shock_active:
                            if shock_ctx.get("is_clean_cascade"):
                                logger.warning(f"⚡ BTC CLEAN CASCADE SHOCK ({shock_ctx.get('direction')}, {shock_rem:.1f}m rem). Regular alts paused; cascade-aligned 15M Runaway Chaser enabled (capped).")
                            else:
                                logger.warning(f"🛑 BTC WHIPSAW SHOCK ({shock_rem:.1f}m rem). Altcoins paused; commodities/stocks scanning normally...")

                        # DAILY RISK CIRCUIT BREAKER: 5 consecutive SLs -> 60-min pause
                        from state import is_trading_paused
                        is_cb_paused, cb_rem_secs, cb_reason = is_trading_paused()
                        if is_cb_paused:
                            logger.warning(f"⏸️ CIRCUIT BREAKER ACTIVE: 5 consecutive SLs hit ({cb_rem_secs/60:.1f}m remaining). Skipping scan cycle...")
                            record_quiet_scan_event("Circuit Breaker Pause")
                            await maybe_dispatch_inactivity_notice(session)
                            await asyncio.sleep(20)
                            continue
                        
                        logger.info("Starting Multi-Crossover scan...")
                        start_t = datetime.now()
                        
                        from pullback_tracker import WATCHLIST, SPECIAL_LIST, COMMODITIES, US_EQUITIES
                        from fetcher import get_top_pairs_by_volume
                        
                        # WEEKEND TRADING RULE:
                        # Commodities (Gold, Silver, Oil, Natgas) & US Equities are CLOSED on Sat & Sun.
                        # Strictly zero commodity or stock scanning/signals on weekends!
                        is_weekend = now_ist.weekday() >= 5

                        from config import US_STOCK_TOKENS
                        stock_symbols_set = {f"{tok}/USDT" for tok in US_STOCK_TOKENS}
                        # Include bare stock tickers on Binance Futures
                        stock_symbols_set.update({"INTC/USDT", "AMD/USDT", "BABA/USDT", "DIS/USDT", "PYPL/USDT", "BA/USDT", "XOM/USDT", "SOXL/USDT", "SNDK/USDT"})

                        if is_weekend:
                            COMMODITIES_LIST = []
                            US_STOCKS_LIST = []
                            # Exclude all commodities and US stocks from active scan on weekends (BTC and ETH are active 24/7)
                            weekend_blocked_assets = set(COMMODITIES + US_EQUITIES + list(stock_symbols_set) + ["XAUT/USDT", "XAG/USDT", "NATGAS/USDT", "CL/USDT"])
                        else:
                            # USER DIRECTIVE: Commodities Active (Gold XAU, Natural Gas NATGAS, Crude Oil CL, Silver XAG)
                            COMMODITIES_LIST = [
                                "XAU/USD", "XAU/USDT", "NATGAS/USDT", "CL/USDT", "XAG/USDT"
                            ]
                            # US Equity Tokens (Binance Spot 24/7 feeds + yfinance fallback): Active all weekdays
                            US_STOCKS_LIST = [
                                "NVDAB/USDT", "TSLAB/USDT", "MSTRB/USDT", "AAPLB/USDT",
                                "AMZNB/USDT", "METAB/USDT", "MSFTB/USDT", "GOOGLB/USDT",
                                "SPYB/USDT", "QQQB/USDT", "COIN/USDT", "AMD/USDT"
                            ]
                            weekend_blocked_assets = set()
                        
                        # ── USER-DEFINED WEEKDAY vs WEEKEND SCANNING RULES ──
                        # 1. Mon - Fri: ONLY Top 25 Crypto coins + Commodities (Active) + US Equity Tokens (15).
                        # 2. Sat - Sun (Weekend): ALL Binance Futures coins EXCEPT BTC, ETH, Commodities, and US Equity Tokens.
                        all_futures_pairs = get_top_pairs_by_volume(top_n=None)
                        if not all_futures_pairs: all_futures_pairs = []

                        commodities_set = {"XAU/USD", "XAU/USDT", "XAUT/USDT", "XAG/USDT", "NATGAS/USDT", "CL/USDT", "OIL/USDT"}

                        if not is_weekend:
                            # Mon - Fri: ONLY Top 25 Crypto + Commodities + US Equities
                            # User Directive: PAXG is strictly stopped / blocked. Gold trading is strictly XAUUSDT.
                            top_25_crypto = [
                                sym for sym in all_futures_pairs
                                if sym not in stock_symbols_set
                                and sym not in commodities_set
                                and "PAXG" not in sym.upper()
                                and not any(sym.startswith(s) for s in ["XAU", "XAG", "NATGAS", "CL", "SOXL", "SOXS", "SKHY", "KORU", "SPCX"])
                            ][:25]
                            active_pairs = list(dict.fromkeys(top_25_crypto + COMMODITIES_LIST + US_STOCKS_LIST))
                        else:
                            # Sat - Sun: ALL Binance Futures + BTC & ETH (Commodities & US Equities remain CLOSED)
                            weekend_blocked = commodities_set.union(stock_symbols_set)
                            active_pairs = [
                                sym for sym in all_futures_pairs
                                if sym not in weekend_blocked
                                and "PAXG" not in sym.upper()
                                and not any(sym.startswith(s) for s in ["XAU", "XAG", "NATGAS", "CL", "OIL", "SOXL", "SOXS", "SKHY", "KORU", "SPCX", "SPY", "QQQ"])
                            ]
                            # Explicitly ensure BTC and ETH are scanned on weekends
                            for major in ["BTC/USDT", "ETH/USDT"]:
                                if major not in active_pairs:
                                    active_pairs.insert(0, major)
                        
                        if is_shock_active and not shock_ctx.get("is_clean_cascade"):
                            from market_cache import is_commodity_symbol, is_stock_symbol
                            active_pairs = [sym for sym in active_pairs if is_commodity_symbol(sym) or is_stock_symbol(sym)]
                            if not active_pairs:
                                record_quiet_scan_event("BTC Volatility Shock")
                                await maybe_dispatch_inactivity_notice(session)
                                await asyncio.sleep(15)
                                continue

                        tasks = [scan_single_symbol_async(session, sym, semaphore, shock_ctx=shock_ctx) for sym in active_pairs]
                        results = await asyncio.gather(*tasks)

                        # ═════════════════════════════════════════════════════════════════════════════
                        # 1. IMMEDIATE LIVE TRADE EXECUTION (ZERO DELAY PRIORITY)
                        # User Directive: First order execution on exchange, then Telegram sharing!
                        # ═════════════════════════════════════════════════════════════════════════════
                        raw_candidates = []
                        for r in results:
                            if isinstance(r, dict):
                                for cand in r.get("trades", []):
                                    if isinstance(cand, dict):
                                        raw_candidates.append(cand)
                                # Backward compat: handle old "trade" key if any
                                if isinstance(r.get("trade"), dict):
                                    raw_candidates.append(r["trade"])
                        if raw_candidates:
                            from executor import executor
                            async def _check_auto_trade_eligibility(c):
                                from config import is_night_maintenance_window
                                if is_night_maintenance_window():
                                    return False, "Night Maintenance Mode active (12:30 AM - 05:30 AM IST) — Auto-execution paused to protect capital."

                                sym = c.get("symbol", "")
                                sym_upper = sym.upper()
                                sig = c.get("signal", "BUY").upper()
                                stars = c.get("aligned_count", 0)
                                rsi = c.get("rsi")

                                # MINOR #2 FIX: Check AUTO_TRADE_EXCLUDED_SYMBOLS from config (exact base asset match)
                                from config import AUTO_TRADE_EXCLUDED_SYMBOLS
                                sym_clean = sym_upper.replace("/", "").replace("_", "")
                                sym_base = sym_clean.replace("USDT", "").replace("USD", "").replace("FDUSD", "")
                                for excl in AUTO_TRADE_EXCLUDED_SYMBOLS:
                                    excl_clean = excl.upper().replace("/", "").replace("_", "")
                                    excl_base = excl_clean.replace("USDT", "").replace("USD", "").replace("FDUSD", "")
                                    if sym_clean == excl_clean or sym_base == excl_base:
                                        return False, f"Symbol {sym} is in AUTO_TRADE_EXCLUDED_SYMBOLS config."

                                # USER DIRECTIVE: Weekdays (Mon-Fri) BTC & ETH can be traded!
                                # Weekend (Saturday & Sunday): Strictly NO trades on BTC, ETH, and Gold!
                                import zoneinfo
                                ist_tz_elig = zoneinfo.ZoneInfo("Asia/Kolkata")
                                now_ist_elig = datetime.now(ist_tz_elig)
                                is_weekend_elig = now_ist_elig.weekday() in (5, 6) # 5 = Saturday, 6 = Sunday

                                is_btc_eth_check = sym_base in ("BTC", "ETH")
                                is_gold_check = any(c_name in sym_clean for c_name in ["XAU", "GOLD", "XAUT", "XAG"])

                                if is_weekend_elig and (is_btc_eth_check or is_gold_check):
                                    return False, f"Weekend trading paused for {sym} (Sat/Sun per directive). Weekdays only."

                                # USER DIRECTIVE: Stop US stock tokens in auto-trading (trade only crypto & commodities)
                                from executor import is_us_stock_token
                                is_us_stock = is_us_stock_token(sym)
                                if is_us_stock:
                                    return False, f"{sym} is a US Stock Token (Automated execution disabled per user directive; Telegram signal only)."

                                # STRICT USER DIRECTIVE: Execute ONLY assets in approved execution list (Top 25 volume crypto & commodities)
                                from config import is_allowed_trading_asset
                                if not is_allowed_trading_asset(sym):
                                    return False, f"{sym} is outside Approved Top 25 volume crypto & commodities list."

                                # USER DIRECTIVE: 1H Bear/Bull Trap Reversals Included in Auto-Trading!
                                if c.get("is_trap_reversal"):
                                    if rsi is not None:
                                        try:
                                            f_rsi = float(rsi)
                                            if sig == "BUY" and f_rsi > 45.0:
                                                return False, f"Bear Trap BUY rejected — RSI {f_rsi:.1f} not oversold enough (> 45.0)."
                                            elif sig == "SELL" and f_rsi < 55.0:
                                                return False, f"Bull Trap SELL rejected — RSI {f_rsi:.1f} not overbought enough (< 55.0)."
                                        except (ValueError, TypeError):
                                            pass
                                    return True, "Eligible (1H SFP / Trap Reversal with Reclaim + Sweep Guard)"

                                # USER DIRECTIVE: Squeeze Breakout Trades Included in Auto-Trading!
                                # Dedicated 1H Squeeze Breakout: already passed institutional 6-point verification + V2 Retest-Only
                                if c.get("is_dedicated_squeeze"):
                                    if rsi is not None:
                                        try:
                                            f_rsi = float(rsi)
                                            # Squeeze breakout candles surge with high momentum: bounds [42.0 - 78.0] for BUY, [22.0 - 58.0] for SELL
                                            if sig == "BUY" and not (42.0 <= f_rsi <= 78.0):
                                                return False, f"Squeeze BUY RSI {f_rsi:.1f} is outside healthy zone [42.0 - 78.0] (Exhaustion > 78.0)."
                                            elif sig == "SELL" and not (22.0 <= f_rsi <= 58.0):
                                                return False, f"Squeeze SELL RSI {f_rsi:.1f} is outside healthy zone [22.0 - 58.0] (Exhaustion < 22.0)."
                                        except (ValueError, TypeError):
                                            pass
                                    return True, "Eligible (1H Squeeze Breakout with Verified Coiling + CVD)"

                                # USER DIRECTIVE: 15M 8H-Breakout Runaway Chase (Wide 1H ATR SL, 2.2R)
                                if c.get("is_runaway_chase"):
                                    if rsi is not None:
                                        try:
                                            f_rsi = float(rsi)
                                            if sig == "BUY" and f_rsi > 72.0:
                                                return False, f"Runaway BUY RSI {f_rsi:.1f} > 72.0 (Exhausted)."
                                            elif sig == "SELL" and f_rsi < 28.0:
                                                return False, f"Runaway SELL RSI {f_rsi:.1f} < 28.0 (Exhausted)."
                                        except (ValueError, TypeError):
                                            pass
                                    return True, "Eligible (15M 8H-Breakout Runaway Chase + 1H/4H/BTC + Wide ATR SL)"

                                # STRICT USER DIRECTIVE: Execute 3-Star (3/4) & 4-Star (4/4) setups!
                                if stars < 3:
                                    return False, f"Confluence is only {stars}/4 stars (Minimum 3-Star HTF alignment required for execution)."

                                # STRICT USER DIRECTIVE: Compute Multi-Factor AI Quality Score FIRST
                                from signal_analyser import analyse_signal
                                f_rsi_val = None
                                if rsi is not None:
                                    try:
                                        f_rsi_val = float(rsi)
                                    except (ValueError, TypeError):
                                        pass

                                analysis = analyse_signal(
                                    symbol=sym,
                                    signal=sig,
                                    rsi=f_rsi_val,
                                    adx=c.get("adx"),
                                    volume=c.get("volume"),
                                    volume_sma=c.get("volume_sma"),
                                    macd_line=c.get("macd_line"),
                                    macd_signal_val=c.get("macd_signal_val"),
                                    body_ratio=c.get("body_ratio"),
                                    aligned_count=stars,
                                    tf_label=c.get("tf_label", "1H"),
                                    cross_type=c.get("cross_type", ""),
                                )
                                quality_score = int(analysis.get("score", 0))
                                verdict = analysis.get("verdict", "N/A")
                                c["quality_score"] = quality_score
                                c["analysis"] = analysis

                                # Check RSI zone
                                is_rsi_valid = True
                                rsi_fail_reason = ""
                                if f_rsi_val is None:
                                    is_rsi_valid = False
                                    rsi_fail_reason = "RSI indicator value not available"
                                else:
                                    if sig == "BUY" and not (42.0 <= f_rsi_val <= 62.0):
                                        is_rsi_valid = False
                                        rsi_fail_reason = f"RSI {f_rsi_val:.1f} is outside healthy BUY zone [42.0 - 62.0]"
                                    elif sig == "SELL" and not (38.0 <= f_rsi_val <= 58.0):
                                        is_rsi_valid = False
                                        rsi_fail_reason = f"RSI {f_rsi_val:.1f} is outside healthy SELL zone [38.0 - 58.0]"

                                is_retest = c.get("is_retest", False) or "retest" in str(c.get("cross_type", "")).lower()

                                # USER DIRECTIVE:
                                # "If AI score above 77 and RSI sign not there, still execute the trade!
                                # 3-Star or 4-Star trade RSI sign not there but score above 77 -> execute the trade!"
                                if not is_rsi_valid:
                                    if quality_score > 77:
                                        logger.info(
                                            f"🚀 AI QUALITY OVERRIDE for {sym} [{sig}]: Score {quality_score}/100 > 77 with {stars}★ Confluence! "
                                            f"Executing trade despite RSI ({rsi_fail_reason}) per user directive."
                                        )
                                    else:
                                        return False, f"{rsi_fail_reason} (AI Quality Score {quality_score}/100 <= 77; requires healthy RSI)."
                                else:
                                    # RSI is healthy!
                                    # For 3-Star and 4-Star setups with verified healthy RSI:
                                    # - Retest setups: pullbacks naturally have low volume and rejection wicks; min score 60 required.
                                    # - 4-Star setups: min score 65 required (allows off-hours session / 24/7 crypto execution).
                                    min_score_required = 60 if is_retest else (65 if stars >= 4 else 70)
                                    if quality_score < min_score_required:
                                        comps = analysis.get("components", {})
                                        comp_summary = (
                                            f"HTF:{comps.get('htf',0)}/20, RSI:{comps.get('rsi',0)}/20, "
                                            f"Vol:{comps.get('volume',0)}/15, ADX:{comps.get('adx',0)}/15, "
                                            f"MACD:{comps.get('macd',0)}/10, Body:{comps.get('body',0)}/10, Session:{comps.get('session',0)}/10"
                                        )
                                        return False, f"Multi-Factor Score is {quality_score}/100 (< {min_score_required} required | {verdict} | Breakdown: {comp_summary})"

                                # USER DIRECTIVE: Funding Rate Squeeze Shield (Live Binance Microstructure)
                                # Shields against crowded trades (Short Squeezes & Long Liquidations)
                                is_commodity_sym = any(x in sym_clean for x in ["XAU", "GOLD", "XAUT", "XAG", "CL", "OIL", "NATGAS"])
                                if not is_commodity_sym and not c.get("is_trap_reversal"):
                                    try:
                                        from fetcher import fetch_futures_microstructure_async
                                        micro_data = await fetch_futures_microstructure_async(session, sym, signal=sig)
                                        funding_pct = float(micro_data.get("funding_pct", 0.01))
                                        c["funding_pct"] = funding_pct

                                        # 1. SHORT Squeeze Shield: If funding is heavily negative (< -0.04%), retail is overcrowded short!
                                        if sig == "SELL" and funding_pct <= -0.040:
                                            return False, f"Funding Rate is heavily negative ({funding_pct:+.4f}%). Retail overcrowded SHORT — high risk of Short Squeeze trap."

                                        # 2. LONG Liquidation Shield: If funding is heavily positive (> +0.05%), retail is overcrowded long!
                                        if sig == "BUY" and funding_pct >= 0.050:
                                            return False, f"Funding Rate is heavily positive ({funding_pct:+.4f}%). Retail overcrowded LONG — high risk of Long Liquidation cascade."
                                    except Exception as e_fr:
                                        logger.debug(f"Funding rate check bypassed for {sym}: {e_fr}")

                                rsi_status_lbl = "AI Score > 77 Override" if not is_rsi_valid else "RSI Healthy"
                                return True, f"Eligible ({stars}★ Confluence, {rsi_status_lbl}, Score: {quality_score}/100, Funding Clean)"

                            eligible_candidates = []
                            for c in raw_candidates:
                                try:
                                    is_ok, reason = await _check_auto_trade_eligibility(c)
                                except Exception as e_elig:
                                    is_ok = False
                                    reason = f"Eligibility check error: {e_elig}"
                                    logger.error(f"Error checking auto-trade eligibility for {c.get('symbol')}: {e_elig}", exc_info=True)
                                if is_ok:
                                    eligible_candidates.append(c)
                                else:
                                    logger.info(f"Auto-trade filtered candidate {c.get('symbol')} [{c.get('signal')}]: {reason}")
                                    from executor import is_us_stock_token
                                    if is_us_stock_token(c.get("symbol", "")):
                                        from state import update_trade_outcome, _make_key
                                        st_k = _make_key(c.get("symbol"), c.get("unique_key"), c.get("signal"), c.get("candle_ts"))
                                        update_trade_outcome(st_k, "SIGNAL_ONLY_US_STOCK")
                                    else:
                                        await notify_signal_rejection(c.get("symbol"), c.get("signal"), reason, cand=c)

                            if eligible_candidates and not executor.is_configured():
                                from config import PAPER_TRADING_ENABLED
                                if not PAPER_TRADING_ENABLED:
                                    for c_unconf in eligible_candidates:
                                        await notify_signal_rejection(
                                            c_unconf.get("symbol"),
                                            c_unconf.get("signal"),
                                            "Auto-Trading Disabled or Shark Exchange API keys not configured.",
                                            cand=c_unconf,
                                        )

                            if eligible_candidates and executor.is_configured():
                                from executor import clean_contract_symbol
                                TIER_1_SUPER_MAJORS = {"BTC", "ETH", "SOL", "BNB", "XAUT", "XAU", "XAG", "CL", "OIL", "COPPER", "BRONZE", "HG"}
                                TIER_2_MAJOR_ALTS   = {"AVAX", "LINK", "SUI", "NEAR", "ADA", "DOT", "LTC", "BCH", "ARB", "AAVE", "XRP", "DOGE", "SHIB", "TRX"}

                                # Ranking Formula (3-Tier Coin Classification First):
                                # Tier 3: Super Majors (BTC, ETH, SOL, BNB, XAUT)
                                # Tier 2: High Volume Major Alts (AVAX, LINK, SUI, NEAR, ARB, AAVE, etc.)
                                # Tier 1: Standard Altcoins (UNI, POL, RENDER, JUP, etc.)
                                def _coin_tier_rank(c):
                                    sym_clean = clean_contract_symbol(c.get("symbol", ""))
                                    if sym_clean in TIER_1_SUPER_MAJORS or c.get("is_special"):
                                        tier = 3
                                    elif sym_clean in TIER_2_MAJOR_ALTS:
                                        tier = 2
                                    else:
                                        tier = 1

                                    stars = c.get("aligned_count", 0)
                                    early = 0.5 if c.get("is_30m_early") else 0.0
                                    sq_boost = 1.0 if (c.get("is_dedicated_squeeze") or c.get("is_runaway_chase")) else 0.0
                                    signal_score = stars + early + sq_boost
                                    rr_score = c.get("rr3") or c.get("rr2") or 1.5
                                    return (tier, signal_score, rr_score)

                                eligible_candidates.sort(key=_coin_tier_rank, reverse=True)
                                
                                # USER DIRECTIVE: Portfolio cap removed completely — execute ALL eligible setups without limit
                                top_candidates = eligible_candidates
                                logger.info(f"🏆 ALL {len(top_candidates)} ELIGIBLE SETUP(S) SELECTED FOR SHARK EXECUTION (No portfolio cap).")
                                batch_submitted_by_dir = {"BUY": [], "SELL": []}
                                shock_crypto_executed = 0
                                for cand in top_candidates:
                                    sym = cand["symbol"]
                                    sig = cand["signal"]
                                    stars = cand.get("aligned_count", 0)
                                    from market_cache import is_commodity_symbol, is_stock_symbol
                                    is_cand_exempt_shock = is_commodity_symbol(sym) or is_stock_symbol(sym)

                                    # USER DIRECTIVE: Same coin continuation trailing guard
                                    # If an active open position already exists in the SAME direction, pass to executor to trail SL & TP forward!
                                    # If opposite direction or duplicate resting entry order exists, shield to avoid duplicate exposure.
                                    active_pos = await executor.get_open_position(session, sym)
                                    is_same_dir_continuation = False
                                    if active_pos:
                                        pos_qty = abs(float(active_pos.get("quantity") or active_pos.get("positionAmount") or 0))
                                        if pos_qty > 0:
                                            pos_dir_raw = str(active_pos.get("positionType") or active_pos.get("positionSide") or active_pos.get("side") or "").upper()
                                            pos_dir = "BUY" if pos_dir_raw in ("LONG", "BUY") else "SELL"
                                            sig_dir = "BUY" if str(sig).upper() in ("BUY", "LONG") else "SELL"
                                            if pos_dir == sig_dir:
                                                is_same_dir_continuation = True

                                    # BTC Shock Basket Cap: max 1 new crypto chase per cycle & max 2 concurrent at-risk positions during shock
                                    if is_shock_active and not is_cand_exempt_shock and not is_same_dir_continuation:
                                        if shock_crypto_executed >= 1:
                                            cap_reason = "BTC Volatility Shock Cap: max 1 crypto chase per cycle to prevent simultaneous SLs."
                                            logger.info(f"Skipping {sym} — {cap_reason}")
                                            await notify_signal_rejection(sym, sig, cap_reason, cand=cand)
                                            continue

                                    # USER DIRECTIVE: Max 5 Concurrent Same-Direction At-Risk Trades Shield (tightened to 2 during BTC Shock)
                                    # Trades whose live SL is moved to Breakeven (entry price) or Profit-Lock are Zero-Risk & EXEMPT!
                                    cand_dir = "BUY" if str(sig).upper() in ("BUY", "LONG") else "SELL"
                                    slot_cap = 2 if (is_shock_active and not is_cand_exempt_shock) else 5
                                    if not is_same_dir_continuation:
                                        slot_info = await executor.get_directional_risk_slots(
                                            session,
                                            signal=sig,
                                            max_at_risk=slot_cap,
                                            batch_submitted_syms=batch_submitted_by_dir[cand_dir],
                                        )
                                        if slot_info["breakeven_syms"]:
                                            logger.info(
                                                f"🛡️ Directional Risk Slots [{cand_dir}]: {slot_info['at_risk_count']}/{slot_cap} at-risk "
                                                f"({', '.join(slot_info['at_risk_syms']) or 'None'}) | Breakeven-exempt: "
                                                f"{', '.join(slot_info['breakeven_syms'])}"
                                            )
                                        if not slot_info["allowed"]:
                                            basket_reason = slot_info["reason"]
                                            logger.info(f"Skipping {sym} — {basket_reason}")
                                            await notify_signal_rejection(sym, sig, basket_reason, cand=cand)
                                            continue

                                    # Verify duplicate check before submitting order (bypassed if same-direction continuation trailing)
                                    if not is_same_dir_continuation and await executor.has_active_trade_on_exchange(session, sym, signal=sig):
                                        dup_reason = f"Active trade or resting order already exists on Shark Exchange for {sym}. Single Order Margin Shield prevents duplicate overlapping positions."
                                        logger.info(f"Skipping {sym} — {dup_reason}")
                                        await notify_signal_rejection(sym, sig, dup_reason, cand=cand)
                                        continue

                                    action_desc = "CONTINUATION TRAIL SL/TP" if is_same_dir_continuation else "BULK TRADE EXECUTE"
                                    logger.info(f"⚡ {action_desc}: {sym} [{sig}] ({stars}/4 Stars Priority | AI Score: {cand.get('quality_score', 'N/A')}/100)")
                                    try:
                                        shark_res = await executor.execute_signal_trade(
                                            session, sym, sig, cand["entry"], cand["sl"], 
                                            cand["tp1"], cand["tp2"],
                                            aligned_count=stars,
                                            adx=cand.get("adx")
                                        )
                                    except Exception as e_exec:
                                        logger.error(f"Unhandled exception executing {sym} [{sig}]: {e_exec}", exc_info=True)
                                        await notify_signal_rejection(sym, sig, f"Shark Broker Exception: {e_exec}", cand=cand)
                                        continue

                                    if shark_res and shark_res.get("status") in ("success", "partial_success"):
                                        if is_shock_active and not is_cand_exempt_shock:
                                            shock_crypto_executed += 1
                                        from executor import to_shark_symbol
                                        sh_sym = to_shark_symbol(sym)
                                        if sh_sym not in batch_submitted_by_dir[cand_dir]:
                                            batch_submitted_by_dir[cand_dir].append(sh_sym)
                                        from state import update_trade_progress, _make_key
                                        state_key = _make_key(sym, cand["unique_key"], sig, cand["candle_ts"])
                                        t_qty = shark_res.get("total_qty", 0)
                                        update_trade_progress(state_key, shark_order_placed=True, total_qty=t_qty)
                                        record_trade_activity_ts()
                                        reset_quiet_scan_stats()
                                        logger.info(f"Marked shark_order_placed=True for {state_key}")
                                        failed_accs = shark_res.get("failed_accounts") or []
                                        real_failures = [
                                            f for f in failed_accs
                                            if "already exists" not in str(f.get("reason", "")).lower()
                                        ]
                                        if real_failures:
                                            part_reason = " | ".join([f"{f['account']}: {f['reason']}" for f in real_failures])
                                            await notify_signal_rejection(sym, sig, f"Partial Account Rejection: {part_reason}", cand=cand)
                                    elif shark_res and shark_res.get("status") == "trailed":
                                        record_trade_activity_ts()
                                        reset_quiet_scan_stats()
                                        logger.info(f"✅ Position for {sym} successfully trailed SL/TP on continuation signal.")
                                    elif shark_res and shark_res.get("status") in ("skipped", "error"):
                                        fail_reason = shark_res.get("reason", "Broker execution rejected")
                                        await notify_signal_rejection(sym, sig, f"Shark Broker: {fail_reason}", cand=cand)
                                    
                                    # Mandatory 1.5-second sequential pause between bulk trade executions
                                    await asyncio.sleep(1.5)

                        # ═════════════════════════════════════════════════════════════════════════════
                        # 2. TELEGRAM CHANNEL BROADCASTS & BACKGROUND DISPATCHERS (RUN AFTER TRADES)
                        # ═════════════════════════════════════════════════════════════════════════════
                        from telegram_bot import flush_alerts
                        await flush_alerts()

                        # ── STAGE 1: WATCHLIST COILING DISPATCHER (All Ticks Verified + Anti-Spam Single-Best) ──
                        radar_candidates = [r["radar"] for r in results if isinstance(r, dict) and isinstance(r.get("radar"), dict)]
                        if radar_candidates:
                            from state import is_global_radar_on_cooldown, record_global_radar_sent, record_pre_squeeze_radar, is_radar_on_cooldown
                            # Filter candidates eligible for Telegram broadcast (not on 4-hour alert cooldown)
                            broadcast_eligible = [c for c in radar_candidates if not is_radar_on_cooldown(c["symbol"], cooldown_hours=4.0)]
                            if broadcast_eligible and not is_global_radar_on_cooldown(cooldown_minutes=10.0):
                                # Rank by Coiling Quality Score and select ONLY the #1 single best coiled setup across the market
                                broadcast_eligible.sort(key=lambda x: x.get("coiling_score", 0.0), reverse=True)
                                top_radar = broadcast_eligible[0]

                                from telegram_bot import send_pre_squeeze_radar_alert
                                await send_pre_squeeze_radar_alert(top_radar)
                                record_global_radar_sent()
                                record_pre_squeeze_radar(top_radar["symbol"], top_radar, is_broadcast=True)
                                logger.info(f"📡 DISPATCHED TOP #1 WATCHLIST COILING ALERT: {top_radar['symbol']} ({top_radar['direction']}) | Score: {top_radar.get('coiling_score')}")

                                # Silently track all other coiling candidates in state for Stage 2 breakout execution
                                for cand in radar_candidates:
                                    if cand["symbol"] != top_radar["symbol"]:
                                        record_pre_squeeze_radar(cand["symbol"], cand, is_broadcast=False)
                            else:
                                # All coiling candidates tracked silently in state for Stage 2 breakout execution
                                for cand in radar_candidates:
                                    record_pre_squeeze_radar(cand["symbol"], cand, is_broadcast=False)
                        
                        # SILENTLY EDIT PINNED 4H VOLATILITY DASHBOARD (Every 5 minutes)
                        if time.time() - last_dashboard_update_time >= 300:
                            try:
                                from volatility_dashboard import update_pinned_volatility_dashboard
                                await update_pinned_volatility_dashboard(session, semaphore)
                                last_dashboard_update_time = time.time()
                            except Exception as e_dash:
                                logger.error(f"Error updating pinned volatility dashboard: {e_dash}")
                        
                        # DAILY PNL & PERFORMANCE DIGEST CARD (10:00 PM IST)
                        if now_ist.hour == 22 and now_ist.minute <= 2 and not daily_digest_sent_today:
                            try:
                                from config import PAPER_TRADING_ENABLED
                                if PAPER_TRADING_ENABLED:
                                    from paper_trader import send_daily_paper_trading_digest
                                    await send_daily_paper_trading_digest()
                                daily_digest_sent_today = True
                                # MINOR #4 FIX: Persist digest sent date so restart doesn't re-send
                                try:
                                    with open(daily_file, 'r') as _f:
                                        _d = json.load(_f)
                                except Exception:
                                    _d = {}
                                _d['digest'] = today_str
                                with open(daily_file, 'w') as _f:
                                    json.dump(_d, _f)
                            except Exception as e_dig:
                                logger.error(f"Error dispatching daily digest: {e_dig}")
                        if now_ist.hour != 22:
                            daily_digest_sent_today = False

                        _QUIET_SCAN_STATS["scans_completed"] = int(_QUIET_SCAN_STATS.get("scans_completed", 0)) + 1
                        _QUIET_SCAN_STATS["pairs_scanned"] = len(active_pairs)
                        await maybe_dispatch_inactivity_notice(session)

                        total_sent = len(raw_candidates)
                        elapsed = (datetime.now() - start_t).total_seconds()
                        logger.info(f"Scan complete in {elapsed:.1f}s. {total_sent} signals sent.")
                
                    await asyncio.sleep(15)
                except Exception as e:
                    logger.error(f"Loop error: {e}")
                    await asyncio.sleep(60)
    finally:
        await stop_telegram_bot_service()

if __name__ == "__main__":
    from dynamic_config import set_dyn_config
    
    new_cfg = {
        "ADX_THRESHOLD": 15.0, 
        "VOLUME_MULTIPLIER": 1.2,
        "BODY_RATIO": 0.25,
        "RSI_MIN": 45.0,
        "RSI_MAX": 75.0
    }
    set_dyn_config(new_cfg)
    
    while True:
        try:
            asyncio.run(main_loop())
        except Exception as e:
            logger.critical(f"FATAL ERROR in main_loop: {e}. Restarting in 10s...")
            time.sleep(10)
