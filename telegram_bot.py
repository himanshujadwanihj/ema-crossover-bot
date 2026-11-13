import channel_manager
import time
import asyncio
import logging
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
import zoneinfo
from datetime import datetime, timezone, timedelta
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE, ALLOWED_15M_COINS
from market_cache import (
    filter_market_cache,
    format_filter_result_card,
    normalize_timeframe_tokens,
    VALID_TFS,
    MARKET_CACHE
)

from signal_analyser import analyse_signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

_app_instance = None
_bot_instance = None
BATCH_CROSSOVERS = []
BATCH_RETESTS = []
BATCH_BREAKOUTS = []

def _get_bot():
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot_instance

_send_lock = asyncio.Lock()

async def send_text_to_chat(chat_id: str, message: str, disable_notification: bool = False):
    if not message:
        return None
    from telegram.error import RetryAfter
    bot = _get_bot()
    async with _send_lock:
        for attempt in range(5):
            try:
                sent = await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML", disable_web_page_preview=False, disable_notification=disable_notification)
                await asyncio.sleep(2.0)
                return sent
            except RetryAfter as e:
                wait_s = getattr(e, 'retry_after', 20)
                logger.warning(f"Telegram Flood Control hit for {chat_id}. Waiting {wait_s}s...")
                await asyncio.sleep(wait_s + 1)
            except Exception as e:
                logger.error(f"Failed to send to chat {chat_id}: {e}")
                await asyncio.sleep(2)
        return None

import os

def get_target_chat_id() -> str:
    """HARDCODED: Returns the personal DM chat ID for Shark Updates."""
    return TELEGRAM_CHAT_ID

async def send_text_alert(message: str):
    """Broadcasts a text alert strictly to Personal DM (771305593)."""
    target = TELEGRAM_CHAT_ID or "771305593"
    await send_text_to_chat(target, message)

async def send_trading_update(message: str):
    """Sends detailed execution updates strictly to Personal DM (771305593)."""
    target = TELEGRAM_CHAT_ID or "771305593"
    await send_text_to_chat(target, message)

TF_ORDER = ["1H", "4H", "1D", "1W"]

def get_htf_macro(tf_label):
    if tf_label not in TF_ORDER: return None, None
    idx = TF_ORDER.index(tf_label)
    htf = TF_ORDER[idx+1] if idx+1 < len(TF_ORDER) else tf_label
    
    macro = "1D"
    if tf_label in ["1D", "1W"]: macro = "1W"
    elif tf_label == "4H": macro = "1D"
    
    return htf, macro

SYMBOL_NAME_MAP = {
    "龙虾": "LOBSTER (LONGXIA)",
    "牛来": "BULL ARRIVES (NIULAI)",
    "币安人生": "BINANCE LIFE (BIANRENSHENG)",
    "我踏马来了": "WTML (I AM COMING)",
    "哈基米": "HAJIMI",
}

def format_display_symbol(symbol: str) -> str:
    """
    Translates foreign/non-ASCII coin names to clean English names.
    Also enhances commodity tickers to clear CFD labels.
    """
    if not symbol:
        return ""
    cfd_labels = {
        "XAU/USD": "XAU/USD",
        "XAUT/USDT": "XAUT/USDT",
        "XAG/USDT": "XAG/USD",
    }
    if symbol in cfd_labels:
        return cfd_labels[symbol]
    stock_display_map = {
        "NVDAB/USDT": "NVIDIA (NVDA)",
        "TSLAB/USDT": "TESLA (TSLA)",
        "AAPLB/USDT": "APPLE (AAPL)",
        "MSTRB/USDT": "MICROSTRATEGY (MSTR)",
        "AMZNB/USDT": "AMAZON (AMZN)",
        "METAB/USDT": "META (META)",
        "MSFTB/USDT": "MICROSOFT (MSFT)",
        "GOOGLB/USDT": "ALPHABET (GOOGL)",
        "SPYB/USDT": "S&P 500 ETF (SPY)",
        "QQQB/USDT": "NASDAQ 100 ETF (QQQ)",
        "NVDA/USDT": "NVIDIA (NVDA)",
        "TSLA/USDT": "TESLA (TSLA)",
        "AAPL/USDT": "APPLE (AAPL)",
        "MSTR/USDT": "MICROSTRATEGY (MSTR)",
        "AMZN/USDT": "AMAZON (AMZN)",
        "META/USDT": "META (META)",
        "MSFT/USDT": "MICROSOFT (MSFT)",
        "GOOGL/USDT": "ALPHABET (GOOGL)",
        "SPY/USDT": "S&P 500 ETF (SPY)",
        "QQQ/USDT": "NASDAQ 100 ETF (QQQ)",
    }
    if symbol in stock_display_map:
        return stock_display_map[symbol]

    if not symbol:
        return ""
    import re
    res = str(symbol)
    for foreign_key, english_name in SYMBOL_NAME_MAP.items():
        if foreign_key in res:
            res = res.replace(foreign_key, english_name)

    # Filter out any leftover CJK characters
    res = re.sub(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]', '', res)
    return res.strip()

def format_price(p: float, symbol: str = None) -> str:
    if p is None or p == 0.0: return "0.00"
    if symbol:
        from executor import round_price
        rounded = round_price(symbol, p)
        if rounded >= 1000:
            return f"{rounded:,.2f}"
        elif rounded >= 10:
            s = f"{rounded:.4f}".rstrip('0').rstrip('.')
            return f"{rounded:.2f}" if len(s.split('.')[-1]) < 2 else s
        elif rounded >= 1:
            s = f"{rounded:.4f}".rstrip('0').rstrip('.')
            return f"{rounded:.2f}" if len(s.split('.')[-1]) < 2 else s
        else:
            return f"{rounded:.8f}".rstrip('0').rstrip('.')
    if p >= 1000: return f"{p:,.2f}"
    elif p >= 10: return f"{p:.4f}"
    elif p >= 0.001: return f"{p:.6f}"
    else: return f"{p:.8f}"

_RECENT_ALERT_TIMESTAMPS = {}

def _is_telegram_alert_in_cooldown(symbol: str, tf_label: str, signal: str, cross_type: str) -> bool:
    """Anti-spam deduplication gate: Prevents repeated notifications for the same symbol & TF."""
    global _RECENT_ALERT_TIMESTAMPS
    clean_sym = symbol.upper()
    clean_tf = str(tf_label).upper()
    
    cooldown_seconds = 3600  # 60 minutes for 1H
    if "15M" in clean_tf:
        cooldown_seconds = 1200  # 20 minutes
    elif "30M" in clean_tf:
        cooldown_seconds = 1800  # 30 minutes
    elif any(k in clean_tf for k in ["4H", "1D", "1W"]):
        cooldown_seconds = 10800  # 3 hours

    kind = "retest" if "retest" in str(cross_type).lower() else ("squeeze" if "squeeze" in str(cross_type).lower() else "cross")
    key = f"{clean_sym}_{clean_tf}_{signal}_{kind}"
    now = time.time()
    last_time = _RECENT_ALERT_TIMESTAMPS.get(key, 0.0)
    if now - last_time < cooldown_seconds:
        return True
    
    _RECENT_ALERT_TIMESTAMPS[key] = now
    return False

async def queue_alert(symbol, tf_label, signal, entry, sl, tp, tf_states, cross_type="9/26 EMA Cross", last_1h_cross_time=None, candle_time=None, macro_struct="Unknown", rr=1.5, tp_label="BSL Pool", tf_data=None, tp1=None, tp2=None, tp3=None, rr1=None, rr2=None, rr3=None, lbl1=None, lbl2=None, lbl3=None, is_special_list=False, rsi=None, adx=None, **kwargs):
    """Queues an alert in memory instead of sending instantly. Flushed at the end of the scan loop."""
    # AIRTIGHT ANTI-DUPLICATE SHIELD: Prevent repeated notifications
    if _is_telegram_alert_in_cooldown(symbol, tf_label, signal, cross_type):
        logger.info(f"🚫 BLOCKED duplicate notification for {symbol} ({tf_label} {signal}) — already alerted recently.")
        return

    # STRICT USER DIRECTIVE: No trades of commodities, BTC, and ETH on Sat and Sunday in any case
    from config import is_weekend_trading_blocked
    if is_weekend_trading_blocked(symbol):
        logger.info(f"🚫 BLOCKED alert for {symbol} ({tf_label} {signal}) — No commodities, BTC, or ETH on Saturday/Sunday.")
        return

    # STRICT SQUEEZE TIMEFRAME RULE (User Directive: '1 hr squeze pr hi raho fir'):
    # Squeeze Breakouts are strictly restricted to 1H timeframe only. All lower timeframe squeezes (15M) are banned.
    is_sq_check = ("Squeeze" in cross_type) or kwargs.get("is_dedicated_squeeze", False) or ("Breakout" in cross_type and "Retest" not in cross_type)
    if is_sq_check and str(tf_label).upper() not in ["1H", "1HR", "60M"]:
        logger.info(f"🚫 BLOCKED Squeeze alert for {symbol} ({tf_label}) — Squeeze breakouts strictly restricted to 1H timeframe.")
        return

    is_retest = "Retest" in cross_type
    if is_retest:
        candle_time = datetime.now(timezone.utc).isoformat()

    target_trend = "bullish" if signal == "BUY" else "bearish"
    aligned_count = sum(1 for t in ["1H", "4H", "1D", "1W"] if tf_states and tf_states.get(t, "neutral").lower() == target_trend)
    is_15m = str(tf_label).upper() in ["15M", "15MIN"]
    is_5m = str(tf_label).upper() in ["5M", "5MIN"]
    is_squeeze_type = ("Squeeze" in cross_type) or ("Breakout" in cross_type and is_15m)
    is_gold_scalp = (symbol in ALLOWED_15M_COINS or "XAU" in symbol.upper()) and (is_15m or is_5m)

    if is_5m and not is_gold_scalp:
        logger.info(f"BLOCKED 5M signal for {symbol} — 5M strictly Gold only.")
        return

    if is_15m and (symbol not in ALLOWED_15M_COINS) and (not is_squeeze_type):
        logger.info(f"BLOCKED 15M signal for {symbol} — not in ALLOWED_15M_COINS.")
        return

    # USER DIRECTIVE & DUAL CHANNEL DISTRIBUTION:
    # 1. Retest alerts: Strictly minimum 3-Star (3/4 ⭐⭐⭐) and 4-Star (4/4 ⭐⭐⭐⭐) for HTF.
    #    For Gold Scalps (5M & 15M): Allowed directly without requiring 3/4 stars!
    # 2. Fresh crossovers:
    #    - If timeframe is 5M/15M Gold Scalp: Allowed directly.
    #    - For all other timeframes (30M, 1H, 4H, 1D, 1W): Strictly minimum 3-Star (3/4 ⭐⭐⭐) and 4-Star (4/4 ⭐⭐⭐⭐).
    if is_retest:
        if not is_gold_scalp and aligned_count < 3:
            logger.info(f"BLOCKED {signal} retest for {symbol} ({tf_label}) — only {aligned_count}/4 stars (minimum 3 required, ignoring 2 stars).")
            return
    else:
        if is_15m or is_5m:
            if (symbol not in ALLOWED_15M_COINS and "XAU" not in symbol.upper()) and (not is_squeeze_type):
                logger.info(f"BLOCKED {tf_label} crossover for {symbol} — not in ALLOWED_15M_COINS.")
                return
        else:
            if aligned_count < 3:
                logger.info(f"BLOCKED {signal} crossover for {symbol} ({tf_label}) — {aligned_count}/4 stars (minimum 3 required for HTF).")
                return

    # STRICT RSI MOMENTUM GATE:
    # All 3-Star and 4-Star signals (Crossovers & Retests) MUST strictly satisfy healthy RSI momentum range:
    # BUY: 40.0 <= RSI <= 75.0 (blocks exhausted assets <40 and overbought peak tops >75)
    # SELL: 25.0 <= RSI <= 60.0 (blocks weak sellers >60 and oversold bottoms <25)
    if rsi is not None:
        try:
            f_rsi = float(rsi)
            if signal == "BUY" and not (40.0 <= f_rsi <= 75.0):
                logger.info(f"🚫 BLOCKED {symbol} BUY ({tf_label}) — RSI {f_rsi:.1f} outside healthy momentum bounds [40, 75].")
                return
            elif signal == "SELL" and not (25.0 <= f_rsi <= 60.0):
                logger.info(f"🚫 BLOCKED {symbol} SELL ({tf_label}) — RSI {f_rsi:.1f} outside healthy momentum bounds [25, 60].")
                return
        except (ValueError, TypeError):
            pass
    
 
            
    # Fetch live Futures Microstructure (Funding Rate, OI trend, CVD Delta & Session timing)
    from fetcher import fetch_futures_microstructure_async, get_market_session_info
    micro = None
    try:
        df_signal = None
        if isinstance(tf_data, dict):
            df_signal = tf_data.get("1H") if tf_data.get("1H") is not None else tf_data.get(tf_label)
        micro = await fetch_futures_microstructure_async(None, symbol, signal, df=df_signal)
    except Exception as e_micro:
        logger.warning(f"Error fetching microstructure for {symbol}: {e_micro}")
        micro = {"session_info": get_market_session_info()}

    session_info = micro.get("session_info") if micro else get_market_session_info()
    
    # Strictly decouple Squeeze from Retest & Crossovers!
    # EMA Retests and EMA Crossovers are NEVER Squeeze setups even if BB is pinched.
    is_retest_sig = "Retest" in cross_type
    is_cross_sig = ("Cross" in cross_type or "Crossover" in cross_type) and not is_retest_sig
    is_dedicated_sq = (
        ("Squeeze" in cross_type or "Breakout" in cross_type or "Breakdown" in cross_type or kwargs.get("is_dedicated_squeeze", False))
        and not is_retest_sig
        and not is_cross_sig
    )

    alert_data = {
        "symbol": symbol, "tf_label": tf_label, "signal": signal, "entry": entry,
        "sl": sl, "tp": tp, "tf_states": tf_states, "cross_type": cross_type,
        "candle_time": candle_time, "macro_struct": macro_struct, "rr": rr,
        "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr1": rr1, "rr2": rr2, "rr3": rr3,
        "is_special_list": is_special_list, "tf_data": tf_data, "aligned_count": aligned_count,
        "rsi": rsi, "adx": adx,
        "microstructure": micro, "session_info": session_info, 
        "is_squeeze": is_dedicated_sq,
        "is_dedicated_squeeze": is_dedicated_sq
    }
    
    if is_retest_sig:
        BATCH_RETESTS.append(alert_data)
        # ARCHITECTURE ROUTING: Retest signals go STRICTLY to EMA Scanner Channel (-1004332146822)!
        # EMA Signal Bot (Personal DM / 771305593) is strictly reserved for TRADING EXECUTION ONLY!
        try:
            header = "🔄 <b>LIVE EMA RETEST</b>\n"
            retest_msg = header + _build_single_coin_block(alert_data)
            EMA_SCANNER_CHANNEL = "-1004332146822"
            asyncio.create_task(send_text_to_chat(EMA_SCANNER_CHANNEL, retest_msg))
            alert_data["instant_sent"] = True
            logger.info(f"⚡ INSTANT RETEST ALERT DISPATCHED: {symbol} {tf_label} {signal} -> {EMA_SCANNER_CHANNEL}")
        except Exception as e_ret:
            logger.error(f"Error dispatching instant retest alert: {e_ret}")
    elif is_dedicated_sq:
        BATCH_BREAKOUTS.append(alert_data)
    else:
        BATCH_CROSSOVERS.append(alert_data)

def calculate_candle_close_time(candle_time_str: str, tf_label: str) -> str:
    """Calculates the exact close time of the candle in IST."""
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    base_dt = None
    if candle_time_str:
        c_str = str(candle_time_str).strip()
        if "IST" in c_str and len(c_str.split(" ")) == 3 and "-" in c_str:
            try:
                base_dt = datetime.strptime(c_str, "%Y-%m-%d %H:%M IST").replace(tzinfo=tz)
            except Exception:
                pass
        else:
            try:
                clean = c_str.replace("Z", "+00:00").replace(" IST", "").strip()
                if "-" not in clean and len(clean.split(":")) >= 2:
                    today = datetime.now(timezone.utc).date()
                    clean = f"{today}T{clean}" if "+" in clean else f"{today}T{clean}+00:00"
                elif " " in clean:
                    clean = clean.replace(" ", "T")
                base_dt = datetime.fromisoformat(clean)
                if base_dt.tzinfo is None:
                    base_dt = base_dt.replace(tzinfo=timezone.utc)
                base_dt = base_dt.astimezone(tz)
            except Exception:
                pass

    if not base_dt:
        base_dt = datetime.now(timezone.utc).astimezone(tz)

    tf_clean = str(tf_label).upper()
    delta = timedelta(minutes=15)
    if "5M" in tf_clean:
        delta = timedelta(minutes=5)
    elif "15M" in tf_clean:
        delta = timedelta(minutes=15)
    elif "30M" in tf_clean:
        delta = timedelta(minutes=30)
    elif "1H" in tf_clean:
        delta = timedelta(hours=1)
    elif "4H" in tf_clean:
        delta = timedelta(hours=4)
    elif "1D" in tf_clean:
        delta = timedelta(days=1)
    elif "1W" in tf_clean:
        delta = timedelta(weeks=1)

    close_dt = base_dt + delta

    # SANITY GUARDRAIL: A completed candle CANNOT close in the future!
    # If close_dt is ahead of current time, base_dt was already the close time or miscalculated.
    now_ist = datetime.now(timezone.utc).astimezone(tz)
    if close_dt > (now_ist + timedelta(minutes=2)):
        # Auto-heal: If close_dt is in the future, fallback to base_dt or current candle close boundary
        if base_dt <= now_ist:
            close_dt = base_dt
        else:
            close_dt = now_ist

    return close_dt.strftime("%d-%b %H:%M IST")

def _build_single_coin_block(data, index=None):
    symbol = data["symbol"]
    display_symbol = format_display_symbol(symbol)
    signal = data["signal"]
    tf_label = data["tf_label"]
    cross_type = data["cross_type"]
    entry = data["entry"]
    sl = data["sl"]
    tp1, tp2, tp3 = data.get("tp1"), data.get("tp2"), data.get("tp3")
    rr1, rr2, rr3 = data.get("rr1"), data.get("rr2"), data.get("rr3")
    tf_states = data["tf_states"]
    
    target_trend = "bullish" if signal == "BUY" else "bearish"
    aligned_count = sum(1 for t in ["1H", "4H", "1D", "1W"] if tf_states.get(t, "neutral").lower() == target_trend)
    
    rsi = data.get("rsi")
    has_rsi_boost = False
    if rsi is not None:
        try:
            f_rsi = float(rsi)
            if signal == "BUY" and 45.0 <= f_rsi <= 72.0:
                has_rsi_boost = True
            elif signal == "SELL" and 28.0 <= f_rsi <= 55.0:
                has_rsi_boost = True
        except (ValueError, TypeError):
            pass

    rsi_star = " ✨" if has_rsi_boost else ""
    star_rating = f"{'⭐' * aligned_count}{rsi_star}"
    
    c_1h = tf_states.get("1H", "neutral").capitalize()
    c_4h = tf_states.get("4H", "neutral").capitalize()
    c_1d = tf_states.get("1D", "neutral").capitalize()
    c_1w = tf_states.get("1W", "neutral").capitalize()
    i_1h = "🟢" if c_1h == "Bullish" else ("🔴" if c_1h == "Bearish" else "⚪")
    i_4h = "🟢" if c_4h == "Bullish" else ("🔴" if c_4h == "Bearish" else "⚪")
    i_1d = "🟢" if c_1d == "Bullish" else ("🔴" if c_1d == "Bearish" else "⚪")
    i_1w = "🟢" if c_1w == "Bullish" else ("🔴" if c_1w == "Bearish" else "⚪")
    
    direction_icon = "🟢" if signal == "BUY" else "🔴"
    direction_tag = "BUY (LONG)" if signal == "BUY" else "SELL (SHORT)"
    special_tag = "💎 " if data.get("is_special_list") else ""
    
    num_prefix = f"{index}. " if index else ""

    # Strict SL Direction Guarantee in Telegram Message Display
    if signal == "BUY" and sl >= entry:
        sl = entry * 0.995
    elif signal == "SELL" and sl <= entry:
        sl = entry * 1.005
    
    is_retest = "Retest" in cross_type
    is_15m = str(tf_label).upper() in ["15M", "15MIN"]
    close_time_str = calculate_candle_close_time(data.get("candle_time"), tf_label)
    retest_time_str = _format_time(data.get("candle_time"))

    # Squeeze Badge header & Market Dynamic explanation (Strict separation: Dedicated Squeeze takes priority)
    is_dedicated_sq = bool(data.get("is_dedicated_squeeze", False))
    if is_dedicated_sq:
        is_squeeze = True
        is_retest = False
        is_cross = False
    else:
        is_retest = "Retest" in cross_type
        is_cross = ("Cross" in cross_type or "Crossover" in cross_type) and not is_retest
        is_squeeze = ("Squeeze" in cross_type or "Breakout" in cross_type or "Breakdown" in cross_type) and not is_retest and not is_cross
    if is_squeeze:
        if signal == "BUY":
            sq_icon = "🟢"
            squeeze_title = "🚀 SHORT-SQUEEZE BREAKOUT"
            action_desc = "🟢 BUY (LONG)"
            market_dynamic = "💥 <b>Short Squeeze:</b> Trapped shorts covering/liquidating — CVD aggressive buying driving price upward"
        else:
            sq_icon = "🔴"
            squeeze_title = "🩸 LONG-SQUEEZE BREAKDOWN"
            action_desc = "🔴 SELL (SHORT)"
            market_dynamic = "💥 <b>Long Squeeze:</b> Trapped longs liquidating — CVD aggressive selling cascading price downward"

        block = f"{sq_icon} <b>{num_prefix}{special_tag}{display_symbol} | {squeeze_title}</b>\n"
        block += f"🎯 <b>Action:</b> <b>{action_desc}</b>\n"
    else:
        block = f"{direction_icon} <b>{num_prefix}{special_tag}{display_symbol} | {direction_tag}</b>\n"
        if is_retest:
            if signal == "BUY":
                market_dynamic = "🟢 <b>EMA Support Bounce:</b> Price tested EMA dynamic support & bounced with buyer defense"
            else:
                market_dynamic = "🔴 <b>EMA Resistance Rejection:</b> Price tested EMA dynamic resistance & rejected with seller defense"
        else:
            if signal == "BUY":
                market_dynamic = "🟢 <b>Bullish EMA Cross:</b> Fast EMA crossed above Slow EMA — Trend expansion starting"
            else:
                market_dynamic = "🔴 <b>Bearish EMA Cross:</b> Fast EMA crossed below Slow EMA — Trend breakdown starting"

    block += f"<b>Setup:</b> {cross_type} ({tf_label} Chart)\n"
    block += f"🧲 <b>Market Dynamic:</b> {market_dynamic}\n"

    if is_retest:
        block += f"⏱ <b>Retest Touch Time:</b> {retest_time_str}\n"
    else:
        block += f"⏱ <b>Candle Closed:</b> {close_time_str}\n"
    if not (is_15m and aligned_count < 3):
        block += f"<b>Confluence:</b> {aligned_count}/4 {star_rating} ({i_1h} 1H | {i_4h} 4H | {i_1d} 1D | {i_1w} 1W)\n"

    # Explicit RSI Momentum Display on all Fresh Crossovers & Live Retests
    if rsi is not None:
        try:
            f_rsi = float(rsi)
            if signal == "BUY":
                rsi_status = "Bullish Momentum 🟢" if f_rsi >= 50.0 else "Healthy Dip 🟢"
            else:
                rsi_status = "Bearish Momentum 🔴" if f_rsi <= 50.0 else "Healthy Pullback 🔴"
            block += f"📊 <b>RSI:</b> <code>{f_rsi:.1f}</code> ({rsi_status})\n"
        except (ValueError, TypeError):
            pass

    micro = data.get("microstructure") or {}
    cvd_metrics = micro.get("cvd_metrics") or {}
    cvd_disp = cvd_metrics.get("cvd_display", "")
    funding_pct = micro.get("funding_pct")
    if is_squeeze and (cvd_disp or funding_pct is not None):
        block += f"⚡ <b>Taker Flow:</b> {cvd_disp}"
        if funding_pct is not None:
            block += f" | <b>Funding:</b> <code>{funding_pct:+.4f}%</code>"
        block += "\n"

    block += f"🎯 <b>ENTRY:</b> {format_price(entry, symbol)}\n"
    block += f"🔴 <b>STOP-LOSS:</b> {format_price(sl, symbol)}\n"
    
    if tp1 and tp3 and (tp1 != tp3):
        f_rr1 = float(rr1) if rr1 is not None else 2.0
        f_rr2 = float(rr3) if rr3 is not None else 3.5
        block += f"🟢 <b>TP1:</b> {format_price(tp1, symbol)} (1:{f_rr1:.1f} R:R)\n"
        block += f"🚀 <b>TP2:</b> {format_price(tp3, symbol)} (1:{f_rr2:.1f} R:R)\n"
    elif tp1:
        f_rr1 = float(rr1) if rr1 is not None else 2.0
        block += f"🎯 <b>TARGET:</b> {format_price(tp1, symbol)} (1:{f_rr1:.1f} R:R)\n"
    else:
        block += f"🎯 <b>TARGET:</b> {format_price(data.get('tp'), symbol)}\n"
        
    # -- AI SIGNAL ANALYSIS BLOCK --
    try:
        df_tf = None
        if isinstance(data.get("tf_data"), dict):
            df_tf = data["tf_data"].get(data.get("tf_label")) or data["tf_data"].get("1H")

        vol_now = None
        vol_sma_now = None
        body_ratio_now = None
        macd_line_now = None
        macd_sig_now = None

        if df_tf is not None and len(df_tf) >= 22:
            try:
                vol_now = float(df_tf["volume"].iloc[-2])
                vol_sma_now = float(df_tf["volume"].iloc[-22:-2].mean())
                c_open  = float(df_tf["open"].iloc[-2])
                c_high  = float(df_tf["high"].iloc[-2])
                c_low   = float(df_tf["low"].iloc[-2])
                c_close = float(df_tf["close"].iloc[-2])
                c_body  = abs(c_close - c_open)
                c_range = max(c_high - c_low, 1e-8)
                body_ratio_now = c_body / c_range
                macd_line_ser  = df_tf["close"].ewm(span=12, adjust=False).mean() - df_tf["close"].ewm(span=26, adjust=False).mean()
                macd_sig_ser   = macd_line_ser.ewm(span=9, adjust=False).mean()
                macd_line_now  = float(macd_line_ser.iloc[-2])
                macd_sig_now   = float(macd_sig_ser.iloc[-2])
            except Exception:
                pass

        analysis = analyse_signal(
            symbol=symbol,
            signal=signal,
            rsi=data.get("rsi"),
            adx=data.get("adx"),
            volume=vol_now,
            volume_sma=vol_sma_now,
            macd_line=macd_line_now,
            macd_signal_val=macd_sig_now,
            body_ratio=body_ratio_now,
            aligned_count=aligned_count,
            tf_label=tf_label,
            cross_type=cross_type,
        )
        # Background only -- do NOT append to Telegram message
        logger.info(
            "[AI-ANALYSER] %s %s %s | Score=%s/100 | %s | "
            "RSI=%s ADX=%s Vol=%s HTF=%s MACD=%s Body=%s Session=%s",
            symbol, tf_label, signal,
            analysis["score"], analysis["verdict"],
            analysis["components"].get("rsi", 0),
            analysis["components"].get("adx", 0),
            analysis["components"].get("volume", 0),
            analysis["components"].get("htf", 0),
            analysis["components"].get("macd", 0),
            analysis["components"].get("body", 0),
            analysis["components"].get("session", 0),
        )
    except Exception as _ae:
        pass
    # -- END AI SIGNAL ANALYSIS BLOCK (background logging only) --
    return block

def _format_time(candle_time):
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    if candle_time:
        c_str = str(candle_time).strip()
        if "IST" in c_str and len(c_str.split(" ")) == 3 and "-" in c_str:
            try:
                dt = datetime.strptime(c_str, "%Y-%m-%d %H:%M IST").replace(tzinfo=tz)
                return dt.strftime("%d-%b %H:%M IST")
            except Exception:
                return c_str
        else:
            try:
                clean = c_str.replace("Z", "+00:00").replace(" IST", "").strip()
                if "-" not in clean and len(clean.split(":")) >= 2:
                    today = datetime.now(timezone.utc).date()
                    clean = f"{today}T{clean}" if "+" in clean else f"{today}T{clean}+00:00"
                elif " " in clean:
                    clean = clean.replace(" ", "T")
                dt = datetime.fromisoformat(clean)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(tz).strftime("%d-%b %H:%M IST")
            except Exception:
                return f"{c_str[:16]} IST"
    return datetime.now(timezone.utc).astimezone(tz).strftime("%d-%b %H:%M IST")

def _get_sort_key(data):
    # USER PRIORITY RANKING:
    # 1. Highest Stars FIRST (4★ before 3★, etc.)
    # 2. Highest Timeframe FIRST (1W > 1D > 4H > 1H > 30M > 15M)
    # 3. Direction (BUY then SELL)
    target_trend = "bullish" if data["signal"] == "BUY" else "bearish"
    aligned_count = sum(1 for t in ["1H", "4H", "1D", "1W"] if data["tf_states"].get(t, "neutral").lower() == target_trend)
    stars_rank = -aligned_count  # -4 comes before -3

    tf_weight = {
        "1W": 0, "1w": 0,
        "1D": 1, "1d": 1,
        "4H": 2, "4h": 2,
        "1H": 3, "1h": 3,
        "30M": 4, "30m": 4,
        "15M": 5, "15m": 5
    }
    tf_rank = tf_weight.get(data.get("tf_label"), 99)
    direction_val = 0 if data["signal"] == "BUY" else 1

    return (stars_rank, tf_rank, direction_val)

async def _send_chunked_digest(items: list, header_title: str, emoji: str, chats: list):
    """Splits queued items into chunked messages (max 5 items each) to keep under Telegram's 4096 char limit."""
    if not items:
        return
    time_str = _format_time(items[0]["candle_time"])
    chunk_size = 5
    chunks = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
    total_chunks = len(chunks)

    # Deduplicate chat IDs
    unique_chats = list(dict.fromkeys([c for c in chats if c]))

    for idx, chunk in enumerate(chunks, 1):
        if len(items) == 1:
            msg = f"{emoji} <b>{header_title}</b>\n⏱ <b>Time:</b> {time_str}\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        elif total_chunks == 1:
            msg = f"{emoji} <b>{len(items)} {header_title}S</b>\n⏱ <b>Time:</b> {time_str}\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        else:
            msg = f"{emoji} <b>{len(items)} {header_title}S (Part {idx}/{total_chunks})</b>\n⏱ <b>Time:</b> {time_str}\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

        start_i = (idx - 1) * chunk_size + 1
        for i, data in enumerate(chunk, start_i):
            try:
                msg += _build_single_coin_block(data, index=i if len(items) > 1 else None)
                msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            except Exception as e_block:
                logger.error(f"Error building block for {data.get('symbol')}: {e_block}", exc_info=True)

        for chat_id in unique_chats:
            try:
                await send_text_to_chat(chat_id, msg)
            except Exception as e:
                logger.error(f"Failed to send digest chunk to {chat_id}: {e}")

async def flush_alerts():
    """
    Flushes queued alerts as stated in strategy:
    - EMA Cross Channel (-1004463573705): All coins, fresh crossovers only (strictly 3★ and 4★, 15M allowed coins only).
    - EMA Scanner Channel (-1004332146822): Curated Pool ONLY (54 assets) for both fresh crossovers and live retests.
    """
    global BATCH_CROSSOVERS, BATCH_RETESTS, BATCH_BREAKOUTS
    from pullback_tracker import CURATED_SCANNER_POOL

    EMA_CROSS_CHANNEL = "-1004463573705"
    EMA_SCANNER_CHANNEL = "-1004332146822"

    # 1. EMA Cross Channel (-1004463573705):
    # Sends fresh crossovers across ALL scanned coins:
    # - 15M: Allowed ONLY for coins in ALLOWED_15M_COINS (sent regardless of star count).
    # - HTF (30M, 1H, 4H, 1D, 1W): Strictly 3★ and 4★ only.
    cross_channel_items = [
        item for item in BATCH_CROSSOVERS
        if (
            (str(item.get("tf_label", "")).upper() in ["15M", "15MIN"] and (item["symbol"] in ALLOWED_15M_COINS or item.get("is_squeeze", False) or "Squeeze" in item.get("cross_type", "")))
            or (str(item.get("tf_label", "")).upper() not in ["5M", "5MIN", "15M", "15MIN"] and item.get("aligned_count", 0) >= 3)
        )
    ]

    if cross_channel_items:
        cross_channel_items.sort(key=_get_sort_key)
        if len(cross_channel_items) == 1:
            try:
                header = "⚡ <b>FRESH EMA CROSSOVER</b>\n"
                msg = header + _build_single_coin_block(cross_channel_items[0])
                await send_text_to_chat(EMA_CROSS_CHANNEL, msg)
            except Exception as e:
                logger.error(f"Failed to send crossover alert to {EMA_CROSS_CHANNEL}: {e}")
        else:
            await _send_chunked_digest(cross_channel_items, "FRESH EMA CROSSOVER", "⚡", [EMA_CROSS_CHANNEL])

    # 2. EMA Scanner Channel (-1004332146822):
    # Curated pool ONLY (Core 10 + 25 Alts + Commodities + US Equities)
    # - 5M & 15M: Strictly Gold Scalps and Squeeze Breakouts only (no generic 15M alts/majors)
    # - 30M, 1H, 4H, 1D, 1W: All standard crossovers and retests
    scanner_crossovers = [
        item for item in BATCH_CROSSOVERS
        if item["symbol"] in CURATED_SCANNER_POOL and (
            str(item.get("tf_label", "")).upper() not in ["5M", "5MIN", "15M", "15MIN"]
            or "XAU" in item["symbol"].upper()
            or item.get("is_squeeze", False)
            or "Squeeze" in item.get("cross_type", "")
        )
    ]
    scanner_retests = [item for item in BATCH_RETESTS if item["symbol"] in CURATED_SCANNER_POOL]

    if scanner_crossovers:
        scanner_crossovers.sort(key=_get_sort_key)
        if len(scanner_crossovers) == 1:
            try:
                header = "⚡ <b>FRESH EMA CROSSOVER</b>\n"
                msg = header + _build_single_coin_block(scanner_crossovers[0])
                await send_text_to_chat(EMA_SCANNER_CHANNEL, msg)
            except Exception as e:
                logger.error(f"Failed to send curated crossover alert to {EMA_SCANNER_CHANNEL}: {e}")
        else:
            await _send_chunked_digest(scanner_crossovers, "FRESH EMA CROSSOVER", "⚡", [EMA_SCANNER_CHANNEL])

    if scanner_retests:
        scanner_retests.sort(key=_get_sort_key)
        unsent_retests = [item for item in scanner_retests if not item.get("instant_sent")]
        if len(unsent_retests) == 1:
            try:
                header = "🔄 <b>LIVE EMA RETEST</b>\n"
                msg = header + _build_single_coin_block(unsent_retests[0])
                await send_text_to_chat(EMA_SCANNER_CHANNEL, msg)
            except Exception as e:
                logger.error(f"Failed to send curated retest alert to {EMA_SCANNER_CHANNEL}: {e}")
        elif len(unsent_retests) > 1:
            await _send_chunked_digest(unsent_retests, "LIVE EMA RETEST", "🔄", [EMA_SCANNER_CHANNEL])

    # 3. AlphaCryptoSignals Filter Bot Subscribers Dispatch:
    try:
        from filter_bot import dispatch_filtered_alert
        for item in BATCH_CROSSOVERS:
            try:
                header = "⚡ <b>FRESH EMA CROSSOVER</b>\n"
                msg = header + _build_single_coin_block(item)
                await dispatch_filtered_alert(item, msg)
            except Exception as e_ind:
                logger.error(f"Error dispatching crossover to filter subscriber: {e_ind}")
        for item in BATCH_RETESTS:
            try:
                header = "🔄 <b>LIVE EMA RETEST</b>\n"
                msg = header + _build_single_coin_block(item)
                await dispatch_filtered_alert(item, msg)
            except Exception as e_ind:
                logger.error(f"Error dispatching retest to filter subscriber: {e_ind}")
    except Exception as e_filter:
        logger.error(f"Error dispatching to filter bot subscribers: {e_filter}")

    # 4. Squeeze Breakouts (Dedicated Independent Alert Flow — Never mixed with Crossovers):
    if BATCH_BREAKOUTS:
        BATCH_BREAKOUTS.sort(key=_get_sort_key)
        for item in BATCH_BREAKOUTS:
            try:
                sig = item.get("signal", "BUY")
                if sig == "BUY":
                    header = "🚀 <b>SHORT-SQUEEZE BREAKOUT TRIGGERED (BULLISH PUMP)</b>\n"
                else:
                    header = "🩸 <b>LONG-SQUEEZE BREAKDOWN TRIGGERED (BEARISH DUMP)</b>\n"
                msg = header + _build_single_coin_block(item)
                for ch in [EMA_SCANNER_CHANNEL, EMA_CROSS_CHANNEL]:
                    try:
                        await send_text_to_chat(ch, msg)
                    except Exception as e:
                        logger.error(f"Failed to send squeeze breakout alert to {ch}: {e}")
                try:
                    from filter_bot import dispatch_filtered_alert
                    await dispatch_filtered_alert(item, msg)
                except Exception as e_f:
                    logger.error(f"Error dispatching squeeze to filter bot: {e_f}")
            except Exception as e_item:
                logger.error(f"Error processing squeeze breakout item for {item.get('symbol')}: {e_item}")

    # 5. Register signals into Virtual Paper Trading Engine (Crossovers, Retests, and Squeeze Breakouts with 4-Star confluence)
    from paper_trader import register_paper_trade
    for item in (BATCH_CROSSOVERS + BATCH_RETESTS + BATCH_BREAKOUTS):
        try:
            await register_paper_trade(item)
        except Exception as e:
            logger.error(f"Error registering paper trade: {e}")

    # Clear all queues
    BATCH_CROSSOVERS.clear()
    BATCH_RETESTS.clear()
    BATCH_BREAKOUTS.clear()

async def send_startup_message():
    await send_text_alert("🚀 <b>BOT STARTED</b>\nRunning Dynamic Liquidity Pool Strategy (Batch Digest Active).")

# ── Bot Management & Channel Helpers ──────────────────────────────────────────

async def on_channel_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    fwd_chat = getattr(msg, 'forward_from_chat', None) if msg else None
    if fwd_chat:
        try:
            await context.bot.send_message(
                chat_id=msg.chat_id,
                text=(
                    f"📋 <b>Channel ID Found!</b>\n\n"
                    f"<b>Channel:</b> {fwd_chat.title or 'Unknown'}\n"
                    f"<b>ID:</b> <code>{fwd_chat.id}</code>\n\n"
                    f"Send this ID to the agent!"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error replying with channel ID: {e}")
        return

    chat = update.effective_chat
    if chat and chat.type in ["channel", "supergroup", "group"]:
        logger.info(f"CHANNEL DETECTED: {chat.id} ({chat.title})")
        with open("channel_id.txt", "w") as f:
            f.write(str(chat.id))

async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if context.args:
        cid = context.args[0].strip()
        with open("channel_id.txt", "w") as f:
            f.write(cid)
        await update.message.reply_text(f"Target channel linked to: <code>{cid}</code>", parse_mode="HTML")
        return
    if chat:
        with open("channel_id.txt", "w") as f:
            f.write(str(chat.id))
        await update.message.reply_text(f"Target channel linked to: <code>{chat.id}</code> ({chat.title})", parse_mode="HTML")

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from state import get_all_signals
    from paper_trader import load_wallet, INITIAL_CAPITAL
    import zoneinfo
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    now_ist = datetime.now(timezone.utc).astimezone(tz)
    today_str = now_ist.strftime("%Y-%m-%d")
    
    # ── Signals sent today (from state) ──
    signals = get_all_signals()
    today_signals = [s for s in signals if today_str in str(s.get("sent_at", ""))]
    total_signals = len(today_signals)

    # ── Executed paper trades closed today (from paper wallet) ──
    wallet = load_wallet()
    closed = wallet.get("closed_trades", [])
    CANCELLED_STATUSES = {"CANCELLED_30MIN_EXPIRED", "CANCELLED_20MIN_EXPIRED", "CANCELLED_SIGNAL_REVERSAL"}
    today_exec = [
        t for t in closed
        if str(t.get("closed_at", "")).startswith(today_str)
        and t.get("status", "") not in CANCELLED_STATUSES
        and "CANCEL" not in str(t.get("status", ""))
        and t.get("symbol") != "PROFIT_INJECTION"
        and float(t.get("cumulative_pnl", 0.0) or 0.0) != 0.0
    ]
    today_expired = [
        t for t in closed
        if str(t.get("closed_at", "")).startswith(today_str)
        and t.get("status", "") in CANCELLED_STATUSES
    ]
    exec_wins = sum(1 for t in today_exec if float(t.get("cumulative_pnl", 0.0) or 0.0) > 0)
    exec_losses = sum(1 for t in today_exec if float(t.get("cumulative_pnl", 0.0) or 0.0) < 0)
    today_pnl = sum(float(t.get("cumulative_pnl", 0.0) or 0.0) for t in today_exec)

    # Active positions open right now
    active_now = len(wallet.get("active_positions", []))
    resting_now = len(wallet.get("resting_orders", []))

    if total_signals == 0 and not today_exec:
        await update.message.reply_text(f"📊 <b>TODAY'S REPORT</b>\n\nNo signals or trades recorded yet for {today_str}.", parse_mode="HTML")
        return

    pnl_sign = "+" if today_pnl >= 0 else ""
    msg = (
        f"📊 <b>TODAY'S COMPLETE REPORT</b>\n"
        f"<code>Date: {today_str}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📡 <b>Signals Sent Today:</b> {total_signals}\n"
        f"⚡ <b>Executed Trades Today:</b> {len(today_exec)} ({exec_wins}W - {exec_losses}L)\n"
        f"⏰ <b>Unfilled (Expired):</b> {len(today_expired)} limit orders\n"
        f"🟢 <b>Still Active Right Now:</b> {active_now} positions + {resting_now} resting\n"
        f"💰 <b>Today's Realized PnL:</b> <code>{pnl_sign}₹{today_pnl:,.2f} INR</code>\n\n"
    )

    if today_signals:
        msg += f"<b>Recent Signals Log (last 15):</b>\n"
        wins = sum(1 for s in today_signals if s.get("outcome") in ["WIN", "TP1_EXIT", "TP2_EXIT", "TP3_EXIT"])
        losses = sum(1 for s in today_signals if s.get("outcome") in ["LOSS", "SL_HIT"])
        pending = total_signals - (wins + losses)
        msg += f"🎯 Wins: {wins} | 🔴 Losses: {losses} | ⏳ Pending/Active: {pending}\n\n"
        for idx, s in enumerate(today_signals[:15], 1):
            sym = s.get("symbol", "")
            sig = s.get("signal", "")
            tf = s.get("tf", "")
            out = s.get("outcome", "PENDING")
            icon = "🎯" if "WIN" in out or "TP" in out else ("🔴" if "LOSS" in out or "SL" in out else "⏳")
            msg += f"{idx}. {icon} <b>{sym}</b> {tf} [{sig}] — <i>{out}</i>\n"
        if total_signals > 15:
            msg += f"\n<i>...and {total_signals - 15} more signals earlier today.</i>"
        
    await update.message.reply_text(msg, parse_mode="HTML")



async def on_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-detects channel ID from any forwarded message and replies instantly."""
    msg = update.message
    if not msg:
        return
    if getattr(msg, "forward_from_chat", None):
        chat = msg.forward_from_chat
        await msg.reply_text(
            f"📋 <b>Channel ID Found!</b>\n\n"
            f"<b>Channel:</b> {chat.title or 'Unknown'}\n"
            f"<b>ID:</b> <code>{chat.id}</code>\n\n"
            f"Send this ID to the agent!",
            parse_mode="HTML"
        )
    elif msg.forward_from:
        await msg.reply_text(f"That was forwarded from a user, not a channel. Please forward a message FROM the channel itself.", parse_mode="HTML")

async def cmd_getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports the channel ID of any forwarded message or current chat."""
    msg = update.message
    if not msg:
        return
    
    # If user forwarded a message from a channel
    if msg.forward_from_chat:
        chat = msg.forward_from_chat
        await msg.reply_text(
            f"📋 <b>Channel Detected!</b>\n\n"
            f"<b>Name:</b> {chat.title or 'Unknown'}\n"
            f"<b>ID:</b> <code>{chat.id}</code>\n\n"
            f"Copy this ID and send it to the agent!",
            parse_mode="HTML"
        )
    else:
        # Report the current chat ID
        await msg.reply_text(
            f"📋 <b>Current Chat ID:</b> <code>{msg.chat_id}</code>\n\n"
            f"<i>To get a channel ID, forward any message FROM that channel TO this bot, then type /getid</i>",
            parse_mode="HTML"
        )


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports real-time Paper Trading Strategy Lookout or Closed Trades Ledger + Shark Live Status."""
    msg = update.message
    if not msg:
        return
    try:
        from paper_trader import generate_strategy_lookout_card, generate_trades_ledger_card
        cmd_text = (msg.text or "").strip().lower()
        if "trade" in cmd_text:
            card = await generate_trades_ledger_card()
        else:
            card = await generate_strategy_lookout_card()

        # If Shark Live Auto-Trading is configured, prepend live exchange summary
        try:
            from executor import executor
            if executor.is_configured():
                import aiohttp
                async with aiohttp.ClientSession() as s:
                    shark_bal = await executor.get_wallet_balance_summary(s)
                    shark_pos = await executor.get_open_positions(s)
                    if shark_bal:
                        pos_lines = []
                        for p in shark_pos:
                            sym = executor.clean_sym(p)
                            side = p.get("side", p.get("direction", "LONG"))
                            sz = p.get("positionAmt", p.get("size", 0))
                            upnl = p.get("unrealizedProfit", p.get("pnl", 0))
                            pos_lines.append(f"  • {sym} [{side}] Qty: {sz} (Floating PnL: ${float(upnl):.2f})")
                        pos_str = "\n".join(pos_lines) if pos_lines else "  • No open exchange positions."
                        shark_header = (
                            f"🦈 <b>SHARK EXCHANGE LIVE AUTO-TRADING</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"{shark_bal}\n"
                            f"🟢 <b>Active Positions ({len(shark_pos)}):</b>\n"
                            f"{pos_str}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        )
                        card = shark_header + card
        except Exception as e_sh:
            logger.warning(f"Could not append Shark balance in cmd_positions: {e_sh}")

        await msg.reply_text(card, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error in cmd_positions: {e}")
        await msg.reply_text(f"⚠️ Error fetching paper trading details: {e}")


async def on_any_channel_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches any message posted in any channel and prints it to the console loudly."""
    if update.channel_post:
        chat = update.channel_post.chat
        print(f"\n\n🔥🔥🔥 CHANNEL DETECTED 🔥🔥🔥")
        print(f"NAME: {chat.title}")
        print(f"ID: {chat.id}")
        print(f"🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥\n\n")
        logging.getLogger("telegram_bot").error(f"CHANNEL DETECTED: {chat.title} -> {chat.id}")

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command in main bot: deletes message across all users and channels."""
    user_id = str(update.effective_user.id if update.effective_user else "")
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593")
    if user_id != admin_id:
        return

    import message_sync
    if update.message and update.message.reply_to_message:
        replied_mid = update.message.reply_to_message.message_id
        try:
            await update.message.reply_to_message.delete()
        except Exception:
            pass
        try:
            await update.message.delete()
        except Exception:
            pass
        found = await message_sync.delete_by_admin_message_id(replied_mid)
        if not found:
            await message_sync.delete_last_n_broadcasts(1)
        return

    count = 1
    if context.args and context.args[0].isdigit():
        count = int(context.args[0])
    try:
        await update.message.delete()
    except Exception:
        pass
    await message_sync.delete_last_n_broadcasts(count)


async def cmd_stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: Immediately stops all bots, scanners, and channels."""
    user_id = str(update.effective_user.id if update.effective_user else "")
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593")
    if user_id != admin_id:
        return
    with open("STOP_BOT.flag", "w") as f:
        f.write("STOPPED via Telegram /stop at " + datetime.now(timezone.utc).isoformat())
    await update.message.reply_text("🛑 <b>EMERGENCY STOP TRIGGERED</b>\n\nAll scanners, trades, and channels have been immediately stopped and frozen!\n\nTo resume, send /startbot", parse_mode="HTML")


# ── CHANNEL MEMBERSHIP & KICK/BAN ADMIN MANAGEMENT ───────────────────

async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to list and manage members across EMA Cross, EMA Signal Scanner, and Alpha."""
    user_id = str(update.effective_user.id if update.effective_user else "")
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593").strip()
    if user_id != admin_id:
        return

    try:
        await channel_manager.sync_channel_admins(context.bot)
    except Exception:
        pass

    import admin_panel
    text, kb = admin_panel.build_admin_overview()
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def cmd_kick_or_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /kick <scanner|cross> <user_id> or /banchannel <scanner|cross> <user_id>"""
    user_id = str(update.effective_user.id if update.effective_user else "")
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593").strip()
    if user_id != admin_id:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "ℹ️ <b>Channel Member Control Usage:</b>\n\n"
            "• <code>/kick &lt;scanner|cross&gt; &lt;user_id&gt;</code> &mdash; Kick user from channel\n"
            "• <code>/banchannel &lt;scanner|cross&gt; &lt;user_id&gt;</code> &mdash; Ban user from channel\n\n"
            "<i>💡 Type <code>/channels</code> to see all members with 1-click kick options next to their names.</i>",
            parse_mode="HTML"
        )
        return

    ch_target = args[0].lower().strip()
    target_uid = args[1].strip()
    cmd_name = (update.message.text or "").split()[0].lower()
    is_ban = "ban" in cmd_name

    ch_id = "-1004332146822" if "scan" in ch_target or "4332146822" in ch_target else "-1004463573705"
    ch_title = channel_manager.CHANNELS.get(ch_id, "Channel")

    if is_ban:
        ok, res = await channel_manager.ban_channel_member(context.bot, ch_id, target_uid)
        act_text = "PERMANENTLY BANNED"
    else:
        ok, res = await channel_manager.kick_channel_member(context.bot, ch_id, target_uid)
        act_text = "KICKED / REMOVED"

    if ok:
        await update.message.reply_text(
            f"✅ User <code>{target_uid}</code> has been <b>{act_text}</b> from <b>{ch_title}</b>.",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            f"⚠️ Failed to remove user <code>{target_uid}</code> from <b>{ch_title}</b>:\n<code>{res}</code>",
            parse_mode="HTML"
        )


async def cmd_quick_channel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles 1-tap commands like /rm_scan_12345, /ban_cross_12345, /unban_alpha_12345 directly."""
    user_id = str(update.effective_user.id if update.effective_user else "")
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593").strip()
    if user_id != admin_id:
        return

    text = (update.message.text or "").strip()
    if not text.startswith("/"):
        return

    import re
    import admin_panel

    match = re.match(r"^/(rm|ban|unban)_(scan|scanner|cross|alpha)_(\d+)", text)
    if match:
        act_verb, platform, target_uid = match.groups()
        plat = "scan" if "scan" in platform else platform
        act_key = f"{act_verb}_{plat}"
        ok, res_msg = await admin_panel.execute_action(context.bot, act_key, target_uid)
        status_emoji = "✅" if ok else "⚠️"
        await update.message.reply_text(f"{status_emoji} <b>{res_msg}</b>", parse_mode="HTML")
        return

    parts = text[1:].split("_")
    if len(parts) == 3:
        act, ch_alias, uid = parts[0].lower(), parts[1].lower(), parts[2].strip()
        ch_id = "-1004332146822" if "scan" in ch_alias else "-1004463573705"
        ch_title = channel_manager.CHANNELS.get(ch_id, "Channel")
        if act == "kick":
            ok, res = await channel_manager.kick_channel_member(context.bot, ch_id, uid)
            act_text = "KICKED"
        else:
            ok, res = await channel_manager.ban_channel_member(context.bot, ch_id, uid)
            act_text = "PERMANENTLY BANNED"
        status_emoji = "✅" if ok else "⚠️"
        await update.message.reply_text(f"{status_emoji} User <code>{uid}</code> <b>{act_text}</b> from <b>{ch_title}</b>.", parse_mode="HTML")


async def on_channel_member_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593").strip()
    user_id = str(update.effective_user.id if update.effective_user else "")

    if user_id != admin_id:
        await query.answer("Access Denied.", show_alert=True)
        return

    if data.startswith("act_"):
        user_id = str(update.effective_user.id if update.effective_user else "")
        from config import TELEGRAM_CHAT_ID
        admin_id = str(TELEGRAM_CHAT_ID or "771305593").strip()
        if user_id != admin_id:
            await query.answer("Access Denied.", show_alert=True)
            return

        import admin_panel
        if data == "act_refresh" or data == "act_back_users":
            text, kb = admin_panel.build_admin_overview()
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if data.startswith("act_user_"):
            target_uid = data.replace("act_user_", "").strip()
            text, kb = admin_panel.build_user_action_menu(target_uid)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        parts = data.split("_")
        if len(parts) >= 4:
            act_key = f"{parts[1]}_{parts[2]}"
            target_uid = parts[3]
            ok, res_msg = await admin_panel.execute_action(context.bot, act_key, target_uid)
            await query.answer(res_msg, show_alert=True)
            text, kb = admin_panel.build_user_action_menu(target_uid)
            status_emoji = "✅" if ok else "⚠️"
            await query.edit_message_text(f"{status_emoji} <b>{res_msg}</b>\n\n" + text, reply_markup=kb, parse_mode="HTML")
            return


    if data == "ch_sync":
        await channel_manager.sync_channel_admins(context.bot)
        await query.answer("Channel admins synced successfully!", show_alert=True)
        return

    parts = data.split("_")
    if len(parts) >= 4:
        act, ch_alias, uid = parts[1], parts[2], parts[3]
        ch_id = "-1004332146822" if "scan" in ch_alias else "-1004463573705"
        ch_title = channel_manager.CHANNELS.get(ch_id, "Channel")

        if act == "k":
            ok, res = await channel_manager.kick_channel_member(context.bot, ch_id, uid)
            act_text = "Kicked"
        else:
            ok, res = await channel_manager.ban_channel_member(context.bot, ch_id, uid)
            act_text = "Banned"

        if ok:
            await query.edit_message_text(
                f"✅ User <code>{uid}</code> has been <b>{act_text}</b> from <b>{ch_title}</b>.",
                parse_mode="HTML"
            )
        else:
            await query.answer(f"Failed: {res}", show_alert=True)


async def on_channel_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Real-time listener for users joining, leaving, or being promoted in channels."""
    result = update.chat_member
    if not result:
        return
    chat_id = str(result.chat.id)
    if chat_id not in channel_manager.CHANNELS:
        return
    u = result.new_chat_member.user
    if u.is_bot:
        return
    status = result.new_chat_member.status
    channel_manager.track_member_update(
        chat_id=chat_id,
        user_id=str(u.id),
        username=u.username,
        first_name=u.first_name,
        status=status
    )

async def cmd_resume_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: Resumes bots."""
    user_id = str(update.effective_user.id if update.effective_user else "")
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593")
    if user_id != admin_id:
        return
    if os.path.exists("STOP_BOT.flag"):
        try:
            os.remove("STOP_BOT.flag")
        except Exception:
            pass
    await update.message.reply_text("✅ <b>BOT RESUMED</b>\n\nSTOP_BOT lock cleared. Scanners are active.", parse_mode="HTML")


def get_application() -> Application:
    """Returns singleton Application instance configured with interactive handlers."""
    global _app_instance
    if _app_instance is None:
        _app_instance = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        from telegram.ext import MessageHandler, filters, ChatMemberHandler
        
        # Core bot command handlers
        _app_instance.add_handler(CommandHandler(["setchannel", "linkchannel"], cmd_setchannel))
        _app_instance.add_handler(CommandHandler(["today", "report", "stats"], cmd_today))
        _app_instance.add_handler(CommandHandler(["getid", "channelid", "id"], cmd_getid))
        _app_instance.add_handler(CommandHandler(["positions", "paper", "wallet", "trades"], cmd_positions))
        _app_instance.add_handler(CommandHandler(["del", "delete", "purge"], cmd_delete))
        _app_instance.add_handler(CommandHandler(["stop", "kill", "halt", "pause"], cmd_stop_bot))
        _app_instance.add_handler(CommandHandler(["startbot", "resume", "resumebot"], cmd_resume_bot))

        # Channel member management handlers
        _app_instance.add_handler(CommandHandler(["admin", "users", "channels", "channelusers", "channelmembers", "members"], cmd_channels))
        _app_instance.add_handler(MessageHandler(filters.Regex(r"^/(rm|ban|unban)_(scan|scanner|cross|alpha)_\\d+"), cmd_quick_channel_action))
        _app_instance.add_handler(CommandHandler(["kick", "kickchannel", "removechanneluser", "banchannel"], cmd_kick_or_ban))
        _app_instance.add_handler(MessageHandler(filters.Regex(r"^/(kick|ban)_(scanner|cross)_\d+"), cmd_quick_channel_action))
        _app_instance.add_handler(CallbackQueryHandler(on_channel_member_callback, pattern=r"^(act_|ch_)"))
        _app_instance.add_handler(ChatMemberHandler(on_channel_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

        _app_instance.add_handler(MessageHandler(filters.FORWARDED, on_forwarded_message))
        _app_instance.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_any_channel_message))
        
        # Auto-detect channel posts and membership updates
        _app_instance.add_handler(MessageHandler(filters.ALL, on_channel_activity))
        _app_instance.add_handler(ChatMemberHandler(on_channel_activity))
        
    return _app_instance

async def start_telegram_bot_service():
    """Starts the Telegram application polling service asynchronously."""
    app = get_application()
    logger.info("Initializing Telegram Interactive Bot Application...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=["message", "channel_post", "my_chat_member", "chat_member", "callback_query"],
        drop_pending_updates=False
    )
    logger.info("Telegram Interactive Bot Polling STARTED (commands & interactive filters active).")

async def stop_telegram_bot_service():
    """Stops the Telegram application polling service cleanly."""
    global _app_instance
    if _app_instance:
        logger.info("Stopping Telegram Interactive Bot Service...")
        try:
            if _app_instance.updater and _app_instance.updater.is_running:
                await _app_instance.updater.stop()
            if _app_instance.is_running:
                await _app_instance.stop()
                await _app_instance.shutdown()
        except Exception as e:
            logger.error(f"Error stopping Telegram application: {e}")
        logger.info("Telegram Bot Service STOPPED.")


# ── STAGE 1: PRE-SQUEEZE RADAR BROADCAST (WATCHLIST COILING WITH PROPER TICKS) ──

async def send_pre_squeeze_radar_alert(radar_data: dict):
    """
    Sends an institutional Stage 1: Watchlist Coiling notification with verified ticks.
    Broadcasts to EMA Scanner Channel, Admin DM, and Filter Bot subscribers.
    Preserves EMA Crossover Channel for trade entries only.
    """
    symbol = radar_data.get("symbol", "")
    direction = str(radar_data.get("direction", "BUY")).upper()
    squeeze_type = radar_data.get("squeeze_type", "SHORT SQUEEZE")
    funding_pct = float(radar_data.get("funding_pct", 0.0))
    oi_change_1h = float(radar_data.get("oi_change_1h", 0.0))
    compression_pct = float(radar_data.get("compression_pct", 0.0))
    vol_24h_usd = float(radar_data.get("vol_24h_usd", 0.0))
    ttm_squeeze = bool(radar_data.get("ttm_squeeze", False))
    bb_width = float(radar_data.get("bb_width", 0.0))
    trigger_entry = float(radar_data.get("trigger_entry", 0.0))
    invalidation_sl = float(radar_data.get("invalidation_sl", 0.0))
    tf_states = radar_data.get("tf_states") or {}

    from fetcher import get_market_session_info
    session_info = radar_data.get("session_info") or get_market_session_info()
    session_tag = session_info.get("tag", "🌐 Global Session")

    display_sym = format_display_symbol(symbol)
    status_icon = "🚀" if direction == "BUY" else "🩸"
    
    if direction == "BUY":
        direction_tag = "SHORT SQUEEZE COILING"
        action_plan = "🟢 Preparing BUY / LONG when Breakout Trigger breaks"
        mechanism = "Trapped shorts paying funding under resistance. Breakout will trigger forced short coverings & liquidations (violently driving price upward)!"
        funding_tag = "Trapped Shorts"
    else:
        direction_tag = "LONG SQUEEZE COILING"
        action_plan = "🔴 Preparing SELL / SHORT when Breakdown Trigger breaks"
        mechanism = "Trapped longs paying funding above support. Breakdown will trigger forced long liquidations (cascading price downward)!"
        funding_tag = "Trapped Longs"

    oi_sign = "+" if oi_change_1h >= 0 else ""
    vol_str = f"${vol_24h_usd/1e6:.1f}M" if vol_24h_usd >= 1e6 else f"${vol_24h_usd:,.0f}"
    ttm_tag = "TTM Squeeze ON (BB inside Keltner)" if ttm_squeeze else f"BB Width Pinch ({bb_width:.2f}%)"

    h1_st = str(tf_states.get("1H", "Neutral")).capitalize()
    h4_st = str(tf_states.get("4H", "Neutral")).capitalize()
    i_1h = "🟢" if h1_st == "Bullish" else ("🔴" if h1_st == "Bearish" else "⚪")
    i_4h = "🟢" if h4_st == "Bullish" else ("🔴" if h4_st == "Bearish" else "⚪")

    sq_tf = radar_data.get("tf_label", "1H")
    text = (
        f"📡 <b>PRE-SQUEEZE RADAR (Coiling Watchlist)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>Pair:</b> <code>{display_sym}</code> ({sq_tf} Base)\n"
        f"⚡ <b>Setup:</b> {status_icon} <b>{direction_tag}</b>\n"
        f"🎯 <b>Planned Trade:</b> <b>{action_plan}</b>\n\n"
        f"🧲 <b>Market Dynamic:</b>\n"
        f"<i>{mechanism}</i>\n\n"
        f"📋 <b>Institutional Coiling Checklist:</b>\n"
        f"✅ <b>[1] Active Market:</b> Liquid (24h Vol {vol_str} &gt; $10M)\n"
        f"✅ <b>[2] Range Compression:</b> <code>{compression_pct:.2f}%</code> (Tight Coiling)\n"
        f"✅ <b>[3] Volatility Pinch:</b> {ttm_tag}\n"
        f"✅ <b>[4] Smart Money OI:</b> <code>{oi_sign}{oi_change_1h:.1f}%</code> (1H Accumulation)\n"
        f"✅ <b>[5] Leverage Bias:</b> <code>{funding_pct:+.4f}%</code> ({funding_tag})\n"
        f"✅ <b>[6] Trend Alignment:</b> {i_1h} 1H {h1_st} | {i_4h} 4H {h4_st}\n\n"
        f"🎯 <b>Action Trigger Levels:</b>\n"
        f"{'🟢' if direction == 'BUY' else '🔴'} <b>{'Breakout' if direction == 'BUY' else 'Breakdown'} Trigger:</b> <code>{format_price(trigger_entry, symbol)}</code>\n"
        f"🛑 <b>Base Invalidation (SL):</b> <code>{format_price(invalidation_sl, symbol)}</code>\n\n"
        f"💡 <b>Execution Strategy:</b>\n"
        f"Do NOT enter early in the middle of the range. Bot is monitoring live orderbook &amp; CVD. You will receive an instant trade entry alert as soon as the candle confirms out of the base!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

    EMA_SCANNER_CHANNEL = "-1004332146822"
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593")

    # Send to Scanner Channel (Admin receives via Filter Bot dispatch below, preventing duplicate notification)
    try:
        await send_text_to_chat(EMA_SCANNER_CHANNEL, text)
    except Exception as e:
        logger.error(f"Failed to dispatch radar alert to {EMA_SCANNER_CHANNEL}: {e}")

    try:
        from filter_bot import dispatch_filtered_alert
        f_alert_data = {
            "symbol": symbol,
            "tf_label": radar_data.get("tf_label", "1H"),
            "signal": direction,
            "is_squeeze": True,
            "is_dedicated_squeeze": True,
            "cross_type": "Pre-Squeeze Radar",
            "aligned_count": 4
        }
        await dispatch_filtered_alert(f_alert_data, text)
    except Exception as e_f:
        logger.error(f"Error dispatching radar to filter subscribers: {e_f}")
