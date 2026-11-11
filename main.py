import pandas as pd
import asyncio
import aiohttp
import logging
import os
import time
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
    
    # 1. Resolve pending trades & paper wallet
    from paper_trader import evaluate_paper_trades_async, load_wallet
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
    
    logger.info(f"📊 RECONCILIATION AUDIT:")
    logger.info(f"   • Active Positions: {active_before} -> {active_after}")
    logger.info(f"   • Resting Orders:   {resting_before} -> {resting_after}")
    logger.info(f"   • Offline Resolved: {newly_closed} closed trades updated")
    logger.info(f"   • Current Balance:  ₹{w_after.get('balance', 0):,.2f} INR")
    
    # Send one clean Telegram admin notification about the reconciliation
    try:
        from telegram_bot import send_text_to_chat
        from config import TELEGRAM_CHAT_ID
        admin_chat = TELEGRAM_CHAT_ID or "771305593"
        recon_msg = (
            "🔄 <b>BOT RE-ACTIVATED & RECONCILED</b>\n\n"
            f"<b>Offline Audit Results:</b>\n"
            f"• Active Positions: <b>{active_after}</b>\n"
            f"• Closed/Expired while offline: <b>{newly_closed}</b>\n"
            f"• Live Paper Balance: <b>₹{w_after.get('balance', 0):,.2f} INR</b>\n\n"
            "⏳ <i>Warming up feeds... Fresh live market scan starting in a few seconds (Deduplication Guard active).</i>"
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
    # Always evaluate virtual paper trading positions (₹1,00,000 INR starting capital)
    try:
        from paper_trader import evaluate_paper_trades_async
        await evaluate_paper_trades_async(session, semaphore)
    except Exception as e:
        logger.error(f"Error in evaluate_paper_trades_async: {e}")

    pending = get_pending_trades()
    if not pending:
        return
        
    for key, data in pending.items():
        try:
            parts = key.split("|")
            if len(parts) < 4: continue
            symbol = parts[0]
            direction = parts[2]
            
            entry = data.get("entry")
            sl = data.get("sl")
            initial_sl = data.get("initial_sl") or sl
            tp = data.get("tp")
            tp1 = data.get("tp1") or tp
            tp2 = data.get("tp2")
            tp3 = data.get("tp3") or tp
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
            raw_tp2 = float(data.get("tp2")) if data.get("tp2") is not None else None
            raw_tp3 = float(data.get("tp3")) if data.get("tp3") is not None else None

            if direction == "BUY":
                tp1 = raw_tp1 if (raw_tp1 and raw_tp1 > entry) else (entry + 1.0 * risk)
                tp2 = raw_tp2 if (raw_tp2 and raw_tp2 > tp1) else max(tp1 * 1.003, entry + 1.6 * risk)
                tp3 = raw_tp3 if (raw_tp3 and raw_tp3 > tp2) else max(tp2 * 1.003, entry + 2.5 * risk)
            else:
                tp1 = raw_tp1 if (raw_tp1 and raw_tp1 < entry) else (entry - 1.0 * risk)
                tp2 = raw_tp2 if (raw_tp2 and raw_tp2 < tp1) else min(tp1 * 0.997, entry - 1.6 * risk)
                tp3 = raw_tp3 if (raw_tp3 and raw_tp3 < tp2) else min(tp2 * 0.997, entry - 2.5 * risk)

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
                        if tp1 and tp2 and tp3:
                            from executor import executor as _exec_tp
                            await _exec_tp.place_tp_limits(session, symbol, direction, entry, tp1, tp2, tp3, initial_sl=initial_sl)

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
                            await executor.place_tp_limits(session, symbol, direction, entry, tp1, tp2, tp3, initial_sl=initial_sl)
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

                # Rule 2.5-Hour Time Shield (If active >2h 30m without hitting TP1, shift SL to Entry)
                time_shield_hit = data.get("be_hit_time_shield", False)
                if limit_filled and fill_dt and not tp1_hit and not time_shield_hit:
                    elapsed_since_fill = candle_dt - fill_dt
                    if elapsed_since_fill >= timedelta(hours=2):
                        time_shield_hit = True
                        update_trade_progress(key, be_hit_time_shield=True, new_sl=entry)

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

                    # 3. Check TP2 Hit (30% profit booked -> move SL to TP1)
                    if tp1_hit and not tp2_hit and row['high'] >= tp2:
                        tp2_hit = True
                        tp2_hit_at_dt = candle_dt
                        tp2_hit_at_str = tp2_hit_at_dt.isoformat()
                        update_trade_progress(key, tp2_hit=True, new_sl=tp1, tp2_hit_at=tp2_hit_at_str)
                        cur_sl = tp1

                    # 4. Check TP3 Hit (Final Moonbag)
                    if tp2_hit and row['high'] >= tp3:
                        outcome = "WIN"
                        final_price = tp3
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

                    # 3. Check TP2 Hit
                    if tp1_hit and not tp2_hit and row['low'] <= tp2:
                        tp2_hit = True
                        tp2_hit_at_dt = candle_dt
                        tp2_hit_at_str = tp2_hit_at_dt.isoformat()
                        update_trade_progress(key, tp2_hit=True, new_sl=tp1, tp2_hit_at=tp2_hit_at_str)
                        cur_sl = tp1

                    # 4. Check TP3 Hit
                    if tp2_hit and row['low'] <= tp3:
                        outcome = "WIN"
                        final_price = tp3
                        break

            if not limit_filled and now_utc > limit_cutoff:
                outcome = "CANCELLED_UNFILLED"
                final_price = entry
                        
            if outcome:
                update_trade_outcome(key, outcome)
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
                    from state import get_active_radar, mark_radar_triggered
                    from fetcher import fetch_futures_microstructure_async
                    radar_entry = get_active_radar(symbol)
                    has_radar = radar_entry and radar_entry.get("status") == "WATCHING"
                    
                    # STRICT SQUEEZE RULE (User Directive):
                    # Squeeze breakout requires an active verified coiling base on Radar!
                    # Do not fire random uncoiled coins in an uptrend/downtrend.
                    if has_radar:
                        recent_12 = df_1h.iloc[-14:-2] if len(df_1h) >= 14 else df_1h.iloc[:-2]
                        range_high = float(recent_12['high'].max())
                        range_low = float(recent_12['low'].min())
                        candle_close = float(df_1h['close'].iloc[-2])
                        candle_ts = str(df_1h.index[-2])
                        
                        radar_trigger = float(radar_entry.get('trigger_entry', 0.0))
                        radar_dir = str(radar_entry.get('direction', '')).upper()
                        
                        sq_signal = None
                        # SOLUTION 3 - RULE 1: STRONG CANDLE BODY ACCEPTANCE (No Wicks, >= 0.20% clean clearance)
                        if radar_dir == 'BUY' and candle_close >= (radar_trigger or (range_high * 1.002)):
                            sq_signal = 'BUY'
                        elif radar_dir == 'SELL' and candle_close <= (radar_trigger or (range_low * 0.998)):
                            sq_signal = 'SELL'
                        
                        if sq_signal:
                            c_open = float(df_1h['open'].iloc[-2])
                            c_high = float(df_1h['high'].iloc[-2])
                            c_low = float(df_1h['low'].iloc[-2])
                            c_close = float(df_1h['close'].iloc[-2])
                            c_body = abs(c_close - c_open)
                            c_range = max(c_high - c_low, 1e-8)
                            body_ratio = c_body / c_range

                            # Solution 3 Rule 1: Body Ratio >= 50% (Solid candle, no long opposing wicks)
                            vol_20_sma = df_1h['volume'].iloc[-22:-2].mean() if len(df_1h) >= 22 else df_1h['volume'].mean()
                            breakout_vol = float(df_1h['volume'].iloc[-2])
                            
                            from market_cache import is_btc_safe_for_altcoins
                            is_safe, btc_reason = is_btc_safe_for_altcoins(sq_signal, symbol, tf_data)

                            if body_ratio < 0.50:
                                logger.info(f"FILTERED 1H SQUEEZE: {symbol} rejected ? Candle Body Ratio {body_ratio:.1%} < 50% (Weak expansion / long wick trap).")
                                sq_signal = None
                            elif sq_signal == 'BUY' and ((c_high - c_close) / c_range) > 0.35:
                                logger.info(f"FILTERED 1H SQUEEZE: {symbol} BUY rejected ? Upper rejection wick > 35% (Shooting star / whale trap).")
                                sq_signal = None
                            elif sq_signal == 'SELL' and ((c_close - c_low) / c_range) > 0.35:
                                logger.info(f"FILTERED 1H SQUEEZE: {symbol} SELL rejected ? Lower rejection wick > 35% (Hammer / bear trap).")
                                sq_signal = None
                            elif vol_20_sma > 0 and breakout_vol < (1.50 * vol_20_sma):
                                logger.info(f"FILTERED 1H SQUEEZE: {symbol} rejected ? Volume {breakout_vol:.0f} < 1.5x 20-SMA {vol_20_sma:.0f} (Whale volume surge missing).")
                                sq_signal = None
                            elif not is_safe:
                                logger.info(f"FILTERED 1H SQUEEZE: {symbol} {sq_signal} rejected by BTC Micro-Gate ({btc_reason}).")
                                sq_signal = None
                            # Breakout Freshness Guard: Reject historical breakouts older than 65 minutes
                            if sq_signal and candle_ts:
                                c_dt = _parse_iso_dt(candle_ts)
                                if c_dt and (datetime.now(timezone.utc) - c_dt).total_seconds() > 3900:
                                    logger.info(f"FILTERED STALE SQUEEZE: {symbol} 1H breakout candle was {(datetime.now(timezone.utc) - c_dt).total_seconds()/60:.1f}m ago while bot was offline.")
                                    sq_signal = None

                            unique_sq_key = f"1H_SqueezeBreakout_{sq_signal}_{candle_ts}"
                            if sq_signal and not is_already_sent(symbol, unique_sq_key, sq_signal, candle_ts):
                                micro_1h = await fetch_futures_microstructure_async(session, symbol, sq_signal, df=df_1h)
                                cvd_metrics = micro_1h.get("cvd_metrics", {})
                                cvd_aligned = cvd_metrics.get("is_aligned", True)
                                
                                if cvd_aligned:
                                    # UPGRADE 2 (Ike Trading Rule): Breakout Level Retest / Pullback Entry Engine
                                    # Instead of chasing the candle_close top, anchor limit entry near broken range boundary
                                    if sq_signal == 'BUY':
                                        retest_target = range_high * 1.0015
                                        entry = min(candle_close, retest_target) if candle_close < range_high * 1.004 else retest_target
                                        sl = float(df_1h['low'].iloc[-4:].min()) * 0.998
                                        cross_type = "🚀 1H Short-Squeeze Breakout (Retest Bounce + CVD ✅)"
                                    else:
                                        retest_target = range_low * 0.9985
                                        entry = max(candle_close, retest_target) if candle_close > range_low * 0.996 else retest_target
                                        sl = float(df_1h['high'].iloc[-4:].max()) * 1.002
                                        cross_type = "🩸 1H Long-Squeeze Breakdown (Retest Bounce + CVD ✅)"

                                    from indicators import find_liquidity_pool_targets, _compute_indicators
                                    dyn_sl, (tp1, tp2, tp3), (rr1, rr2, rr3), (lbl1, lbl2, lbl3) = find_liquidity_pool_targets(df_1h, sq_signal, entry, sl, symbol=symbol, tf_data=tf_data)
                                    
                                    df_1h_ind = _compute_indicators(df_1h)
                                    sq_rsi = float(df_1h_ind['rsi'].iloc[-2]) if 'rsi' in df_1h_ind and not pd.isna(df_1h_ind['rsi'].iloc[-2]) else 50.0

                                    from telegram_bot import queue_alert
                                    await queue_alert(
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
                                        tp1=tp1, tp2=tp2, tp3=tp3,
                                        rr1=rr1, rr2=rr2, rr3=rr3,
                                        lbl1=lbl1, lbl2=lbl2, lbl3=lbl3,
                                        tf_data=tf_data,
                                        is_special_list=(symbol in SPECIAL_LIST),
                                        rsi=sq_rsi,
                                        is_dedicated_squeeze=True
                                    )
                                    mark_sent(symbol, unique_sq_key, sq_signal, candle_ts, entry=entry, sl=dyn_sl, tp=tp1, tp1=tp1, tp2=tp2, tp3=tp3)
                                    mark_radar_triggered(symbol)
                                    logger.info(f"💥 DEDICATED 1H SQUEEZE BREAKOUT TRIGGERED for {symbol} ({sq_signal}) @ {entry}")
            except Exception as e_sq_eng:
                logger.debug(f"Squeeze engine error for {symbol}: {e_sq_eng}")

        # -- 1. EMA CROSSOVER ENGINE --
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
                    "BCH/USDT", "XAU/USD", "XAUT/USDT", "PAXG/USDT"
                }
                MAJOR_PREFIXES = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI", "NEAR", "DOT", "LTC", "UNI", "AAVE", "BCH", "XAUT", "PAXG"]
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
                tp3 = cross.get("tp3")
                rr1 = cross.get("rr1")
                rr2 = cross.get("rr2")
                rr3 = cross.get("rr3")
                lbl1 = cross.get("lbl1")
                lbl2 = cross.get("lbl2")
                lbl3 = cross.get("lbl3")
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

                logger.info(f"NEW SIGNAL: {signal} {symbol} {label} ({cross_type}) | entry={entry} sl={sl} tp1={tp1} tp2={tp2} tp3={tp3}")
                from pullback_tracker import SPECIAL_LIST
                is_special = symbol in SPECIAL_LIST
                # Pass candle_time_utc (candle open time in UTC) to queue_alert so calculate_candle_close_time calculates the exact close time accurately
                open_time_iso = candle_time_utc.isoformat()
                
                # Queue alert for Telegram
                from telegram_bot import queue_alert
                await queue_alert(symbol, label, signal, entry, sl, tp, tf_states, cross_type, last_1h_cross_time, candle_time=open_time_iso, macro_struct=macro_struct, rr=rr, tp_label=tp_label, tf_data=tf_data, tp1=tp1, tp2=tp2, tp3=tp3, rr1=rr1, rr2=rr2, rr3=rr3, lbl1=lbl1, lbl2=lbl2, lbl3=lbl3, is_special_list=is_special, rsi=rsi, adx=cross.get("adx"))
                
                is_3_htf_aligned = (
                    tf_states.get("1H", "").lower() == target_trend and
                    tf_states.get("4H", "").lower() == target_trend and
                    tf_states.get("1D", "").lower() == target_trend
                )
                aligned_count = sum(1 for t in ["1H", "4H", "1D", "1W"] if tf_states.get(t, "neutral").lower() == target_trend)
                is_30m_early = "30M Early Cross" in cross_type

                mark_sent(symbol, unique_key, signal, candle_ts, entry=entry, sl=sl, tp=tp, tp1=tp1, tp2=tp2, tp3=tp3)
                from state import set_coin_cooldown
                set_coin_cooldown(symbol, tf=label)

                cand = {
                    "symbol": symbol,
                    "signal": signal,
                    "entry": entry,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp3": tp3,
                    "tp": tp,
                    "aligned_count": aligned_count,
                    "is_3_htf_aligned": is_3_htf_aligned,
                    "is_30m_early": is_30m_early,
                    "is_special": is_special,
                    "unique_key": unique_key,
                    "candle_ts": candle_ts,
                    "rr3": rr3 or 1.5,
                    "adx": cross.get("adx"),
                    "rsi": rsi,
                }
                return cand
                
        return None
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
        crossover_cand = await process_symbol_async(session, symbol, semaphore, tf_data=tf_data)

        # Prioritize crossover candidate if both exist, else retest candidate
        trade_cand = crossover_cand if isinstance(crossover_cand, dict) else (retest_cand if isinstance(retest_cand, dict) else None)
        return {
            "trade": trade_cand,
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
    

    # Start Render Web Health-Check server if PORT environment variable is provided (Render Web Service compatibility)
    port_env = os.getenv('PORT')
    if port_env:
        try:
            from aiohttp import web
            async def health_handler(request):
                return web.Response(text="AlphaCryptoSignals EMA Bot is RUNNING 24/7 \u2705\n")
            web_app = web.Application()
            web_app.router.add_get("/", health_handler)
            web_app.router.add_get("/health", health_handler)
            runner = web.AppRunner(web_app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", int(port_env))
            await site.start()
            logger.info(f"Render Web Service health-check server started on port {port_env}!")
        except Exception as e_port:
            logger.warning(f"Could not bind to PORT {port_env}: {e_port}")

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
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=30, ttl_dns_cache=300)
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

                    await asyncio.sleep(5)

            asyncio.create_task(tp_monitor_worker())
            from message_sync import start_deletion_sync_watcher
            asyncio.create_task(start_deletion_sync_watcher())

            last_dashboard_update_time = 0
            daily_digest_sent_today = False

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
                            
                        # BTC Volatility Shock Shield Check
                        is_shock_active, shock_rem = await check_btc_volatility_shock(session, semaphore)
                        if is_shock_active:
                            logger.warning(f"⚡ BTC Volatility Shock ACTIVE ({shock_rem:.1f}m remaining). Skipping scan cycle...")
                            await asyncio.sleep(15)
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
                            METALS_LIST = []
                            US_STOCKS_LIST = []
                            # Exclude all commodities and US stocks from active scan on weekends (BTC and ETH are active 24/7)
                            weekend_blocked_assets = set(COMMODITIES + US_EQUITIES + list(stock_symbols_set) + ["XAUT/USDT", "XAG/USDT", "NATGAS/USDT", "CL/USDT"])
                        else:
                            METALS_LIST = ["XAU/USD"]  # Gold Spot only on weekdays
                            # US Equity Tokens regular trading hours (19:00 to 01:30 IST, Mon-Fri):
                            # Outside US market hours, tokenized US equities are closed/flatline in crypto, so skip scanning them
                            is_us_open = (now_ist.weekday() < 5) and (now_ist.hour >= 19 or now_ist.hour == 0 or (now_ist.hour == 1 and now_ist.minute <= 30))
                            US_STOCKS_LIST = [
                                "NVDAB/USDT", "TSLAB/USDT", "MSTRB/USDT", "AAPLB/USDT",
                                "AMZNB/USDT", "METAB/USDT", "MSFTB/USDT", "GOOGLB/USDT",
                                "SPYB/USDT", "QQQB/USDT", "COIN/USDT", "AMD/USDT"
                            ] if is_us_open else []
                            weekend_blocked_assets = set()
                        
                        # ── USER-DEFINED WEEKDAY vs WEEKEND SCANNING RULES ──
                        # 1. Mon - Fri: ONLY Top 25 Crypto coins + Commodities (4) + US Equity Tokens (15).
                        # 2. Sat - Sun (Weekend): ALL Binance Futures coins EXCEPT BTC, ETH, Commodities, and US Equity Tokens.
                        all_futures_pairs = get_top_pairs_by_volume(top_n=None)
                        if not all_futures_pairs: all_futures_pairs = []

                        commodities_set = {"XAU/USDT", "XAUT/USDT", "XAG/USDT", "NATGAS/USDT", "CL/USDT", "OIL/USDT"}

                        if not is_weekend:
                            # Mon - Fri: ONLY Top 25 Crypto + Commodities (4) + US Equities (15)
                            top_25_crypto = [sym for sym in all_futures_pairs if sym not in stock_symbols_set and sym not in commodities_set and not any(sym.startswith(s) for s in ["XAU", "XAG", "NATGAS", "CL", "SOXL", "SOXS", "SKHY", "KORU", "SPCX"])][:25]
                            active_pairs = list(dict.fromkeys(top_25_crypto + METALS_LIST + US_STOCKS_LIST))
                        else:
                            # Sat - Sun: ALL Binance Futures + BTC & ETH (Commodities & US Equities remain CLOSED)
                            weekend_blocked = commodities_set.union(stock_symbols_set)
                            active_pairs = [
                                sym for sym in all_futures_pairs
                                if sym not in weekend_blocked
                                and not any(sym.startswith(s) for s in ["XAU", "XAG", "NATGAS", "CL", "OIL", "SOXL", "SOXS", "SKHY", "KORU", "SPCX", "SPY", "QQQ"])
                            ]
                            # Explicitly ensure BTC and ETH are scanned on weekends
                            for major in ["BTC/USDT", "ETH/USDT"]:
                                if major not in active_pairs:
                                    active_pairs.insert(0, major)
                        
                        tasks = [scan_single_symbol_async(session, sym, semaphore) for sym in active_pairs]
                        results = await asyncio.gather(*tasks)
                        
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
                                from paper_trader import send_daily_paper_trading_digest
                                await send_daily_paper_trading_digest()
                                daily_digest_sent_today = True
                            except Exception as e_dig:
                                logger.error(f"Error dispatching daily digest: {e_dig}")
                        if now_ist.hour != 22:
                            daily_digest_sent_today = False

                        # BULK SIGNAL BATCH PROCESSOR & PRIORITY SEQUENCER:
                        raw_candidates = [r["trade"] for r in results if isinstance(r, dict) and isinstance(r.get("trade"), dict)]
                        if raw_candidates:
                            from executor import executor
                            def _is_auto_trade_eligible(c):
                                stars = c.get("aligned_count", 0)
                                # STRICT USER DIRECTIVE: Execute 3-Star (3/4) & 4-Star (4/4) setups WITH Verified RSI Boost!
                                if stars < 3:
                                    return False
                                rsi = c.get("rsi")
                                sig = c.get("signal", "BUY").upper()
                                if rsi is not None:
                                    try:
                                        f_rsi = float(rsi)
                                        if sig == "BUY" and not (45.0 <= f_rsi <= 72.0):
                                            return False
                                        elif sig == "SELL" and not (28.0 <= f_rsi <= 55.0):
                                            return False
                                    except (ValueError, TypeError):
                                        pass
                                return True

                            eligible_candidates = [c for c in raw_candidates if _is_auto_trade_eligible(c)]
                            
                            if eligible_candidates and executor.is_configured():
                                from executor import clean_contract_symbol
                                TIER_1_SUPER_MAJORS = {"BTC", "ETH", "SOL", "BNB", "XAUT"}
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
                                    signal_score = stars + early
                                    rr_score = c.get("rr3") or 1.5
                                    return (tier, signal_score, rr_score)

                                eligible_candidates.sort(key=_coin_tier_rank, reverse=True)
                                
                                # Query available portfolio cap slots on exchange
                                positions = await executor.get_open_positions(session)
                                open_orders = await executor.get_open_orders(session)
                                active_syms = {executor.clean_sym(p) for p in positions}
                                for o in open_orders:
                                    if not o.get("reduceOnly") and o.get("placeType") != "POSITION":
                                        s = executor.clean_sym(o)
                                        if s: active_syms.add(s)

                                if executor.max_concurrent_trades and executor.max_concurrent_trades >= 999:
                                    available_slots = len(eligible_candidates)
                                else:
                                    available_slots = max(0, executor.max_concurrent_trades - len(active_syms))

                                if available_slots <= 0:
                                    logger.info(f"🚫 PORTFOLIO FULL ({len(active_syms)}/{executor.max_concurrent_trades} slots occupied). Skipping auto-execution for all {len(eligible_candidates)} candidate(s).")
                                else:
                                    # Select ONLY the top N best crossovers matching available slots (Max 4 Total)
                                    top_candidates = eligible_candidates[:available_slots]
                                    skipped_candidates = eligible_candidates[available_slots:]
                                    
                                    logger.info(f"🏆 TOP {len(top_candidates)} BEST CROSSOVER(S) SELECTED FOR SHARK EXECUTION (out of {len(eligible_candidates)} candidate(s)).")
                                    if skipped_candidates:
                                        skipped_names = [c["symbol"] for c in skipped_candidates]
                                        logger.info(f"ℹ️ Skipped {len(skipped_candidates)} lower-ranked candidate(s) for auto-trade: {skipped_names}")
                                    for cand in top_candidates:
                                        sym = cand["symbol"]
                                        sig = cand["signal"]
                                        stars = cand.get("aligned_count", 0)

                                        # Re-verify slot availability and duplicate check before submitting order
                                        if await executor.has_active_trade_on_exchange(session, sym):
                                            logger.info(f"Skipping {sym} — active trade/order exists or portfolio cap full.")
                                            continue

                                        logger.info(f"⚡ BULK TRADE EXECUTE [Top Pick]: {sym} [{sig}] ({stars}/4 Stars Priority)")
                                        
                                        shark_res = await executor.execute_signal_trade(
                                            session, sym, sig, cand["entry"], cand["sl"], 
                                            cand["tp1"], cand["tp2"], cand["tp3"],
                                            aligned_count=stars,
                                            adx=cand.get("adx")
                                        )
                                        
                                        if shark_res and shark_res.get("status") == "success":
                                            from state import update_trade_progress, _make_key
                                            state_key = _make_key(sym, cand["unique_key"], sig, cand["candle_ts"])
                                            t_qty = shark_res.get("total_qty", 0)
                                            update_trade_progress(state_key, shark_order_placed=True, total_qty=t_qty)
                                            logger.info(f"Marked shark_order_placed=True for {state_key}")
                                        
                                        # Mandatory 1.5-second sequential pause between bulk trade executions
                                        await asyncio.sleep(1.5)

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
