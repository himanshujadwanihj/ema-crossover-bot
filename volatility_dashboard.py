"""
volatility_dashboard.py — 4H Highest Volatility Movers Dashboard.
Calculates top 2 Gainers and top 2 Losers in the last 4 hours across Binance Futures pairs,
analyzes EMA crossovers/retests and coin categories, and maintains a silently edited PINNED message
in the Telegram EMA Signal Channel (-1004463573705).
"""

import asyncio
import logging
import json
import os
import time
import aiohttp
from ta.trend import EMAIndicator
from datetime import datetime, timezone
import zoneinfo

from config import TELEGRAM_BOT_TOKEN
from telegram_bot import format_display_symbol

logger = logging.getLogger("volatility_dashboard")

STATE_FILE = "/data/pinned_dashboard_state.json" if os.path.isdir("/data") else "pinned_dashboard_state.json"
BACKUP_STATE_FILE = "pinned_dashboard_state.json"
KNOWN_DASHBOARD_MSG_ID = 6697
TARGET_CHANNELS = ["-1004332146822"]  # EMA Scanner Channel ONLY (Removed from EMA Cross Channel)

# Coin Category Classification Rules
HIGH_CAP_MAJORS = {
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "SUI/USDT",
    "NEAR/USDT", "LTC/USDT", "BCH/USDT", "DOT/USDT", "TRX/USDT"
}

COMMODITY_SYNTHETICS = {
    "XAU/USDT", "XAG/USDT", "XAUT/USDT", "PAXG/USDT"
}

AI_TECH_COINS = {
    "FET/USDT", "TAO/USDT", "RENDER/USDT", "AIN/USDT", "GRIFFAIN/USDT",
    "GRT/USDT", "IO/USDT", "ACT/USDT", "FIL/USDT", "AR/USDT"
}

MEME_SPECULATIVE = {
    "PEPE/USDT", "WIF/USDT", "FLOKI/USDT", "BONK/USDT", "PNUT/USDT",
    "SHIB/USDT", "PUMP/USDT", "PUMPBTC/USDT", "MUBARAK/USDT", "HYPE/USDT", "NOT/USDT"
}


def get_coin_category(symbol: str) -> str:
    """Returns human-readable category for a coin symbol."""
    sym_upper = symbol.upper()
    if sym_upper in HIGH_CAP_MAJORS:
        return "High-Cap Major"
    elif sym_upper in COMMODITY_SYNTHETICS:
        return "Precious Metal Commodity"
    elif sym_upper in AI_TECH_COINS or "AI" in sym_upper:
        return "AI / Tech Volatility Altcoin"
    elif sym_upper in MEME_SPECULATIVE:
        return "Meme / Speculative Volatility"
    else:
        return "DeFi / Layer-1 Volatility Altcoin"


def load_pinned_state() -> dict:
    for path in [STATE_FILE, BACKUP_STATE_FILE]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    if d and isinstance(d, dict):
                        return d
            except Exception:
                pass
    return {"-1004332146822": KNOWN_DASHBOARD_MSG_ID}


def save_pinned_state(state: dict):
    for path in [STATE_FILE, BACKUP_STATE_FILE]:
        try:
            os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save pinned dashboard state to {path}: {e}")


async def analyze_coin_ema(session: aiohttp.ClientSession, symbol: str, semaphore: asyncio.Semaphore) -> dict:
    """Fetches OHLCV across 30M, 1H, 4H, 1D, 1W timeframes and determines exact Crossover, Retest TF, and HTF Alignment status."""
    from fetcher import fetch_ohlcv_async
    
    df_30m = await fetch_ohlcv_async(session, symbol, "30m", 50, semaphore)
    df_1h  = await fetch_ohlcv_async(session, symbol, "1h", 50, semaphore)
    df_4h  = await fetch_ohlcv_async(session, symbol, "4h", 50, semaphore)
    df_1d  = await fetch_ohlcv_async(session, symbol, "1d", 50, semaphore)
    df_1w  = await fetch_ohlcv_async(session, symbol, "1w", 50, semaphore)
    
    if df_1h is None or df_1h.empty or len(df_1h) < 28:
        return {"events": ["Active Trend Monitoring"], "alignment": "⭐ HTF Alignment: Monitoring"}

    for df in [df_30m, df_1h, df_4h, df_1d, df_1w]:
        if df is not None and not df.empty and len(df) >= 28:
            if "ema9" not in df:
                df["ema9"] = EMAIndicator(close=df["close"], window=9).ema_indicator()
                df["ema26"] = EMAIndicator(close=df["close"], window=26).ema_indicator()

    events = []

    # 1. Check Fresh Crossovers (30M, 1H, 4H)
    for label, df in [("30M", df_30m), ("1H", df_1h), ("4H", df_4h)]:
        if df is not None and len(df) >= 3:
            prev = df.iloc[-3]
            curr = df.iloc[-2]
            if prev["ema9"] <= prev["ema26"] and curr["ema9"] > curr["ema26"]:
                events.append(f"⚡ <b>{label}:</b> Fresh Bullish Crossover")
            elif prev["ema9"] >= prev["ema26"] and curr["ema9"] < curr["ema26"]:
                events.append(f"⚡ <b>{label}:</b> Fresh Bearish Crossover")

    # 2. Check EMA Retests (1H, 4H, 1D)
    for label, df in [("1H", df_1h), ("4H", df_4h), ("1D", df_1d)]:
        if df is not None and len(df) >= 28:
            c = df.iloc[-1]
            c_low, c_high, c_close = c["low"], c["high"], c["close"]
            e9, e26 = c["ema9"], c["ema26"]

            if c_close >= e9 and c_low <= e9:
                events.append(f"🔄 <b>{label}:</b> EMA 9 Support Retest")
            elif c_close <= e9 and c_high >= e9:
                events.append(f"🔄 <b>{label}:</b> EMA 9 Resistance Retest")
            elif c_close >= e26 and c_low <= e26:
                events.append(f"🔄 <b>{label}:</b> EMA 26 Support Retest")
            elif c_close <= e26 and c_high >= e26:
                events.append(f"🔄 <b>{label}:</b> EMA 26 Resistance Retest")

    # 3. HTF Trend Alignment (1H, 4H, 1D, 1W)
    curr_1h = df_1h.iloc[-2]
    is_bullish = curr_1h["ema9"] > curr_1h["ema26"]
    
    aligned_count = 0
    total_tfs = 0
    for label, df in [("1H", df_1h), ("4H", df_4h), ("1D", df_1d), ("1W", df_1w)]:
        if df is not None and len(df) >= 28:
            total_tfs += 1
            c = df.iloc[-1]
            if (is_bullish and c["ema9"] > c["ema26"]) or (not is_bullish and c["ema9"] < c["ema26"]):
                aligned_count += 1

    trend_label = "Bullish Expansion" if is_bullish else "Bearish Collapse"
    alignment_str = f"⭐ <b>HTF Alignment:</b> {aligned_count}/{total_tfs} {trend_label}"

    if not events:
        events.append(f"📈 <b>Trend State:</b> {trend_label}")

    return {
        "events": events,
        "alignment": alignment_str
    }


async def fetch_4h_volatility_movers(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
    """
    Identifies top 2 Gainers and top 2 Losers in the last 4 Hours and locks them for 4 hours.
    During the 4-hour window, monitors and updates EMA status & prices of those locked coins.
    Returns (gainers_list, losers_list).
    """
    state = load_pinned_state()
    now = time.time()
    
    locked_gainers = state.get("locked_gainers", [])
    locked_losers = state.get("locked_losers", [])
    locked_time = float(state.get("locked_time", 0.0))
    
    FOUR_HOURS = 14400.0  # 4 Hours in seconds
    is_expired = (now - locked_time) >= FOUR_HOURS or not locked_gainers or not locked_losers

    from fetcher import fetch_ohlcv_async

    if not is_expired:
        # ── TRACK LOCKED COINS DURING THE 4-HOUR WINDOW ──
        gainers_res = []
        losers_res = []

        for sym in locked_gainers:
            try:
                df = await fetch_ohlcv_async(session, sym, "1h", 6, semaphore)
                if df is not None and len(df) >= 5:
                    open_4h_ago = float(df.iloc[-5]["open"])
                    current_close = float(df.iloc[-1]["close"])
                    pct_4h = ((current_close - open_4h_ago) / open_4h_ago) * 100.0 if open_4h_ago > 0 else 0.0
                    ema_status = await analyze_coin_ema(session, sym, semaphore)
                    category = get_coin_category(sym)
                    gainers_res.append({
                        "symbol": sym,
                        "pct_4h": pct_4h,
                        "close": current_close,
                        "category": category,
                        "ema_status": ema_status
                    })
            except Exception as e:
                logger.debug(f"Error updating locked gainer {sym}: {e}")

        for sym in locked_losers:
            try:
                df = await fetch_ohlcv_async(session, sym, "1h", 6, semaphore)
                if df is not None and len(df) >= 5:
                    open_4h_ago = float(df.iloc[-5]["open"])
                    current_close = float(df.iloc[-1]["close"])
                    pct_4h = ((current_close - open_4h_ago) / open_4h_ago) * 100.0 if open_4h_ago > 0 else 0.0
                    ema_status = await analyze_coin_ema(session, sym, semaphore)
                    category = get_coin_category(sym)
                    losers_res.append({
                        "symbol": sym,
                        "pct_4h": pct_4h,
                        "close": current_close,
                        "category": category,
                        "ema_status": ema_status
                    })
            except Exception as e:
                logger.debug(f"Error updating locked loser {sym}: {e}")

        if gainers_res and losers_res:
            return gainers_res, losers_res

    # ── RESCAN MARKET FOR NEW 4-HOUR WINDOW ──
    logger.info("⚡ 4-Hour Window Expired: Rescanning market for new 4H Volatility Movers across FULL coin list (No Volume Constraint)...")
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    raw = None
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                raw = await resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch 24h tickers: {e}")

    candidates = []
    stable_prefixes = ("USD", "EUR", "GBP", "AUD", "CAD", "JPY", "CHF", "BRL", "TRY")
    known_stables = {"U", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDD", "RLUSD", "AEUR", "PYUSD", "USDE", "USD1", "USD0", "USDS", "WBTC", "CBETH", "WBETH", "STETH"}

    if raw and isinstance(raw, list):
        for item in raw:
            sym = item.get("symbol", "")
            if sym.endswith("USDT"):
                base = sym[:-4].upper()
                if base not in known_stables and not any(base.startswith(p) for p in stable_prefixes):
                    vol = float(item.get("quoteVolume") or 0.0)
                    # USER DIRECTIVE: NO VOLUME CONSTRAINT! Every available coin on Binance Futures is evaluated.
                    candidates.append((f"{base}/USDT", sym, vol))
    else:
        from fetcher import get_top_pairs_by_volume
        top_pairs = get_top_pairs_by_volume(100)
        candidates = [(sym, sym.replace('/', ''), 10000000.0) for sym in top_pairs]

    logger.info(f"Scanning 4H returns across FULL coin list ({len(candidates)} pairs, no volume constraint)...")

    scan_sem = asyncio.Semaphore(20)
    from fetcher import fetch_ohlcv_async
    async def _fetch_candidate_4h(formatted_sym, bin_sym, vol):
        try:
            df = await fetch_ohlcv_async(session, formatted_sym, "1h", 6, scan_sem)
            if df is not None and len(df) >= 5:
                open_4h_ago = float(df.iloc[-5]["open"])
                current_close = float(df.iloc[-1]["close"])
                if open_4h_ago > 0:
                    pct_4h = ((current_close - open_4h_ago) / open_4h_ago) * 100.0
                    return {
                        "symbol": formatted_sym,
                        "pct_4h": pct_4h,
                        "close": current_close,
                        "volume": vol
                    }
        except Exception:
            pass
        return None

    tasks = [_fetch_candidate_4h(c[0], c[1], c[2]) for c in candidates]
    all_res = await asyncio.gather(*tasks)
    mover_results = [r for r in all_res if r is not None]

    if not mover_results:
        return [], []

    mover_results.sort(key=lambda x: x["pct_4h"], reverse=True)
    gainers_raw = mover_results[:2]
    losers_raw = mover_results[-2:]
    losers_raw.reverse()

    gainers = []
    for g in gainers_raw:
        ema_status = await analyze_coin_ema(session, g["symbol"], semaphore)
        category = get_coin_category(g["symbol"])
        gainers.append({
            "symbol": g["symbol"],
            "pct_4h": g["pct_4h"],
            "close": g["close"],
            "category": category,
            "ema_status": ema_status,
            "volume": g["volume"]
        })

    losers = []
    for l in losers_raw:
        ema_status = await analyze_coin_ema(session, l["symbol"], semaphore)
        category = get_coin_category(l["symbol"])
        losers.append({
            "symbol": l["symbol"],
            "pct_4h": l["pct_4h"],
            "close": l["close"],
            "category": category,
            "ema_status": ema_status,
            "volume": l["volume"]
        })

    # Save newly locked coins to state for the next 4 hours
    state["locked_gainers"] = [g["symbol"] for g in gainers]
    state["locked_losers"] = [l["symbol"] for l in losers]
    state["locked_time"] = now
    save_pinned_state(state)

    return gainers, losers


def build_dashboard_html(gainers: list, losers: list) -> str:
    """Builds a clean, aesthetic HTML formatted pinned dashboard text."""
    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(timezone.utc).astimezone(tz).strftime("%d-%b %H:%M IST")

    lines = [
        f"⚡ <b>4H VOLATILITY MOVERS DASHBOARD</b> 📊",
        f"⏱ <i>Updated: {now_ist}</i>",
        "──────────────────────────",
        "",
        "🚀 <b>TOP 2 GAINERS (4H EXPLOSION ↑)</b>",
        ""
    ]

    for idx, item in enumerate(gainers, 1):
        sign = "+" if item["pct_4h"] > 0 else ""
        c_val = item['close']
        price_str = f"${c_val:.6f}" if c_val < 0.01 else (f"${c_val:.4f}" if c_val < 1.0 else f"${c_val:.2f}")
        display_sym = format_display_symbol(item['symbol'])
        lines.append(f"🟢 <b>{idx}. {display_sym}</b> | <code>{sign}{item['pct_4h']:.2f}%</code>")
        lines.append(f"├ <b>Price:</b> {price_str} | <i>{item['category']}</i>")
        ema_data = item.get("ema_status")
        if isinstance(ema_data, dict):
            evs = ema_data.get("events", [])
            for ev in evs:
                lines.append(f"├ {ev}")
            alignment = ema_data.get("alignment", "")
            lines.append(f"└ {alignment}")
        else:
            lines.append(f"└ <b>EMA Status:</b> {ema_data}")
        lines.append("")

    lines.append("──────────────────────────")
    lines.append("🔻 <b>TOP 2 LOSERS (4H COLLAPSE ↓)</b>")
    lines.append("")

    for idx, item in enumerate(losers, 1):
        sign = "+" if item["pct_4h"] > 0 else ""
        c_val = item['close']
        price_str = f"${c_val:.6f}" if c_val < 0.01 else (f"${c_val:.4f}" if c_val < 1.0 else f"${c_val:.2f}")
        display_sym = format_display_symbol(item['symbol'])
        lines.append(f"🔴 <b>{idx}. {display_sym}</b> | <code>{sign}{item['pct_4h']:.2f}%</code>")
        lines.append(f"├ <b>Price:</b> {price_str} | <i>{item['category']}</i>")
        ema_data = item.get("ema_status")
        if isinstance(ema_data, dict):
            evs = ema_data.get("events", [])
            for ev in evs:
                lines.append(f"├ {ev}")
            alignment = ema_data.get("alignment", "")
            lines.append(f"└ {alignment}")
        else:
            lines.append(f"└ <b>EMA Status:</b> {ema_data}")
        lines.append("")

    lines.append("──────────────────────────")

    return "\n".join(lines)


async def update_pinned_volatility_dashboard(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
    """
    Maintains strictly 1 single pinned card in EMA Scanner Channel (-1004332146822),
    silently editing text in-place every 5 minutes.
    NEVER dumps duplicate messages on network timeouts or transient errors.
    """
    try:
        gainers, losers = await fetch_4h_volatility_movers(session, semaphore)
        if not gainers or not losers:
            logger.warning("No mover data returned for pinned dashboard.")
            return

        html_text = build_dashboard_html(gainers, losers)
        state = load_pinned_state()
        
        from telegram import Bot
        from telegram.constants import ParseMode
        bot = Bot(token=TELEGRAM_BOT_TOKEN)

        channel_id = "-1004332146822"  # EMA Scanner Channel ONLY
        msg_id = state.get(channel_id) or KNOWN_DASHBOARD_MSG_ID

        # Query Telegram API for existing pinned message if msg_id is missing from local state file
        if not msg_id:
            try:
                chat = await bot.get_chat(chat_id=channel_id)
                if chat and chat.pinned_message:
                    msg_id = chat.pinned_message.message_id
                    state[channel_id] = msg_id
                    save_pinned_state(state)
                    logger.info(f"Auto-discovered existing pinned message ID {msg_id} in {channel_id}")
            except Exception as get_chat_err:
                logger.warning(f"Could not check pinned message for {channel_id}: {get_chat_err}")

        # Edit existing pinned message in-place
        if msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=channel_id,
                    message_id=msg_id,
                    text=html_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
                logger.info(f"Silently edited existing pinned 4H volatility dashboard (msg_id={msg_id}) in {channel_id}")
                return
            except Exception as edit_err:
                err_str = str(edit_err)
                if "Message is not modified" in err_str:
                    logger.debug(f"Pinned message {msg_id} text unchanged.")
                    return
                elif "Message to edit not found" in err_str:
                    logger.warning(f"Message {msg_id} was deleted from {channel_id}. Checking for active pinned message...")
                    # Try checking if another dashboard message was pinned before creating any new message
                    try:
                        chat = await bot.get_chat(chat_id=channel_id)
                        if chat and chat.pinned_message and chat.pinned_message.message_id != msg_id:
                            alt_id = chat.pinned_message.message_id
                            await bot.edit_message_text(
                                chat_id=channel_id,
                                message_id=alt_id,
                                text=html_text,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True
                            )
                            state[channel_id] = alt_id
                            save_pinned_state(state)
                            logger.info(f"Re-linked and edited pinned message {alt_id}")
                            return
                    except Exception as e_repin:
                        logger.warning(f"Could not re-link pinned message: {e_repin}")
                    msg_id = None
                else:
                    # Transient error (network timeout, rate limit, etc.)
                    # STRICT RULE: DO NOT send a new message! Just log and wait for next 5-min cycle.
                    logger.warning(f"Transient error editing pinned dashboard {msg_id}: {edit_err}. Skipping to next cycle.")
                    return

        # Only send a new message if NO pinned message exists AT ALL in the channel
        if not msg_id:
            try:
                sent_msg = await bot.send_message(
                    chat_id=channel_id,
                    text=html_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    disable_notification=True
                )
                new_msg_id = sent_msg.message_id
                try:
                    await bot.pin_chat_message(
                        chat_id=channel_id,
                        message_id=new_msg_id,
                        disable_notification=True
                    )
                    logger.info(f"Created initial pinned dashboard message {new_msg_id} in {channel_id}")
                except Exception as pin_err:
                    logger.warning(f"Could not pin message {new_msg_id} in {channel_id}: {pin_err}")

                state[channel_id] = new_msg_id
                save_pinned_state(state)
            except Exception as send_err:
                logger.error(f"Failed to create initial dashboard in {channel_id}: {send_err}")

    except Exception as e:
        logger.error(f"Error in update_pinned_volatility_dashboard: {e}")
