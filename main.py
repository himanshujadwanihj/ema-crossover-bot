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
    is_coin_on_cooldown, set_coin_cooldown, _make_key
)
from telegram_bot import (
    send_startup_message, send_text_alert, format_price, queue_alert, flush_alerts, 
    start_telegram_bot_service, stop_telegram_bot_service, send_text_to_chat
)
from executor import executor
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
                tp1 = raw_tp1 if (raw_tp1 and raw_tp1 > entry) else (entry + 2.0 * risk)
                tp2 = raw_tp2 if (raw_tp2 and raw_tp2 > tp1) else max(tp1 * 1.003, entry + 6.0 * risk)
                tp3 = tp2
            else:
                tp1 = raw_tp1 if (raw_tp1 and raw_tp1 < entry) else (entry - 2.0 * risk)
                tp2 = raw_tp2 if (raw_tp2 and raw_tp2 < tp1) else min(tp1 * 0.997, entry - 6.0 * risk)
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

                    # 2. Check TP1 Hit (70% profit booked -> move SL to +0.4R profit lock)
                    if not tp1_hit and row['high'] >= tp1:
                        tp1_hit = True
                        tp1_hit_at_dt = candle_dt
                        tp1_hit_at_str = tp1_hit_at_dt.isoformat()
                        update_trade_progress(key, tp1_hit=True, be_hit=True, new_sl=profit_lock_sl, tp1_hit_at=tp1_hit_at_str)
                        cur_sl = profit_lock_sl

                    # 3. Check TP2 Hit (30% Remaining Runner -> FULL WIN)
                    if tp1_hit and not tp2_hit and row['high'] >= tp2:
                        tp2_hit = True
                        tp2_hit_at_dt = candle_dt
                        tp2_hit_at_str = tp2_hit_at_dt.isoformat()
                        update_trade_progress(key, tp2_hit=True, new_sl=tp1, tp2_hit_at=tp2_hit_at_str)
                        outcome = "WIN"
                        final_price = tp2
                        break

                elif direction == "SELL":
                    # 1. Stop-Loss / Trailed SL Hit FIRST
                    if row['high'] >= cur_sl:
                        outcome = "TP2_EXIT" if tp2_hit else ("PROFIT_LOCK_WIN" if tp1_hit else "LOSS")
                        final_price = cur_sl
                        break

                    # 2. Check TP1 Hit (70% profit booked -> move SL to +0.4R profit lock)
                    if not tp1_hit and row['low'] <= tp1:
                        tp1_hit = True
                        tp1_hit_at_dt = candle_dt
                        tp1_hit_at_str = tp1_hit_at_dt.isoformat()
                        update_trade_progress(key, tp1_hit=True, be_hit=True, new_sl=profit_lock_sl, tp1_hit_at=tp1_hit_at_str)
                        cur_sl = profit_lock_sl

                    # 3. Check TP2 Hit (30% Remaining Runner -> FULL WIN)
                    if tp1_hit and not tp2_hit and row['low'] <= tp2:
                        tp2_hit = True
                        tp2_hit_at_dt = candle_dt
                        tp2_hit_at_str = tp2_hit_at_dt.isoformat()
                        update_trade_progress(key, tp2_hit=True, new_sl=tp1, tp2_hit_at=tp2_hit_at_str)
                        outcome = "WIN"
                        final_price = tp2
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

async def process_symbol_async(session, symbol, semaphore, tf_data=None):
    try:
        from state import is_already_sent, mark_sent
        from pullback_tracker import SPECIAL_LIST
        
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
        # Evaluates on 1H (Institutional 1H TF and above per User Directive): checks verified coiling radar breakout + CVD confirmation
        df_1h = tf_data.get("1H") if tf_data.get("1H") is not None else tf_data.get("1h")
        if df_1h is not None and len(df_1h) >= 25:
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
                    # DUAL ENGINE: BRANCH B — CONTINUATION BREAKDOWN / BREAKOUT CHASER
                    # ─────────────────────────────────────────────────────────────
                    if not sq_signal:
                        # MODE A: Dedicated Pre-Squeeze Radar (if active on Radar)
                        if has_radar:
                            radar_trigger = float(radar_entry.get('trigger_entry', 0.0))
                            radar_dir = str(radar_entry.get('direction', '')).upper()
                            if radar_dir == 'BUY' and candle_close >= (radar_trigger or (range_high * 1.002)):
                                sq_signal = 'BUY'
                            elif radar_dir == 'SELL' and candle_close <= (radar_trigger or (range_low * 0.998)):
                                sq_signal = 'SELL'
                        # MODE B: Direct Multi-Hour Consolidation Breakdown / Breakout
                        # User Directive: Short Squeezes & Long Squeezes are genuine breakout moves;
                        # they must NOT be blocked by counter HTF trend!
                        elif is_valid_consolidation:
                            if candle_close <= (range_low * 0.998):
                                sq_signal = 'SELL'
                            elif candle_close >= (range_high * 1.002):
                                sq_signal = 'BUY'

                        if sq_signal:
                            # Chaser Validation Rules:
                            if body_ratio < 0.45:
                                logger.info(f"FILTERED 1H CHASER: {symbol} rejected — Candle Body Ratio {body_ratio:.1%} < 45% (Weak expansion / long wick trap).")
                                sq_signal = None
                            elif sq_signal == 'BUY' and (sq_rsi > 78.0 or sq_rsi < 42.0):
                                logger.info(f"FILTERED 1H CHASER: {symbol} BUY rejected — RSI {sq_rsi:.1f} outside healthy momentum [42.0, 78.0].")
                                sq_signal = None
                            elif sq_signal == 'SELL' and (sq_rsi < 22.0 or sq_rsi > 58.0):
                                logger.info(f"FILTERED 1H CHASER: {symbol} SELL rejected — RSI {sq_rsi:.1f} outside healthy momentum [22.0, 58.0].")
                                sq_signal = None
                            elif sq_signal == 'BUY' and upper_wick_ratio > 0.38:
                                logger.info(f"FILTERED 1H CHASER: {symbol} BUY rejected — Upper rejection wick > 38% (Shooting star / whale trap).")
                                sq_signal = None
                            elif sq_signal == 'SELL' and lower_wick_ratio > 0.38:
                                logger.info(f"FILTERED 1H CHASER: {symbol} SELL rejected — Lower rejection wick > 38% (Hammer / bear trap).")
                                sq_signal = None
                            elif vol_20_sma > 0 and breakout_vol < (1.25 * vol_20_sma):
                                logger.info(f"FILTERED 1H CHASER: {symbol} rejected — Volume {breakout_vol:.0f} < 1.25x 20-SMA {vol_20_sma:.0f} (Volume surge missing).")
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
                            micro_1h = await fetch_futures_microstructure_async(session, symbol, sq_signal, df=df_1h)
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
                                        tp2 = round_price(symbol, entry + (2.2 * risk))
                                        cross_type = "🚀 1H Bear Trap Reversal (Short Squeeze Bounce + SFP ✅)"
                                    else:
                                        entry = round_price(symbol, candle_close)
                                        sl = round_price(symbol, float(c_high) * 1.002)
                                        risk = abs(entry - sl) if (entry and sl and entry != sl) else (candle_close * 0.015)
                                        if risk > (candle_close * 0.025):
                                            logger.info(f"FILTERED REVERSAL: {symbol} Bull trap risk {risk/candle_close:.1%} > 2.5% (Wick too long).")
                                            sq_signal = None
                                        tp1 = round_price(symbol, entry - (target_r * risk))
                                        tp2 = round_price(symbol, entry - (2.2 * risk))
                                        cross_type = "🩸 1H Bull Trap Reversal (Long Squeeze Flush + SFP ✅)"
                                else:
                                    # DUAL-MODE HYBRID CONTINUATION ENGINE:
                                    # 1. WATERFALL MOMENTUM CHASE: If extreme volume (>= 2.0x SMA), solid body (>= 58%),
                                    #    and strong CVD delta alignment exist, CHASE IMMEDIATELY at candle close so the big dump is NEVER missed!
                                    # 2. DISCIPLINED RETEST LIMIT: In normal/moderate volume, place a resting LIMIT order at range boundary
                                    #    to avoid buying top / selling bottom and completely eliminate fakeout wick losses.
                                    target_r = 2.2
                                    is_waterfall_rush = (
                                        vol_20_sma > 0 and 
                                        breakout_vol >= (2.0 * vol_20_sma) and 
                                        body_ratio >= 0.58 and
                                        cvd_aligned
                                    )

                                    if sq_signal == 'BUY':
                                        retest_target = range_high * 1.0015
                                        if is_waterfall_rush:
                                            # Immediate market chase — explosive rally in progress
                                            entry = candle_close
                                            logger.info(f"⚡ WATERFALL BREAKOUT RUSH: {symbol} Vol {breakout_vol/vol_20_sma:.1f}x SMA, Body {body_ratio:.1%} -> Immediate chase @ {entry}")
                                        else:
                                            # Disciplined pullback tap limit entry
                                            entry = retest_target if candle_close > retest_target * 1.002 else min(candle_close, retest_target)
                                            logger.info(f"🎯 RETEST LIMIT DISCIPLINE: {symbol} Normal volume -> Limit entry waiting at retest {entry}")

                                        sl = float(df_1h['low'].iloc[-4:].min()) * 0.998
                                        if ema_26_1h:
                                            sl = min(sl, ema_26_1h - (0.25 * atr_1h))
                                        # Max Risk Clamp: Prevent SL from exceeding 2.5% on 10x leverage
                                        if entry and sl and (entry - sl) > (candle_close * 0.025):
                                            sl = round_price(symbol, entry - (candle_close * 0.025))
                                        risk = abs(entry - sl) if (entry and sl and entry != sl) else (candle_close * 0.015)
                                        tp1 = round_price(symbol, entry + (target_r * risk))
                                        tp2 = round_price(symbol, entry + (3.2 * risk))
                                        cross_type = "🚀 1H Waterfall Breakout (Momentum Rush + CVD ✅)" if is_waterfall_rush else ("🚀 1H Range Breakout (Volume Expansion + CVD ✅)" if not has_radar else "🚀 1H Short-Squeeze Breakout (Retest Bounce + CVD ✅)")
                                    else:
                                        retest_target = range_low * 0.9985
                                        if is_waterfall_rush:
                                            # Immediate market chase — panic waterfall dump in progress
                                            entry = candle_close
                                            logger.info(f"⚡ WATERFALL BREAKDOWN RUSH: {symbol} Vol {breakout_vol/vol_20_sma:.1f}x SMA, Body {body_ratio:.1%} -> Immediate chase @ {entry}")
                                        else:
                                            # Disciplined pullback tap limit entry
                                            entry = retest_target if candle_close < retest_target * 0.998 else max(candle_close, retest_target)
                                            logger.info(f"🎯 RETEST LIMIT DISCIPLINE: {symbol} Normal volume -> Limit entry waiting at retest {entry}")

                                        sl = float(df_1h['high'].iloc[-4:].max()) * 1.002
                                        if ema_26_1h:
                                            sl = max(sl, ema_26_1h + (0.25 * atr_1h))
                                        # Max Risk Clamp: Prevent SL from exceeding 2.5% on 10x leverage
                                        if entry and sl and (sl - entry) > (candle_close * 0.025):
                                            sl = round_price(symbol, entry + (candle_close * 0.025))
                                        risk = abs(entry - sl) if (entry and sl and entry != sl) else (candle_close * 0.015)
                                        tp1 = round_price(symbol, entry - (target_r * risk))
                                        tp2 = round_price(symbol, entry - (3.2 * risk))
                                        cross_type = "🩸 1H Waterfall Breakdown (Momentum Rush + CVD ✅)" if is_waterfall_rush else ("🩸 1H Range Breakdown (Volume Expansion + CVD ✅)" if not has_radar else "🩸 1H Long-Squeeze Breakdown (Retest Bounce + CVD ✅)")

                                if sq_signal:
                                    dyn_sl = sl
                                    rr1 = target_r
                                    rr2 = 2.2 if is_trap_reversal else 3.2
                                    lbl1 = f"{target_r}R Target"
                                    lbl2 = f"{rr2}R Extended"

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
            except Exception as e_sq_eng:
                logger.debug(f"Squeeze engine error for {symbol}: {e_sq_eng}")

        # -- 1. EMA CROSSOVER ENGINE --
        # BUG #1 FIX: Collect ALL crossover candidates from ALL timeframes (not just the first one).
        # Previously `return cand` was inside the loop causing all 4H/1D signals to be silently dropped.
        all_crossover_candidates = []
        if sq_candidate:
            all_crossover_candidates.append(sq_candidate)

        for label in ["5M", "15M", "30M", "1H", "4H", "1D", "1W"]:
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

async def scan_single_symbol_async(session, symbol, semaphore):
    try:
        tf_data = await fetch_all_timeframes_async(session, symbol, semaphore)
        if not tf_data:
            return None

        from indicators import get_trend_state
        from telegram_bot import TF_ORDER
        tf_states = {l: get_trend_state(tf_data.get(l)) for l in TF_ORDER}
        clean_sym = symbol.upper()
        is_super = any(m in clean_sym for m in ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "XAU", "GOLD"])

        # ── STAGE 1: WATCHLIST COILING CANDIDATE EVALUATION (1H TF & Above per User Directive) ──
        radar_cand = None
        try:
            from config import is_weekend_trading_blocked
            if not is_weekend_trading_blocked(symbol):
                df_1h = tf_data.get("1H") if tf_data.get("1H") is not None else tf_data.get("1h")
                if df_1h is not None and len(df_1h) >= 25:
                    from state import is_radar_on_cooldown
                    if not is_radar_on_cooldown(symbol, cooldown_hours=4.0):
                        from fetcher import fetch_futures_microstructure_async
                        micro_radar = await fetch_futures_microstructure_async(session, symbol, signal=None, df=df_1h)
                        from indicators import evaluate_watchlist_coiling_candidate
                        eval_res = evaluate_watchlist_coiling_candidate(symbol, df_1h, micro_radar, tf_states, tf_label="1H")
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
        is_retest_eligible = ((symbol in retest_allowed_coins) if is_weekend else True) and not is_weekend_trading_blocked(symbol)

        if is_retest_eligible:
            # Run EMA Support/Resistance Retest Check
            from pullback_tracker import check_symbol_pullbacks
            retest_cand = await check_symbol_pullbacks(symbol, tf_data, session=session)

        # 2. Run Multi-Crossover & Breakout Check (Fresh Crossovers)
        # BUG #1 FIX: process_symbol_async now returns a LIST of candidates (all TF crossovers)
        crossover_cands = await process_symbol_async(session, symbol, semaphore, tf_data=tf_data)

        # Build unified trades list: crossovers (all TFs) + retest candidate
        trade_cands = []
        if isinstance(crossover_cands, list):
            trade_cands.extend(crossover_cands)
        elif isinstance(crossover_cands, dict):
            trade_cands.append(crossover_cands)  # Backward compat
        if isinstance(retest_cand, dict):
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

async def notify_signal_rejection(symbol: str, signal: str, reason: str, cand: dict = None):
    """Notifies the user immediately via Telegram when a signal is rejected/filtered from Shark execution."""
    cand = cand or {}
    uniq_k = cand.get("unique_key") or "signal"
    c_ts = str(cand.get("candle_ts") or "")
    stars = cand.get("aligned_count", 0)
    rsi_val = cand.get("rsi")
    entry_p = cand.get("entry")
    
    dedup_key = f"{symbol}_{signal}_{uniq_k}_{c_ts}_{reason[:25]}"
    now_ts = time.time()
    
    # Prune old cache (> 2 hours)
    for k, ts in list(_rejection_notified_cache.items()):
        if now_ts - ts > 7200:
            _rejection_notified_cache.pop(k, None)
            
    if dedup_key in _rejection_notified_cache and (now_ts - _rejection_notified_cache[dedup_key] < 1800):
        return  # Deduplicated within 30 minutes
        
    _rejection_notified_cache[dedup_key] = now_ts
    
    stars_str = f"{stars}/4 Stars" if stars > 0 else "Signal"
    rsi_str = f" | 📊 <b>RSI:</b> <code>{float(rsi_val):.1f}</code>" if rsi_val is not None else ""
    funding_val = cand.get("funding_pct")
    funding_str = f" | ⚡ <b>Funding:</b> <code>{funding_val:+.4f}%</code>" if funding_val is not None else ""
    entry_str = f"\n🎯 <b>Signal Entry:</b> <code>{entry_p}</code>" if entry_p else ""
    
    score_val = cand.get("quality_score") or (cand.get("analysis") or {}).get("score")
    score_verdict = (cand.get("analysis") or {}).get("verdict")
    comps = (cand.get("analysis") or {}).get("components") or {}
    
    score_block = ""
    if score_val is not None:
        comp_lines = ""
        if comps:
            comp_lines = (
                f"\n📊 <b>Score Breakdown:</b>\n"
                f"  • HTF Trend: <code>{comps.get('htf', 0)}/20</code> | RSI: <code>{comps.get('rsi', 0)}/20</code>\n"
                f"  • Volume Surge: <code>{comps.get('volume', 0)}/15</code> | ADX Trend: <code>{comps.get('adx', 0)}/15</code>\n"
                f"  • MACD: <code>{comps.get('macd', 0)}/10</code> | Candle Body: <code>{comps.get('body', 0)}/10</code>\n"
                f"  • Session: <code>{comps.get('session', 0)}/10</code>"
            )
        score_block = f"\n🧠 <b>AI Quality Score:</b> <code>{score_val}/100</code> ({score_verdict}){comp_lines}"

    msg = (
        f"🛡️ <b>SHARK AUTO-TRADE FILTER NOTICE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>Pair:</b> <code>{symbol}</code> [<b>{signal.upper()}</b>]\n"
        f"⭐ <b>Confluence:</b> {stars_str}{rsi_str}{funding_str}{entry_str}{score_block}\n"
        f"🚫 <b>Execution Status:</b> Order Suppressed by Shield\n"
        f"📋 <b>Exact Reason:</b> {reason}\n\n"
        f"💡 <i>Low-probability or weak score setup filtered out to prevent unnecessary SL loss.</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        from telegram_bot import send_trading_update
        await send_trading_update(msg)
        logger.info(f"Dispatched trade rejection notice for {symbol} [{signal}]: {reason}")
    except Exception as e_ntf:
        logger.error(f"Error dispatching rejection notice: {e_ntf}")

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
                last_lookout_time = time.time()
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
                        if now_t - last_lookout_time >= 3600:
                            try:
                                wallet = load_wallet()
                                if wallet.get("active_positions") or wallet.get("resting_orders"):
                                    card = await generate_strategy_lookout_card(session)
                                    await notify_telegram(card)
                                    logger.info("Dispatched regular hourly paper trading strategy lookout update.")
                                last_lookout_time = now_t
                            except Exception as e_look:
                                logger.error(f"Error sending regular paper lookout update: {e_look}")
                    else:
                        # REAL LIVE TRADING: Regular Automated Hourly Position Tracking Lookout Update
                        now_t = time.time()
                        if now_t - last_lookout_time >= 3600:
                            try:
                                if executor.is_configured():
                                    import zoneinfo
                                    ist_h = zoneinfo.ZoneInfo("Asia/Kolkata")
                                    time_str_h = datetime.now(ist_h).strftime("%d %b %Y, %I:%M %p IST")
                                    shark_bal = await executor.get_wallet_balance_summary(session)
                                    shark_pos = await executor.get_open_positions(session)
                                    open_orders = await executor.get_open_orders(session)

                                    pos_lines = []
                                    from web_dashboard import fetch_live_market_price
                                    for p in shark_pos:
                                        p_sym = executor.clean_sym(p)
                                        pos_dir = str(p.get("positionType") or p.get("positionSide") or p.get("side") or "LONG").upper()
                                        p_side = "SHORT" if pos_dir in ("SHORT", "SELL") else "LONG"
                                        p_sz = abs(float(p.get("quantity") or p.get("positionAmount") or 0))
                                        p_entry = float(p.get("entryPrice", 0) or p.get("avgPrice", 0) or 0)
                                        
                                        # Live sub-second market price with multi-exchange fallback
                                        p_mark = await fetch_live_market_price(session, p_sym)
                                        if p_mark <= 0:
                                            p_mark = float(p.get("markPrice", 0) or p.get("lastPrice", 0) or p_entry)

                                        # Real-time Mathematical Floating PnL
                                        if p_side == "SHORT":
                                          p_upnl_usd = (
                                              (p_entry - p_mark) * p_sz
                                              if p_entry > 0
                                              else 0.0
                                          )
                                          p_pct = (
                                              (
                                                  (p_entry - p_mark)
                                                  / p_entry
                                                  * 100.0
                                                  * 10
                                              )
                                              if p_entry > 0
                                              else 0.0
                                          )
                                        else:
                                          p_upnl_usd = (
                                              (p_mark - p_entry) * p_sz
                                              if p_entry > 0
                                              else 0.0
                                          )
                                          p_pct = (
                                              (
                                                  (p_mark - p_entry)
                                                  / p_entry
                                                  * 100.0
                                                  * 10
                                              )
                                              if p_entry > 0
                                              else 0.0
                                          )

                                        p_upnl_inr = (
                                            p_upnl_usd * executor.usd_inr_rate
                                        )
                                        pnl_sign = (
                                            "+" if p_upnl_inr >= 0 else ""
                                        )
                                        pct_sign = "+" if p_pct >= 0 else ""
                                        p_entry_disp = format_price(
                                            p_entry, p_sym
                                        )
                                        p_mark_disp = format_price(
                                            p_mark, p_sym
                                        )
                                        pos_lines.append(
                                            f"  • <b>{p_sym}</b> [{p_side}] Qty:"
                                            f" {p_sz} | Entry: {p_entry_disp} |"
                                            f" Mark: {p_mark_disp} | Floating:"
                                            f" <b>{pnl_sign}₹{p_upnl_inr:,.2f}</b>"
                                            f" ({pct_sign}{p_pct:.2f}%)"
                                        )

                                    pos_str = (
                                        "\n".join(pos_lines)
                                        if pos_lines
                                        else (
                                            "  • <i>No open exchange positions"
                                            " currently.</i>"
                                        )
                                    )

                                    ord_lines = []
                                    for o in open_orders:
                                      if (
                                          not o.get("reduceOnly")
                                          and o.get("placeType") != "POSITION"
                                      ):
                                        o_sym = executor.clean_sym(o)
                                        o_side = str(
                                            o.get("side", "BUY")
                                        ).upper()
                                        o_px = float(o.get("price", 0) or 0)
                                        o_qty = float(
                                            o.get("orderAmount")
                                            or o.get("quantity")
                                            or o.get("origQty")
                                            or 0
                                        )
                                        ord_lines.append(
                                            f"  • <b>{o_sym}</b> [{o_side}]"
                                            f" Limit @ {format_price(o_px, o_sym)} (Qty: {o_qty})"
                                        )

                                    ord_str = "\n".join(ord_lines[:8]) if ord_lines else "  • <i>No pending entry orders resting on book.</i>"

                                    live_lookout_card = (
                                        f"🦈 <b>REAL LIVE TRADING: HOURLY POSITION LOOKOUT</b>\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"⏰ <b>Time:</b> {time_str_h}\n"
                                        f"💼 <b>Status:</b> 🟢 <b>Real Live Trading Active</b> (Shark Exchange)\n\n"
                                        f"{shark_bal}\n\n"
                                        f"🟢 <b>Active Positions ({len(shark_pos)}):</b>\n"
                                        f"{pos_str}\n\n"
                                        f"⏳ <b>Resting Entry Orders ({len(ord_lines)}):</b>\n"
                                        f"{ord_str}\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"<i>Automated Hourly Position Tracker • 100% Target Engine Active</i>"
                                    )
                                    from telegram_bot import send_text_alert
                                    await send_text_alert(live_lookout_card)
                                    logger.info("Dispatched regular hourly real live trading lookout update to Telegram.")
                                last_lookout_time = now_t
                            except Exception as e_live_look:
                                logger.error(f"Error sending regular live lookout update: {e_live_look}")

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
                    is_night_pause = (now_ist.hour == 0 and now_ist.minute >= 30) or (1 <= now_ist.hour < 5) or (now_ist.hour == 5 and now_ist.minute < 30)
                    
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
                            
                        # BTC Volatility Shock Shield Check (Protects crypto from whipsaws; commodities exempt per user directive)
                        is_shock_active, shock_rem = await check_btc_volatility_shock(session, semaphore)
                        if is_shock_active:
                            logger.warning(f"⚡ BTC Volatility Shock ACTIVE ({shock_rem:.1f}m remaining). Altcoins paused; commodities/stocks scanning normally...")

                        # DAILY RISK CIRCUIT BREAKER: 5 consecutive SLs -> 60-min pause
                        from state import is_trading_paused
                        is_cb_paused, cb_rem_secs, cb_reason = is_trading_paused()
                        if is_cb_paused:
                            logger.warning(f"⏸️ CIRCUIT BREAKER ACTIVE: 5 consecutive SLs hit ({cb_rem_secs/60:.1f}m remaining). Skipping scan cycle...")
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
                        
                        if is_shock_active:
                            from market_cache import is_commodity_symbol, is_stock_symbol
                            active_pairs = [sym for sym in active_pairs if is_commodity_symbol(sym) or is_stock_symbol(sym)]
                            if not active_pairs:
                                await asyncio.sleep(15)
                                continue

                        tasks = [scan_single_symbol_async(session, sym, semaphore) for sym in active_pairs]
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
                                # Dedicated 1H Squeeze Breakout: already passed institutional 6-point verification (TTM squeeze coiling + volume surge >= 1.5x + CVD aligned + body ratio >= 50% + wick cap <= 35% + BTC micro-gate)
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

                                # STRICT USER DIRECTIVE: Execute 3-Star (3/4) & 4-Star (4/4) setups WITH Verified RSI Boost!
                                if stars < 3:
                                    return False, f"Confluence is only {stars}/4 stars (Minimum 3-Star HTF alignment required for execution)."

                                # STRICT RSI MOMENTUM GATE (Same across Crypto & Commodities per user directive):
                                # BUY: 42.0 <= RSI <= 62.0 (filters late overbought tops > 62)
                                # SELL: 38.0 <= RSI <= 58.0 (filters late oversold bottoms < 38)
                                if rsi is not None:
                                    try:
                                        f_rsi = float(rsi)
                                        if sig == "BUY" and not (42.0 <= f_rsi <= 62.0):
                                            return False, f"RSI {f_rsi:.1f} is outside healthy BUY zone [42.0 - 62.0] (Overbought peak/exhausted > 62.0)."
                                        elif sig == "SELL" and not (38.0 <= f_rsi <= 58.0):
                                            return False, f"RSI {f_rsi:.1f} is outside healthy SELL zone [38.0 - 58.0] (Oversold bottom/exhausted < 38.0)."
                                    except (ValueError, TypeError):
                                        pass
                                # STRICT USER DIRECTIVE: Multi-Factor AI Quality Score >= 75 Hard Gate
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

                                if quality_score < 75:
                                    comps = analysis.get("components", {})
                                    comp_summary = (
                                        f"HTF:{comps.get('htf',0)}/20, RSI:{comps.get('rsi',0)}/20, "
                                        f"Vol:{comps.get('volume',0)}/15, ADX:{comps.get('adx',0)}/15, "
                                        f"MACD:{comps.get('macd',0)}/10, Body:{comps.get('body',0)}/10, Session:{comps.get('session',0)}/10"
                                    )
                                    return False, f"Multi-Factor Score is {quality_score}/100 (< 75 required | {verdict} | Breakdown: {comp_summary})"

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

                                return True, f"Eligible (3★+ Confluence, RSI Healthy, Score: {quality_score}/100, Funding Clean)"

                            eligible_candidates = []
                            for c in raw_candidates:
                                is_ok, reason = await _check_auto_trade_eligibility(c)
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
                                    sq_boost = 1.0 if c.get("is_dedicated_squeeze") else 0.0
                                    signal_score = stars + early + sq_boost
                                    rr_score = c.get("rr3") or c.get("rr2") or 1.5
                                    return (tier, signal_score, rr_score)

                                eligible_candidates.sort(key=_coin_tier_rank, reverse=True)
                                
                                # USER DIRECTIVE: Portfolio cap removed completely — execute ALL eligible setups without limit
                                top_candidates = eligible_candidates
                                logger.info(f"🏆 ALL {len(top_candidates)} ELIGIBLE SETUP(S) SELECTED FOR SHARK EXECUTION (No portfolio cap).")
                                for cand in top_candidates:
                                    sym = cand["symbol"]
                                    sig = cand["signal"]
                                    stars = cand.get("aligned_count", 0)

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

                                    # USER DIRECTIVE: Max 5 Concurrent Same-Direction Trades Shield (Max 5 LONG, Max 5 SHORT)
                                    # Allows up to 5 concurrent LONGs and 5 concurrent SHORTs simultaneously.
                                    if not is_same_dir_continuation:
                                        all_shark_pos = await executor.get_open_positions(session)
                                        cand_dir = "BUY" if str(sig).upper() in ("BUY", "LONG") else "SELL"
                                        cand_dir_label = "LONG" if cand_dir == "BUY" else "SHORT"
                                        same_dir_count = 0
                                        active_same_syms = []
                                        for p_check in all_shark_pos:
                                            p_q = abs(float(p_check.get("quantity") or p_check.get("positionAmount") or 0))
                                            if p_q > 0:
                                                p_dr = str(p_check.get("positionType") or p_check.get("positionSide") or p_check.get("side") or "").upper()
                                                p_d = "BUY" if p_dr in ("LONG", "BUY") else "SELL"
                                                if p_d == cand_dir:
                                                    same_dir_count += 1
                                                    active_same_syms.append(executor.clean_sym(p_check))
                                        if same_dir_count >= 5:
                                            basket_reason = f"Directional Limit Reached (Already {same_dir_count} active {cand_dir_label} positions: {', '.join(active_same_syms)}). Max 5 {cand_dir_label} trades allowed at a time."
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
                                    
                                    shark_res = await executor.execute_signal_trade(
                                        session, sym, sig, cand["entry"], cand["sl"], 
                                        cand["tp1"], cand["tp2"],
                                        aligned_count=stars,
                                        adx=cand.get("adx")
                                    )
                                    
                                    if shark_res and shark_res.get("status") == "success":
                                        from state import update_trade_progress, _make_key
                                        state_key = _make_key(sym, cand["unique_key"], sig, cand["candle_ts"])
                                        t_qty = shark_res.get("total_qty", 0)
                                        update_trade_progress(state_key, shark_order_placed=True, total_qty=t_qty)
                                        logger.info(f"Marked shark_order_placed=True for {state_key}")
                                    elif shark_res and shark_res.get("status") == "trailed":
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
                            # Re-verify not on symbol cooldown
                            eligible_radars = [c for c in radar_candidates if not is_radar_on_cooldown(c["symbol"], cooldown_hours=4.0)]
                            if eligible_radars and not is_global_radar_on_cooldown(cooldown_minutes=10.0):
                                # Rank by Coiling Quality Score and select ONLY the #1 single best coiled setup across the market
                                eligible_radars.sort(key=lambda x: x.get("coiling_score", 0.0), reverse=True)
                                top_radar = eligible_radars[0]

                                from telegram_bot import send_pre_squeeze_radar_alert
                                await send_pre_squeeze_radar_alert(top_radar)
                                record_global_radar_sent()
                                record_pre_squeeze_radar(top_radar["symbol"], top_radar)
                                logger.info(f"📡 DISPATCHED TOP #1 WATCHLIST COILING ALERT: {top_radar['symbol']} ({top_radar['direction']}) | Score: {top_radar.get('coiling_score')}")
                            else:
                                # Keep track of active radars in state silently for Stage 2 breakout execution
                                for cand in eligible_radars:
                                    record_pre_squeeze_radar(cand["symbol"], cand)
                        
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
