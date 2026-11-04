"""
paper_trader.py — Virtual Paper Trading Engine (₹1,00,000 INR Starting Capital).
Tracks resting limit orders, active positions, 2-stage TP/SL trailing brackets,
30-minute limit fill expiry, 20-hour position timeout at CTC, realized PnL,
wallet balance, and broadcasts Telegram notifications.
"""

import os
import json
import logging
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta

from config import (
    TELEGRAM_TRADING_CHANNEL_ID,
    TELEGRAM_CHAT_ID
)

logger = logging.getLogger("paper_trader")

WALLET_FILE = "/data/paper_wallet.json" if os.path.isdir("/data") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_wallet.json")
MARGIN_INR = 1500.0
LEVERAGE = 10
NOTIONAL_INR = MARGIN_INR * LEVERAGE  # Baseline fallback
RISK_PER_TRADE_PCT = 0.025            # 2.5% Account Risk per Trade
MIN_MARGIN_INR = 500.0                # Minimum margin per slot
MAX_MARGIN_PCT = 0.20                 # Max 20% account margin per slot
INITIAL_CAPITAL = 100000.0
USD_INR_RATE = 88.50
MAKER_FEE_RATE = 0.0002  # 0.02% Exchange Maker Fee
TAKER_FEE_RATE = 0.0005  # 0.05% Exchange Taker / Settlement Fee
FEE_RATE = TAKER_FEE_RATE

DEFAULT_WALLET = {
    "balance": INITIAL_CAPITAL,
    "initial_capital": INITIAL_CAPITAL,
    "realized_pnl": 0.0,
    "total_fees_paid": 0.0,
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "resting_orders": [],
    "active_positions": [],
    "closed_trades": []
}

_wallet_lock = asyncio.Lock()


def load_wallet() -> dict:
    data = {}
    if os.path.exists(WALLET_FILE):
        try:
            with open(WALLET_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading {WALLET_FILE}: {e}")

    # Initial seed ONLY if WALLET_FILE does not exist or has empty structure
    if not data and WALLET_FILE != "paper_wallet.json" and os.path.exists("paper_wallet.json"):
        try:
            with open("paper_wallet.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            save_wallet(data)
            logger.info("Initialized paper wallet on first boot from seed file.")
        except Exception as seed_err:
            logger.warning(f"Failed to seed paper wallet from local file: {seed_err}")

    # Ensure all required keys exist
    for k, v in DEFAULT_WALLET.items():
        if k not in data:
            data[k] = v

    # HARD LOCK: Initial Capital is ALWAYS immutable at INITIAL_CAPITAL (1,00,000 INR).
    # Balance ALWAYS strictly equals INITIAL_CAPITAL + cumulative realized PnL.
    # No arbitrary money change, reset drift, or artificial boost can EVER occur.
    data["initial_capital"] = INITIAL_CAPITAL
    data["balance"] = round(INITIAL_CAPITAL + float(data.get("realized_pnl", 0.0)), 2)



    return data


def save_wallet(data: dict) -> None:
    try:
        # HARD LOCK: Money cannot be randomly changed by any caller!
        # Initial capital is permanently locked at 1,00,000 INR.
        # Balance is strictly anchored to Initial Capital + Realized PnL.
        data["initial_capital"] = INITIAL_CAPITAL
        data["balance"] = round(INITIAL_CAPITAL + float(data.get("realized_pnl", 0.0)), 2)



        dirname = os.path.dirname(WALLET_FILE)
        if dirname and not os.path.exists(dirname):
            os.makedirs(dirname, exist_ok=True)
        temp_file = f"{WALLET_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp_file, WALLET_FILE)
    except Exception as e:
        logger.error(f"Error saving {WALLET_FILE}: {e}")


def format_price(p: float, symbol: str = "") -> str:
    if p is None: return "0.00"
    if "USDT" in symbol or "USD" in symbol or p >= 1.0:
        if p >= 1000: return f"{p:,.2f}"
        elif p >= 10: return f"{p:.2f}"
        elif p >= 1: return f"{p:.4f}"
        else: return f"{p:.6f}"
    return f"{p:.6f}"


def format_inr(amount: float) -> str:
    sign = "+" if amount > 0 else ("-" if amount < 0 else "")
    abs_amt = abs(amount)
    return f"{sign}₹{abs_amt:,.2f} INR"


async def notify_telegram(message: str):
    from telegram_bot import send_text_to_chat, format_display_symbol
    # STRICT DIRECTIVE: Trading activity ONLY to 771305593 nowhere else!
    personal_dm = "771305593"
    try:
        clean_msg = format_display_symbol(message)
        await send_text_to_chat(personal_dm, clean_msg)
    except Exception as e:
        logger.error(f"Failed to send paper trade alert to {personal_dm}: {e}")


async def register_paper_trade(alert_data: dict):
    """
    Registers a new virtual Limit Entry order at entry price.
    Fired when a 3-star or 4-star trade signal is shared.
    """
    symbol = alert_data.get("symbol")
    signal = alert_data.get("signal", "BUY").upper()
    entry = float(alert_data.get("entry", 0.0))
    sl = float(alert_data.get("sl", 0.0))
    tp1 = float(alert_data.get("tp1") or alert_data.get("tp") or 0.0)
    # 2-Tier Target System: TP2 is the final extended runner target (mapped from tp3 or tp2)
    tp2 = float(alert_data.get("tp3") or alert_data.get("tp2") or 0.0)
    tp3 = tp2
    adx = float(alert_data.get("adx")) if alert_data.get("adx") is not None else None

    if not symbol or entry <= 0 or sl <= 0:
        return

    cross_type = str(alert_data.get('cross_type', ''))

    # STRICT USER DIRECTIVE: Trade ONLY in Top 25 Coins in Volume, Commodities, and US Tokens ("thats it nothng else")
    from config import is_allowed_trading_asset
    if not is_allowed_trading_asset(symbol):
        logger.info(f"PAPER TRADER BLOCKED {symbol} {signal} ({cross_type}) — Trade restricted: Asset not in Top 25 Volume, Commodities, or US Tokens.")
        return

    # STRICT USER DIRECTIVE: No trades of commodities, BTC, and ETH on Sat and Sunday in any case
    from config import is_weekend_trading_blocked
    if is_weekend_trading_blocked(symbol):
        logger.info(f"PAPER TRADER BLOCKED {symbol} {signal} — Zero trades for commodities, BTC, or ETH on Saturday/Sunday.")
        return

    # STRICT SQUEEZE TIMEFRAME RULE (User Directive: '1 hr squeze pr hi raho fir'):
    # Squeeze Breakouts are strictly restricted to 1H timeframe only.
    tf_label = str(alert_data.get('tf_label', '')).upper()
    is_squeeze_trade = alert_data.get('is_dedicated_squeeze', False) or ('Squeeze' in str(alert_data.get('cross_type', '')))
    if is_squeeze_trade and tf_label and tf_label not in ['1H', '1HR', '60M']:
        logger.info(f"PAPER TRADER BLOCKED {symbol} {signal} — Squeeze trades restricted to 1H timeframe only (Got {tf_label}).")
        return

    # STRICT USER DIRECTIVE: Execute ONLY 3-Star (3/4 ⭐⭐⭐) & 4-Star (4/4 ⭐⭐⭐⭐) setups WITH Verified RSI Boost Sign (✨)!
    aligned_count = alert_data.get('aligned_count', 0)

    if aligned_count == 0 and alert_data.get('tf_states'):
        tf_states = alert_data.get('tf_states', {})
        target_trend = 'bullish' if signal == 'BUY' else 'bearish'
        aligned_count = sum(1 for t in ['1H', '4H', '1D', '1W'] if tf_states.get(t, 'neutral').lower() == target_trend)

    # 1. Confluence Gate: Strictly minimum 3-Star (3/4) or 4-Star (4/4) required across ALL trade types
    if aligned_count < 3:
        logger.info(f"PAPER TRADER BLOCKED {symbol} {signal} ({cross_type}) — aligned_count={aligned_count}/4 (Minimum 3-Star Confluence Required).")
        return

    # 2. Strict RSI Momentum Gate: Must fulfill RSI condition for the RSI Boost Sign (✨)
    # BUY: 45.0 <= RSI <= 72.0 (Healthy bullish momentum zone, avoids exhausted tops >72 and dumps <45)
    # SELL: 28.0 <= RSI <= 55.0 (Healthy bearish momentum zone, avoids oversold bottoms <28 and weak sellers >55)
    rsi = alert_data.get('rsi')
    has_rsi_boost = False
    f_rsi = None
    if rsi is not None:
        try:
            f_rsi = float(rsi)
            if signal == 'BUY' and (45.0 <= f_rsi <= 72.0):
                has_rsi_boost = True
            elif signal == 'SELL' and (28.0 <= f_rsi <= 55.0):
                has_rsi_boost = True
        except (ValueError, TypeError):
            pass

    if not has_rsi_boost:
        logger.info(
            f"PAPER TRADER BLOCKED {symbol} {signal} ({cross_type}) — "
            f"RSI={f_rsi if f_rsi is not None else 'None'} outside required RSI Boost bounds [BUY 45-72, SELL 28-55]. "
            f"(Mandatory: 3-Star/4-Star WITH RSI Sign ✨ Required)."
        )
        return

    # Enforce strict Liquidation Shield on SL (SL MUST ALWAYS be above Liq for BUY / below Liq for SELL)
    from executor import enforce_sl_above_liquidation
    sl = enforce_sl_above_liquidation(symbol, entry, sl, signal, LEVERAGE)

    # Calculate default TPs if missing
    risk = abs(entry - sl)
    if risk == 0: risk = entry * 0.015

    if signal == "BUY":
        if tp1 <= entry: tp1 = entry + 1.6 * risk
        if tp2 <= tp1: tp2 = entry + 3.5 * risk
        tp3 = tp2
    else:
        if tp1 >= entry or tp1 == 0: tp1 = entry - 1.6 * risk
        if tp2 >= tp1 or tp2 == 0: tp2 = entry - 3.5 * risk
        tp3 = tp2

    async with _wallet_lock:
        wallet = load_wallet()

        # 1. Check if order or active position already exists in SAME direction -> TRAIL SL & TPs!
        for order in wallet.get("resting_orders", []) + wallet.get("active_positions", []):
            if order["symbol"] == symbol and order["signal"] == signal:
                old_sl = order.get("current_sl", order["sl"])
                order["current_sl"] = sl
                order["tp1"] = tp1
                order["tp2"] = tp2
                order["tp3"] = tp3
                save_wallet(wallet)

                msg = (
                    f"🔄 <b>PAPER TRADE: SL & TP TRAILED (SAME COIN SIGNAL)</b>\n"
                    f"<b>Asset:</b> {symbol} [{signal}]\n"
                    f"🛡️ <b>Previous SL:</b> {format_price(old_sl, symbol)} ➔ <b>New Trailed SL:</b> {format_price(sl, symbol)}\n"
                    f"🎯 <b>New Targets:</b> TP1: {format_price(tp1, symbol)} (70% Book) | TP2: {format_price(tp2, symbol)} (30% Runner)\n"
                    f"<i>Status: Fresh {signal} signal received for active position. SL & TPs updated!</i>"
                )
                asyncio.create_task(notify_telegram(msg))
                return

        # 2. Check if an OPPOSITE direction trade exists -> Close/cancel old trade and execute new reversal!
        opp_signal = "SELL" if signal == "BUY" else "BUY"
        for item in list(wallet.get("active_positions", [])):
            if item["symbol"] == symbol and item["signal"] == opp_signal:
                item["status"] = "CLOSED_SIGNAL_REVERSAL"
                item["closed_at"] = datetime.now(timezone.utc).isoformat()
                wallet["active_positions"].remove(item)
                wallet["closed_trades"].append(item)

        for item in list(wallet.get("resting_orders", [])):
            if item["symbol"] == symbol and item["signal"] == opp_signal:
                item["status"] = "CANCELLED_SIGNAL_REVERSAL"
                item["closed_at"] = datetime.now(timezone.utc).isoformat()
                wallet["resting_orders"].remove(item)
                wallet["closed_trades"].append(item)

        # 3. User Directive: NO LIMIT on Paper Trades — ALL 4-Star trades allowed!
        active_list = wallet.get("active_positions", [])
        resting_list = wallet.get("resting_orders", [])
        total_open_slots = len(active_list) + len(resting_list)

        risk_pct = abs(entry - sl) / entry if entry > 0 else 0.02
        if risk_pct <= 0:
            risk_pct = 0.015

        # USER DIRECTIVE: Exact 2.0% Margin Sizing on AVAILABLE (Free) Margin on Broker / Wallet
        wallet_bal = float(wallet.get("balance", INITIAL_CAPITAL))
        used_margin = sum(item.get("margin_inr", MARGIN_INR) for item in active_list + resting_list)
        available_margin = max(0.0, wallet_bal - used_margin)

        # STRICT: If free available margin < MIN_MARGIN_INR (₹500), skip trade!
        if available_margin < MIN_MARGIN_INR:
            logger.info(f"PAPER TRADER: Insufficient free available margin (Available: ₹{available_margin:,.2f} < Min ₹{MIN_MARGIN_INR:,.2f}). Skipping {symbol}.")
            return

        trade_margin_inr = round(max(MIN_MARGIN_INR, available_margin * 0.02), 2)
        trade_notional_inr = round(trade_margin_inr * LEVERAGE, 2)
        actual_risk_inr = trade_notional_inr * risk_pct
        target_risk_inr = actual_risk_inr

        # 4. Create & Activate new Virtual Limit Order with Real Contract Sizing
        notional_usd = trade_notional_inr / USD_INR_RATE
        qty = notional_usd / entry if entry > 0 else 0.0
        liq_price = entry * 0.905 if signal == "BUY" else entry * 1.095
        trade_id = f"paper_{symbol.replace('/', '')}_{signal}_{int(datetime.now(timezone.utc).timestamp())}"
        
        order = {
            "id": trade_id,
            "symbol": symbol,
            "signal": signal,
            "entry_limit": entry,
            "sl": sl,
            "initial_sl": sl,
            "current_sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "adx": adx,
            "rsi": f_rsi,
            "aligned_count": aligned_count,
            "margin_inr": trade_margin_inr,
            "leverage": LEVERAGE,
            "notional_inr": trade_notional_inr,
            "notional_usd": notional_usd,
            "risk_pct": risk_pct,
            "target_risk_inr": target_risk_inr,
            "qty": qty,
            "initial_qty": qty,
            "remaining_qty": qty,
            "liq_price": liq_price,
            "fee_paid": 0.0,
            "status": "PENDING_LIMIT",
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "tp1_realized_pnl": 0.0,
            "tp2_realized_pnl": 0.0,
            "tp3_realized_pnl": 0.0,
            "cumulative_pnl": 0.0,
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        wallet["resting_orders"].append(order)
        save_wallet(wallet)

        balance_str = f"₹{wallet['balance']:,.2f} INR"

    # Broadcast Telegram Alert
    msg = (
        f"🦈 <b>PAPER TRADE: LIMIT ORDER PLACED (EXCHANGE ORDERBOOK)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Asset:</b> {symbol} [{signal}] 10x Isolated\n"
        f"⭐ <b>Quality:</b> {'⭐' * aligned_count}✨ (RSI: {f_rsi:.1f} Momentum Qualified)\n"
        f"🎯 <b>Entry Limit:</b> {format_price(entry, symbol)} | <b>SL:</b> {format_price(sl, symbol)} ({risk_pct * 100:.2f}% risk)\n"
        f"📦 <b>Size:</b> {qty:.4f} contracts (~${notional_usd:.2f} USD / ₹{trade_notional_inr:,.0f} INR)\n"
        f"💰 <b>Margin Required:</b> ₹{trade_margin_inr:,.2f} INR (Free Margin: ₹{available_margin - trade_margin_inr:,.2f} INR)\n"
        f"🛡️ <b>Target Risk ({risk_pct * 100:.2f}%):</b> ₹{target_risk_inr:,.2f} INR\n"
        f"🎯 <b>Targets:</b> TP1: {format_price(tp1, symbol)} (70% Book + +0.4R Lock) | TP2: {format_price(tp2, symbol)} (30% Runner)\n"
        f"🛡️ <b>Est. Liquidation:</b> ~{format_price(liq_price, symbol)}\n"
        f"⏱️ <b>Rules:</b> 30-Min Fill Window | 20-Hour Timeout @ Market Price\n"
        f"💼 <b>Virtual Wallet Balance:</b> {balance_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await notify_telegram(msg)


async def evaluate_paper_trades_async(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
    """
    Evaluates resting orders and active paper positions against live Binance 5m/1h candle data.
    Enforces:
    1. 30-Minute Limit Fill Expiration rule (Auto-cancel if unfilled after 30 mins).
    2. 20-Hour Position Timeout rule (Auto-close at CTC if TP3 not reached in 20 hours).
    """
    async with _wallet_lock:
        wallet = load_wallet()
        resting = list(wallet.get("resting_orders", []))
        active = list(wallet.get("active_positions", []))

    if not resting and not active:
        return

    # ?? INDEPENDENT 30-MIN EXPIRY SWEEP (Never blocked by API glitches / timeouts) ??
    now_utc = datetime.now(timezone.utc)
    if resting:
        async with _wallet_lock:
            w_check = load_wallet()
            r_orders = list(w_check.get("resting_orders", []))
            c_trades = list(w_check.get("closed_trades", []))
            r_updated = False
            r_remaining = []
            for ro in r_orders:
                c_ts = ro.get("created_at")
                is_expired = False
                if c_ts:
                    try:
                        dt_c = datetime.fromisoformat(c_ts.replace("Z", "+00:00"))
                        if dt_c.tzinfo is None: dt_c = dt_c.replace(tzinfo=timezone.utc)
                        if (now_utc - dt_c).total_seconds() >= 1800:
                            is_expired = True
                    except Exception:
                        pass
                if is_expired:
                    ro["status"] = "CANCELLED_30MIN_EXPIRED"
                    ro["closed_at"] = now_utc.isoformat()
                    c_trades.append(ro)
                    r_updated = True
                    logger.info(f"? Auto-cancelled 30m expired resting limit order for {ro['symbol']}")
                else:
                    r_remaining.append(ro)
            if r_updated:
                w_check["resting_orders"] = r_remaining
                w_check["closed_trades"] = c_trades
                save_wallet(w_check)
                resting = r_remaining


    symbols_to_check = set(o["symbol"] for o in resting + active)
    from fetcher import fetch_ohlcv_async

    for symbol in symbols_to_check:
        try:
            is_older_pos = False
            for p in active:
                if p["symbol"] == symbol:
                    f_ts = p.get("filled_at") or p.get("created_at")
                    if f_ts:
                        try:
                            dt_f = datetime.fromisoformat(f_ts.replace("Z", "+00:00"))
                            if dt_f.tzinfo is None:
                                dt_f = dt_f.replace(tzinfo=timezone.utc)
                            if (datetime.now(timezone.utc) - dt_f).total_seconds() > 21600:
                                is_older_pos = True
                        except Exception:
                            pass

            interval = "1h" if is_older_pos else "5m"
            df = await fetch_ohlcv_async(session, symbol, interval, 100, semaphore)
            if df is None or df.empty:
                df = await fetch_ohlcv_async(session, symbol, "1h", 100, semaphore)
            if df is None or df.empty:
                continue

            live_price = None
            try:
                sym_clean = symbol.replace("/", "")
                url = f"https://data-api.binance.vision/api/v3/ticker/price?symbol={sym_clean}"
                async with session.get(url, timeout=4) as resp:
                    if resp.status == 200:
                        p_data = await resp.json()
                        live_price = float(p_data.get("price", 0.0))
            except Exception:
                pass

            await _evaluate_symbol_paper_trades(symbol, df, live_price=live_price)
        except Exception as e:
            logger.error(f"Error evaluating paper trades for {symbol}: {e}")


def _process_pos_price_triggers(pos: dict, wallet: dict, closed: list, p_high: float, p_low: float, now_utc: datetime, live_tick: bool = False) -> tuple[bool, bool]:
    """
    Evaluates real-time price against position liquidation, stop-loss, and take-profit targets.
    Implements Elite Profit Booking Strategy:
    - 70% Profit Booked at TP1, SL Trailed to +0.4R Profit-Lock Floor.
    - Choppy/Ranging Regime (ADX < 20): 100% Exit at TP1.
    - Remaining 30% Free Runner to TP3.
    - If market pulls back after TP1, trailing SL exits at +0.4R for guaranteed net green profit.
    Returns: (is_closed: bool, is_updated: bool)
    """
    from executor import enforce_sl_above_liquidation
    symbol = pos["symbol"]
    signal = pos["signal"]
    entry = pos["entry_limit"]
    sl = pos["current_sl"]
    initial_sl = pos.get("initial_sl", pos.get("sl", sl))
    tp1 = pos.get("tp1", 0.0)
    tp2 = pos.get("tp2", 0.0)
    tp3 = pos.get("tp3", 0.0)
    target_tp2 = tp2 if (tp2 and tp2 > 0 and ((signal == "BUY" and tp2 > entry) or (signal == "SELL" and tp2 < entry))) else (tp3 if tp3 and tp3 > 0 else (tp1 * 1.02 if signal == "BUY" else tp1 * 0.98))
    target_tp3 = target_tp2
    liq = pos.get("liq_price", entry * 0.905 if signal == "BUY" else entry * 1.095)
    tick_tag = " (LIVE TICK)" if live_tick else ""
    pos_margin = float(pos.get("margin_inr", MARGIN_INR))
    pos_notional = float(pos.get("notional_inr", NOTIONAL_INR))
    adx_val = pos.get("adx")
    is_ranging = (adx_val is not None and adx_val < 20.0)

    if signal == "BUY":
        # 1. STOP-LOSS CHECK (Evaluated FIRST: SL executes on orderbook before liquidation price can be reached)
        if p_low <= sl:
            pos["closed_at"] = now_utc.isoformat()
            if not pos.get("tp1_hit", False):
                # Full SL Hit before TP1 (100% position exited at SL)
                gross_loss = pos_notional * ((sl - entry) / entry)
                taker_fee = pos_notional * TAKER_FEE_RATE
                net_loss = gross_loss - taker_fee
                wallet["balance"] += net_loss
                wallet["realized_pnl"] += net_loss
                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                pos["cumulative_pnl"] = net_loss
                pos["remaining_qty"] = 0.0
                wallet["losses"] += 1
                wallet["total_trades"] += 1
                pos["status"] = "SL_HIT"

                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                msg = (
                    f"🔴 <b>PAPER STOP-LOSS HIT{tick_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [BUY]\n"
                    f"🎯 <b>Exit SL:</b> {format_price(sl, symbol)} | <b>Entry:</b> {format_price(entry, symbol)}\n"
                    f"<b>Loss:</b> -₹{abs(net_loss):,.2f} INR (incl. ₹{taker_fee:.2f} fee)\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                if not pos.get("closed_alert_sent"):
                    pos["closed_alert_sent"] = True
                    asyncio.create_task(notify_telegram(msg))
            elif pos.get("tp1_hit", False):
                # Trailed SL Hit at +0.4R Profit-Lock for remaining 30% runner
                rem_gross = (pos_notional * 0.30) * ((sl - entry) / entry)
                taker_fee = (pos_notional * 0.30) * TAKER_FEE_RATE
                rem_net = rem_gross - taker_fee
                pos["tp3_realized_pnl"] = rem_net
                pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + rem_net
                wallet["balance"] += rem_net
                wallet["realized_pnl"] += rem_net
                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                pos["remaining_qty"] = 0.0
                wallet["wins"] += 1
                wallet["total_trades"] += 1
                pos["status"] = "CLOSED_PROFIT_LOCK_WIN"

                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                risk_val = abs(entry - initial_sl) if (initial_sl and initial_sl != entry) else (entry * 0.015)
                locked_r = (sl - entry) / risk_val if risk_val > 0 else 0.4
                stage_tag = f"+{locked_r:.1f}R" if locked_r >= 0.3 else "+0.4R"
                msg = (
                    f"🎯🛡️ <b>PAPER TRAILING SL HIT AT {stage_tag} PROFIT-LOCK{tick_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [BUY]\n"
                    f"🎯 <b>Exit:</b> {format_price(sl, symbol)} ({stage_tag} Profit Floor)\n"
                    f"<b>30% Runner PnL:</b> +₹{rem_net:,.2f} INR\n"
                    f"<b>Total Trade Realized Profit:</b> +₹{pos['cumulative_pnl']:,.2f} INR (Winner! 💰)\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                if not pos.get("closed_alert_sent"):
                    pos["closed_alert_sent"] = True
                    asyncio.create_task(notify_telegram(msg))

            closed.append(pos)
            return True, True

        # 2. LIQUIDATION CHECK FOR BUY (Safety fallback: only triggered if price dropped without a valid SL)
        if p_low <= liq:
            pos["status"] = "LIQUIDATED"
            pos["closed_at"] = now_utc.isoformat()
            liq_loss = -pos_margin
            wallet["balance"] += liq_loss
            wallet["realized_pnl"] += liq_loss
            wallet["losses"] += 1
            wallet["total_trades"] += 1
            pos["cumulative_pnl"] = liq_loss
            pos["remaining_qty"] = 0.0
            closed.append(pos)
            if not pos.get("closed_alert_sent"):
                pos["closed_alert_sent"] = True
                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                msg = (
                    f"💀 <b>PAPER POSITION LIQUIDATED (10x ISOLATED){tick_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [BUY]\n"
                    f"<b>Liq Price:</b> {format_price(liq, symbol)} | <b>Entry:</b> {format_price(entry, symbol)}\n"
                    f"<b>Loss:</b> -₹{pos_margin:,.2f} INR (Margin Lost)\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                asyncio.create_task(notify_telegram(msg))
            return True, True

        # 3. TAKE-PROFIT BRACKETS CHECK
        updated = False
        # TP1 (70% Booked or 100% in Chop Regime)
        if not pos["tp1_hit"] and p_high >= tp1:
            pos["tp1_hit"] = True
            pos["tp1_hit_at"] = now_utc.isoformat()

            if is_ranging:
                # 100% Position closed at TP1 in chop regime
                tp1_gross = pos_notional * ((tp1 - entry) / entry)
                taker_fee = pos_notional * TAKER_FEE_RATE
                tp1_net = tp1_gross - taker_fee
                pos["tp1_realized_pnl"] = tp1_net
                pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + tp1_net
                wallet["balance"] += tp1_net
                wallet["realized_pnl"] += tp1_net
                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                pos["remaining_qty"] = 0.0
                wallet["wins"] += 1
                wallet["total_trades"] += 1
                pos["status"] = "FULL_WIN_TP1_CHOP"
                pos["closed_at"] = now_utc.isoformat()
                closed.append(pos)

                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                msg = (
                    f"🏆 <b>PAPER TARGET HIT — FULL WIN (RANGE REGIME) 🚀{tick_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [BUY]\n"
                    f"🎯 <b>Target:</b> {format_price(tp1, symbol)}\n"
                    f"<b>Realized PnL (100%):</b> +₹{tp1_net:,.2f} INR (Fee: ₹{taker_fee:.2f})\n"
                    f"💡 <b>Market Regime:</b> Choppy/Range (ADX {adx_val:.1f} < 20) — 100% Cashed Out Safely!\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                if not pos.get("closed_alert_sent"):
                    pos["closed_alert_sent"] = True
                    asyncio.create_task(notify_telegram(msg))
                return True, True
            else:
                # Trending: 70% Booked at TP1, 30% Runner
                tp1_gross = (pos_notional * 0.70) * ((tp1 - entry) / entry)
                taker_fee = (pos_notional * 0.70) * TAKER_FEE_RATE
                tp1_net = tp1_gross - taker_fee
                pos["tp1_realized_pnl"] = tp1_net
                pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + tp1_net
                wallet["balance"] += tp1_net
                wallet["realized_pnl"] += tp1_net
                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                pos["remaining_qty"] = pos.get("initial_qty", pos.get("qty", 0.0)) * 0.30

                # Compute +0.4R Profit-Lock SL
                risk = abs(entry - initial_sl) if (initial_sl and initial_sl != entry) else (entry * 0.015)
                profit_lock_sl = entry + (0.40 * risk)
                if profit_lock_sl >= tp1:
                    profit_lock_sl = entry + (0.20 * risk)
                profit_lock_sl = max(profit_lock_sl, entry * 1.002)

                # Clamp with liquidation shield
                profit_lock_sl = enforce_sl_above_liquidation(symbol, entry, profit_lock_sl, signal, LEVERAGE, liq)
                pos["current_sl"] = profit_lock_sl
                pos["profit_lock_sl"] = profit_lock_sl
                updated = True

                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                msg = (
                    f"🎯 <b>PAPER TP1 HIT ✅ (70% Booked){tick_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [BUY]\n"
                    f"🎯 <b>Target:</b> {format_price(tp1, symbol)}\n"
                    f"<b>Realized PnL:</b> +₹{tp1_net:,.2f} INR (Fee: ₹{taker_fee:.2f})\n"
                    f"🛡️ <b>SL Trailed to +0.4R Profit-Lock ({format_price(profit_lock_sl, symbol)})</b>\n"
                    f"🚀 <b>Remaining 30% Runner:</b> Risk-free aiming for TP2 {format_price(target_tp3, symbol)}!\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                if not pos.get("tp1_alert_sent"):
                    pos["tp1_alert_sent"] = True
                    asyncio.create_task(notify_telegram(msg))

        # Point 2: Multi-Stage 'Anti-Giveback' Stepped Trailing Engine Ratchet (BUY)
        if pos.get("tp1_hit", False) and not pos.get("tp3_hit", False):
            risk_step = abs(entry - initial_sl) if (initial_sl and initial_sl != entry) else (entry * 0.015)
            # Stage 2 (+3.0R Expansion -> Ratchet SL to +2.0R)
            if p_high >= entry + (3.0 * risk_step) and pos.get("trail_stage", 0) < 2:
                ratchet_sl = entry + (2.0 * risk_step)
                ratchet_sl = enforce_sl_above_liquidation(symbol, entry, ratchet_sl, signal, LEVERAGE, liq)
                if ratchet_sl > pos["current_sl"]:
                    pos["current_sl"] = ratchet_sl
                    pos["trail_stage"] = 2
                    pos["profit_lock_sl"] = ratchet_sl
                    updated = True
                    msg = (
                        f"🔥 <b>PAPER TRAILING SL RATCHET (+2.0R LOCKED) 🏆{tick_tag}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<b>Asset:</b> {symbol} [BUY]\n"
                        f"🚀 <b>Expansion:</b> Price surged past +3.0R ({format_price(entry + 3.0*risk_step, symbol)})!\n"
                        f"🛡️ <b>SL Ratcheted to:</b> {format_price(ratchet_sl, symbol)} (+2.0R Profit Floor)\n"
                        f"<i>30% runner gains locked — Anti-Giveback engine active!</i>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    asyncio.create_task(notify_telegram(msg))
            # Stage 1 (+2.2R Expansion -> Ratchet SL to +1.2R)
            elif p_high >= entry + (2.2 * risk_step) and pos.get("trail_stage", 0) < 1:
                ratchet_sl = entry + (1.2 * risk_step)
                ratchet_sl = enforce_sl_above_liquidation(symbol, entry, ratchet_sl, signal, LEVERAGE, liq)
                if ratchet_sl > pos["current_sl"]:
                    pos["current_sl"] = ratchet_sl
                    pos["trail_stage"] = 1
                    pos["profit_lock_sl"] = ratchet_sl
                    updated = True
                    msg = (
                        f"🛡️ <b>PAPER TRAILING SL RATCHET (+1.2R LOCKED) 🚀{tick_tag}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<b>Asset:</b> {symbol} [BUY]\n"
                        f"🚀 <b>Expansion:</b> Price reached +2.2R ({format_price(entry + 2.2*risk_step, symbol)})!\n"
                        f"🛡️ <b>SL Ratcheted to:</b> {format_price(ratchet_sl, symbol)} (+1.2R Profit Floor)\n"
                        f"<i>Anti-Giveback active — Protecting runner profits!</i>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    asyncio.create_task(notify_telegram(msg))

        # TP3 (Remaining 30% Runner Booked - Full Win)
        if pos["tp1_hit"] and not pos.get("tp3_hit") and p_high >= target_tp3:
            pos["tp3_hit"] = True
            tp3_gross = (pos_notional * 0.30) * ((target_tp3 - entry) / entry)
            taker_fee = (pos_notional * 0.30) * TAKER_FEE_RATE
            tp3_net = tp3_gross - taker_fee
            pos["tp3_realized_pnl"] = tp3_net
            pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + tp3_net
            wallet["balance"] += tp3_net
            wallet["realized_pnl"] += tp3_net
            wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
            pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
            pos["remaining_qty"] = 0.0
            wallet["wins"] += 1
            wallet["total_trades"] += 1
            pos["status"] = "FULL_WIN_TP3"
            pos["closed_at"] = now_utc.isoformat()
            closed.append(pos)

            roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
            msg = (
                f"🏆 <b>PAPER TP2 HIT — FULL WIN! 🚀{tick_tag}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Asset:</b> {symbol} [BUY]\n"
                f"🎯 <b>Target:</b> {format_price(target_tp3, symbol)}\n"
                f"<b>Total Trade Profit:</b> +₹{pos['cumulative_pnl']:,.2f} INR\n"
                f"💼 <b>Final Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}% Total ROI)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            if not pos.get("closed_alert_sent"):
                pos["closed_alert_sent"] = True
                asyncio.create_task(notify_telegram(msg))
            return True, True

        return False, updated

    else:  # SELL POSITION
        # 1. STOP-LOSS CHECK FOR SELL (Evaluated FIRST: SL executes on orderbook before liquidation price can be reached)
        if p_high >= sl:
            pos["closed_at"] = now_utc.isoformat()
            if not pos.get("tp1_hit", False):
                # Full SL Hit before TP1
                gross_loss = pos_notional * ((entry - sl) / entry)
                taker_fee = pos_notional * TAKER_FEE_RATE
                net_loss = gross_loss - taker_fee
                wallet["balance"] += net_loss
                wallet["realized_pnl"] += net_loss
                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                pos["cumulative_pnl"] = net_loss
                pos["remaining_qty"] = 0.0
                wallet["losses"] += 1
                wallet["total_trades"] += 1
                pos["status"] = "SL_HIT"

                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                msg = (
                    f"🔴 <b>PAPER STOP-LOSS HIT{tick_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [SELL]\n"
                    f"🎯 <b>Exit SL:</b> {format_price(sl, symbol)} | <b>Entry:</b> {format_price(entry, symbol)}\n"
                    f"<b>Loss:</b> -₹{abs(net_loss):,.2f} INR (incl. ₹{taker_fee:.2f} fee)\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                if not pos.get("closed_alert_sent"):
                    pos["closed_alert_sent"] = True
                    asyncio.create_task(notify_telegram(msg))
            elif pos.get("tp1_hit", False):
                # Trailed SL Hit at +0.4R Profit-Lock for remaining 30% runner
                rem_gross = (pos_notional * 0.30) * ((entry - sl) / entry)
                taker_fee = (pos_notional * 0.30) * TAKER_FEE_RATE
                rem_net = rem_gross - taker_fee
                pos["tp3_realized_pnl"] = rem_net
                pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + rem_net
                wallet["balance"] += rem_net
                wallet["realized_pnl"] += rem_net
                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                pos["remaining_qty"] = 0.0
                wallet["wins"] += 1
                wallet["total_trades"] += 1
                pos["status"] = "CLOSED_PROFIT_LOCK_WIN"

                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                risk_val = abs(entry - initial_sl) if (initial_sl and initial_sl != entry) else (entry * 0.015)
                locked_r = (entry - sl) / risk_val if risk_val > 0 else 0.4
                stage_tag = f"+{locked_r:.1f}R" if locked_r >= 0.3 else "+0.4R"
                msg = (
                    f"🎯🛡️ <b>PAPER TRAILING SL HIT AT {stage_tag} PROFIT-LOCK{tick_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [SELL]\n"
                    f"🎯 <b>Exit:</b> {format_price(sl, symbol)} ({stage_tag} Profit Floor)\n"
                    f"<b>30% Runner PnL:</b> +₹{rem_net:,.2f} INR\n"
                    f"<b>Total Trade Realized Profit:</b> +₹{pos['cumulative_pnl']:,.2f} INR (Winner! 💰)\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                if not pos.get("closed_alert_sent"):
                    pos["closed_alert_sent"] = True
                    asyncio.create_task(notify_telegram(msg))

            closed.append(pos)
            return True, True

        # 2. LIQUIDATION CHECK FOR SELL
        if p_high >= liq:
            pos["status"] = "LIQUIDATED"
            pos["closed_at"] = now_utc.isoformat()
            liq_loss = -pos_margin
            wallet["balance"] += liq_loss
            wallet["realized_pnl"] += liq_loss
            wallet["losses"] += 1
            wallet["total_trades"] += 1
            pos["cumulative_pnl"] = liq_loss
            pos["remaining_qty"] = 0.0
            closed.append(pos)
            if not pos.get("closed_alert_sent"):
                pos["closed_alert_sent"] = True
                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                msg = (
                    f"💀 <b>PAPER POSITION LIQUIDATED (10x ISOLATED){tick_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [SELL]\n"
                    f"<b>Liq Price:</b> {format_price(liq, symbol)} | <b>Entry:</b> {format_price(entry, symbol)}\n"
                    f"<b>Loss:</b> -₹{pos_margin:,.2f} INR (Margin Lost)\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                asyncio.create_task(notify_telegram(msg))
            return True, True

        # 3. TAKE-PROFIT BRACKETS CHECK FOR SELL
        updated = False
        # TP1 (70% Booked or 100% in Chop Regime)
        if not pos["tp1_hit"] and p_low <= tp1:
            pos["tp1_hit"] = True
            pos["tp1_hit_at"] = now_utc.isoformat()

            if is_ranging:
                # 100% Position closed at TP1 in chop regime
                tp1_gross = pos_notional * ((entry - tp1) / entry)
                taker_fee = pos_notional * TAKER_FEE_RATE
                tp1_net = tp1_gross - taker_fee
                pos["tp1_realized_pnl"] = tp1_net
                pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + tp1_net
                wallet["balance"] += tp1_net
                wallet["realized_pnl"] += tp1_net
                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                pos["remaining_qty"] = 0.0
                wallet["wins"] += 1
                wallet["total_trades"] += 1
                pos["status"] = "FULL_WIN_TP1_CHOP"
                pos["closed_at"] = now_utc.isoformat()
                closed.append(pos)

                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                msg = (
                    f"🏆 <b>PAPER TARGET HIT — FULL WIN (RANGE REGIME) 🚀{tick_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [SELL]\n"
                    f"🎯 <b>Target:</b> {format_price(tp1, symbol)}\n"
                    f"<b>Realized PnL (100%):</b> +₹{tp1_net:,.2f} INR (Fee: ₹{taker_fee:.2f})\n"
                    f"💡 <b>Market Regime:</b> Choppy/Range (ADX {adx_val:.1f} < 20) — 100% Cashed Out Safely!\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                if not pos.get("closed_alert_sent"):
                    pos["closed_alert_sent"] = True
                    asyncio.create_task(notify_telegram(msg))
                return True, True
            else:
                # Trending: 70% Booked at TP1, 30% Runner
                tp1_gross = (pos_notional * 0.70) * ((entry - tp1) / entry)
                taker_fee = (pos_notional * 0.70) * TAKER_FEE_RATE
                tp1_net = tp1_gross - taker_fee
                pos["tp1_realized_pnl"] = tp1_net
                pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + tp1_net
                wallet["balance"] += tp1_net
                wallet["realized_pnl"] += tp1_net
                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                pos["remaining_qty"] = pos.get("initial_qty", pos.get("qty", 0.0)) * 0.30

                # Compute +0.4R Profit-Lock SL
                risk = abs(entry - initial_sl) if (initial_sl and initial_sl != entry) else (entry * 0.015)
                profit_lock_sl = entry - (0.40 * risk)
                if profit_lock_sl <= tp1:
                    profit_lock_sl = entry - (0.20 * risk)
                profit_lock_sl = min(profit_lock_sl, entry * 0.998)

                # Clamp with liquidation shield
                profit_lock_sl = enforce_sl_above_liquidation(symbol, entry, profit_lock_sl, signal, LEVERAGE, liq)
                pos["current_sl"] = profit_lock_sl
                pos["profit_lock_sl"] = profit_lock_sl
                updated = True

                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                msg = (
                    f"🎯 <b>PAPER TP1 HIT ✅ (70% Booked){tick_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [SELL]\n"
                    f"🎯 <b>Target:</b> {format_price(tp1, symbol)}\n"
                    f"<b>Realized PnL:</b> +₹{tp1_net:,.2f} INR (Fee: ₹{taker_fee:.2f})\n"
                    f"🛡️ <b>SL Trailed to +0.4R Profit-Lock ({format_price(profit_lock_sl, symbol)})</b>\n"
                    f"🚀 <b>Remaining 30% Runner:</b> Risk-free aiming for TP2 {format_price(target_tp3, symbol)}!\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                if not pos.get("tp1_alert_sent"):
                    pos["tp1_alert_sent"] = True
                    asyncio.create_task(notify_telegram(msg))

        # Point 2: Multi-Stage 'Anti-Giveback' Stepped Trailing Engine Ratchet (SELL)
        if pos.get("tp1_hit", False) and not pos.get("tp3_hit", False):
            risk_step = abs(entry - initial_sl) if (initial_sl and initial_sl != entry) else (entry * 0.015)
            # Stage 2 (+3.0R Expansion -> Ratchet SL to +2.0R)
            if p_low <= entry - (3.0 * risk_step) and pos.get("trail_stage", 0) < 2:
                ratchet_sl = entry - (2.0 * risk_step)
                ratchet_sl = enforce_sl_above_liquidation(symbol, entry, ratchet_sl, signal, LEVERAGE, liq)
                if ratchet_sl < pos["current_sl"]:
                    pos["current_sl"] = ratchet_sl
                    pos["trail_stage"] = 2
                    pos["profit_lock_sl"] = ratchet_sl
                    updated = True
                    msg = (
                        f"🔥 <b>PAPER TRAILING SL RATCHET (+2.0R LOCKED) 🏆{tick_tag}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<b>Asset:</b> {symbol} [SELL]\n"
                        f"🚀 <b>Expansion:</b> Price dumped past +3.0R ({format_price(entry - 3.0*risk_step, symbol)})!\n"
                        f"🛡️ <b>SL Ratcheted to:</b> {format_price(ratchet_sl, symbol)} (+2.0R Profit Floor)\n"
                        f"<i>30% runner gains locked — Anti-Giveback engine active!</i>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    asyncio.create_task(notify_telegram(msg))
            # Stage 1 (+2.2R Expansion -> Ratchet SL to +1.2R)
            elif p_low <= entry - (2.2 * risk_step) and pos.get("trail_stage", 0) < 1:
                ratchet_sl = entry - (1.2 * risk_step)
                ratchet_sl = enforce_sl_above_liquidation(symbol, entry, ratchet_sl, signal, LEVERAGE, liq)
                if ratchet_sl < pos["current_sl"]:
                    pos["current_sl"] = ratchet_sl
                    pos["trail_stage"] = 1
                    pos["profit_lock_sl"] = ratchet_sl
                    updated = True
                    msg = (
                        f"🛡️ <b>PAPER TRAILING SL RATCHET (+1.2R LOCKED) 🚀{tick_tag}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<b>Asset:</b> {symbol} [SELL]\n"
                        f"🚀 <b>Expansion:</b> Price reached +2.2R ({format_price(entry - 2.2*risk_step, symbol)})!\n"
                        f"🛡️ <b>SL Ratcheted to:</b> {format_price(ratchet_sl, symbol)} (+1.2R Profit Floor)\n"
                        f"<i>Anti-Giveback active — Protecting runner profits!</i>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    asyncio.create_task(notify_telegram(msg))

        # TP3 (Remaining 30% Runner Booked - Full Win)
        if pos["tp1_hit"] and not pos.get("tp3_hit") and p_low <= target_tp3:
            pos["tp3_hit"] = True
            tp3_gross = (pos_notional * 0.30) * ((entry - target_tp3) / entry)
            taker_fee = (pos_notional * 0.30) * TAKER_FEE_RATE
            tp3_net = tp3_gross - taker_fee
            pos["tp3_realized_pnl"] = tp3_net
            pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + tp3_net
            wallet["balance"] += tp3_net
            wallet["realized_pnl"] += tp3_net
            wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
            pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
            pos["remaining_qty"] = 0.0
            wallet["wins"] += 1
            wallet["total_trades"] += 1
            pos["status"] = "FULL_WIN_TP3"
            pos["closed_at"] = now_utc.isoformat()
            closed.append(pos)

            roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
            msg = (
                f"🏆 <b>PAPER TP2 HIT — FULL WIN! 🚀{tick_tag}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Asset:</b> {symbol} [SELL]\n"
                f"🎯 <b>Target:</b> {format_price(target_tp3, symbol)}\n"
                f"<b>Total Trade Profit:</b> +₹{pos['cumulative_pnl']:,.2f} INR\n"
                f"💼 <b>Final Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}% Total ROI)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            if not pos.get("closed_alert_sent"):
                pos["closed_alert_sent"] = True
                asyncio.create_task(notify_telegram(msg))
            return True, True

        return False, updated


async def _evaluate_symbol_paper_trades(symbol: str, df, live_price: float = None):
    now_utc = datetime.now(timezone.utc)

    async with _wallet_lock:
        wallet = load_wallet()
        resting = wallet.get("resting_orders", [])
        active = wallet.get("active_positions", [])
        closed = wallet.get("closed_trades", [])

        wallet_updated = False

        # ── 1. CHECK RESTING LIMIT ORDERS (20-MIN RULE & LIVE TICK MATCH) ────────────────
        remaining_resting = []
        for order in resting:
            if order["symbol"] != symbol:
                remaining_resting.append(order)
                continue

            entry = order["entry_limit"]
            signal = order["signal"]

            created_ts = order.get("created_at")
            dt_c = None
            limit_cutoff_dt = None
            if created_ts:
                try:
                    dt_c = datetime.fromisoformat(created_ts.replace("Z", "+00:00"))
                    if dt_c.tzinfo is None:
                        dt_c = dt_c.replace(tzinfo=timezone.utc)
                    limit_cutoff_dt = dt_c + timedelta(minutes=30)
                except Exception:
                    pass

            if dt_c:
                start_dt = dt_c - timedelta(minutes=15)
                if limit_cutoff_dt:
                    df_window = df[(df.index >= start_dt) & (df.index <= limit_cutoff_dt)]
                else:
                    df_window = df[df.index >= start_dt]
            else:
                df_window = df

            if df_window is None or df_window.empty:
                df_window = df

            filled = False
            fill_time_str = None

            # Instant Real-Time Live Ticker Match Check
            if live_price is not None and live_price > 0:
                if signal == "BUY" and live_price <= entry:
                    filled = True
                    fill_time_str = now_utc.isoformat()
                elif signal == "SELL" and live_price >= entry:
                    filled = True
                    fill_time_str = now_utc.isoformat()

            # Historical / 5m Candle High-Low Match Check
            if not filled and df_window is not None and not df_window.empty:
                for idx, candle in df_window.iterrows():
                    c_high = float(candle["high"])
                    c_low = float(candle["low"])

                    if signal == "BUY" and c_low <= entry:
                        filled = True
                        fill_time_str = str(idx)
                        break
                    elif signal == "SELL" and c_high >= entry:
                        filled = True
                        fill_time_str = str(idx)
                        break

            if filled:
                order["status"] = "FILLED"
                order["filled_at"] = fill_time_str or now_utc.isoformat()

                # Deduct Maker Entry Fee
                ord_notional = order.get("notional_inr", NOTIONAL_INR)
                maker_fee = ord_notional * MAKER_FEE_RATE
                wallet["balance"] -= maker_fee
                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + maker_fee
                order["fee_paid"] = order.get("fee_paid", 0.0) + maker_fee

                active.append(order)
                wallet_updated = True
                
                # Broadcast Filled Alert
                msg = (
                    f"⚡ <b>PAPER ORDER FILLED (EXCHANGE MAKER MATCH)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [{signal}]\n"
                    f"🎯 <b>Filled Entry:</b> {format_price(entry, symbol)}\n"
                    f"📦 <b>Size:</b> {order.get('qty', 0.0):.4f} contracts (~₹{ord_notional:,.0f} INR)\n"
                    f"💳 <b>Maker Fee (0.02%):</b> -₹{maker_fee:.2f} INR\n"
                    f"🛡️ <b>Est. Liq Price:</b> ~{format_price(order.get('liq_price', 0.0), symbol)}\n"
                    f"💼 <b>Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR\n"
                    f"<i>Status: Position Active. 2-Stage TP/SL brackets armed (TP1: 70% Book | TP2: 30% Runner)!</i>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                asyncio.create_task(notify_telegram(msg))
            else:
                # Check 30-minute expiry
                if limit_cutoff_dt and now_utc >= limit_cutoff_dt:
                    order["status"] = "CANCELLED_30MIN_EXPIRED"
                    order["closed_at"] = now_utc.isoformat()
                    closed.append(order)
                    wallet_updated = True

                    closest_px = entry
                    missed_by = 0.0
                    missed_pct = 0.0
                    if df_window is not None and not df_window.empty:
                        if signal == "BUY":
                            closest_px = float(df_window["low"].min())
                            missed_by = closest_px - entry
                        else:
                            closest_px = float(df_window["high"].max())
                            missed_by = entry - closest_px
                        missed_pct = (abs(missed_by) / entry) * 100.0 if entry > 0 else 0.0

                    closest_str = format_price(closest_px, symbol)
                    missed_str = f"{format_price(abs(missed_by), symbol)} ({missed_pct:.2f}%)"

                    msg = (
                        f"⏰ <b>PAPER ORDER UNFULFILLED (30-MIN EXPIRATION)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<b>Asset:</b> {symbol} [{signal}]\n"
                        f"🎯 <b>Entry Limit:</b> {format_price(entry, symbol)}\n"
                        f"📊 <b>Closest Price in 30m:</b> {closest_str}\n"
                        f"❌ <b>Missed Fill By:</b> {missed_str}\n"
                        f"💡 <b>Strategy Lookout:</b> Price moved away without pulling back into the 9 EMA limit.\n"
                        f"🛡️ <b>Capital Status:</b> Cancelled safely with ₹0 INR loss.\n"
                        f"💼 <b>Virtual Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    asyncio.create_task(notify_telegram(msg))
                else:
                    remaining_resting.append(order)

        wallet["resting_orders"] = remaining_resting
        wallet["active_positions"] = active

        # ── 2. CHECK ACTIVE POSITIONS (TP/SL + 20-HOUR TIMEOUT RULE) ───────────────
        remaining_active = []
        for pos in active:
            if pos["symbol"] != symbol:
                remaining_active.append(pos)
                continue

            entry = pos["entry_limit"]
            signal = pos["signal"]
            sl = pos["current_sl"]
            tp1 = pos["tp1"]
            tp2 = pos["tp2"]
            tp3 = pos["tp3"]

            filled_ts = pos.get("filled_at") or pos.get("created_at")
            dt_f = None
            pos_cutoff_dt = None
            if filled_ts:
                try:
                    dt_f = datetime.fromisoformat(filled_ts.replace("Z", "+00:00"))
                    pos_cutoff_dt = dt_f + timedelta(hours=20)
                except Exception:
                    pass

            if dt_f:
                df_pos = df[df.index >= dt_f]
            else:
                df_pos = df

            if df_pos.empty:
                df_pos = df

            position_closed = False

            # Candle-by-candle evaluation
            for idx, candle in df_pos.iterrows():
                c_high = float(candle["high"])
                c_low = float(candle["low"])
                candle_dt = idx if isinstance(idx, datetime) else None

                # Check 20-Hour Timeout Cutoff on historical candles
                is_timeout = False
                if pos_cutoff_dt and candle_dt and candle_dt >= pos_cutoff_dt:
                    is_timeout = True

                # Check 12-Hour Stale Runner rule (after TP1 hit)
                if pos.get("tp1_hit") and not position_closed:
                    tp1_hit_at_str = pos.get("tp1_hit_at")
                    if tp1_hit_at_str:
                        try:
                            dt_tp1 = datetime.fromisoformat(tp1_hit_at_str.replace("Z", "+00:00"))
                            if dt_tp1.tzinfo is None:
                                dt_tp1 = dt_tp1.replace(tzinfo=timezone.utc)
                            if (now_utc - dt_tp1).total_seconds() >= 43200:  # 12 hours
                                position_closed = True
                                pos["status"] = "CLOSED_12HR_STALE_RUNNER"
                                pos["closed_at"] = now_utc.isoformat()
                                curr_p = live_price if (live_price and live_price > 0) else entry
                                if signal == "BUY":
                                    rem_gross = (pos.get("notional_inr", NOTIONAL_INR) * 0.30) * ((curr_p - entry) / entry)
                                else:
                                    rem_gross = (pos.get("notional_inr", NOTIONAL_INR) * 0.30) * ((entry - curr_p) / entry)
                                taker_fee = (pos.get("notional_inr", NOTIONAL_INR) * 0.30) * TAKER_FEE_RATE
                                rem_net = rem_gross - taker_fee
                                pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + rem_net
                                wallet["balance"] += rem_net
                                wallet["realized_pnl"] += rem_net
                                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                                pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                                pos["remaining_qty"] = 0.0
                                if pos["cumulative_pnl"] >= 0:
                                    wallet["wins"] += 1
                                else:
                                    wallet["losses"] += 1
                                wallet["total_trades"] += 1
                                closed.append(pos)
                                wallet_updated = True
                                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                                msg = (
                                    f"⏳ <b>PAPER RUNNER CLOSED (12-HR STALE TRADE RULE)</b>\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"<b>Asset:</b> {symbol} [{signal}]\n"
                                    f"<b>Reason:</b> 12 hours elapsed after TP1 hit without hitting TP2 or SL.\n"
                                    f"🎯 <b>Exit Price:</b> {format_price(curr_p, symbol)}\n"
                                    f"<b>Total Trade Realized Profit:</b> +₹{pos['cumulative_pnl']:,.2f} INR\n"
                                    f"💡 <i>Freed ₹{pos.get('margin_inr', MARGIN_INR):,.0f} INR margin for fresh high-momentum setups!</i>\n"
                                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                                )
                                if not pos.get("closed_alert_sent"):
                                    pos["closed_alert_sent"] = True
                                    asyncio.create_task(notify_telegram(msg))
                                break
                        except Exception as ex_stale:
                            logger.warning(f"Error checking 12-hr stale runner for {symbol}: {ex_stale}")

                if is_timeout and not pos.get("tp3_hit", False):
                    # 20-Hour Position Timeout: Close remaining size at Current Market Price
                    position_closed = True
                    pos["status"] = "CLOSED_20HR_TIMEOUT_MARKET"
                    pos["closed_at"] = now_utc.isoformat()

                    # Live exit price (from live ticker if available, else candle close, else entry)
                    exit_p = live_price if (live_price is not None and live_price > 0) else float(candle.get("close", entry))
                    rem_portion = 1.0 if not pos.get("tp1_hit", False) else 0.30
                    rem_notional = pos.get("notional_inr", NOTIONAL_INR) * rem_portion

                    if signal == "BUY":
                        rem_gross = rem_notional * ((exit_p - entry) / entry)
                    else:
                        rem_gross = rem_notional * ((entry - exit_p) / entry)

                    taker_fee = rem_notional * TAKER_FEE_RATE
                    rem_net = rem_gross - taker_fee

                    wallet["balance"] += rem_net
                    wallet["realized_pnl"] += rem_net
                    wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                    pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                    pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + rem_net
                    pos["exit_price"] = exit_p
                    pos["remaining_qty"] = 0.0

                    if pos["cumulative_pnl"] >= 0:
                        wallet["wins"] += 1
                    else:
                        wallet["losses"] += 1
                    wallet["total_trades"] += 1

                    closed.append(pos)
                    wallet_updated = True

                    roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                    pnl_str = format_inr(pos["cumulative_pnl"])
                    pts_diff = (exit_p - entry) if signal == "BUY" else (entry - exit_p)
                    pts_pct = (pts_diff / entry) * 100.0 if entry > 0 else 0.0
                    msg = (
                        f"⏱️ <b>PAPER POSITION CLOSED (20-HOUR TIMEOUT @ MARKET PRICE)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<b>Asset:</b> {symbol} [{signal}]\n"
                        f"<b>Reason:</b> Position active for 20 hours. Squared off at live market price!\n"
                        f"🎯 <b>Entry Price:</b> {format_price(entry, symbol)}\n"
                        f"🏁 <b>Exit Price (Market):</b> {format_price(exit_p, symbol)} ({pts_diff:+.4f} pts | {pts_pct:+.2f}%)\n"
                        f"💰 <b>Total Trade Realized Profit:</b> {pnl_str}\n"
                        f"💳 <b>Total Fees Paid:</b> ₹{pos['fee_paid']:,.2f} INR\n"
                        f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    if not pos.get("closed_alert_sent"):
                        pos["closed_alert_sent"] = True
                        asyncio.create_task(notify_telegram(msg))
                    break

                is_closed, is_upd = _process_pos_price_triggers(pos, wallet, closed, c_high, c_low, now_utc, live_tick=False)
                if is_upd:
                    wallet_updated = True
                if is_closed:
                    position_closed = True
                    break

            # ── REAL-TIME TICKER PRICE LIVE CHECK ──
            if not position_closed and live_price is not None and live_price > 0:
                is_closed, is_upd = _process_pos_price_triggers(pos, wallet, closed, live_price, live_price, now_utc, live_tick=True)
                if is_upd:
                    wallet_updated = True
                if is_closed:
                    position_closed = True

            # ── 20-HOUR TIMEOUT CHECK (AFTER FULL SWEEP) ──
            if not position_closed and pos_cutoff_dt and now_utc >= pos_cutoff_dt and not pos.get("tp3_hit", False):
                position_closed = True
                pos["status"] = "CLOSED_20HR_TIMEOUT_MARKET"
                pos["closed_at"] = now_utc.isoformat()
                exit_p = live_price if (live_price is not None and live_price > 0) else entry
                rem_portion = 1.0 if not pos.get("tp1_hit", False) else 0.30
                rem_notional = pos.get("notional_inr", NOTIONAL_INR) * rem_portion

                if signal == "BUY":
                    rem_gross = rem_notional * ((exit_p - entry) / entry)
                else:
                    rem_gross = rem_notional * ((entry - exit_p) / entry)

                taker_fee = rem_notional * TAKER_FEE_RATE
                rem_net = rem_gross - taker_fee

                wallet["balance"] += rem_net
                wallet["realized_pnl"] += rem_net
                wallet["total_fees_paid"] = wallet.get("total_fees_paid", 0.0) + taker_fee
                pos["fee_paid"] = pos.get("fee_paid", 0.0) + taker_fee
                pos["cumulative_pnl"] = pos.get("cumulative_pnl", 0.0) + rem_net
                pos["exit_price"] = exit_p
                pos["remaining_qty"] = 0.0

                if pos["cumulative_pnl"] >= 0:
                    wallet["wins"] += 1
                else:
                    wallet["losses"] += 1
                wallet["total_trades"] += 1
                closed.append(pos)
                wallet_updated = True
                roi_pct = ((wallet["balance"] - wallet["initial_capital"]) / wallet["initial_capital"]) * 100.0
                pnl_str = format_inr(pos["cumulative_pnl"])
                pts_diff = (exit_p - entry) if signal == "BUY" else (entry - exit_p)
                pts_pct = (pts_diff / entry) * 100.0 if entry > 0 else 0.0
                msg = (
                    f"⏱️ <b>PAPER POSITION CLOSED (20-HOUR TIMEOUT @ MARKET PRICE)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {symbol} [{signal}]\n"
                    f"<b>Reason:</b> Position active for 20 hours. Squared off at live market price!\n"
                    f"🎯 <b>Entry Price:</b> {format_price(entry, symbol)}\n"
                    f"🏁 <b>Exit Price (Market):</b> {format_price(exit_p, symbol)} ({pts_diff:+.4f} pts | {pts_pct:+.2f}%)\n"
                    f"💰 <b>Total Trade Realized Profit:</b> {pnl_str}\n"
                    f"💳 <b>Total Fees Paid:</b> ₹{pos['fee_paid']:,.2f} INR\n"
                    f"💼 <b>Updated Wallet Balance:</b> ₹{wallet['balance']:,.2f} INR ({roi_pct:+.2f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                if not pos.get("closed_alert_sent"):
                    pos["closed_alert_sent"] = True
                    asyncio.create_task(notify_telegram(msg))

            if not position_closed:
                remaining_active.append(pos)

        wallet["active_positions"] = remaining_active
        wallet["closed_trades"] = closed

        if wallet_updated:
            save_wallet(wallet)


async def generate_strategy_lookout_card(session=None) -> str:
    """
    Generates a real-time Strategy Lookout card tracking all active open positions,
    floating PnL in ₹ INR and %, distance to TPs/SL, and remaining expiration for resting orders.
    """
    wallet = load_wallet()
    balance = wallet.get("balance", INITIAL_CAPITAL)
    initial_cap = wallet.get("initial_capital", INITIAL_CAPITAL)
    total_roi = ((balance - initial_cap) / initial_cap) * 100.0
    closed = wallet.get("closed_trades", [])
    exec_trades = [t for t in closed if "CANCEL" not in str(t.get("status", "")) and t.get("symbol") != "PROFIT_INJECTION"]
    wins = sum(1 for t in exec_trades if float(t.get("cumulative_pnl", 0.0) or 0.0) > 0)
    losses = sum(1 for t in exec_trades if float(t.get("cumulative_pnl", 0.0) or 0.0) <= 0)
    total_trades = len(exec_trades)
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    active = wallet.get("active_positions", [])
    resting = wallet.get("resting_orders", [])
    now_utc = datetime.now(timezone.utc)

    # Fetch live ticker prices for active positions
    live_prices = {}
    if active:
        need_session_close = False
        if session is None:
            session = aiohttp.ClientSession()
            need_session_close = True
        try:
            for pos in active:
                sym = pos["symbol"].replace("/", "")
                url = f"https://data-api.binance.vision/api/v3/ticker/price?symbol={sym}"
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        live_prices[pos["symbol"]] = float(data.get("price", 0.0))
        except Exception as e:
            logger.error(f"Error fetching live prices for lookout: {e}")
        finally:
            if need_session_close:
                await session.close()

    msg = (
        f"💼 <b>PAPER TRADING — REAL-TIME STRATEGY LOOKOUT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Wallet Balance:</b> ₹{balance:,.2f} INR ({total_roi:+.2f}% ROI)\n"
        f"📊 <b>Scoreboard:</b> {wins}W - {losses}L ({win_rate:.1f}% WR | {total_trades} Closed)\n"
    )

    if active:
        msg += f"\n🟢 <b>ACTIVE POSITIONS ({len(active)}):</b>\n"
        for i, pos in enumerate(active, 1):
            sym = pos["symbol"]
            sig = pos["signal"]
            entry = pos["entry_limit"]
            sl = pos.get("current_sl", pos["sl"])
            tp1 = pos.get("tp1", 0.0)
            tp2 = pos.get("tp2", 0.0)
            tp3 = pos.get("tp3", 0.0)
            curr_px = live_prices.get(sym, entry)

            pnl_pct = ((curr_px - entry) / entry) * 100.0 if sig == "BUY" else ((entry - curr_px) / entry) * 100.0
            pos_notional = pos.get("notional_inr", NOTIONAL_INR)
            pnl_inr = pos_notional * (pnl_pct / 100.0)
            pnl_sign = "+" if pnl_inr >= 0 else ""
            pnl_icon = "🟢" if pnl_inr >= 0 else "🔴"

            dist_tp1 = ((tp1 - curr_px) / curr_px) * 100.0 if sig == "BUY" else ((curr_px - tp1) / curr_px) * 100.0
            dist_sl = ((curr_px - sl) / curr_px) * 100.0 if sig == "BUY" else ((sl - curr_px) / curr_px) * 100.0

            # Duration active
            dur_str = "Active"
            filled_ts = pos.get("filled_at")
            if filled_ts:
                try:
                    f_dt = datetime.fromisoformat(filled_ts.replace("Z", "+00:00"))
                    if f_dt.tzinfo is None:
                        f_dt = f_dt.replace(tzinfo=timezone.utc)
                    elapsed_m = int((now_utc - f_dt).total_seconds() / 60)
                    h, m = divmod(elapsed_m, 60)
                    rem_h = max(0, 20 - (h + 1))
                    dur_str = f"{h}h {m}m active (20h CTC in ~{rem_h}h)"
                except Exception:
                    pass

            pos_notional = pos.get("notional_inr", NOTIONAL_INR)
            rem_contracts = pos.get('remaining_qty', pos.get('qty', 0.0))
            notional_u = pos.get('notional_usd', pos_notional / USD_INR_RATE)
            liq_p = pos.get('liq_price', 0.0)

            msg += (
                f"{i}. <b>{sym} [{sig}] 10x Isolated</b>\n"
                f"   • Entry: {format_price(entry, sym)} | Live: {format_price(curr_px, sym)}\n"
                f"   • {pnl_icon} Floating PnL: {pnl_sign}₹{pnl_inr:,.2f} INR ({pnl_pct:+.2f}%)\n"
                f"   • 📦 Contracts: {rem_contracts:.4f} (~${notional_u:.2f} USD / ₹{pos_notional:,.0f} INR)\n"
                f"   • 🎯 Targets: TP1: {format_price(tp1, sym)} | TP2: {format_price(tp2 or tp3, sym)}\n"
                f"   • 🛡️ Current SL: {format_price(sl, sym)} (Buffer: {dist_sl:.2f}%)\n"
                f"   • 💀 Est. Liq: ~{format_price(liq_p, sym)}\n"
                f"   • ⏱️ Duration: {dur_str}\n"
            )
    else:
        msg += "\n🟢 <b>ACTIVE POSITIONS:</b> None currently open.\n"

    if resting:
        msg += f"\n⏳ <b>RESTING LIMIT ORDERS ({len(resting)}):</b>\n"
        for i, order in enumerate(resting, 1):
            sym = order["symbol"]
            sig = order["signal"]
            entry = order["entry_limit"]
            c_ts = order.get("created_at")
            rem_m = 20
            if c_ts:
                try:
                    c_dt = datetime.fromisoformat(c_ts.replace("Z", "+00:00"))
                    if c_dt.tzinfo is None:
                        c_dt = c_dt.replace(tzinfo=timezone.utc)
                    elapsed_m = int((now_utc - c_dt).total_seconds() / 60)
                    rem_m = max(0, 30 - elapsed_m)
                except Exception:
                    pass
            qty_r = order.get("qty", 0.0)
            msg += f"{i}. {sym} [{sig}] Limit @ {format_price(entry, sym)} ({qty_r:.4f} contracts, ~{rem_m}m left)\n"
    else:
        msg += "\n⏳ <b>RESTING LIMIT ORDERS:</b> None currently resting.\n"

    msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━"
    return msg


async def generate_trades_ledger_card() -> str:
    """
    Generates an institutional Exchange Ledger & Audit History card showing closed trades,
    gross profit/loss, profit factor, exchange fees paid, win rate, and outcome status.
    """
    wallet = load_wallet()
    balance = wallet.get("balance", INITIAL_CAPITAL)
    initial_cap = wallet.get("initial_capital", INITIAL_CAPITAL)
    total_roi = ((balance - initial_cap) / initial_cap) * 100.0
    fees_paid = wallet.get("total_fees_paid", 0.0)
    closed = wallet.get("closed_trades", [])
    exec_trades = [t for t in closed if "CANCEL" not in str(t.get("status", "")) and t.get("symbol") != "PROFIT_INJECTION"]
    wins = sum(1 for t in exec_trades if float(t.get("cumulative_pnl", 0.0) or 0.0) > 0)
    losses = sum(1 for t in exec_trades if float(t.get("cumulative_pnl", 0.0) or 0.0) <= 0)
    total_trades = len(exec_trades)
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0

    gross_wins = sum(t.get("cumulative_pnl", 0.0) for t in closed if t.get("cumulative_pnl", 0.0) > 0)
    gross_losses = sum(abs(t.get("cumulative_pnl", 0.0)) for t in closed if t.get("cumulative_pnl", 0.0) < 0)
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (99.0 if gross_wins > 0 else 0.0)

    msg = (
        f"📖 <b>EXCHANGE TRADE LEDGER & HISTORY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Wallet Balance:</b> ₹{balance:,.2f} INR ({total_roi:+.2f}% ROI)\n"
        f"📊 <b>Scoreboard:</b> {wins}W - {losses}L | Win Rate: {win_rate:.1f}%\n"
        f"⚖️ <b>Profit Factor:</b> {profit_factor:.2f} (Gross: +₹{gross_wins:,.2f} / -₹{gross_losses:,.2f})\n"
        f"💳 <b>Exchange Fees Paid:</b> ₹{fees_paid:,.2f} INR\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    if not closed:
        msg += "\n<i>No completed trades logged yet.</i>\n"
    else:
        msg += f"\n<b>Recent Closed Trades (Last 10):</b>\n"
        recent_closed = closed[-10:][::-1]
        for i, t in enumerate(recent_closed, 1):
            sym = t.get("symbol", "UNKNOWN")
            sig = t.get("signal", "N/A")
            entry = t.get("entry_limit", 0.0)
            status = t.get("status", "CLOSED")
            pnl = t.get("cumulative_pnl", 0.0)
            fee = t.get("fee_paid", 0.0)
            pnl_icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            pnl_str = f"+₹{pnl:,.2f}" if pnl > 0 else f"-₹{abs(pnl):,.2f}"

            status_label = {
                "FULL_WIN_TP3": "TP2 Full Win 🚀",
    "FULL_WIN_TP2": "TP2 Full Win 🚀",
                "CLOSED_TP1_TRAIL_WIN": "TP2 + TP1 Trail ✅",
                "CLOSED_CTC_BREAKEVEN": "TP1 + CTC Breakeven 🛡️",
                "SL_HIT": "Stop-Loss Hit 🔴",
                "LIQUIDATED": "Liquidated 💀",
                "CLOSED_20HR_TIMEOUT_MARKET": "20H Timeout Market ⏱️",
                "CLOSED_20HR_TIMEOUT_CTC": "20H Timeout Market ⏱️",
                "CANCELLED_20MIN_EXPIRED": "20m Expired (Cancelled) ⏳",
                "CLOSED_SIGNAL_REVERSAL": "Reversal Close 🔄"
            }.get(status, status)

            msg += (
                f"{i}. {pnl_icon} <b>{sym} [{sig}]</b> — <b>{pnl_str} INR</b>\n"
                f"   • Outcome: {status_label}\n"
                f"   • Entry: {format_price(entry, sym)} | Fees: ₹{fee:,.2f}\n"
            )

    msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━"
    return msg



async def send_daily_paper_trading_digest():
    """Generates and broadcasts daily performance digest card at 10:00 PM IST."""
    wallet = load_wallet()
    import zoneinfo
    from datetime import datetime, timezone
    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(timezone.utc).astimezone(ist_tz)
    today_str = now_ist.strftime("%Y-%m-%d")

    closed_trades = wallet.get("closed_trades", [])
    today_closed = [t for t in closed_trades if str(t.get("closed_at", "")).startswith(today_str)]

    total_today = len(today_closed)
    wins_today = sum(1 for t in today_closed if float(t.get("cumulative_pnl", 0)) > 0)
    losses_today = sum(1 for t in today_closed if float(t.get("cumulative_pnl", 0)) <= 0)
    win_rate_today = (wins_today / total_today * 100) if total_today > 0 else 0.0
    pnl_today_inr = sum(float(t.get("cumulative_pnl", 0)) for t in today_closed)
    pnl_today_usd = pnl_today_inr / USD_INR_RATE

    balance_inr = float(wallet.get("balance", 100000.0))
    initial_cap = float(wallet.get("initial_capital", 100000.0))
    roi_pct = ((balance_inr - initial_cap) / initial_cap) * 100.0

    sign_pnl = "+" if pnl_today_inr >= 0 else ""

    msg = (
        f"📊 <b>DAILY PERFORMANCE DIGEST ({now_ist.strftime('%d %B %Y')})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Today's Trades:</b> {total_today} | <b>Wins:</b> {wins_today} | <b>Losses:</b> {losses_today}\n"
        f"🏆 <b>Today's Win Rate:</b> <code>{win_rate_today:.1f}%</code>\n"
        f"💰 <b>Today's Realized PnL:</b> <code>{sign_pnl}₹{pnl_today_inr:,.2f} INR ({sign_pnl}${pnl_today_usd:.2f} USD)</code>\n\n"
        f"💼 <b>RUNNING PAPER PORTFOLIO:</b>\n"
        f"├ <b>Wallet Capital:</b> ₹{balance_inr:,.2f} INR\n"
        f"├ <b>All-Time Realized ROI:</b> <code>{roi_pct:+.2f}%</code>\n"
        f"└ <b>Total Closed Setups:</b> {len(closed_trades)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <i>100% Data-Driven & Audited 4-Star Execution Engine</i>"
    )

    from telegram_bot import send_text_alert, send_text_to_chat
    personal_dm = "771305593"
    try:
        await send_text_to_chat(personal_dm, msg)
        await send_text_alert(msg)
        logger.info("Sent daily performance digest card.")
    except Exception as e_dig:
        logger.error(f"Error sending daily digest: {e_dig}")
