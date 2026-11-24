import channel_manager
import time
import asyncio
import logging
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    MenuButtonCommands
)
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
        return False

    # STRICT USER DIRECTIVE: No trades of commodities, BTC, and ETH on Sat and Sunday in any case
    from config import is_weekend_trading_blocked
    if is_weekend_trading_blocked(symbol):
        logger.info(f"🚫 BLOCKED alert for {symbol} ({tf_label} {signal}) — No commodities, BTC, or ETH on Saturday/Sunday.")
        return False

    # STRICT SQUEEZE TIMEFRAME RULE (User Directive: '1 hr squeze pr hi raho fir'):
    # Squeeze Breakouts are strictly restricted to 1H timeframe only. All lower timeframe squeezes (15M) are banned.
    is_sq_check = ("Squeeze" in cross_type) or kwargs.get("is_dedicated_squeeze", False) or ("Breakout" in cross_type and "Retest" not in cross_type)
    if is_sq_check and str(tf_label).upper() not in ["1H", "1HR", "60M"]:
        logger.info(f"🚫 BLOCKED Squeeze alert for {symbol} ({tf_label}) — Squeeze breakouts strictly restricted to 1H timeframe.")
        return False

    is_retest = "Retest" in cross_type
    if is_retest:
        candle_time = datetime.now(timezone.utc).isoformat()

    target_trend = "bullish" if signal == "BUY" else "bearish"
    aligned_count = sum(1 for t in ["1H", "4H", "1D", "1W"] if tf_states and tf_states.get(t, "neutral").lower() == target_trend)
    is_15m = str(tf_label).upper() in ["15M", "15MIN"]
    is_5m = str(tf_label).upper() in ["5M", "5MIN"]
    is_trap_type = ("Trap" in cross_type) or ("SFP" in cross_type) or kwargs.get("is_trap_reversal", False)
    is_squeeze_type = ("Squeeze" in cross_type) or ("Breakout" in cross_type) or ("Breakdown" in cross_type) or is_trap_type
    is_gold_scalp = (symbol in ALLOWED_15M_COINS or "XAU" in symbol.upper()) and (is_15m or is_5m)

    if is_5m and not is_gold_scalp:
        logger.info(f"BLOCKED 5M signal for {symbol} — 5M strictly Gold only.")
        return False

    if is_15m and (symbol not in ALLOWED_15M_COINS) and (not is_squeeze_type):
        logger.info(f"BLOCKED 15M signal for {symbol} — not in ALLOWED_15M_COINS.")
        return False

    # USER DIRECTIVE & DUAL CHANNEL DISTRIBUTION:
    # 1. Retest alerts: Strictly minimum 3-Star (3/4 ⭐⭐⭐) and 4-Star (4/4 ⭐⭐⭐⭐) for HTF.
    #    For Gold Scalps (5M & 15M): Allowed directly without requiring 3/4 stars!
    # 2. Fresh crossovers:
    #    - If timeframe is 5M/15M Gold Scalp: Allowed directly.
    #    - For all other timeframes (30M, 1H, 4H, 1D, 1W): Strictly minimum 3-Star (3/4 ⭐⭐⭐) and 4-Star (4/4 ⭐⭐⭐⭐).
    if is_retest:
        if not is_gold_scalp and aligned_count < 3:
            logger.info(f"BLOCKED {signal} retest for {symbol} ({tf_label}) — only {aligned_count}/4 stars (minimum 3 required, ignoring 2 stars).")
            return False
    else:
        if is_15m or is_5m:
            if (symbol not in ALLOWED_15M_COINS and "XAU" not in symbol.upper()) and (not is_squeeze_type):
                logger.info(f"BLOCKED {tf_label} crossover for {symbol} — not in ALLOWED_15M_COINS.")
                return False
        else:
            if not is_trap_type and not is_squeeze_type and aligned_count < 3:
                logger.info(f"BLOCKED {signal} crossover for {symbol} ({tf_label}) — {aligned_count}/4 stars (minimum 3 required for HTF).")
                return False

    # STRICT RSI MOMENTUM GATE:
    # All signals MUST strictly satisfy healthy RSI momentum range:
    # Trap Reversals: BUY <= 45.0 (oversold bounce), SELL >= 55.0 (overbought flush)
    # Trend Setups: BUY: 42.0 <= RSI <= 62.0, SELL: 38.0 <= RSI <= 58.0
    if rsi is not None:
        try:
            f_rsi = float(rsi)
            if is_trap_type:
                if signal == "BUY" and f_rsi > 45.0:
                    logger.info(f"🚫 BLOCKED {symbol} Bear Trap BUY ({tf_label}) — RSI {f_rsi:.1f} not oversold enough (> 45.0).")
                    return False
                elif signal == "SELL" and f_rsi < 55.0:
                    logger.info(f"🚫 BLOCKED {symbol} Bull Trap SELL ({tf_label}) — RSI {f_rsi:.1f} not overbought enough (< 55.0).")
                    return False
            elif is_squeeze_type or kwargs.get("is_dedicated_squeeze", False):
                # Squeeze Breakouts: High momentum candles naturally surge!
                # Healthy breakout bounds: BUY [42.0 - 78.0], SELL [22.0 - 58.0]
                if signal == "BUY" and not (42.0 <= f_rsi <= 78.0):
                    logger.info(f"🚫 BLOCKED {symbol} Squeeze BUY ({tf_label}) — RSI {f_rsi:.1f} outside squeeze breakout bounds [42, 78].")
                    return False
                elif signal == "SELL" and not (22.0 <= f_rsi <= 58.0):
                    logger.info(f"🚫 BLOCKED {symbol} Squeeze SELL ({tf_label}) — RSI {f_rsi:.1f} outside squeeze breakdown bounds [22, 58].")
                    return False
            else:
                if signal == "BUY" and not (42.0 <= f_rsi <= 62.0):
                    logger.info(f"🚫 BLOCKED {symbol} BUY ({tf_label}) — RSI {f_rsi:.1f} outside healthy momentum bounds [42, 62] (Overbought peak/exhausted).")
                    return False
                elif signal == "SELL" and not (38.0 <= f_rsi <= 58.0):
                    logger.info(f"🚫 BLOCKED {symbol} SELL ({tf_label}) — RSI {f_rsi:.1f} outside healthy momentum bounds [38, 58] (Oversold bottom/exhausted < 38).")
                    return False
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
        "tp1": tp1, "tp2": tp2, "tp3": tp2, "rr1": rr1, "rr2": rr2, "rr3": rr2,
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
    return True

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
    tp1 = data.get("tp1")
    tp2 = data.get("tp2") or data.get("tp3")
    rr1 = data.get("rr1")
    rr2 = data.get("rr2") or data.get("rr3")
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
        if "Bear Trap" in cross_type:
            sq_icon = "🚀"
            squeeze_title = "🚀 1H BEAR TRAP REVERSAL"
            action_desc = "🟢 BUY (LONG) — Support Reclaim SFP"
            market_dynamic = "🪤 <b>Bear Trap Reclaim:</b> Price swept liquidity below range support, trapped breakout shorters, and reclaimed support with strong buying absorption!"
        elif "Bull Trap" in cross_type:
            sq_icon = "🩸"
            squeeze_title = "🩸 1H BULL TRAP REVERSAL"
            action_desc = "🔴 SELL (SHORT) — Resistance Flush SFP"
            market_dynamic = "🪤 <b>Bull Trap Rejection:</b> Price swept liquidity above range resistance, trapped breakout longs, and rejected back inside range with strong selling absorption!"
        elif signal == "BUY":
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
    
    target_tp = tp1 or data.get("tp")
    f_rr = float(rr1) if rr1 is not None else (float(data.get("rr")) if data.get("rr") is not None else 1.5)
    block += f"🎯 <b>TARGET ({f_rr:.1f}R — 100% Full Exit):</b> {format_price(target_tp, symbol)} (1:{f_rr:.1f} R:R)\n"
        
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
    from config import PAPER_TRADING_ENABLED
    if PAPER_TRADING_ENABLED:
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
    import zoneinfo
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    now_ist = datetime.now(timezone.utc).astimezone(tz)
    today_str = now_ist.strftime("%Y-%m-%d")
    
    # ── Signals sent today (from state) ──
    signals = get_all_signals()
    today_signals = [s for s in signals if today_str in str(s.get("sent_at", ""))]
    total_signals = len(today_signals)

    from config import PAPER_TRADING_ENABLED
    from executor import executor

    if not PAPER_TRADING_ENABLED and executor.is_configured():
        # REAL LIVE TRADING REPORT: Direct from Shark Exchange
        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                shark_pos = await executor.get_open_positions(sess)
                open_orders = await executor.get_open_orders(sess)
                shark_bal = await executor.get_wallet_balance_summary(sess)
                st_cl, closed_raw = await executor.shark_get(sess, "/v1/positions/CLOSED")
                if st_cl == 200 and isinstance(closed_raw, list):
                    today_closed = [p for p in closed_raw if str(p.get("updatedAt", "")) >= today_str]
                else:
                    today_closed = []
        except Exception:
            shark_pos = []
            open_orders = []
            shark_bal = ""
            today_closed = []

        resting_entries = [o for o in open_orders if not o.get("reduceOnly") and o.get("placeType") != "POSITION"]

        # User Explicit Directive: Trades exiting <= ₹15 INR are classified as BREAKEVEN (BE / CTC)
        wins = sum(1 for p in today_closed if float(p.get("realizedProfitInMarginAsset") or 0) > 15.0)
        losses = sum(1 for p in today_closed if float(p.get("realizedProfitInMarginAsset") or 0) < -15.0)
        breakevens = sum(1 for p in today_closed if abs(float(p.get("realizedProfitInMarginAsset") or 0)) <= 15.0)
        realized_pnl = sum(float(p.get("realizedProfitInMarginAsset") or 0) for p in today_closed)
        pnl_sign = "+" if realized_pnl >= 0 else ""

        closed_lines = []
        for cp in today_closed:
            sym_c = executor.clean_sym(cp)
            pos_t = str(cp.get("positionType") or "SHORT").upper()
            sig_c = "SHORT" if pos_t in ("SHORT", "SELL") else "LONG"
            pnl_inr = float(cp.get("realizedProfitInMarginAsset") or 0.0)
            sign = "+" if pnl_inr >= 0 else ""
            if abs(pnl_inr) <= 15.0:
                status_txt = "BREAKEVEN (CTC)"
            elif pnl_inr > 15.0:
                status_txt = "TP WIN"
            else:
                status_txt = "SL HIT"
            closed_lines.append(f"  • {sym_c} [{sig_c}] — <b>{status_txt}</b>: {sign}₹{pnl_inr:,.2f}")
        closed_str = "\n".join(closed_lines) if closed_lines else "  • <i>No closed trades today.</i>"

        msg = (
            f"🦈 <b>TODAY'S REAL LIVE TRADING REPORT</b>\n"
            f"<code>Date: {today_str}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💼 <b>Status:</b> 🟢 <b>Real Live Trading Active (Shark Exchange)</b>\n"
            f"{shark_bal}\n\n"
            f"📡 <b>Signals Sent Today:</b> {total_signals}\n"
            f"⚡ <b>Executed Trades Today:</b> {len(today_closed)} ({wins}W - {losses}L - {breakevens}BE)\n"
            f"💰 <b>Realized PnL Today:</b> <b>{pnl_sign}₹{realized_pnl:,.2f} INR</b>\n\n"
            f"📜 <b>Closed Trades Summary:</b>\n{closed_str}\n\n"
            f"🟢 <b>Active Broker Positions:</b> {len(shark_pos)}\n"
            f"⏳ <b>Resting Entry Orders:</b> {len(resting_entries)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        # Paper trading fallback
        from paper_trader import load_wallet, INITIAL_CAPITAL
        wallet = load_wallet()
        closed = wallet.get("closed_trades", [])
        CANCELLED_STATUSES = {"CANCELLED_30MIN_EXPIRED", "CANCELLED_20MIN_EXPIRED", "CANCELLED_SIGNAL_REVERSAL", "CANCELLED_UNFILLED"}
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

        active_now = len(wallet.get("active_positions", []))
        resting_now = len(wallet.get("resting_orders", []))

        if total_signals == 0 and not today_exec:
            await update.message.reply_text(f"📊 <b>TODAY'S REPORT</b>\n\nNo signals or trades recorded yet for {today_str}.", parse_mode="HTML")
            return

        pnl_sign = "+" if today_pnl >= 0 else ""
        msg = (
            f"📊 <b>TODAY'S REPORT</b>\n"
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

ANUJ_CHAT_ID = "1010973596"
ADMIN_CHAT_ID = "771305593"

def resolve_user_account_context(chat_id: str | int) -> tuple[int, bool]:
    """
    Resolves the account index and admin status for a given Telegram chat ID.
    - Anuj Sharma ('1010973596'): Strictly Account 2, is_admin=False (100% Personal).
    - Himanshu ('771305593'): Account 1 default, is_admin=True (Admin controller).
    - Other users: Default Account 1, is_admin=False.
    """
    cid = str(chat_id).strip()
    if cid == ANUJ_CHAT_ID:
        return 2, False
    elif cid == ADMIN_CHAT_ID:
        return 1, True
    return 1, False


def get_permanent_reply_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """
    Builds a permanent bottom docked keyboard (is_persistent=True) so the user
    always has instant menu access docked right at their keyboard bar.
    """
    if is_admin:
        kb = [
            [KeyboardButton("🏠 Front Page"), KeyboardButton("🟢 Live Trades")],
            [KeyboardButton("📡 Active Radars"), KeyboardButton("💰 Realized Profits")],
            [KeyboardButton("📜 Trade History"), KeyboardButton("🌐 Web Dashboard")],
            [KeyboardButton("🌐 All Accounts"), KeyboardButton("🔄 Refresh")]
        ]
    else:
        kb = [
            [KeyboardButton("🏠 Front Page"), KeyboardButton("🟢 Live Trades")],
            [KeyboardButton("📡 Active Radars"), KeyboardButton("💰 Realized Profits")],
            [KeyboardButton("📜 Trade History"), KeyboardButton("🌐 Web Dashboard")],
            [KeyboardButton("🔄 Refresh"), KeyboardButton("⚙️ Filter Settings")]
        ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, is_persistent=True)


def get_account_keyboard(acc_idx: int = 1, view: str = "front", is_admin: bool = False) -> InlineKeyboardMarkup:
    """
    Builds interactive inline buttons.
    - If is_admin=False (e.g., Anuj Sharma):
        Strictly personal buttons only. Never displays Himanshu's account or All Accounts Overview.
    - If is_admin=True (Himanshu):
        Full admin switcher tabs: Mine (Himanshu), Anuj Sharma, All Accounts Overview.
    """
    if not is_admin:
        btn_front = InlineKeyboardButton("🏠 Front Page ✅" if view == "front" else "🏠 Front Page", callback_data=f"acc_front_{acc_idx}")
        btn_pos = InlineKeyboardButton("🟢 Live Trades ✅" if view == "pos" else "🟢 Live Trades", callback_data=f"acc_pos_{acc_idx}")
        btn_trades = InlineKeyboardButton("📜 Trade History ✅" if view == "trades" else "📜 Trade History", callback_data=f"acc_trades_{acc_idx}")
        btn_refresh = InlineKeyboardButton("🔄 Refresh", callback_data=f"acc_refresh_{acc_idx}_{view}")
        btn_web = InlineKeyboardButton("🌐 Open Web Dashboard", url="https://worker-production-e82be7c6.up.railway.app/anuj")
        keyboard = [
            [btn_front, btn_pos],
            [btn_trades, btn_refresh],
            [btn_web]
        ]
        return InlineKeyboardMarkup(keyboard)

    # Admin controls (Himanshu)
    mine_label = "👑 Mine (Himanshu) ✅" if acc_idx == 1 else "👑 Mine (Himanshu)"
    anuj_label = "👤 Anuj Sharma ✅" if acc_idx == 2 else "👤 Anuj Sharma"
    btn_mine = InlineKeyboardButton(mine_label, callback_data=f"acc_{view}_1")
    btn_anuj = InlineKeyboardButton(anuj_label, callback_data=f"acc_{view}_2")

    btn_front = InlineKeyboardButton("🏠 Front Page ✅" if view == "front" else "🏠 Front Page", callback_data=f"acc_front_{acc_idx}")
    btn_pos = InlineKeyboardButton("🟢 Live Trades ✅" if view == "pos" else "🟢 Live Trades", callback_data=f"acc_pos_{acc_idx}")
    btn_trades = InlineKeyboardButton("📜 Trade History ✅" if view == "trades" else "📜 Trade History", callback_data=f"acc_trades_{acc_idx}")
    btn_refresh = InlineKeyboardButton("🔄 Refresh", callback_data=f"acc_refresh_{acc_idx}_{view}")
    btn_all = InlineKeyboardButton("🌐 All Accounts Overview", callback_data="acc_all_summary")
    web_url = "https://worker-production-e82be7c6.up.railway.app/anuj" if acc_idx == 2 else "https://worker-production-e82be7c6.up.railway.app/"
    btn_web = InlineKeyboardButton("🌐 Open Web Dashboard", url=web_url)

    keyboard = [
        [btn_mine, btn_anuj],
        [btn_front, btn_pos],
        [btn_trades, btn_refresh],
        [btn_all, btn_web]
    ]
    return InlineKeyboardMarkup(keyboard)


async def build_front_page_card(acc, session, acc_idx: int = 1, is_admin: bool = False) -> str:
    """
    Builds the Front Page dashboard displaying:
    1. Account header & isolation badge
    2. Wallet & margin balance
    3. Live active positions (entry, mark, floating PnL in INR & %, SL/TP)
    4. Resting entry limit orders
    5. Performance scorecard & realized profits (Win Rate, Total PnL, Last closed trade)
    """
    from web_dashboard import fetch_live_market_price

    # 1. Balances & Margins
    bal_str = await acc.get_wallet_balance_summary(session)
    if not bal_str:
        bal_str = "💼 <b>Futures Wallet:</b> <i>Balance loading or broker syncing...</i>"

    # 2. Live Positions
    positions = await acc.get_open_positions(session)
    pos_lines = []
    tot_unrealized_inr = 0.0
    for p in positions:
        sym = acc.clean_sym(p)
        pos_dir = str(p.get("positionType") or p.get("positionSide") or p.get("side") or "LONG").upper()
        side = "SHORT" if pos_dir in ("SHORT", "SELL") else "LONG"
        sz = abs(float(p.get("quantity") or p.get("positionAmount") or 0))
        entry_p = float(p.get("entryPrice") or p.get("avgPrice") or 0)
        mark_p = await fetch_live_market_price(session, sym)
        if mark_p <= 0:
            mark_p = float(p.get("markPrice") or p.get("lastPrice") or entry_p)
        sl_p = float(p.get("stopLossPrice") or p.get("stopLoss") or 0)
        tp_p = float(p.get("takeProfitPrice") or p.get("takeProfit") or 0)

        usd_rate = getattr(acc, "usd_inr_rate", 102.0)
        if side == "SHORT":
            upnl_usd = (entry_p - mark_p) * sz if entry_p > 0 else 0.0
            pct = ((entry_p - mark_p) / entry_p * 100.0 * 10) if entry_p > 0 else 0.0
        else:
            upnl_usd = (mark_p - entry_p) * sz if entry_p > 0 else 0.0
            pct = ((mark_p - entry_p) / entry_p * 100.0 * 10) if entry_p > 0 else 0.0
        upnl_inr = upnl_usd * usd_rate
        tot_unrealized_inr += upnl_inr
        pnl_sign = "+" if upnl_inr >= 0 else ""
        pct_sign = "+" if pct >= 0 else ""
        icon = "🟢" if upnl_inr >= 0 else "🔴"

        sl_tp_info = []
        if sl_p > 0:
            sl_tp_info.append(f"SL: <code>{sl_p}</code>")
        if tp_p > 0:
            sl_tp_info.append(f"TP: <code>{tp_p}</code>")
        sl_tp_str = f"\n     {' | '.join(sl_tp_info)}" if sl_tp_info else ""

        pos_lines.append(
            f"  {icon} <b>{sym}</b> [{side}] Qty: {sz}\n"
            f"     Entry: <code>{entry_p}</code> | Mark: <code>{mark_p}</code>"
            f"{sl_tp_str}\n"
            f"     Floating PnL: <b>{pnl_sign}₹{upnl_inr:,.2f}</b> ({pct_sign}{pct:.2f}%)"
        )
    pos_str = "\n".join(pos_lines) if pos_lines else "  • <i>No open active positions right now.</i>"

    # 3. Resting Orders
    open_orders = await acc.get_open_orders(session)
    ord_lines = []
    for o in open_orders:
        if not o.get("reduceOnly", False) and o.get("placeType") != "POSITION":
            osym = acc.clean_sym(o)
            oside = o.get("side", "BUY")
            opx = o.get("price", 0)
            oqty = float(o.get("orderAmount") or o.get("quantity") or o.get("origQty") or 0)
            ord_lines.append(f"  • <b>{osym}</b> [{oside}] Limit @ <code>{opx}</code> (Qty: {oqty})")
    ord_str = "\n".join(ord_lines[:6]) if ord_lines else "  • <i>No resting entry limit orders.</i>"

    # 4. Closed Trades Ledger & Scorecard / Realized Profits
    st_cl, closed_raw = await acc.shark_get(session, "/v1/positions/CLOSED")
    c_wins = 0
    c_losses = 0
    c_be = 0
    gross_wins = 0.0
    gross_losses = 0.0
    be_friction = 0.0
    last_trade_str = "None yet"

    if st_cl == 200 and isinstance(closed_raw, list) and closed_raw:
        for idx, cp in enumerate(closed_raw):
            pnl_inr = float(cp.get("realizedProfitInMarginAsset") or 0.0)
            if abs(pnl_inr) <= 15.0:
                c_be += 1
                be_friction += pnl_inr
            elif pnl_inr > 15.0:
                c_wins += 1
                gross_wins += pnl_inr
            else:
                c_losses += 1
                gross_losses += abs(pnl_inr)

            if idx == 0:
                csym = acc.clean_sym(cp)
                cpos_t = str(cp.get("positionType") or "SHORT").upper()
                csig = "BUY" if cpos_t in ("LONG", "BUY") else "SELL"
                cstat = "WIN 🎯" if pnl_inr > 15 else ("LOSS 🔴" if pnl_inr < -15 else "BE ⚖️")
                csign = "+" if pnl_inr >= 0 else ""
                last_trade_str = f"<b>{csym}</b> [{csig}] <code>{csign}₹{pnl_inr:,.2f}</code> ({cstat})"

    total_closed = c_wins + c_losses + c_be
    decisive = c_wins + c_losses
    win_rate = (c_wins / decisive * 100.0) if decisive > 0 else 0.0
    net_realized_pnl = gross_wins - gross_losses + be_friction
    rpnl_sign = "+" if net_realized_pnl >= 0 else ""

    # Header and tag
    if not is_admin:
        tag = "👤 <b>ANUJ SHARMA (PERSONAL PORTFOLIO)</b>"
        footer = "<i>Strict Personal Isolation • Direct Broker Link</i>"
    else:
        if acc_idx == 2:
            tag = "👤 <b>ANUJ SHARMA (ACCOUNT 2)</b>"
            footer = "<i>Viewing as Admin • Multi-Account Managed</i>"
        else:
            tag = "👑 <b>MINE (HIMANSHU - ADMIN MASTER)</b>"
            footer = "<i>Admin Master Controller Active</i>"

    card = (
        f"🦈 <b>SHARK EXCHANGE — DASHBOARD FRONT PAGE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Account: {tag}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{bal_str}\n\n"
        f"🟢 <b>Live Active Positions ({len(positions)}):</b>\n"
        f"{pos_str}\n\n"
        f"⏳ <b>Resting Entry Orders ({len(ord_lines)}):</b>\n"
        f"{ord_str}\n\n"
        f"💰 <b>Profits & Performance Scorecard:</b>\n"
        f"  • Scorecard: <b>{c_wins}W</b> - <b>{c_losses}L</b> - <b>{c_be}BE</b>\n"
        f"  • Win Rate: <b>{win_rate:.1f}%</b> ({total_closed} Total Closed)\n"
        f"  • Net Realized Profit: <b>{rpnl_sign}₹{net_realized_pnl:,.2f} INR</b>\n"
        f"  • Last Closed Trade: {last_trade_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{footer}"
    )
    return card


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responds to /start by loading the permanent docked menu and Front Page Dashboard."""
    msg = update.message
    if not msg:
        return
    chat_id = str(update.effective_chat.id)
    user = msg.from_user
    name = f"{user.first_name or ''} {user.last_name or ''}".strip() if user else "Trader"
    acc_idx, is_admin = resolve_user_account_context(chat_id)
    logger.info(f"📩 /start received from {name} (Chat ID: {chat_id}, acc_idx: {acc_idx}, is_admin: {is_admin})")

    # Send docked persistent keyboard first
    reply_kb = get_permanent_reply_keyboard(is_admin=is_admin)
    welcome_text = (
        f"👋 <b>Welcome {name}!</b>\n"
        f"⚡ <i>Permanent Navigation Menu is docked below ⬇️</i>\n"
        f"📋 <b>Your Chat ID:</b> <code>{chat_id}</code>"
    )
    await msg.reply_text(welcome_text, reply_markup=reply_kb, parse_mode="HTML")

    from executor import executor
    import aiohttp
    acc = executor.get_account(str(acc_idx)) or executor.primary
    async with aiohttp.ClientSession() as s:
        card = await build_front_page_card(acc, s, acc_idx=acc_idx, is_admin=is_admin)
        kb = get_account_keyboard(acc_idx=acc_idx, view="front", is_admin=is_admin)
        await msg.reply_text(card, reply_markup=kb, parse_mode="HTML")


async def cmd_getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports the channel ID of any forwarded message or current chat."""
    msg = update.message
    if not msg:
        return
    
    logger.info(f"📩 /id received from {msg.chat_id}")
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
        await msg.reply_text(
            f"📋 <b>Current Chat ID:</b> <code>{msg.chat_id}</code>\n\n"
            f"<i>To get a channel ID, forward any message FROM that channel TO this bot, then type /getid</i>",
            parse_mode="HTML"
        )


async def build_account_positions_card(acc, session, acc_idx: int = 1, is_admin: bool = False) -> str:
    from web_dashboard import fetch_live_market_price
    bal_str = await acc.get_wallet_balance_summary(session)
    if not bal_str:
        bal_str = "💼 <b>Futures Wallet:</b> <i>Balance loading or broker syncing...</i>"
    positions = await acc.get_open_positions(session)
    open_orders = await acc.get_open_orders(session)
    
    pos_lines = []
    for p in positions:
        sym = acc.clean_sym(p)
        pos_dir = str(p.get("positionType") or p.get("positionSide") or p.get("side") or "LONG").upper()
        side = "SHORT" if pos_dir in ("SHORT", "SELL") else "LONG"
        sz = abs(float(p.get("quantity") or p.get("positionAmount") or 0))
        entry_p = float(p.get("entryPrice") or p.get("avgPrice") or 0)
        mark_p = await fetch_live_market_price(session, sym)
        if mark_p <= 0:
            mark_p = float(p.get("markPrice") or p.get("lastPrice") or entry_p)
        sl_p = float(p.get("stopLossPrice") or p.get("stopLoss") or 0)
        tp_p = float(p.get("takeProfitPrice") or p.get("takeProfit") or 0)

        usd_rate = getattr(acc, "usd_inr_rate", 102.0)
        if side == "SHORT":
            upnl_usd = (entry_p - mark_p) * sz if entry_p > 0 else 0.0
            pct = ((entry_p - mark_p) / entry_p * 100.0 * 10) if entry_p > 0 else 0.0
        else:
            upnl_usd = (mark_p - entry_p) * sz if entry_p > 0 else 0.0
            pct = ((mark_p - entry_p) / entry_p * 100.0 * 10) if entry_p > 0 else 0.0
        upnl_inr = upnl_usd * usd_rate
        pnl_sign = "+" if upnl_inr >= 0 else ""
        pct_sign = "+" if pct >= 0 else ""
        icon = "🟢" if upnl_inr >= 0 else "🔴"

        sl_tp_info = []
        if sl_p > 0:
            sl_tp_info.append(f"SL: <code>{sl_p}</code>")
        if tp_p > 0:
            sl_tp_info.append(f"TP: <code>{tp_p}</code>")
        sl_tp_str = f"\n     {' | '.join(sl_tp_info)}" if sl_tp_info else ""

        pos_lines.append(
            f"  {icon} <b>{sym}</b> [{side}] Qty: {sz}\n"
            f"     Entry: <code>{entry_p}</code> | Mark: <code>{mark_p}</code>"
            f"{sl_tp_str}\n"
            f"     Floating PnL: <b>{pnl_sign}₹{upnl_inr:,.2f}</b> ({pct_sign}{pct:.2f}%)"
        )
        
    pos_str = "\n".join(pos_lines) if pos_lines else "  • <i>No open active positions right now.</i>"
    
    ord_lines = []
    for o in open_orders:
        if not o.get("reduceOnly", False) and o.get("placeType") != "POSITION":
            osym = acc.clean_sym(o)
            oside = o.get("side", "BUY")
            opx = o.get("price", 0)
            oqty = float(o.get("orderAmount") or o.get("quantity") or o.get("origQty") or 0)
            ord_lines.append(f"  • <b>{osym}</b> [{oside}] Limit @ <code>{opx}</code> (Qty: {oqty})")
    ord_str = "\n".join(ord_lines[:6]) if ord_lines else "  • <i>No resting entry limit orders.</i>"
    
    if not is_admin:
        tag = "👤 <b>ANUJ SHARMA (PERSONAL PORTFOLIO)</b>"
        footer = "<i>Status: Strict Personal Isolation Active (Direct Broker Link)</i>"
    else:
        tag = "👑 <b>MINE (HIMANSHU - ADMIN)</b>" if acc_idx == 1 else "👤 <b>ANUJ SHARMA (ACCOUNT 2)</b>"
        footer = "<i>Status: Multi-Account Management Active</i>"
    
    card = (
        f"🦈 <b>SHARK EXCHANGE — ISOLATED POSITIONS VIEW</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Account: {tag}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{bal_str}\n\n"
        f"🟢 <b>Live Active Positions ({len(positions)}):</b>\n"
        f"{pos_str}\n\n"
        f"⏳ <b>Resting Entry Orders ({len(ord_lines)}):</b>\n"
        f"{ord_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{footer}"
    )
    return card


async def build_account_trades_card(acc, session, acc_idx: int = 1, is_admin: bool = False) -> str:
    st_cl, closed_raw = await acc.shark_get(session, "/v1/positions/CLOSED")
    if not is_admin:
        tag = "👤 <b>ANUJ SHARMA (PERSONAL PORTFOLIO)</b>"
        footer = "<i>Strictly filtered to Anuj Sharma — No other account's trades mixed here.</i>"
    else:
        tag = "👑 <b>MINE (HIMANSHU - ADMIN)</b>" if acc_idx == 1 else "👤 <b>ANUJ SHARMA (ACCOUNT 2)</b>"
        footer = f"<i>Filtered to {acc.name} — Admin View</i>"
    
    if st_cl != 200 or not isinstance(closed_raw, list):
        return (
            f"📜 <b>TRADE HISTORY LEDGER</b>\n"
            f"Account: {tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Unable to query closed trades from Shark Exchange API (Status: {st_cl}).</i>"
        )
        
    if not closed_raw:
        return (
            f"📜 <b>TRADE HISTORY LEDGER</b>\n"
            f"Account: {tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>No closed trades recorded yet for this account. Active positions will appear here once closed.</i>"
        )
        
    c_wins = 0
    c_losses = 0
    c_be = 0
    gross_wins = 0.0
    gross_losses = 0.0
    be_friction = 0.0
    
    trade_items = []
    for cp in closed_raw:
        sym = acc.clean_sym(cp)
        pos_t = str(cp.get("positionType") or "SHORT").upper()
        sig = "BUY" if pos_t in ("LONG", "BUY") else "SELL"
        pnl_inr = float(cp.get("realizedProfitInMarginAsset") or 0.0)
        up_at = str(cp.get("updatedAt") or cp.get("createdAt") or "")
        
        ts_clean = up_at[:16].replace("T", " ") if up_at else "Recent"
        
        if abs(pnl_inr) <= 15.0:
            c_be += 1
            be_friction += pnl_inr
            status_txt = "BE"
            icon = "⚖️"
        elif pnl_inr > 15.0:
            c_wins += 1
            gross_wins += pnl_inr
            status_txt = "WIN"
            icon = "🎯"
        else:
            c_losses += 1
            gross_losses += abs(pnl_inr)
            status_txt = "LOSS"
            icon = "🔴"
            
        sign = "+" if pnl_inr >= 0 else ""
        trade_items.append(f"{icon} <b>{sym}</b> [{sig}] <code>{sign}₹{pnl_inr:,.2f}</code> ({status_txt}) — <i>{ts_clean}</i>")
        
    total_exec = c_wins + c_losses + c_be
    decisive = c_wins + c_losses
    win_rate = (c_wins / decisive * 100.0) if decisive > 0 else 0.0
    net_pnl = gross_wins - gross_losses + be_friction
    pnl_sign = "+" if net_pnl >= 0 else ""
    
    recent_display = "\n".join(trade_items[:18])
    more_count = max(0, total_exec - 18)
    more_txt = f"\n<i>...plus {more_count} earlier closed trades recorded in broker state.</i>" if more_count > 0 else ""
    
    card = (
        f"📜 <b>ALL CLOSED TRADES (PURANE + NEW)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Account: {tag}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Scorecard:</b> <b>{c_wins}W</b> - <b>{c_losses}L</b> - <b>{c_be}BE</b>\n"
        f"📈 <b>Win Rate:</b> <b>{win_rate:.1f}%</b> ({total_exec} Total Closed Setups)\n"
        f"💰 <b>Net Realized PnL:</b> <b>{pnl_sign}₹{net_pnl:,.2f} INR</b>\n"
        f"   └ Gross Wins: +₹{gross_wins:,.2f} | Losses: -₹{gross_losses:,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Closed Trades Ledger (Most Recent):</b>\n"
        f"{recent_display}{more_txt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{footer}"
    )
    return card


async def build_all_accounts_summary_card(session) -> str:
    from executor import executor
    accs = executor.get_all_configured_accounts()
    lines = []
    lines.append("🌐 <b>MULTI-ACCOUNT PORTFOLIO OVERVIEW</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    for i, acc in enumerate(accs, 1):
        tag = "👑 Mine (Himanshu)" if i == 1 else f"👤 {acc.name}"
        bal_str = await acc.get_wallet_balance_summary(session)
        pos = await acc.get_open_positions(session)
        orders = await acc.get_open_orders(session)
        resting = [o for o in orders if not o.get("reduceOnly") and o.get("placeType") != "POSITION"]
        
        st_cl, closed_raw = await acc.shark_get(session, "/v1/positions/CLOSED")
        c_count = len(closed_raw) if st_cl == 200 and isinstance(closed_raw, list) else 0
        
        lines.append(f"<b>[{i}] {tag}</b>")
        lines.append(f"  • Active Positions: <b>{len(pos)}</b> | Resting Limits: <b>{len(resting)}</b>")
        lines.append(f"  • Total Closed Trades: <b>{c_count}</b>")
        if bal_str:
            lines.append(f"  {bal_str}")
        lines.append("──────────────────────────")
        
    lines.append("<i>Use buttons below to inspect individual account trades & positions.</i>")
    return "\n".join(lines)


async def on_account_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    
    chat_id = str(update.effective_chat.id)
    acc_idx, is_admin = resolve_user_account_context(chat_id)
    
    from executor import executor
    import aiohttp
    
    async with aiohttp.ClientSession() as s:
        # Non-admin users (e.g., Anuj): strictly locked to Account 2
        if not is_admin:
            acc_idx = 2
            acc = executor.get_account("2") or executor.primary
            if data.startswith("acc_pos_"):
                view = "pos"
                card = await build_account_positions_card(acc, s, acc_idx=2, is_admin=False)
            elif data.startswith("acc_trades_"):
                view = "trades"
                card = await build_account_trades_card(acc, s, acc_idx=2, is_admin=False)
            else:
                view = "front"
                card = await build_front_page_card(acc, s, acc_idx=2, is_admin=False)

            kb = get_account_keyboard(acc_idx=2, view=view, is_admin=False)
            try:
                await query.edit_message_text(card, reply_markup=kb, parse_mode="HTML")
            except Exception as e_edit:
                logger.debug(f"Edit message skipped: {e_edit}")
            return

        # Admin (Himanshu)
        if data == "acc_all_summary":
            card = await build_all_accounts_summary_card(s)
            kb = get_account_keyboard(acc_idx=1, view="pos", is_admin=True)
            try:
                await query.edit_message_text(card, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            return
            
        parts = data.split("_")
        # formats: acc_pos_1, acc_trades_2, acc_front_1, acc_refresh_1_front, acc_refresh_2_pos
        view = "front"
        acc_idx = 1
        if len(parts) >= 3:
            action = parts[1]
            if action == "refresh":
                acc_idx = int(parts[2])
                view = parts[3] if len(parts) > 3 else "front"
            elif action in ("pos", "trades", "front"):
                view = action
                acc_idx = int(parts[2])
                
        acc = executor.get_account(str(acc_idx)) or executor.primary
        if view == "trades":
            card = await build_account_trades_card(acc, s, acc_idx=acc_idx, is_admin=True)
        elif view == "pos":
            card = await build_account_positions_card(acc, s, acc_idx=acc_idx, is_admin=True)
        else:
            card = await build_front_page_card(acc, s, acc_idx=acc_idx, is_admin=True)
            
        kb = get_account_keyboard(acc_idx=acc_idx, view=view, is_admin=True)
        try:
            await query.edit_message_text(card, reply_markup=kb, parse_mode="HTML")
        except Exception as e_edit:
            logger.debug(f"Edit message skipped: {e_edit}")


async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends interactive account switcher & filter menu."""
    msg = update.message
    if not msg:
        return
    chat_id = str(update.effective_chat.id)
    acc_idx, is_admin = resolve_user_account_context(chat_id)
    if not is_admin:
        await cmd_start(update, context)
        return

    import aiohttp
    from executor import executor
    async with aiohttp.ClientSession() as s:
        card = await build_all_accounts_summary_card(s)
        kb = get_account_keyboard(acc_idx=1, view="pos", is_admin=True)
        await msg.reply_text(card, reply_markup=kb, parse_mode="HTML")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Syncs missing active trades from Admin to secondary accounts."""
    msg = update.message
    if not msg:
        return
    chat_id = str(update.effective_chat.id)
    acc_idx, is_admin = resolve_user_account_context(chat_id)
    if not is_admin:
        await msg.reply_text("⛔ Admin permission required.")
        return

    import aiohttp
    from executor import executor
    from web_dashboard import fetch_live_market_price
    
    target_idx = 2
    target_acc = executor.get_account(str(target_idx))
    if not target_acc or not target_acc.is_configured():
        await msg.reply_text("❌ Secondary account (Anuj Sharma) not configured or disabled.")
        return
        
    await msg.reply_text("🔄 <i>Checking open positions across accounts and synchronizing...</i>", parse_mode="HTML")
    async with aiohttp.ClientSession() as s:
        primary_pos = await executor.primary.get_open_positions(s)
        target_pos = await target_acc.get_open_positions(s)
        target_orders = await target_acc.get_open_orders(s)
        
        target_syms = set()
        for p in target_pos:
            target_syms.add(target_acc.clean_sym(p))
        for o in target_orders:
            target_syms.add(target_acc.clean_sym(o))
            
        synced = []
        for p in primary_pos:
            sym = executor.primary.clean_sym(p)
            if sym in target_syms:
                continue
            qty = abs(float(p.get("quantity") or p.get("positionAmount") or 0))
            if qty <= 0:
                continue
            pos_dir_raw = str(p.get("positionType") or p.get("positionSide") or p.get("side") or "").upper()
            sig = "SELL" if pos_dir_raw in ("SHORT", "SELL") else "BUY"
            entry = float(p.get("entryPrice") or p.get("price") or 0)
            sl = float(p.get("stopLossPrice") or p.get("stopLoss") or 0)
            tp1 = float(p.get("takeProfitPrice") or p.get("takeProfit") or 0)
            tp2 = float(p.get("takeProfitPrice2") or 0)
            curr_p = await fetch_live_market_price(s, sym)
            use_entry = curr_p if curr_p > 0 else entry
            if sl == 0:
                sl = use_entry * 1.022 if sig == "SELL" else use_entry * 0.978
            if tp1 == 0:
                tp1 = use_entry * 0.96 if sig == "SELL" else use_entry * 1.04
            if tp2 == 0:
                tp2 = use_entry * 0.93 if sig == "SELL" else use_entry * 1.07
            res = await target_acc.execute_signal_trade(s, sym, sig, use_entry, sl, tp1, tp2, aligned_count=4)
            synced.append(f"• <b>{sym}</b> [{sig}]: {res.get('status')} ({res.get('reason', 'OK')})")
            
        if synced:
            await msg.reply_text(f"✅ <b>SYNC COMPLETED for {target_acc.name}!</b>\n\n" + "\n".join(synced), parse_mode="HTML")
        else:
            await msg.reply_text(f"ℹ️ All active positions are already in sync on {target_acc.name}!", parse_mode="HTML")


async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports all closed trades (purane + naye sab) for specified or primary account."""
    msg = update.message
    if not msg:
        return
    chat_id = str(update.effective_chat.id)
    acc_idx, is_admin = resolve_user_account_context(chat_id)

    if is_admin and context.args:
        arg = context.args[0].lower()
        acc_idx = 2 if arg in ("2", "anuj", "sharma") else 1
    elif not is_admin:
        acc_idx = 2

    from executor import executor
    import aiohttp
    acc = executor.get_account(str(acc_idx)) or executor.primary
    async with aiohttp.ClientSession() as s:
        card = await build_account_trades_card(acc, s, acc_idx=acc_idx, is_admin=is_admin)
        kb = get_account_keyboard(acc_idx=acc_idx, view="trades", is_admin=is_admin)
        await msg.reply_text(card, reply_markup=kb, parse_mode="HTML")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports real-time Shark Live Positions & Margins."""
    msg = update.message
    if not msg:
        return
    chat_id = str(update.effective_chat.id)
    acc_idx, is_admin = resolve_user_account_context(chat_id)

    try:
        from executor import executor
        import aiohttp

        if is_admin and context.args:
            arg = context.args[0].lower()
            if arg in ("all", "overview", "summary"):
                async with aiohttp.ClientSession() as s:
                    card = await build_all_accounts_summary_card(s)
                    kb = get_account_keyboard(acc_idx=1, view="pos", is_admin=True)
                    await msg.reply_text(card, reply_markup=kb, parse_mode="HTML")
                    return
            acc_idx = 2 if arg in ("2", "anuj", "sharma") else 1
        elif not is_admin:
            acc_idx = 2

        acc = executor.get_account(str(acc_idx)) or executor.primary
        async with aiohttp.ClientSession() as s:
            card = await build_account_positions_card(acc, s, acc_idx=acc_idx, is_admin=is_admin)
            kb = get_account_keyboard(acc_idx=acc_idx, view="pos", is_admin=is_admin)
            await msg.reply_text(card, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"cmd_positions error: {e}")
        await msg.reply_text(f"⚠️ Error querying positions: {e}")


async def on_user_quick_button_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on the persistent docked keyboard buttons."""
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if text.startswith("/"):
        return

    chat_id = str(update.effective_chat.id)
    acc_idx, is_admin = resolve_user_account_context(chat_id)
    t_lower = text.lower()

    if "front" in t_lower or "home" in t_lower or "dashboard" in t_lower or (t_lower == "🔄 refresh"):
        from executor import executor
        import aiohttp
        acc = executor.get_account(str(acc_idx)) or executor.primary
        async with aiohttp.ClientSession() as s:
            card = await build_front_page_card(acc, s, acc_idx=acc_idx, is_admin=is_admin)
            kb = get_account_keyboard(acc_idx=acc_idx, view="front", is_admin=is_admin)
            await msg.reply_text(card, reply_markup=kb, parse_mode="HTML")
    elif "live" in t_lower or "position" in t_lower or "active" in t_lower:
        await cmd_positions(update, context)
    elif "profit" in t_lower or "scorecard" in t_lower or "stat" in t_lower:
        await cmd_trades(update, context)
    elif "radar" in t_lower or "coiling" in t_lower or "squeeze" in t_lower:
        await cmd_radar(update, context)
    elif "trade" in t_lower or "history" in t_lower or "ledger" in t_lower or "closed" in t_lower:
        await cmd_trades(update, context)
    elif "filter" in t_lower or "setting" in t_lower:
        await msg.reply_text(
            "⚙️ <b>Signal Filter & Alert Settings:</b>\n\n"
            "Open @Alpha_Crypto_SignalBot to customize your coin watchlists, setups (Crossover/Retest), and star quality!",
            parse_mode="HTML"
        )
    elif "web" in t_lower or "site" in t_lower or "url" in t_lower or "portfolio" in t_lower:
        await cmd_web(update, context)
    elif "all account" in t_lower or "overview" in t_lower:
        if is_admin:
            await cmd_accounts(update, context)
        else:
            await cmd_start(update, context)


async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends personal live web dashboard link."""
    msg = update.message
    if not msg:
        return
    chat_id = str(update.effective_chat.id)
    acc_idx, is_admin = resolve_user_account_context(chat_id)
    url = "https://worker-production-e82be7c6.up.railway.app/anuj" if not is_admin else ("https://worker-production-e82be7c6.up.railway.app/anuj" if acc_idx == 2 else "https://worker-production-e82be7c6.up.railway.app/")
    title = "ANUJ SHARMA — Personal Live Dashboard" if (not is_admin or acc_idx == 2) else "HIMANSHU — AlphaSniper Master Dashboard"
    await msg.reply_text(
        f"🌐 <b>{title}</b>\n\n"
        f"🔗 <a href='{url}'>{url}</a>\n\n"
        f"⚡ <i>Direct browser link with real-time live positions, margin balance, resting limit orders, and realized trade ledger!</i>",
        parse_mode="HTML"
    )


async def cmd_radar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports all active Pre-Squeeze Radars currently on Watchlist."""
    msg = update.message
    if not msg:
        return
    from state import get_all_active_radars
    radars = get_all_active_radars()
    if not radars:
        await msg.reply_text(
            "📡 <b>PRE-SQUEEZE RADAR (Coiling Watchlist)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>No active coiling setups on the radar right now.</i>\n\n"
            "Scanner evaluates 1H Binance candles every scan loop.\n"
            "As soon as a tight squeeze coil is verified, it will alert here and track for breakout!",
            parse_mode="HTML"
        )
        return

    lines = [
        f"📡 <b>ACTIVE PRE-SQUEEZE RADARS ({len(radars)})</b>",
        "<i>Continuous Breakout & Invalidation Monitoring Active</i>",
        "━━━━━━━━━━━━━━━━━━━━━━"
    ]
    for sym, r in radars.items():
        dir_t = r.get("direction", "BUY")
        icon = "🟢 LONG" if dir_t == "BUY" else "🔴 SHORT"
        sq_type = r.get("squeeze_type", "SQUEEZE")
        trig = float(r.get("trigger_entry", 0.0))
        sl = float(r.get("invalidation_sl", 0.0))
        comp = float(r.get("compression_pct", 0.0))
        score = float(r.get("coiling_score", 0.0))
        tf_l = r.get("tf_label", "1H")
        lines.append(
            f"• <b>{sym}</b> ({tf_l}) [{icon}]\n"
            f"   Setup: {sq_type}\n"
            f"   🎯 Breakout Trigger: <code>{format_price(trig, sym)}</code>\n"
            f"   🛑 Invalidation SL: <code>{format_price(sl, sym)}</code>\n"
            f"   📊 Compression: <code>{comp:.2f}%</code> | Score: <b>{score:.1f}</b>"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 <i>Breakout order executes automatically on 1H candle confirmation, OR failure alert is dispatched!</i>")
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


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

        if data == "act_view_banned":
            text, kb = admin_panel.build_banned_users_overview()
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
        _app_instance.add_handler(CommandHandler(["start", "help", "menu", "front", "dashboard"], cmd_start))
        _app_instance.add_handler(CommandHandler(["accounts", "account", "users", "filter"], cmd_accounts))
        _app_instance.add_handler(CommandHandler(["trades", "trade", "history", "closed", "ledger"], cmd_trades))
        _app_instance.add_handler(CommandHandler(["sync", "syncaccounts", "copysync"], cmd_sync))
        _app_instance.add_handler(CommandHandler(["setchannel", "linkchannel"], cmd_setchannel))
        _app_instance.add_handler(CommandHandler(["today", "report", "stats"], cmd_today))
        _app_instance.add_handler(CommandHandler(["getid", "channelid", "id"], cmd_getid))
        _app_instance.add_handler(CommandHandler(["positions", "paper", "wallet"], cmd_positions))
        _app_instance.add_handler(CommandHandler(["del", "delete", "purge"], cmd_delete))
        _app_instance.add_handler(CommandHandler(["stop", "kill", "halt", "pause"], cmd_stop_bot))
        _app_instance.add_handler(CommandHandler(["startbot", "resume", "resumebot"], cmd_resume_bot))
        _app_instance.add_handler(CommandHandler(["radar", "radars", "coiling", "squeezes"], cmd_radar))
        _app_instance.add_handler(CommandHandler(["web", "site", "dashboardlink", "link", "url", "portfolio"], cmd_web))

        # Account Filter Callback Handler (Seamless multi-account toggling)
        _app_instance.add_handler(CallbackQueryHandler(on_account_filter_callback, pattern=r"^acc_"))

        # Channel member management handlers
        _app_instance.add_handler(CommandHandler(["admin", "users", "channels", "channelusers", "channelmembers", "members"], cmd_channels))
        _app_instance.add_handler(MessageHandler(filters.Regex(r"^/(rm|ban|unban)_(scan|scanner|cross|alpha)_\\d+"), cmd_quick_channel_action))
        _app_instance.add_handler(CommandHandler(["kick", "kickchannel", "removechanneluser", "banchannel"], cmd_kick_or_ban))
        _app_instance.add_handler(MessageHandler(filters.Regex(r"^/(kick|ban)_(scanner|cross)_\d+"), cmd_quick_channel_action))
        _app_instance.add_handler(CallbackQueryHandler(on_channel_member_callback, pattern=r"^(act_|ch_)"))
        _app_instance.add_handler(ChatMemberHandler(on_channel_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

        _app_instance.add_handler(MessageHandler(filters.FORWARDED, on_forwarded_message))
        _app_instance.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_any_channel_message))
        
        # User persistent keyboard button taps (Private DM)
        _app_instance.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_user_quick_button_text))

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

    # Configure permanent Telegram [Menu] button and commands
    try:
        from telegram import BotCommand, MenuButtonCommands
        commands = [
            BotCommand("start", "🏠 Front Page Dashboard"),
            BotCommand("positions", "🟢 Live Active Trades"),
            BotCommand("trades", "📜 Closed Trades Ledger"),
            BotCommand("radar", "📡 Active Pre-Squeeze Radars"),
            BotCommand("web", "🌐 Personal Web Dashboard"),
            BotCommand("accounts", "💼 Account Overview"),
            BotCommand("today", "📊 Today's Signals Summary"),
            BotCommand("help", "ℹ️ Help & Status"),
        ]
        await app.bot.set_my_commands(commands)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Permanent Telegram Menu button successfully registered for @Ema_signals_alerts_bot")
    except Exception as e_cmd:
        logger.warning(f"Could not set permanent bot commands: {e_cmd}")

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
        f"⚠️ <i>[WATCHLIST RADAR ONLY — NO AUTO-ORDER SUBMITTED YET]</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>Pair:</b> <code>{display_sym}</code> ({sq_tf} Base)\n"
        f"⚡ <b>Setup:</b> {status_icon} <b>{direction_tag}</b>\n"
        f"🎯 <b>Planned Trade:</b> <b>{action_plan}</b>\n\n"
        f"🧲 <b>Market Dynamic:</b>\n"
        f"<i>{mechanism}</i>\n\n"
        f"📋 <b>Institutional Coiling Checklist:</b>\n"
        f"✅ <b>[1] Active Market:</b> Liquid (24h Vol {vol_str} &gt; $2.5M)\n"
        f"✅ <b>[2] Range Compression:</b> <code>{compression_pct:.2f}%</code> (Tight Coiling)\n"
        f"✅ <b>[3] Volatility Pinch:</b> {ttm_tag}\n"
        f"✅ <b>[4] Smart Money OI:</b> <code>{oi_sign}{oi_change_1h:.1f}%</code> (1H Accumulation)\n"
        f"✅ <b>[5] Leverage Bias:</b> <code>{funding_pct:+.4f}%</code> ({funding_tag})\n"
        f"✅ <b>[6] Trend Alignment:</b> {i_1h} 1H {h1_st} | {i_4h} 4H {h4_st}\n\n"
        f"🎯 <b>Action Trigger Levels:</b>\n"
        f"{'🟢' if direction == 'BUY' else '🔴'} <b>{'Breakout' if direction == 'BUY' else 'Breakdown'} Trigger:</b> <code>{format_price(trigger_entry, symbol)}</code>\n"
        f"🛑 <b>Base Invalidation (SL):</b> <code>{format_price(invalidation_sl, symbol)}</code>\n\n"
        f"💡 <b>Execution Strategy:</b>\n"
        f"⚠️ <b>Status:</b> Watching only (Stage 1). No order placed on Shark Exchange yet.\n"
        f"Trade order will execute automatically as soon as the 1H candle confirms a clean breakout beyond <code>{format_price(trigger_entry, symbol)}</code> with whale CVD buying!\n"
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


async def send_radar_status_update(radar_data: dict, status: str, reason: str, candle_close: float = None):
    """
    Broadcasts a real-time status update when an active Pre-Squeeze Radar setup:
    1. FAILS (Fakeout wick rejection / lack of volume / CVD selling into breakout)
    2. INVALIDATES (Price breaches the invalidation SL structure level)
    3. EXPIRES (4-hour coiling window elapsed without breakout)

    Ensures complete transparency per user directive:
    Either the order executes on breakout, OR a clear diagnostic message is dispatched!
    """
    symbol = radar_data.get("symbol", "")
    direction = str(radar_data.get("direction", "BUY")).upper()
    trigger_entry = float(radar_data.get("trigger_entry", 0.0))
    invalidation_sl = float(radar_data.get("invalidation_sl", 0.0))
    sq_tf = radar_data.get("tf_label", "1H")
    display_sym = format_display_symbol(symbol)

    direction_tag = "SHORT SQUEEZE" if direction == "BUY" else "LONG SQUEEZE"

    if status == "FAILED":
        header = "⚠️ <b>PRE-SQUEEZE RADAR: BREAKOUT FAILED (Rejection)</b>"
        icon = "❌"
        action_note = "<b>Capital Protection Guard:</b>\nBreakout conditions rejected. No order placed on Shark Exchange. Safe!"
        levels_str = (
            f"🎯 <b>Breakout Trigger:</b> <code>{format_price(trigger_entry, symbol)}</code>\n"
            f"📉 <b>Candle Close:</b> <code>{format_price(candle_close or trigger_entry, symbol)}</code>\n"
        )
    elif status == "INVALIDATED":
        header = "🛑 <b>PRE-SQUEEZE RADAR: SETUP INVALIDATED</b>"
        icon = "🛑"
        action_note = "<b>Watchlist Action:</b>\nCoiling structure collapsed. Radar removed from watchlist. Zero trade taken!"
        levels_str = (
            f"🛑 <b>Invalidation Level:</b> <code>{format_price(invalidation_sl, symbol)}</code>\n"
            f"📉 <b>Breached Price:</b> <code>{format_price(candle_close or invalidation_sl, symbol)}</code>\n"
        )
    else:  # EXPIRED
        header = "⏱️ <b>PRE-SQUEEZE RADAR: TIMED OUT / EXPIRED</b>"
        icon = "⏳"
        action_note = "<b>Watchlist Action:</b>\nConsolidation lost momentum after 4 hours without breakout. Symbol removed from active radar."
        levels_str = (
            f"🎯 <b>Untriggered Level:</b> <code>{format_price(trigger_entry, symbol)}</code>\n"
        )

    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>Pair:</b> <code>{display_sym}</code> ({sq_tf})\n"
        f"⚡ <b>Setup:</b> {direction_tag}\n"
        f"{levels_str}\n"
        f"{icon} <b>Diagnostic:</b>\n"
        f"<i>{reason}</i>\n\n"
        f"🛡️ {action_note}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

    EMA_SCANNER_CHANNEL = "-1004332146822"
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593")

    # Send to Scanner Channel
    try:
        await send_text_to_chat(EMA_SCANNER_CHANNEL, text)
    except Exception as e:
        logger.error(f"Failed to dispatch radar status update to {EMA_SCANNER_CHANNEL}: {e}")

    # Always ensure Admin receives it directly
    try:
        if admin_id and admin_id != EMA_SCANNER_CHANNEL:
            await send_text_to_chat(admin_id, text)
    except Exception as e:
        logger.error(f"Failed to dispatch radar status update to Admin DM {admin_id}: {e}")

    # Dispatch to Filter Bot subscribers
    try:
        from filter_bot import dispatch_filtered_alert
        f_alert_data = {
            "symbol": symbol,
            "tf_label": sq_tf,
            "signal": direction,
            "is_squeeze": True,
            "is_dedicated_squeeze": True,
            "cross_type": f"Pre-Squeeze Radar ({status})",
            "aligned_count": 4
        }
        await dispatch_filtered_alert(f_alert_data, text)
    except Exception as e_f:
        logger.error(f"Error dispatching radar update to filter subscribers: {e_f}")


async def send_circuit_breaker_alert(paused_until_ts: float, streak_count: int, trigger_symbol: str = "") -> None:
    """
    Sends high-priority Telegram alert when 10 consecutive SLs occur in a day,
    pausing trading for 60 minutes.
    """
    try:
        import zoneinfo
        tz_ist = zoneinfo.ZoneInfo("Asia/Kolkata")
    except Exception:
        tz_ist = timezone(timedelta(hours=5, minutes=30))
    
    resume_dt = datetime.fromtimestamp(paused_until_ts, tz=tz_ist)
    resume_str = resume_dt.strftime("%I:%M %p IST")

    sym_txt = f"🪙 Trigger Coin: <b>{trigger_symbol}</b>\n" if trigger_symbol else ""
    msg = (
        f"🚨 <b>RISK CIRCUIT BREAKER TRIGGERED</b> 🚨\n\n"
        f"⚠️ <b>10 Consecutive Stop Losses Hit in a Day!</b>\n"
        f"{sym_txt}"
        f"🛑 Consecutive Loss Streak: <b>{streak_count} Trades</b>\n\n"
        f"⏸️ <b>TRADING IS PAUSED FOR 60 MINUTES</b>\n"
        f"⏰ Trading Resumes At: <code>{resume_str}</code>\n\n"
        f"🛡️ <i>Capital Preservation Shield: All new scanner entries, crossovers, and breakout limit orders are frozen for 60 minutes. Active positions remain protected by exchange TP/SL brackets.</i>"
    )
    try:
        await send_text_alert(msg)
    except Exception as e:
        logger.error(f"Failed to send circuit breaker alert: {e}")


async def send_circuit_breaker_resumed_alert() -> None:
    """Sends notification when 60-min circuit breaker cooldown completes."""
    msg = (
        f"✅ <b>CIRCUIT BREAKER COOLDOWN COMPLETE</b>\n\n"
        f"▶️ <b>Trading and scanning have officially RESUMED.</b>\n"
        f"🔍 Scanner is active 24/7 across all monitored pairs."
    )
    try:
        await send_text_alert(msg)
    except Exception as e:
        logger.error(f"Failed to send circuit breaker resumed alert: {e}")
