# -*- coding: utf-8 -*-
"""
executor.py — Automated Trade Execution & Order Bracket Engine for Shark Exchange (India).

Enforces Master Production Strategy:
1. Fixed 10x Leverage on all executed trades.
2. Dynamic 2.0% Available (Free) Margin Sizing per trade (Min ₹500 floor).
3. Single Target Bracket Structure (User Directive: 100% Full Profit Booking at TP1):
   - 1 Entry Limit Order at Entry Price (30-min fill timeout).
   - TP1 (100% Full Position Quantity) @ exact Target R (1.5R for retest/cross/reversal, 2.2R for breakdown/breakout).
   - 1 Stop-Loss STOP_LIMIT Order placed strictly with 26 EMA buffer (+0.25*ATR).
4. 100% Cash-In-Wallet Exit:
   - Positions close 100% at TP1 with zero runner retracement and zero trailing giveback.
   - 20-Hour Position Timeout: Squares off stagnant trades at live market price.
5. Strict Single Order Margin Shield:
   - Maximum 1 active order or open position per coin across exchange.
   - Rejects duplicate signal orders for the same coin to prevent multiple margin blocks.
6. Uncapped Concurrent Portfolio:
   - Driven dynamically by broker free margin availability (zero artificial trade count caps).
7. 15-Second Background Auto-Healing Loop:
   - Verifies bracket integrity and restores any missing SL/TP orders automatically.
8. Liquidation Shield:
   - SL mathematically clamped >= 2.5% inside exchange liquidation price.
"""


import os
import time
import hmac
import hashlib
import json
import logging
import asyncio
import aiohttp
from datetime import datetime, timezone
import random

from config import (
    SHARK_API_KEY,
    SHARK_API_SECRET,
    SHARK_BASE_URL,
    AUTO_TRADING_ENABLED,
    AUTO_TRADE_EXCLUDED_SYMBOLS,
    AUTO_TRADE_MARGIN_INR,
    AUTO_TRADE_LEVERAGE,
    AUTO_TRADE_RISK_PER_TRADE_PCT,
    AUTO_TRADE_MIN_MARGIN_INR,
    AUTO_TRADE_MAX_MARGIN_PCT,
    AUTO_TRADE_MAX_CONCURRENT,
    US_STOCK_TOKENS,
    TELEGRAM_CHAT_ID,
    clean_contract_symbol,
    to_shark_symbol,
    get_symbol_price_multiplier
)
from telegram_bot import format_price, send_text_alert, send_text_to_chat

logger = logging.getLogger("executor")

USD_INR_RATE = 102.0  # Shark Exchange native USDT/INR conversion rate


def is_us_stock_token(symbol: str) -> bool:
    """Identifies US stock tokens / synthetic equities using exact base-asset matching.
    Avoids substring collisions: SHIB≠SHI, ARB≠AR, METAM≠META, TRB≠TR, BNB≠BN etc.
    """
    clean = clean_contract_symbol(symbol).replace("USDT", "").replace("USD", "").replace("FDUSD", "").upper()
    us_set = set(US_STOCK_TOKENS)
    # Exact match (e.g. META, NVDA, MSTR, SPYB)
    if clean in us_set:
        return True
    # B-suffix synthetic strip: NVDAB→NVDA, TSLABUSDT already cleaned to TSLAB→TSLA
    if len(clean) > 2 and clean.endswith("B") and clean[:-1] in us_set:
        return True
    return False


def get_stop_limit_execution_price(symbol: str, side: str, stop_price: float) -> float:
    """
    Calculates guaranteed execution price with slippage buffer for STOP_LIMIT orders:
    - LONG (SELL exit): limit price is 0.20% BELOW trigger price so SELL order fills instantly on market dip.
    - SHORT (BUY exit): limit price is 0.20% ABOVE trigger price so BUY order fills instantly on market pump.
    Ensures STRICT LIMIT ORDER execution (NEVER submits MARKET or STOP_MARKET orders).
    """
    clean_sym = clean_contract_symbol(symbol)
    buffer_pct = 0.0035  # Upgraded to 0.35% execution buffer for guaranteed fill on fast altcoin wicks
    if side.upper() in ["SELL", "LONG"]:
        limit_price = stop_price * (1.0 - buffer_pct)
    else:
        limit_price = stop_price * (1.0 + buffer_pct)
    return round_price(clean_sym, limit_price)


def sanitize_payload(obj):
    """
    Normalizes floats for Node.js HMAC parity:
    Node.js JSON.stringify(10.0) outputs '10' instead of '10.0'.
    Converting any whole-number float to int ensures exact signature parity.
    """
    if isinstance(obj, dict):
        return {k: sanitize_payload(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_payload(v) for v in obj]
    elif isinstance(obj, float):
        if obj.is_integer():
            return int(obj)
        return round(obj, 6)
    return obj


def round_price(symbol: str, price: float):
    """
    Rounds price according to Shark Exchange precision rules:
    - BTC: max 1 decimal
    - ETH, SOL, BNB, XAU, CL: max 2 decimals
    - Mid-tier (NEAR, AVAX, LINK): max 3 decimals
    - High-value (>= 1000): max 1 decimal
    - Mid-value (>= 10): max 2 decimals
    - Low-cost altcoins: max 4 decimals
    """
    if price is None:
        return 0.0
    clean = clean_contract_symbol(symbol)
    try:
        p = float(price)
    except (ValueError, TypeError):
        return 0.0
    if 'BTC' in clean:
        rounded = round(p, 1)
    elif any(c in clean for c in ('ETH', 'SOL', 'BNB', 'XAU', 'CL', 'OIL', 'WTI', 'BRENT', 'LTC', 'BCH', 'AAVE', 'LINK', 'AVAX', 'DOT', 'NEAR', 'ATOM', 'ETC')):
        rounded = round(p, 2)
    elif any(c in clean for c in ('XAG', 'SILVER', 'COPPER', 'BRONZE', 'HG', 'NATGAS')):
        rounded = round(p, 3)
    elif p >= 1000:
        rounded = round(p, 1)
    elif p >= 10:
        rounded = round(p, 2)
    elif p >= 1.0:
        rounded = round(p, 3)
    elif p >= 0.01:
        rounded = round(p, 4)
    elif p >= 0.0001:
        rounded = round(p, 6)
    else:
        rounded = round(p, 9)

    if isinstance(rounded, float) and rounded.is_integer():
        return int(rounded)
    return rounded



def enforce_sl_above_liquidation(
    symbol: str,
    entry: float,
    sl: float,
    direction: str,
    leverage: float = 10.0,
    exchange_liq_price: float = None,
    is_trailed: bool = False
) -> float:
    """
    STRICT LIQUIDATION SHIELD:
    Guarantees that Stop-Loss is ALWAYS placed SAFELY INSIDE the liquidation threshold:
    - BUY (LONG): SL must be strictly ABOVE Liquidation Price (by at least 2.5% safety buffer).
    - SELL (SHORT): SL must be strictly BELOW Liquidation Price (by at least 2.5% safety buffer).

    Prevents positions from ever being liquidated before Stop-Loss triggers,
    and prevents exchanges from rejecting SL orders that are worse than liquidation price.
    """
    try:
        entry = float(entry)
        sl = float(sl)
        lev = max(float(leverage or 10.0), 1.0)
        is_buy = str(direction).upper() in ("BUY", "LONG")

        # Maintenance Margin Rate (~0.005 to 0.01)
        mmr = 0.008

        # Calculate theoretical liquidation price if exchange_liq_price not provided
        if exchange_liq_price and float(exchange_liq_price) > 0:
            liq_price = float(exchange_liq_price)
        else:
            if is_buy:
                liq_price = entry * (1.0 - (1.0 / lev) + mmr)
            else:
                liq_price = entry * (1.0 + (1.0 / lev) - mmr)

        # Max allowable adverse price move before SL must trigger (leaves at least 2.5% safety buffer before liquidation)
        max_adverse_pct = max(0.02, (1.0 / lev) - 0.025)

        if is_buy:
            # If SL is trailed into profit or breakeven, ensure strictly above or at entry
            if is_trailed:
                if liq_price > 0:
                    sl = max(sl, liq_price * 1.025)
                return round_price(symbol, max(sl, entry))

            # For BUY initial SL: SL must be strictly ABOVE liquidation price and strictly BELOW entry!
            safe_floor_from_liq = liq_price * 1.025
            safe_floor_from_entry = entry * (1.0 - max_adverse_pct)
            min_allowed_sl = max(safe_floor_from_liq, safe_floor_from_entry)

            if sl <= min_allowed_sl or sl <= liq_price:
                logger.warning(
                    f"🛡️ LIQUIDATION SHIELD CLAMP [BUY]: Clamping SL from {sl} to safe floor {min_allowed_sl} "
                    f"(Liq: {liq_price:.4f}, Entry: {entry:.4f}, Lev: {lev}x)"
                )
                sl = min_allowed_sl

            if sl >= entry:
                sl = entry * 0.990
        else:
            # If SL is trailed into profit or breakeven for SELL, ensure strictly below or at entry
            if is_trailed:
                if liq_price > 0:
                    sl = min(sl, liq_price * 0.975)
                return round_price(symbol, min(sl, entry))

            # For SELL initial SL: SL must be strictly BELOW liquidation price and strictly ABOVE entry!
            safe_ceil_from_liq = liq_price * 0.975
            safe_ceil_from_entry = entry * (1.0 + max_adverse_pct)
            max_allowed_sl = min(safe_ceil_from_liq, safe_ceil_from_entry)

            if sl >= max_allowed_sl or sl >= liq_price:
                logger.warning(
                    f"🛡️ LIQUIDATION SHIELD CLAMP [SELL]: Clamping SL from {sl} to safe ceiling {max_allowed_sl} "
                    f"(Liq: {liq_price:.4f}, Entry: {entry:.4f}, Lev: {lev}x)"
                )
                sl = max_allowed_sl

            if sl <= entry:
                sl = entry * 1.010

        return round_price(symbol, sl)
    except Exception as e:
        logger.error(f"Error in enforce_sl_above_liquidation for {symbol}: {e}")
        return round_price(symbol, sl)


# Shark Exchange strict minimum order quantities per asset (verified on live exchange)
SHARK_MIN_ORDER_QTY = {
    "BTC": 0.002,     # Shark min: 0.002 BTC (0.001 rejected with Error 3010)
    "ETH": 0.02,      # Shark min: 0.02 ETH (0.01 rejected with Error 3010)
    "SOL": 0.1,       # Shark min: 0.1 SOL (0.01/0.05 rejected with Error 3010)
    "XAU": 0.005,     # Shark min: 0.005 XAU (0.001 rejected with Error 3010)
    "PAXG": 0.005,    # Shark min: 0.005 PAXG
    "BNB": 0.01,      # Shark min: 0.01 BNB
    "XAG": 0.1,       # Shark min: 0.1 XAG (Silver)
    "LTC": 1.0,       # Shark min: 1.0 LTC (0.5 rejected with Error 3010)
}

def get_exchange_min_qty(symbol: str) -> float:
    """Returns the strict minimum allowable order quantity on Shark Exchange."""
    clean = clean_contract_symbol(symbol).upper()
    for base, min_q in SHARK_MIN_ORDER_QTY.items():
        if base in clean:
            return min_q
    if any(c in clean for c in ('AVAX', 'PENDLE', 'TIA', 'SEI', 'ONDO', 'RENDER', 'FET', 'ENA', 'EIGEN', 'RAY')):
        return 1.0
    return 0.1


def round_coin_qty(symbol: str, qty: float, price: float = 0.0) -> float:
    """Helper to round quantity according to symbol lot sizes on Shark Exchange."""
    if qty is None:
        return 0.0
    clean_sym = clean_contract_symbol(symbol).upper()
    try:
        f_qty = float(qty)
    except (ValueError, TypeError):
        return 0.0

    # Zero / negative qty: always return 0.0 so upstream guards (if qty > 0) work correctly
    if f_qty <= 0:
        return 0.0

    if 'BTC' in clean_sym:
        rounded = max(0.002, round(f_qty, 3))
    elif 'ETH' in clean_sym:
        rounded = max(0.02, round(f_qty, 3))
    elif any(c in clean_sym for c in ('XAU', 'PAXG')):
        rounded = max(0.005, round(f_qty, 3))
    elif any(c in clean_sym for c in ('NATGAS',)):
        rounded = max(1, int(round(f_qty)))
    elif 'SOL' in clean_sym:
        rounded = max(0.1, round(f_qty, 2))
    elif 'BNB' in clean_sym:
        rounded = max(0.01, round(f_qty, 2))
    elif any(c in clean_sym for c in ('XAG', 'SILVER', 'CL', 'OIL', 'WTI', 'BRENT', 'COPPER', 'BRONZE', 'HG')):
        rounded = max(0.1, round(f_qty, 1))
    elif any(c in clean_sym for c in ('AAPL', 'NVDA', 'TSLA', 'MSTR', 'AMZN', 'META', 'MSFT', 'GOOGL', 'SPY', 'QQQ', 'COIN', 'AMD')):
        rounded = max(0.1, round(f_qty, 1))
    elif any(c in clean_sym for c in ('TAO', 'MKR')):
        rounded = max(0.01, round(f_qty, 2))
    elif any(c in clean_sym for c in ('AAVE', 'BCH', 'COMP', 'QNT', 'ZEC')):
        rounded = max(0.1, round(f_qty, 1))
    elif 'LTC' in clean_sym:
        rounded = max(1.0, round(f_qty, 1))
    elif any(c in clean_sym for c in (
        'NEAR', 'AVAX', 'PENDLE', 'TIA', 'SEI', 'ONDO', 'RENDER', 'FET', 'ENA', 
        'EIGEN', 'RAY', 'DOGE', 'ADA', 'XRP', 'SUI', 'ARB', 'OP', 'DOT', 'ATOM', 
        'APT', 'ETC', 'LINK', 'UNI', 'POL', 'JUP', 'SHIB', 'TRX', 'PEPE',
        'BONK', 'FLOKI', 'CAT', 'RATS', 'SATS', 'BABYDOGE', 'MUBARAK', 'RAYSOL'
    )):
        rounded = max(1, int(round(f_qty)))
    elif price >= 100:
        rounded = max(0.1, round(f_qty, 1))
    elif price >= 30:
        rounded = max(0.1, round(f_qty, 1))
    elif f_qty < 1.0:
        # High-value assets where notional size is under 1 unit (e.g. 0.15 TAO, 0.11 ZEC)
        rounded = max(0.01, round(f_qty, 2)) if f_qty < 0.1 else max(0.1, round(f_qty, 1))
    else:
        # Standard low-priced altcoins (< $30): whole integers (0 decimals)
        rounded = max(1, int(round(f_qty)))

    min_exchange_q = get_exchange_min_qty(clean_sym)
    if min_exchange_q > 0 and rounded < min_exchange_q:
        rounded = min_exchange_q

    if isinstance(rounded, float) and rounded.is_integer():
        return int(rounded)
    return rounded


import asyncio
_global_execution_lock = None

def _resolve_target_r(state_entry: dict) -> float:
    """Resolve target R-multiple (2.2R for Squeeze/Runaway/RangeBreak, 1.5R default)."""
    if not state_entry:
        return 1.5
    uk = str(state_entry.get("unique_key", ""))
    default_r = 2.2 if ("1H_RangeBreak" in uk or "Squeeze" in uk or "RunawayChase" in uk) else 1.5
    return float(state_entry.get("target_r") or default_r)

def _get_execution_lock():
    global _global_execution_lock
    if _global_execution_lock is None:
        _global_execution_lock = asyncio.Lock()
    return _global_execution_lock

class SharkExchangeExecutor:
    """
    Handles Futures trade execution, leverage configuration, multi-order TP/SL brackets,
    and trailing SL updates directly on Shark Exchange Native API (India).
    """

    def __init__(self, name="Main_Account", api_key=None, api_secret=None, base_url=None, telegram_chat_id=None):
        self.name = name
        self.api_key = api_key or os.getenv("SHARK_API_KEY", SHARK_API_KEY)
        self.api_secret = api_secret or os.getenv("SHARK_API_SECRET", SHARK_API_SECRET)
        self.base_url = base_url or os.getenv("SHARK_BASE_URL", SHARK_BASE_URL)
        self.telegram_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID", "771305593")
        self.margin_inr = 1000.0
        self.leverage = AUTO_TRADE_LEVERAGE
        self.max_concurrent_trades = AUTO_TRADE_MAX_CONCURRENT
        self.excluded_symbols = AUTO_TRADE_EXCLUDED_SYMBOLS
        self.usd_inr_rate = USD_INR_RATE
        self._tp_order_count_cache = {}
        self._tp_sl_heal_lock = {}
        self._already_cancelled = set()  # order IDs cancelled this session — prevents repeat cancel loops
        self._session_active_symbols = {}  # symbol -> added_timestamp (strict in-memory active slot shield)
        self._pending_entry_orders = {}  # symbol -> {clientOrderId, placed_at, orderId, price, side, qty}
        self._known_live_positions = {}  # symbol -> dict of open positions
        self._notified_closed_ids = set()  # set of closed positionIds already notified to Telegram
        self._tp_touch_timer = {}  # symbol -> timestamp when mark price first reached TP1 (Maker Grace Period)
        self._notified_filled_positions = set()  # set of positionIds already notified of entry fill to Telegram
        self._last_positions_fetch_ok = True
        self._last_orders_fetch_ok = True

    async def send_alert(self, message: str):
        """Sends isolated alert to this specific account's Telegram DM and CCs Master Admin."""
        from telegram_bot import send_text_to_chat
        target = str(self.telegram_chat_id or "771305593")
        acc_tag = f"👤 <b>Account:</b> <code>{self.name}</code>\n"
        tagged_msg = acc_tag + message
        try:
            await send_text_to_chat(target, tagged_msg)
        except Exception as e:
            logger.error(f"Failed to send alert to {target} for {self.name}: {e}")
        
        master_admin = str(os.getenv("TELEGRAM_CHAT_ID", "771305593"))
        if target != master_admin:
            try:
                cc_msg = f"📋 <b>[Admin CC: {self.name}]</b>\n" + message
                await send_text_to_chat(master_admin, cc_msg)
            except Exception as e_cc:
                logger.debug(f"Failed to CC master admin: {e_cc}")

    def is_configured(self) -> bool:
        """Returns True if API keys are configured and auto trading is enabled."""
        auto_enabled = os.getenv("AUTO_TRADING_ENABLED", "true").lower() in ("true", "1", "yes")
        return (
            auto_enabled
            and bool(self.api_key)
            and bool(self.api_secret)
            and len(self.api_key) > 10
            and len(self.api_secret) > 10
        )

    def _get_headers(self, sig: str) -> dict:
        """Generates standard authenticated headers for Shark Exchange."""
        return {
            "api-key": self.api_key,
            "signature": sig,
            "Content-Type": "application/json"
        }

    def clean_sym(self, obj) -> str:
        """Extract and clean symbol from a position or order dict, normalized to Shark contract format."""
        if isinstance(obj, dict):
            raw = obj.get("contractPair") or obj.get("symbol") or obj.get("contractName") or ""
        else:
            raw = str(obj)
        return to_shark_symbol(raw)

    def extract_order_qty(self, obj: dict) -> float:
        """Extracts order quantity across all exchange field conventions (e.g. orderAmount, quantity, origQty)."""
        if not isinstance(obj, dict):
            return 0.0
        for k in ("orderAmount", "quantity", "origQty", "amount", "size", "holdQty", "positionAmount", "volume"):
            val = obj.get(k)
            if val is not None:
                try:
                    v = float(val)
                    if abs(v) > 0:
                        return abs(v)
                except Exception:
                    pass
        return 0.0

    async def shark_get(self, session: aiohttp.ClientSession, endpoint: str, params: dict = None) -> tuple:
        """Executes an authenticated GET request to Shark Exchange."""
        if params is None:
            params = {}
        params["timestamp"] = str(int(time.time() * 1000))
        query_str = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
        sig = hmac.new(self.api_secret.encode('utf-8'), query_str.encode('utf-8'), hashlib.sha256).hexdigest()
        headers = self._get_headers(sig)
        url = f"{self.base_url}{endpoint}?{query_str}"
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = await resp.text()
                return resp.status, data
        except Exception as e:
            logger.error(f"Shark GET {endpoint} error: {e}")
            return 500, str(e)

    async def shark_post(self, session: aiohttp.ClientSession, endpoint: str, payload: dict, _retries: int = 3) -> tuple:
        """Executes an authenticated POST request to Shark Exchange with Node.js sanitized floats.
        Auto-retries up to 3 times on transient 3029 (Internal Server Error) from Shark."""
        clean = sanitize_payload(payload)
        clean["timestamp"] = int(time.time() * 1000)
        body_str = json.dumps(clean, separators=(',', ':'))
        sig = hmac.new(self.api_secret.encode('utf-8'), body_str.encode('utf-8'), hashlib.sha256).hexdigest()
        headers = self._get_headers(sig)
        url = f"{self.base_url}{endpoint}"
        for attempt in range(1, _retries + 1):
            try:
                # Rebuild timestamp + signature on each retry (stale ts causes auth failure)
                clean["timestamp"] = int(time.time() * 1000)
                body_str = json.dumps(clean, separators=(',', ':'))
                sig = hmac.new(self.api_secret.encode('utf-8'), body_str.encode('utf-8'), hashlib.sha256).hexdigest()
                headers = self._get_headers(sig)
                async with session.post(url, headers=headers, data=body_str, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = await resp.text()
                    # Retry on 3029 (Shark transient Internal Server Error)
                    is_3029 = (resp.status == 500 or resp.status == 503) and "3029" in str(data)
                    if is_3029 and attempt < _retries:
                        backoff_delay = (1.5 * attempt) + random.uniform(0.1, 0.4)
                        logger.warning(f"Shark POST {endpoint} → 3029 transient error (attempt {attempt}/{_retries}). Retrying in {backoff_delay:.2f}s...")
                        await asyncio.sleep(backoff_delay)
                        continue
                    return resp.status, data
            except Exception as e:
                logger.error(f"Shark POST {endpoint} error (attempt {attempt}): {e}")
                if attempt < _retries:
                    backoff_delay = (1.5 * attempt) + random.uniform(0.1, 0.4)
                    await asyncio.sleep(backoff_delay)
        return 500, {"error": "3029", "message": "Max retries exceeded"}

    async def shark_delete(self, session: aiohttp.ClientSession, endpoint: str, payload: dict) -> tuple:
        """Executes an authenticated DELETE request to Shark Exchange."""
        clean = sanitize_payload(payload)
        clean["timestamp"] = int(time.time() * 1000)
        body_str = json.dumps(clean, separators=(',', ':'))
        sig = hmac.new(self.api_secret.encode('utf-8'), body_str.encode('utf-8'), hashlib.sha256).hexdigest()
        headers = self._get_headers(sig)
        url = f"{self.base_url}{endpoint}"
        try:
            async with session.request("DELETE", url, headers=headers, data=body_str, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = await resp.text()
                return resp.status, data
        except Exception as e:
            logger.error(f"Shark DELETE {endpoint} error: {e}")
            return 500, str(e)

    async def get_available_margin_inr(self, session: aiohttp.ClientSession) -> float:
        """Fetches available (free) margin in INR from Shark Exchange futures wallet for dynamic 2% sizing."""
        if not self.is_configured() or not session:
            return 0.0
        try:
            status, resp = await self.shark_get(session, "/v1/wallet/futures-wallet/details")
            if status == 200 and isinstance(resp, dict):
                data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
                is_inr = str(data.get("marginAsset", "")).upper() == "INR" or "inrBalance" in data
                raw_avail = float(data.get("marginBalance") or data.get("withdrawableBalance") or data.get("availableBalance") or data.get("walletBalance") or data.get("inrBalance") or 0.0)
                if raw_avail > 0:
                    return raw_avail if is_inr else (raw_avail * self.usd_inr_rate)
        except Exception as e:
            logger.error(f"Error fetching available margin for dynamic sizing: {e}")
        return 0.0

    def calculate_position_size(self, symbol: str, entry_price: float, sl_price: float = None, available_margin_inr: float = None, adx: float = None) -> tuple:
        """
        Calculates position quantities with dynamic 2.0% available margin sizing.
        Enforces exchange minimum lot floors for BTC, ETH, SOL, XAU so orders are never rejected with Error 3010.
        Adaptive TP:
        - If ADX < 20 (Ranging/Chop) OR total_qty < 2 * min_lot: 100% exit at TP1
        - If ADX >= 20 (Trending) AND total_qty >= 2 * min_lot: 70% at TP1, 30% runner at TP2
        Returns (total_qty, tp1_qty, tp2_qty, tp3_qty, margin_usdt, notional_usdt).
        """
        clean_sym = clean_contract_symbol(symbol)
        # USER DIRECTIVE: Exact 2.0% Dynamic Margin Sizing based on AVAILABLE (Free) Margin on Broker
        # Trade Margin is strictly 2.0% of Available Margin (e.g. ₹1,500 on ₹75,000 free margin).
        if available_margin_inr and float(available_margin_inr) > 0:
            margin_inr = round(max(500.0, float(available_margin_inr) * 0.02), 2)
        elif self.margin_inr and float(self.margin_inr) > 0:
            margin_inr = float(self.margin_inr)
        else:
            margin_inr = 1500.0

        min_lot = get_exchange_min_qty(clean_sym)
        # Enforce Minimum Margin floor for high-priced coins (BTC, ETH, etc.) to ensure total_qty >= exchange min lot
        if entry_price > 0 and min_lot > 0:
            min_notional_usdt = min_lot * entry_price
            min_required_margin_inr = (min_notional_usdt / self.leverage) * self.usd_inr_rate * 1.05  # 5% safety buffer
            if margin_inr < min_required_margin_inr:
                logger.info(
                    f"Boosting margin for {clean_sym} from ₹{margin_inr:.2f} to ₹{min_required_margin_inr:.2f} "
                    f"to strictly satisfy Shark minimum order size {min_lot}"
                )
                margin_inr = round(min_required_margin_inr, 2)

        notional_inr = margin_inr * self.leverage
        margin_usdt = margin_inr / self.usd_inr_rate
        notional_usdt = margin_usdt * self.leverage

        if entry_price <= 0:
            return 0.0, 0.0, 0.0, 0.0, margin_usdt, notional_usdt

        raw_total_qty = notional_usdt / entry_price
        total_qty = round_coin_qty(clean_sym, raw_total_qty, price=entry_price)

        if min_lot > 0 and total_qty < min_lot:
            total_qty = min_lot

        # USER DIRECTIVE: 100% Full Profit Booking at TP1 (1.5R Target)
        # Eliminates runner retracement, guarantees 100% full cash profit lock into wallet.
        tp1_qty = total_qty
        tp2_qty = 0.0
        tp3_qty = 0.0

        return total_qty, tp1_qty, 0.0, 0.0, margin_usdt, notional_usdt

    async def update_leverage(self, session: aiohttp.ClientSession, symbol: str, leverage: int = 10) -> bool:
        """Configures symbol leverage to 10x on Shark Exchange."""
        clean_sym = to_shark_symbol(symbol)
        if "PAXG" in clean_sym.upper():
            return False
        status, data = await self.shark_post(session, "/v1/exchange/update/leverage", {
            "contractName": clean_sym,
            "leverage": leverage
        })
        if status in (200, 201):
            logger.info(f"Updated Shark leverage for {clean_sym} to {leverage}x")
            return True
        logger.warning(f"Failed to update Shark leverage for {clean_sym}: {data}")
        return False

    async def get_open_orders(self, session: aiohttp.ClientSession, symbol: str = None) -> list:
        """Queries active open orders from Shark Exchange. Returns list of orders, or [] if empty/failed."""
        status, response = await self.shark_get(session, "/v1/order/open-orders")
        self._last_orders_fetch_ok = (status == 200)
        if status == 200:
            if isinstance(response, list):
                data = response
            elif isinstance(response, dict):
                data = response.get("data", [])
            else:
                data = []
            if isinstance(data, list):
                if symbol:
                    clean_sym = clean_contract_symbol(symbol)
                    return [o for o in data if clean_contract_symbol(o.get("symbol", "")) == clean_sym]
                return data
        logger.warning(f"Shark get_open_orders returned non-200 status {status}: {response}")
        return []

    async def get_open_positions(self, session: aiohttp.ClientSession) -> list:
        """Queries active open positions from Shark Exchange. Returns list of open positions, or [] if empty/failed."""
        status, response = await self.shark_get(session, "/v1/positions/OPEN")
        self._last_positions_fetch_ok = (status == 200)
        if status == 200:
            if isinstance(response, list):
                data = response
            elif isinstance(response, dict):
                data = response.get("data") or response.get("positions") or response.get("list") or []
            else:
                data = []
            if isinstance(data, list):
                result = []
                for p in data:
                    if not isinstance(p, dict): continue
                    # Parse all possible position quantity keys used by Shark Exchange
                    qty = 0.0
                    for k in ("quantity", "holdQty", "positionAmount", "amount", "size", "volume", "openQty", "posQty"):
                        val = p.get(k)
                        if val is not None:
                            try:
                                v = float(val)
                                if abs(v) > 0:
                                    qty = abs(v)
                                    break
                            except Exception:
                                pass
                    if qty > 0:
                        result.append(p)
                return result
        logger.warning(f"Shark get_open_positions returned non-200 status {status}: {response}")
        return []

    async def get_open_position(self, session: aiohttp.ClientSession, symbol: str) -> dict:
        """Gets single active open position for symbol."""
        shark_sym = to_shark_symbol(symbol)
        positions = await self.get_open_positions(session)
        if not positions:
            return None
        for p in positions:
            p_sym = to_shark_symbol(p.get("contractPair") or p.get("symbol") or p.get("contractName") or "")
            if p_sym == shark_sym:
                return p
        return None

    async def has_active_trade_on_exchange(self, session: aiohttp.ClientSession, symbol: str, signal: str = None) -> bool:
        """
        SINGLE ORDER SHIELD & TREND SCALE-IN ENGINE:
        Checks if symbol already has an active open position or pending entry order on Shark Exchange.
        - Prevents duplicate entries on the same symbol while allowing unlimited concurrent trades across different symbols.
        - TREND SCALE-IN (PYRAMIDING): If an existing position has already secured TP1 (70% profit booked,
          only 30% runner remaining, tp1_hit == True) AND the new signal is in the SAME direction,
          AND no pending entry limit order is currently resting on exchange, allows scaling in!
        """
        if not self.is_configured():
            return False

        clean_sym = to_shark_symbol(symbol)
        if "PAXG" in clean_sym.upper():
            return True  # Block any PAXG trade attempt

        # 1. Fetch Exchange State
        positions = await self.get_open_positions(session)
        open_orders = await self.get_open_orders(session)

        if not self._last_positions_fetch_ok or not self._last_orders_fetch_ok:
            logger.warning(f"has_active_trade_on_exchange: API fetch failed for {clean_sym}. Shielding against duplicate submission.")
            return True

        # Symbols with an active open position on exchange
        position_symbols = set()
        for p in positions:
            s_clean = to_shark_symbol(p.get("contractPair") or p.get("symbol") or p.get("contractName") or "")
            if s_clean:
                position_symbols.add(s_clean)

        # Symbols with a pending ENTRY limit order on exchange
        pending_entry_symbols = set()
        for o in open_orders:
            is_reduce_only = o.get("reduceOnly", False)
            order_type     = str(o.get("type") or "").upper()
            place_type     = str(o.get("placeType") or "").upper()
            sub_type       = str(o.get("subType") or "").upper()
            # Position-bound bracket orders (SL/TP) should not count as entry orders
            if is_reduce_only or place_type == "POSITION" or sub_type in ("STOP_LOSS", "TAKE_PROFIT") or order_type in ("STOP_MARKET", "STOP_LIMIT", "STOP"):
                continue
            if order_type == "LIMIT" and place_type in ("ORDER_FORM", ""):
                sym_raw = to_shark_symbol(o.get("symbol") or o.get("contractPair") or "")
                if sym_raw:
                    pending_entry_symbols.add(sym_raw)

        # Clean up stale in-memory active symbols (older than 120s and no longer on exchange)
        now_ts = time.time()
        unexpired_local = set()
        for sym_k, ts_val in list(self._session_active_symbols.items()):
            if sym_k in position_symbols or sym_k in pending_entry_symbols or (now_ts - ts_val < 120.0):
                unexpired_local.add(sym_k)
            else:
                self._session_active_symbols.pop(sym_k, None)

        # TREND SCALE-IN ENGINE:
        # If position exists on exchange, but NO pending entry order is resting:
        if clean_sym in position_symbols and clean_sym not in pending_entry_symbols and signal:
            sig_upper = str(signal).upper()
            sig_dir = "BUY" if sig_upper in ("BUY", "LONG") else "SELL"

            pos_obj = None
            for p in positions:
                if to_shark_symbol(p.get("contractPair") or p.get("symbol") or p.get("contractName") or "") == clean_sym:
                    pos_obj = p
                    break

            if pos_obj:
                pos_dir_raw = str(pos_obj.get("positionType") or pos_obj.get("positionSide") or pos_obj.get("side") or pos_obj.get("type") or "").upper()
                pos_dir = "BUY" if pos_dir_raw in ("LONG", "BUY") else "SELL"

                # If new signal is in the SAME direction as existing runner
                if pos_dir == sig_dir:
                    from state import get_latest_symbol_state
                    st_entry = get_latest_symbol_state(clean_sym)
                    tp1_hit = bool(st_entry and st_entry.get("tp1_hit"))
                    realized_pnl = float(pos_obj.get("realizedProfitInMarginAsset") or pos_obj.get("realizedProfit") or 0.0)

                    # Only allow scale-in if TP1 (70%) was already booked or profit secured
                    if tp1_hit or realized_pnl > 15.0:
                        logger.info(
                            f"🚀 TREND SCALE-IN PERMITTED: {clean_sym} [{sig_dir}] has booked TP1 (70%) / 30% runner active "
                            f"(Realized PnL: ₹{realized_pnl:,.2f}). Allowing scale-in entry order to ride the trend!"
                        )
                        return False

        # Merge Exchange state with local in-memory active slots set (Zero Race Condition Shield)
        active_symbols = position_symbols | pending_entry_symbols | unexpired_local
        logger.info(f"Exchange active symbols check: positions={position_symbols} pending={pending_entry_symbols} local={unexpired_local}")

        # Check if active trade or pending order already exists for this exact symbol (duplicate guard)
        if clean_sym in active_symbols:
            logger.warning(f"SINGLE ORDER SHIELD: Active trade or order already exists for {clean_sym}. Skipping duplicate.")
            return True

        return False

    async def get_directional_risk_slots(
        self,
        session: aiohttp.ClientSession,
        signal: str,
        max_at_risk: int = 5,
        batch_submitted_syms: list = None,
    ) -> dict:
        """
        Evaluates same-direction concurrent trade slots, exempting any active position
        whose live Stop-Loss on Shark Exchange is already moved to Breakeven (entry price)
        or locked in profit (SL >= Entry for LONG, SL <= Entry for SHORT).
        Zero-risk breakeven trades do not block new concurrent trades.
        """
        cand_dir = "BUY" if str(signal).upper() in ("BUY", "LONG") else "SELL"
        cand_label = "LONG" if cand_dir == "BUY" else "SHORT"
        positions = await self.get_open_positions(session)
        open_orders = await self.get_open_orders(session)

        orders_by_sym = {}
        for o in open_orders:
            s_o = self.clean_sym(o)
            if s_o:
                orders_by_sym.setdefault(s_o, []).append(o)

        at_risk_syms = []
        breakeven_syms = []

        for p in positions:
            p_sym = self.clean_sym(p)
            if not p_sym:
                continue
            p_q = 0.0
            for qk in ("quantity", "holdQty", "openQty", "posQty", "size", "amount", "positionAmount"):
                val = p.get(qk)
                if val is not None:
                    try:
                        fv = abs(float(val))
                        if fv > 0:
                            p_q = fv
                            break
                    except Exception:
                        pass
            if p_q <= 0:
                continue

            p_dr = str(p.get("positionType") or p.get("positionSide") or p.get("side") or "").upper()
            p_d = "BUY" if p_dr in ("LONG", "BUY") else "SELL"
            if p_d != cand_dir:
                continue

            entry_p = float(p.get("entryPrice") or p.get("avgPrice") or 0)
            if entry_p <= 0:
                pos_sz = abs(float(p.get("positionSize") or 0))
                if pos_sz > 0 and p_q > 0:
                    entry_p = pos_sz / p_q

            # Extract all live SL prices attached to this position
            sl_prices = []
            for o_ord in orders_by_sym.get(p_sym, []):
                typ_u = str(o_ord.get("type") or "").upper()
                sub_u = str(o_ord.get("subType") or "").upper()
                lnk_u = str(o_ord.get("linkType") or "").upper()
                place_u = str(o_ord.get("placeType") or "").upper()
                is_sl = (
                    typ_u in ("STOP_MARKET", "STOP_LIMIT", "STOP")
                    or sub_u == "STOP_LOSS"
                    or lnk_u == "ORDER_SL"
                    or (place_u == "POSITION" and (o_ord.get("stopLossPrice") or o_ord.get("stopPrice")))
                ) and not (place_u == "ORDER_FORM" and typ_u == "LIMIT")
                if is_sl:
                    px = float(o_ord.get("stopLossPrice") or o_ord.get("stopPrice") or o_ord.get("triggerPrice") or o_ord.get("price") or 0)
                    if px > 0:
                        sl_prices.append(px)

            # Position is Breakeven / Zero-Risk ONLY if it has an active live SL on exchange
            # placed at or beyond Entry (0.05% tolerance for exchange price tick rounding).
            is_breakeven = False
            if entry_p > 0 and sl_prices:
                if cand_dir == "BUY":
                    worst_sl = min(sl_prices)
                    if worst_sl >= entry_p * 0.9995:
                        is_breakeven = True
                else:
                    worst_sl = max(sl_prices)
                    if worst_sl <= entry_p * 1.0005:
                        is_breakeven = True

            if is_breakeven:
                breakeven_syms.append(p_sym)
            else:
                at_risk_syms.append(p_sym)

        # Account for new same-direction trades already submitted earlier in this batch loop
        extra_syms = [
            s for s in (batch_submitted_syms or [])
            if s not in at_risk_syms and s not in breakeven_syms
        ]
        all_at_risk = at_risk_syms + extra_syms
        at_risk_count = len(all_at_risk)
        allowed = at_risk_count < max_at_risk

        be_note = f" | Breakeven-exempt (Zero-Risk): {len(breakeven_syms)} ({', '.join(breakeven_syms)})" if breakeven_syms else ""
        reason = (
            f"Directional Limit Reached (Already {at_risk_count}/{max_at_risk} at-risk {cand_label} trades: "
            f"{', '.join(all_at_risk)}{be_note}). Max {max_at_risk} at-risk {cand_label} trades allowed at a time."
        )
        return {
            "allowed": allowed,
            "at_risk_count": at_risk_count,
            "max_at_risk": max_at_risk,
            "at_risk_syms": all_at_risk,
            "breakeven_syms": breakeven_syms,
            "reason": reason,
        }

    async def trail_position_on_new_signal(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        sig_dir: str,
        pos_obj: dict,
        entry: float,
        sl: float,
        tp1: float
    ) -> bool:
        """
        TREND CONTINUATION TRAIL ENGINE (User Directive):
        When a new signal arrives for an active open position in the SAME direction:
        - Trails the Stop-Loss forward to the tighter/safer structural SL (locking in profit / reducing risk).
        - Trails the Take-Profit limit order forward (extending the target deeper into the trend, min 1.5R).
        - Preserves 100% position size on the TP limit order.
        - Sends Telegram alert detailing the trailed SL and extended TP levels.
        """
        clean_sym = clean_contract_symbol(symbol)
        pos_id = pos_obj.get("positionId")
        pos_qty = abs(float(pos_obj.get("quantity") or pos_obj.get("positionAmount") or 0))
        pos_entry = float(pos_obj.get("entryPrice") or 0)
        exit_side = "SELL" if sig_dir == "BUY" else "BUY"

        if pos_qty <= 0:
            return False

        try:
            # 1. Fetch current open orders for this symbol
            cur_orders = await self.get_open_orders(session, symbol=clean_sym)
            sl_orders = [o for o in cur_orders if (
                o.get("type", "").upper() in ("STOP_MARKET", "STOP_LIMIT", "STOP")
                or o.get("subType") == "STOP_LOSS"
                or (o.get("placeType") == "POSITION" and o.get("stopLossPrice"))
            ) and not (str(o.get("placeType") or "").upper() == "ORDER_FORM" and str(o.get("type") or "").upper() == "LIMIT")]
            
            tp_orders = [o for o in cur_orders if (
                (o.get("type", "").upper() == "LIMIT" and o.get("placeType") == "POSITION")
                or o.get("subType") == "TAKE_PROFIT"
                or (o.get("reduceOnly") and o.get("type", "").upper() == "LIMIT")
            ) and str(o.get("placeType") or "").upper() != "ORDER_FORM"]

            current_sl = float(sl_orders[0].get("stopLossPrice") or sl_orders[0].get("stopPrice") or sl_orders[0].get("price") or 0) if sl_orders else 0
            current_tp = float(tp_orders[0].get("price") or 0) if tp_orders else 0

            # Scale prices if needed for 1000x multiplier contracts
            mult = get_symbol_price_multiplier(clean_sym, entry)
            if mult != 1.0:
                entry = entry * mult
                sl = sl * mult
                tp1 = tp1 * mult

            clean_new_sl = round_price(clean_sym, sl)
            clean_new_tp = round_price(clean_sym, tp1)

            sl_updated = False
            tp_updated = False

            # Cooldown guard: do not trail orders more than once every 3 minutes per symbol
            last_trail_time = self._tp_sl_heal_lock.get(clean_sym + "_continuation_trail", 0)
            if time.time() - last_trail_time < 180:
                logger.info(f"Continuation trail for {clean_sym} on cooldown ({int(time.time() - last_trail_time)}s < 180s) — skipping.")
                return True

            # Check if new SL is tighter/better with minimum 0.20% improvement (avoids flickering modifications):
            # For BUY: new SL >= current_sl * 1.002 (moves upward to lock profit / reduce risk)
            # For SELL: new SL <= current_sl * 0.998 (moves downward to lock profit / reduce risk)
            is_better_sl = False
            if current_sl == 0:
                is_better_sl = True
            elif sig_dir == "BUY" and clean_new_sl >= current_sl * 1.002:
                is_better_sl = True
            elif sig_dir == "SELL" and clean_new_sl <= current_sl * 0.998:
                is_better_sl = True

            if is_better_sl:
                logger.info(f"🔄 CONTINUATION TRAIL SL: {clean_sym} [{sig_dir}] SL {current_sl} → {clean_new_sl}")
                sl_ok = await self.modify_stop_loss(session, clean_sym, sig_dir, clean_new_sl, pos_qty, is_trailed=True)
                if sl_ok:
                    sl_updated = True
                    self._tp_sl_heal_lock[clean_sym + "_continuation_trail"] = time.time()

            # USER DIRECTIVE (Updated):
            # When a new continuation signal arrives, ALWAYS update/trail both SL AND TP:
            # - For BUY: new TP >= current_tp * 1.002 (extends target upwards into trend)
            # - For SELL: new TP <= current_tp * 0.998 (extends target downwards into trend)
            # - If TP is missing (current_tp == 0), restore it immediately.
            is_better_tp = False
            if current_tp == 0:
                is_better_tp = True
            elif sig_dir == "BUY" and clean_new_tp >= current_tp * 1.002:
                is_better_tp = True
            elif sig_dir == "SELL" and clean_new_tp <= current_tp * 0.998:
                is_better_tp = True

            if is_better_tp:
                logger.info(f"🔄 CONTINUATION TRAIL TP: {clean_sym} [{sig_dir}] TP {current_tp} → {clean_new_tp}")
                # Cancel existing TP orders
                for o in tp_orders:
                    cid = o.get("clientOrderId")
                    if cid:
                        await self.shark_delete(session, "/v1/order/delete-order", {"clientOrderId": str(cid)})
                
                # Place new single 100% TP order at clean_new_tp
                tp_payload = {
                    "placeType": "POSITION",
                    "positionId": pos_id,
                    "reduceOnly": True,
                    "type": "LIMIT",
                    "side": exit_side,
                    "symbol": clean_sym,
                    "quantity": pos_qty,
                    "price": clean_new_tp
                }
                st_tp, res_tp = await self.shark_post(session, "/v1/order/place-order", tp_payload)
                if st_tp in (200, 201):
                    tp_updated = True
                    logger.info(f"Placed trailed 100% TP for {clean_sym} at {clean_new_tp} qty={pos_qty}")

            if sl_updated or tp_updated:
                self._tp_sl_heal_lock[clean_sym + "_continuation_trail"] = time.time()
                if tp_updated:
                    self._tp_sl_heal_lock[clean_sym + "_tp1_realigned"] = True
                from state import update_latest_shark_state
                st_updates = {"continuation_trailed": True}
                if sl_updated:
                    st_updates["cur_sl"] = clean_new_sl
                if tp_updated:
                    st_updates["tp1"] = clean_new_tp
                update_latest_shark_state(clean_sym, st_updates)

                from telegram_bot import send_text_alert
                sl_info = f"🛡️ <b>SL Trailed:</b> ${current_sl} → <b>${clean_new_sl}</b> (Risk Locked)\n" if sl_updated else f"🛡️ <b>SL:</b> ${current_sl}\n"
                tp_info = f"🎯 <b>TP Trailed:</b> ${current_tp} → <b>${clean_new_tp}</b> (Target Extended)\n" if tp_updated else f"🎯 <b>TP:</b> ${current_tp}\n"
                msg = (
                    f"🔄 <b>TREND CONTINUATION: SAME COIN TRAIL UPDATE</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {clean_sym} [{sig_dir}]\n"
                    f"<b>Entry Price:</b> ${format_price(pos_entry, clean_sym)}\n"
                    f"{sl_info}"
                    f"{tp_info}"
                    f"<b>Quantity:</b> {pos_qty} (100% Position Protected)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>New continuation signal detected. SL locked in tighter & TP extended to ride the bigger trend!</i>"
                )
                await self.send_alert(msg)
                return True
            else:
                logger.info(f"Continuation signal for {clean_sym} evaluated: current SL ({current_sl}) and TP ({current_tp}) are already optimal.")
                return True
        except Exception as e:
            logger.error(f"Error in trail_position_on_new_signal for {clean_sym}: {e}")
            return False

    async def get_wallet_balance_summary(self, session: aiohttp.ClientSession) -> str:
        """Queries Shark Exchange Futures Wallet Details and returns formatted Telegram string."""
        if not self.is_configured() or not session:
            return ""
        try:
            await asyncio.sleep(1.0)
            status, resp = await self.shark_get(session, "/v1/wallet/futures-wallet/details")
            if status == 200 and isinstance(resp, dict):
                data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
                is_inr = str(data.get("marginAsset", "")).upper() == "INR" or "inrBalance" in data
                raw_total = float(data.get("walletBalance") or data.get("inrBalance") or data.get("totalBalance") or 0.0)
                raw_avail = float(data.get("marginBalance") or data.get("withdrawableBalance") or data.get("availableBalance") or raw_total)
                raw_locked = float(data.get("lockedBalance") or data.get("lockedBalanceIsolated") or max(0.0, raw_total - raw_avail))
                if is_inr:
                    inr_total = raw_total
                    inr_avail = raw_avail
                    inr_locked = raw_locked
                    usdt_total = inr_total / self.usd_inr_rate
                    usdt_avail = inr_avail / self.usd_inr_rate
                    usdt_locked = inr_locked / self.usd_inr_rate
                else:
                    usdt_total = raw_total
                    usdt_avail = raw_avail
                    usdt_locked = raw_locked
                    inr_total = usdt_total * self.usd_inr_rate
                    inr_avail = usdt_avail * self.usd_inr_rate
                    inr_locked = usdt_locked * self.usd_inr_rate
                return (
                    f"💼 <b>Shark Exchange Futures Wallet:</b>\n"
                    f"  • Total Balance:    <b>₹{inr_total:,.2f} INR</b>  (${usdt_total:.2f} USDT)\n"
                    f"  • Available Margin: <b>₹{inr_avail:,.2f} INR</b>  (${usdt_avail:.2f} USDT)\n"
                    f"  • Margin In-Use:     <b>₹{inr_locked:,.2f} INR</b>  (${usdt_locked:.2f} USDT)"
                )
        except Exception as e:
            logger.error(f"Error fetching Shark wallet balance summary: {e}")
        return ""

    async def cancel_over_limit_pending_orders(self, session: aiohttp.ClientSession) -> None:
        """Disabled: Portfolio cap removed per user directive."""
        return

    async def execute_signal_trade(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        signal: str,
        entry: float,
        sl: float,
        tp1: float,
        tp2: float,
        tp3: float = None,
        aligned_count: int = 4,
        adx: float = None,
        allow_during_maintenance: bool = False
    ) -> dict:
        async with _get_execution_lock():
            res = await self._execute_signal_trade_internal(
                session, symbol, signal, entry, sl, tp1, tp2, tp3,
                aligned_count=aligned_count, adx=adx,
                allow_during_maintenance=allow_during_maintenance,
            )
            # Mandatory 1.5s (1500ms) sequential execution pause between trade submissions
            await asyncio.sleep(1.5)
            return res

    async def _execute_signal_trade_internal(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        signal: str,
        entry: float,
        sl: float,
        tp1: float,
        tp2: float,
        tp3: float = None,
        aligned_count: int = 4,
        adx: float = None,
        allow_during_maintenance: bool = False
    ) -> dict:
        """
        Executes a new trade on Shark Exchange from a bot signal.
        - Enforces 7x Leverage.
        - Enforces ₹1,000 INR Margin cap.
        - Excludes BTC and ETH from execution.
        - Enforces Single Order Margin Shield (rejects duplicate margin blocks).
        - Submits Entry Limit Order with Stop-Loss attached.
        """
        clean_sym = to_shark_symbol(symbol)
        if "PAXG" in clean_sym.upper():
            logger.info(f"EXCLUSION: {clean_sym} blocked per user directive. Only XAUUSDT is allowed for Gold.")
            return {"status": "skipped", "reason": "PAXG trading is permanently blocked. Only XAUUSDT is allowed for Gold."}

        # Scale prices if needed for 1000x / 1Mx multiplier contracts (e.g. 1000PEPE, 1000SHIB, 1000BONK, 1MBABYDOGE)
        multiplier = get_symbol_price_multiplier(clean_sym, entry)
        if multiplier != 1.0:
            logger.info(f"⚡ MULTIPLIER APPLIED: Scaling {clean_sym} prices by {multiplier}x (Entry: {entry} -> {entry * multiplier})")
            entry = entry * multiplier
            sl = sl * multiplier
            tp1 = tp1 * multiplier
            if tp2: tp2 = tp2 * multiplier
            if tp3: tp3 = tp3 * multiplier

        # 1. BTC / ETH / Gold Weekend Rule (Weekdays Mon-Fri Allowed; Sat-Sun No Trade)
        clean_base = clean_sym.upper().replace("USDT", "").replace("USD", "").replace("-PERP", "")
        is_btc_or_eth = clean_base in ("BTC", "ETH")
        is_commodity = any(m in clean_sym.upper() for m in ("XAU", "GOLD", "XAUT", "XAG", "CL", "NATGAS"))
        is_weekend_restricted = is_btc_or_eth or is_commodity
        if is_weekend_restricted:
            try:
                import zoneinfo
                now_ist = datetime.now(timezone.utc).astimezone(zoneinfo.ZoneInfo("Asia/Kolkata"))
                is_weekday = now_ist.weekday() < 5  # 0-4 = Mon-Fri, 5-6 = Sat-Sun
            except Exception:
                is_weekday = True

            if not is_weekday:
                logger.info(f"WEEKEND EXCLUSION: {clean_sym} [{signal}] skipped on Saturday/Sunday per user directive. Telegram alert only.")
                return {"status": "skipped", "reason": f"{clean_sym} trading is paused on weekends (Sat/Sun). Weekdays only."}
            else:
                logger.info(f"⚡ WEEKDAY EXECUTION: {clean_sym} [{signal}] active for Shark execution on Weekdays (Mon-Fri).")

        # 2. US Stock Tokens: AUTO-TRADING STOPPED per User Directive
        # Signals continue to be broadcast to Telegram, but automatic execution is disabled.
        # NOTE: Use exact base-asset match (not substring startswith/endswith) to prevent collision with real crypto:
        #       e.g. METAM is a real crypto NOT Meta stock, ARB is NOT AMRB, BNB is NOT blank etc.
        is_us_stock = is_us_stock_token(symbol)
        if is_us_stock:
            logger.info(f"AUTO-TRADE EXCLUSION: {symbol} is a US Stock token. Skipping auto-trade per user directive. Telegram alert only.")
            return {"status": "skipped", "reason": f"{symbol} is a US stock (auto-trade disabled per directive, alerts only)"}

        # 3. DAILY RISK CIRCUIT BREAKER & NIGHT MAINTENANCE PAUSE GUARD
        if not allow_during_maintenance:
            from config import is_night_maintenance_window
            if is_night_maintenance_window():
                logger.info(f"🌙 NIGHT MAINTENANCE ACTIVE: Blocked execution for {clean_sym} [{signal}] (12:30 AM - 05:30 AM IST).")
                return {"status": "skipped", "reason": "Night Maintenance Mode active (12:30 AM - 05:30 AM IST) — Auto-execution paused."}

        from state import is_trading_paused
        is_paused, rem_secs, pause_reason = is_trading_paused()
        if is_paused:
            logger.warning(f"⏸️ CIRCUIT BREAKER ACTIVE: Blocked execution for {clean_sym}. {pause_reason} ({rem_secs/60:.1f}m remaining).")
            return {"status": "skipped", "reason": f"Circuit Breaker Active ({rem_secs/60:.1f}m remaining): {pause_reason}"}

        if not self.is_configured():
            logger.info(f"AUTO-TRADING NOT CONFIGURED: Keys or toggle not set. Skipping execution for {clean_sym}.")
            return {"status": "skipped", "reason": "Exchange executor not configured or disabled"}

        # 2. Single Order Margin Shield Guard & Trend Continuation Trailing
        try:
            active_pos = await self.get_open_position(session, clean_sym)
            if active_pos:
                pos_qty = abs(float(active_pos.get("quantity") or active_pos.get("positionAmount") or 0))
                if pos_qty > 0:
                    pos_dir_raw = str(active_pos.get("positionType") or active_pos.get("positionSide") or active_pos.get("side") or "").upper()
                    pos_dir = "BUY" if pos_dir_raw in ("LONG", "BUY") else "SELL"
                    sig_dir = "BUY" if str(signal).upper() in ("BUY", "LONG") else "SELL"

                    if pos_dir == sig_dir:
                        # USER DIRECTIVE: Same coin continuation trade -> Trail SL and Trail TP forward!
                        logger.info(f"🔄 SAME COIN CONTINUATION: {clean_sym} [{sig_dir}]. Trailing open position SL and TP forward!")
                        await self.trail_position_on_new_signal(session, clean_sym, sig_dir, active_pos, entry, sl, tp1)
                        return {"status": "trailed", "reason": f"Trailed active {clean_sym} position on trend continuation signal"}
                    else:
                        logger.info(f"Opposite signal {sig_dir} for active {pos_dir} position on {clean_sym}. Shielding active trade.")
                        return {"status": "skipped", "reason": f"Opposite signal on active {clean_sym} position"}

            if await self.has_active_trade_on_exchange(session, clean_sym, signal=signal):
                return {"status": "skipped", "reason": f"Active trade or orders already exist on Shark for {clean_sym}"}
        except Exception as e:
            logger.error(f"Error checking exchange state for {clean_sym}: {e}")

        # 3. Position Sizing (Exact 2.0% Dynamic sizing on AVAILABLE Free Margin on Broker)
        avail_margin_inr = await self.get_available_margin_inr(session)
        if avail_margin_inr is not None and avail_margin_inr < 500.0:
            logger.warning(f"INSUFFICIENT AVAILABLE MARGIN ON BROKER: Only ₹{avail_margin_inr:,.2f} INR free (Min ₹500 required). Skipping {clean_sym}.")
            bal_summary = await self.get_wallet_balance_summary(session)
            bal_block = f"\n\n{bal_summary}" if bal_summary else ""
            msg = (
                f"⚠️ <b>SHARK AUTO-ORDER MARGIN ALERT</b>\n\n"
                f"Signal for <b>{clean_sym}</b> [{signal}] could not be executed.\n"
                f"<b>Reason:</b> Insufficient Free Margin in Shark Futures Wallet (Current: ₹{avail_margin_inr:,.2f} INR).\n"
                f"<b>Minimum Required:</b> ₹500 INR (or 2.0% of capital).\n"
                f"<i>Please deposit/transfer funds into your Shark Exchange Futures Wallet.</i>{bal_block}"
            )
            await self.send_alert(msg)
            return {"status": "skipped", "reason": f"Insufficient available margin on broker (₹{avail_margin_inr:,.2f} INR)"}
        total_qty, tp1_qty, tp2_qty, tp3_qty, margin_usdt, notional_usdt = self.calculate_position_size(clean_sym, entry, sl_price=sl, available_margin_inr=avail_margin_inr, adx=adx)
        if total_qty <= 0:
            logger.error(f"Position size calculation resulted in 0 quantity for {clean_sym}.")
            return {"status": "error", "reason": "Invalid position size calculation"}

        clean_entry = round_price(clean_sym, entry)
        clean_sl = round_price(clean_sym, sl)
        clean_tp1 = round_price(clean_sym, tp1)
        clean_tp2 = round_price(clean_sym, tp2)
        clean_tp3 = round_price(clean_sym, tp3) if tp3 is not None else clean_tp2

        side = "BUY" if signal.upper() in ("BUY", "LONG") else "SELL"

        # Strict Direction SL Guarantee & Liquidation Shield (SL MUST ALWAYS be above Liq for BUY / below Liq for SELL)
        clean_sl = enforce_sl_above_liquidation(clean_sym, clean_entry, clean_sl, side, self.leverage)
        if side == "BUY" and clean_sl >= clean_entry:
            clean_sl = round_price(clean_sym, clean_entry * 0.995)
        elif side == "SELL" and clean_sl <= clean_entry:
            clean_sl = round_price(clean_sym, clean_entry * 1.005)

        # 4. Set Leverage on Shark Exchange to 10x
        await self.update_leverage(session, clean_sym, self.leverage)

        # 5. Place Entry Limit Order with Stop-Loss attached (2-TP 70/30 orders placed upon fill)
        # NOTE: Shark Exchange forbids clientOrderId in place-order payloads ('property clientOrderId should not exist')
        # Shark generates its own clientOrderId upon placement and returns it in response.
        order_payload = {
            "placeType": "ORDER_FORM",
            "symbol": clean_sym,
            "side": side,
            "type": "LIMIT",
            "price": clean_entry,
            "quantity": total_qty,
            "reduceOnly": False,
            "stopLossPrice": clean_sl
        }

        logger.info(
            f"SUBMITTING SHARK ORDER: {clean_sym} [{side}] Qty={total_qty} Price={clean_entry} "
            f"SL={clean_sl} Lev={self.leverage}x Margin=₹{self.margin_inr:.0f}"
        )

        status, data = await self.shark_post(session, "/v1/order/place-order", order_payload)

        is_order_accepted = (
            status in (200, 201)
            and isinstance(data, dict)
            and not data.get("error")
            and (bool(data.get("id")) or bool(data.get("orderId")) or bool(data.get("clientOrderId")))
        )

        if is_order_accepted:
            order_id = data.get("orderId") or data.get("id") or "CONFIRMED"
            actual_cid = data.get("clientOrderId") or ""
            logger.info(f"SHARK ORDER PLACED SUCCESSFULLY: {clean_sym} OrderID={order_id} ClientID={actual_cid}")
            self._tp_sl_heal_lock.pop(clean_sym + "_continuation_trail", None)
            self._tp_sl_heal_lock.pop(clean_sym + "_tp1_realigned", None)
            self._session_active_symbols[clean_sym] = time.time()
            self._pending_entry_orders[clean_sym] = {
                "clientOrderId": actual_cid,
                "orderId": order_id,
                "placed_at": time.time(),
                "symbol": clean_sym,
                "price": clean_entry,
                "side": side,
                "qty": total_qty
            }
            logger.info(f"STRICT SLOT RESERVED: {clean_sym} added to local active slots set (Total active: {len(self._session_active_symbols)})")

            # Send Telegram Execution Alert
            bal_summary = await self.get_wallet_balance_summary(session)
            bal_block = f"\n\n{bal_summary}" if bal_summary else ""

            risk_step_est = abs(clean_entry - clean_sl) if (clean_entry and clean_sl and clean_entry != clean_sl) else (clean_entry * 0.015)
            r_ratio_est = round(abs(clean_tp1 - clean_entry) / max(risk_step_est, 1e-8), 1) if (clean_tp1 and clean_entry) else 1.5
            r_label = f"{r_ratio_est:.1f}R" if abs(r_ratio_est - 1.5) > 0.1 else "1.5R"

            exec_card = (
                f"🦈 <b>REAL LIVE TRADE: LIMIT ORDER PLACED (SHARK EXCHANGE)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Asset:</b> {clean_sym} [{side}]\n"
                f"<b>Entry Limit:</b> {format_price(clean_entry)}\n"
                f"<b>Position Qty:</b> {total_qty}\n"
                f"<b>Margin Used:</b> ₹{self.margin_inr:,.0f} INR (~${margin_usdt:.2f} USDT)\n"
                f"<b>Leverage:</b> {self.leverage}x Fixed\n"
                f"<b>Notional:</b> ~${notional_usdt:.2f} USDT\n\n"
                f"🎯 <b>Take-Profit Target ({r_label}):</b> {format_price(clean_tp1)} (100% Full Booking)\n"
                f"🛡️ <b>Stop-Loss Limit:</b> {format_price(clean_sl)}{bal_block}\n\n"
                f"<i>Status: Entry limit resting on order book. {r_label} Maker Limit TP armed.</i>"
            )
            await self.send_alert(exec_card)

            return {
                "status": "success",
                "symbol": clean_sym,
                "order_id": order_id,
                "client_order_id": actual_cid,
                "total_qty": total_qty,
                "tp1_qty": tp1_qty,
                "tp2_qty": tp2_qty,
                "tp3_qty": tp3_qty,
                "entry": clean_entry,
                "sl": clean_sl,
                "tp1": clean_tp1,
                "tp2": clean_tp2,
                "tp3": clean_tp3
            }
        else:
            err_msg = str(data)
            logger.error(f"Shark order placement failed for {clean_sym} (Status {status}): {err_msg}")
            
            # If rejected due to insufficient margin, notify account owner + Admin CC cleanly
            if "3018" in err_msg or "Insufficient margin" in err_msg:
                msg = (
                    f"⚠️ <b>SHARK AUTO-ORDER MARGIN ALERT</b>\n\n"
                    f"Signal for <b>{clean_sym}</b> [{side}] could not be executed.\n"
                    f"<b>Reason:</b> Insufficient Margin in Shark Futures Wallet.\n"
                    f"<b>Required:</b> ₹{self.margin_inr:,.0f} INR.\n"
                    f"<i>Please deposit/transfer funds into your Shark Exchange Futures Wallet.</i>"
                )
                await self.send_alert(msg)

            return {"status": "error", "reason": err_msg, "http_status": status}

    async def place_tp_limits(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        direction: str,
        entry: float,
        tp1: float,
        tp2: float,
        tp3: float = None,
        initial_sl: float = None,
        adx: float = None
    ) -> bool:
        """
        Called when entry order is filled.
        Locates active position on Shark Exchange and attaches 1 SL + 3 TP LIMIT orders
        (40% at TP1, 30% at TP2, 30% at TP3) via placeType: 'POSITION'.

        IMPORTANT: initial_sl must be the REAL stop-loss price from the signal,
        NOT the entry price. If not provided, reads from state. Falls back to
        a safe 2% buffer below/above entry only as last resort.
        """
        if not self.is_configured() or not session:
            return False

        clean_sym = clean_contract_symbol(symbol)
        try:
            # Resolve actual SL: prefer explicit param → exchange live SL order → state → safe fallback (NOT entry!)
            actual_sl = initial_sl
            if not actual_sl or actual_sl == entry:
                open_ords = await self.get_open_orders(session, symbol=clean_sym)
                sl_ords = [
                    o for o in open_ords
                    if (
                        o.get("type", "").upper() in ("STOP_MARKET", "STOP_LIMIT", "STOP")
                        or o.get("subType") == "STOP_LOSS"
                        or (o.get("placeType") == "POSITION" and (o.get("stopLossPrice") or o.get("stopPrice")))
                    ) and str(o.get("type") or "").upper() != "LIMIT"
                ]
                if sl_ords:
                    actual_sl = float(sl_ords[0].get("stopLossPrice") or sl_ords[0].get("stopPrice") or sl_ords[0].get("price") or 0)
            if not actual_sl or actual_sl == entry:
                from state import get_latest_symbol_state
                state_entry = get_latest_symbol_state(clean_sym)
                if state_entry:
                    actual_sl = float(state_entry.get("cur_sl") or state_entry.get("initial_sl") or state_entry.get("sl") or 0)
            # Last resort: 2% buffer (never breakeven)
            if not actual_sl or actual_sl == entry:
                if direction.upper() in ("BUY", "LONG"):
                    actual_sl = entry * 0.98
                else:
                    actual_sl = entry * 1.02
                logger.warning(f"place_tp_limits: Could not find initial_sl for {clean_sym}, using 2% fallback: {actual_sl}")

            pos = await self.get_open_position(session, clean_sym)
            if pos:
                pos_id = pos.get("positionId")
                actual_qty = abs(float(pos.get("quantity") or pos.get("positionAmount") or pos.get("amount") or 0.0))
                # Use actual fill price from exchange if available (handles slippage)
                fill_price = float(pos.get("entryPrice") or pos.get("avgPrice") or entry)

                # Authoritative direction from live position
                pos_dir_raw = str(pos.get("positionType") or pos.get("positionSide") or pos.get("side") or pos.get("type") or "").upper()
                if pos_dir_raw in ("LONG", "BUY"):
                    pos_direction = "BUY"
                elif pos_dir_raw in ("SHORT", "SELL"):
                    pos_direction = "SELL"
                else:
                    pos_direction = direction

                # Enforce Liquidation Shield on actual_sl before placing brackets
                live_liq = float(pos.get("liquidationPrice") or pos.get("liqPrice") or pos.get("forceClosePrice") or 0)
                actual_sl = enforce_sl_above_liquidation(clean_sym, fill_price, actual_sl, pos_direction, self.leverage, live_liq)

                # User Directive: Dynamic target policy (2.2R for Breakdown continuation, 1.5R for standard)
                from state import get_latest_symbol_state
                st_entry_check = get_latest_symbol_state(clean_sym)
                target_r = 1.5
                if st_entry_check:
                    target_r = _resolve_target_r(st_entry_check)

                is_cont_trailed = bool(st_entry_check and st_entry_check.get("continuation_trailed"))
                trailed_tp_candidate = float((st_entry_check.get("tp1") if st_entry_check else 0) or tp1 or 0)

                # Validate & recalculate TPs if state TPs clash with live position direction (e.g. stale signal in state)
                risk = abs(fill_price - actual_sl) if (actual_sl and actual_sl != fill_price) else (fill_price * 0.015)
                if pos_direction == "BUY":
                    if actual_sl >= fill_price and not is_cont_trailed:
                        actual_sl = round_price(clean_sym, fill_price - risk)
                    if is_cont_trailed and trailed_tp_candidate > fill_price:
                        tp1 = round_price(clean_sym, trailed_tp_candidate)
                    else:
                        tp1 = round_price(clean_sym, fill_price + target_r * risk)
                    if not tp2 or tp2 <= tp1:
                        tp2 = round_price(clean_sym, max(tp1 * 1.003, fill_price + 3.5 * risk))
                    if not tp3 or tp3 <= tp2:
                        tp3 = round_price(clean_sym, max(tp2 * 1.003, fill_price + 3.5 * risk))
                else:  # SHORT
                    if actual_sl <= fill_price and not is_cont_trailed:
                        actual_sl = round_price(clean_sym, fill_price + risk)
                    if is_cont_trailed and 0 < trailed_tp_candidate < fill_price:
                        tp1 = round_price(clean_sym, trailed_tp_candidate)
                    else:
                        tp1 = round_price(clean_sym, fill_price - target_r * risk)
                    if not tp2 or tp2 >= tp1:
                        tp2 = round_price(clean_sym, min(tp1 * 0.997, fill_price - 3.5 * risk))
                    if not tp3 or tp3 >= tp2:
                        tp3 = round_price(clean_sym, min(tp2 * 0.997, fill_price - 3.5 * risk))

                # Update state so downstream monitoring uses correct direction TPs
                from state import update_latest_shark_state
                st_updates = {"tp1": tp1, "tp2": tp2, "tp3": tp3}
                if not is_cont_trailed or not (st_entry_check and st_entry_check.get("initial_sl")):
                    st_updates["initial_sl"] = actual_sl
                update_latest_shark_state(clean_sym, st_updates)

                if pos_id and actual_qty > 0:
                    logger.info(f"place_tp_limits: Placing SL={actual_sl} + 3 TPs ({tp1}/{tp2}/{tp3}) for {clean_sym} fill@{fill_price} qty={actual_qty}")
                    return await self.execute_fill_protection(
                        session, clean_sym, pos_direction, pos_id, actual_qty,
                        sl=actual_sl, tp1=tp1, tp2=tp2, tp3=tp3, adx=adx, fill_price=fill_price
                    )
            logger.info(f"place_tp_limits: No live position on Shark for {clean_sym} (paper/pending fill).")
        except Exception as e:
            logger.error(f"Error in place_tp_limits for {clean_sym}: {e}")
        return False

    async def execute_fill_protection(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        direction: str,
        pos_id: str,
        actual_qty: float,
        sl: float,
        tp1: float,
        tp2: float,
        tp3: float = None,
        adx: float = None,
        fill_price: float = None
    ) -> bool:
        """
        Attaches TP1 (70%), TP2 (30%), and 1 Stop-Loss order directly to the open position
        using placeType: 'POSITION' (requires ₹0 additional margin!).
        - TP1: 70% Qty @ tp1 (LIMIT reduce-only)
        - TP2: 30% Qty @ target_tp2 (LIMIT reduce-only)
        - SL:  100% Qty @ sl (STOP_MARKET reduce-only)
        """
        clean_sym = clean_contract_symbol(symbol)
        exit_side = "SELL" if direction.upper() in ("BUY", "LONG") else "BUY"

        clean_sl = round_price(clean_sym, sl)
        clean_tp1 = round_price(clean_sym, tp1)
        clean_tp2 = round_price(clean_sym, tp2)
        clean_tp3 = round_price(clean_sym, tp3) if tp3 is not None else clean_tp2

        # 100% Full Profit Booking Model at TP1 (1.5R Target)
        # Positions are 100% closed at 1.5R target with zero runner retracement!
        target_tp2 = clean_tp2 or clean_tp3
        tp1_qty = actual_qty
        tp2_qty = 0.0
        tp3_qty = 0.0

        # 1. Place Stop-Loss Order (Shark POSITION bracket SL)
        sl_payload = {
            "placeType": "POSITION",
            "positionId": pos_id,
            "reduceOnly": True,
            "type": "STOP_MARKET",
            "side": exit_side,
            "symbol": clean_sym,
            "quantity": actual_qty,
            "stopLossPrice": clean_sl
        }
        status_sl, res_sl = await self.shark_post(session, "/v1/order/place-order", sl_payload)
        if status_sl == 400 and isinstance(res_sl, dict) and res_sl.get("error") in ("3023", "3025"):
            logger.info(f"Fill Protection: SL already exists for {clean_sym} (Skipped redundant placement).")
        elif status_sl not in (200, 201):
            logger.warning(f"Fill Protection SL initial attempt failed for {clean_sym} (Status {status_sl}): {res_sl}. Retrying with safe emergency SL...")
            safe_retry_sl = clean_sl * 1.005 if direction.upper() in ("BUY", "LONG") else clean_sl * 0.995
            sl_payload["stopLossPrice"] = round_price(clean_sym, safe_retry_sl)
            st_retry, res_retry = await self.shark_post(session, "/v1/order/place-order", sl_payload)
            if st_retry in (200, 201):
                logger.info(f"Fill Protection SL emergency retry SUCCESS for {clean_sym} at {sl_payload['stopLossPrice']}")
            else:
                logger.error(f"🚨 CRITICAL: Fill Protection SL FAILED for {clean_sym} after retry: {res_retry}")
        else:
            logger.info(f"Fill Protection SL for {clean_sym}: Status {status_sl} - {res_sl}")

        # 0. Clean up any existing TP bracket/limit orders on position to enforce fresh split
        try:
            cur_orders = await self.get_open_orders(session, symbol=clean_sym)
            for o in cur_orders:
                is_tp = (o.get("subType") == "TAKE_PROFIT" or 
                         o.get("linkType") in ("ORDER_TP", "ORDER_TP_SL") or 
                         (o.get("type") == "LIMIT" and (o.get("placeType") == "POSITION" or o.get("reduceOnly"))))
                if is_tp:
                    cid_tp = o.get("clientOrderId")
                    if cid_tp:
                        logger.info(f"Purging old TP order {cid_tp} for {clean_sym} to enforce fresh split")
                        await self.shark_delete(session, "/v1/order/delete-order", {"clientOrderId": str(cid_tp)})
        except Exception as e_clean:
            logger.debug(f"TP bracket cleanup check error for {clean_sym}: {e_clean}")

        # Track placed TP orders for 1-TP fallback
        tp1_order_placed = False
        tp1_cid = None
        tp2_order_placed = False

        # 2. Place Take-Profit 1 Order as POSITION LIMIT Reduce-Only
        if tp1_qty > 0:
            tp1_valid = (exit_side == "SELL" and clean_tp1 > clean_sl) or (exit_side == "BUY" and clean_tp1 < clean_sl)
            if not tp1_valid:
                logger.warning(f"TP1 price {clean_tp1} appears invalid for {clean_sym} {exit_side} — skipping")
            else:
                tp1_payload = {
                    "placeType": "POSITION",
                    "positionId": pos_id,
                    "reduceOnly": True,
                    "type": "LIMIT",
                    "side": exit_side,
                    "symbol": clean_sym,
                    "quantity": tp1_qty,
                    "price": clean_tp1
                }
                st, res = await self.shark_post(session, "/v1/order/place-order", tp1_payload)
                if st in (200, 201):
                    tp1_order_placed = True
                    tp1_cid = res.get("clientOrderId") if isinstance(res, dict) else None
                    logger.info(f"TP1 placed for {clean_sym} at {clean_tp1} qty={tp1_qty} (LIMIT)")
                else:
                    logger.warning(f"TP1 failed for {clean_sym}: {res}")

        # 3. Place Take-Profit 2 Order (30% Runner) as POSITION LIMIT Reduce-Only
        if tp2_qty > 0:
            tp2_valid = (exit_side == "SELL" and target_tp2 > clean_sl and target_tp2 > clean_tp1) or \
                        (exit_side == "BUY" and target_tp2 < clean_sl and target_tp2 < clean_tp1)
            if not tp2_valid:
                logger.warning(f"TP2 price {target_tp2} appears invalid for {clean_sym} {exit_side} — skipping")
            else:
                tp2_payload = {
                    "placeType": "POSITION",
                    "positionId": pos_id,
                    "reduceOnly": True,
                    "type": "LIMIT",
                    "side": exit_side,
                    "symbol": clean_sym,
                    "quantity": tp2_qty,
                    "price": target_tp2
                }
                st2, res2 = await self.shark_post(session, "/v1/order/place-order", tp2_payload)
                if st2 in (200, 201):
                    tp2_order_placed = True
                    logger.info(f"TP2 placed for {clean_sym} at {target_tp2} qty={tp2_qty} (30% Runner LIMIT)")
                else:
                    logger.warning(f"TP2 failed for {clean_sym}: {res2}")

        # ── IRONCLAD BRACKET FALLBACK: If 2 TPs could not be fully placed, fallback to 1 TP (100%) at TP1 ──
        if tp2_qty > 0 and not tp2_order_placed:
            logger.warning(f"🛡️ 1-TP FALLBACK: {clean_sym} unable to support 2nd TP ({tp2_qty}). Enforcing 1 single TP (100% position: {actual_qty}) at TP1 ({clean_tp1})!")
            if tp1_order_placed and tp1_cid:
                # Cancel the partial TP1 order to place the full 100% order
                await self.shark_delete(session, "/v1/order/delete-order", {"clientOrderId": str(tp1_cid)})
            
            full_tp_payload = {
                "placeType": "POSITION",
                "positionId": pos_id,
                "reduceOnly": True,
                "type": "LIMIT",
                "side": exit_side,
                "symbol": clean_sym,
                "quantity": actual_qty,
                "price": clean_tp1
            }
            st_full, res_full = await self.shark_post(session, "/v1/order/place-order", full_tp_payload)
            if st_full in (200, 201):
                logger.info(f"🛡️ 1-TP FALLBACK SUCCESS: {clean_sym} protected with 1 single TP for 100% ({actual_qty}) at {clean_tp1}")
            else:
                logger.error(f"🚨 CRITICAL: 1-TP Fallback failed for {clean_sym}: {res_full}")
        elif not tp1_order_placed and not tp2_order_placed:
            # Emergency: Neither TP placed, try placing 1 single TP for full 100% quantity
            logger.warning(f"🚨 EMERGENCY: Neither TP placed for {clean_sym}. Placing emergency 1-TP for full {actual_qty} at {clean_tp1}!")
            emergency_tp = {
                "placeType": "POSITION",
                "positionId": pos_id,
                "reduceOnly": True,
                "type": "LIMIT",
                "side": exit_side,
                "symbol": clean_sym,
                "quantity": actual_qty,
                "price": clean_tp1
            }
            await self.shark_post(session, "/v1/order/place-order", emergency_tp)

        # 4. Safe Telegram Fill Alert (Fires only once per unique positionId on broker)
        if pos_id and pos_id not in self._notified_filled_positions:
            self._notified_filled_positions.add(pos_id)
            try:
                bal_summary = await self.get_wallet_balance_summary(session)
                bal_block = f"\n\n{bal_summary}" if bal_summary else ""
                px_disp = f"{fill_price:,.4f}" if fill_price else "Limit Entry"
                pos_dir_disp = "BUY" if direction.upper() in ("BUY", "LONG") else "SELL"
                fill_msg = (
                    f"🟢 <b>REAL LIVE TRADE: LIMIT ORDER FILLED (SHARK EXCHANGE)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {clean_sym} [{pos_dir_disp}] 10x Isolated\n"
                    f"🎯 <b>Fill Price:</b> {px_disp}\n"
                    f"📦 <b>Active Quantity:</b> {actual_qty} contracts\n"
                    f"🛡️ <b>Stop-Loss Armed:</b> {clean_sl}\n"
                    f"🎯 <b>Take-Profit Target:</b> {clean_tp1} (1.5R - 100% Full Booking)"
                    f"{bal_block}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>Status: Active position live on Shark Exchange. Real capital in play!</i>"
                )
                await self.send_alert(fill_msg)
            except Exception as e_alert:
                logger.warning(f"Failed to send fill alert for {clean_sym}: {e_alert}")

        return True

    async def modify_stop_loss(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        direction: str,
        new_sl: float,
        qty: float,
        is_trailed: bool = False
    ) -> bool:
        """
        Trails/replaces the stop-loss order on Shark Exchange:
        1. Queries open orders to find the old SL order clientOrderId.
        2. Queries open position to get positionId.
        3. Places new SL order with placeType: 'POSITION' at new_sl.
        4. Cancels old SL order via DELETE /v1/order/delete-order.
        """
        clean_sym = clean_contract_symbol(symbol)
        exit_side = "SELL" if direction.upper() in ("BUY", "LONG") else "BUY"
        clean_new_sl = round_price(clean_sym, new_sl)

        if not self.is_configured() or not session:
            return False

        try:
            # 1. Fetch current open orders to locate existing SL order(s)
            open_orders = await self.get_open_orders(session, symbol=clean_sym)
            sl_orders = [
                o for o in open_orders
                if (
                    o.get("type", "").upper() in ("STOP_MARKET", "STOP_LIMIT", "STOP")
                    or o.get("subType") == "STOP_LOSS"
                    or (o.get("placeType") == "POSITION" and (o.get("stopLossPrice") or o.get("stopPrice")))
                ) and str(o.get("type") or "").upper() != "LIMIT"
            ]

            # 2. Get active position for positionId and actual qty
            pos = await self.get_open_position(session, clean_sym)
            if not pos:
                logger.error(f"CRITICAL: Cannot modify SL for {clean_sym} — position not found. Aborting.")
                return False

            pos_id = pos.get("positionId")
            if not pos_id:
                logger.error(f"CRITICAL: positionId is None for {clean_sym}. Aborting SL update.")
                return False

            actual_qty = abs(float(pos.get("quantity") or pos.get("positionAmount") or qty))
            # Enforce Liquidation Shield on modified SL
            live_liq = float(pos.get("liquidationPrice") or pos.get("liqPrice") or pos.get("forceClosePrice") or 0)
            entry_p = float(pos.get("entryPrice") or pos.get("avgPrice") or clean_new_sl)
            clean_new_sl = enforce_sl_above_liquidation(clean_sym, entry_p, clean_new_sl, direction, self.leverage, live_liq, is_trailed=is_trailed)
            clean_qty  = round_coin_qty(clean_sym, actual_qty, price=clean_new_sl)

            # ── KEY FIX: Cancel OLD SL FIRST by orderId or clientOrderId to avoid error 3023 ──
            for o in sl_orders:
                oid = o.get("orderId") or o.get("id")
                cid = o.get("clientOrderId")
                deleted = False

                if cid:
                    del_status, del_res = await self.shark_delete(session, "/v1/order/delete-order", {"clientOrderId": str(cid)})
                    if del_status in (200, 201):
                        deleted = True
                        logger.info(f"Cancelled old SL clientOrderId {cid} for {clean_sym} before placing new one.")

                if not deleted:
                    logger.warning(f"Failed to cancel old SL order for {clean_sym} ({oid}/{cid}). Attempting placement anyway.")

            sl_payload = {
                "placeType":    "POSITION",
                "positionId":   pos_id,
                "reduceOnly":   True,
                "type":         "STOP_MARKET",
                "side":         exit_side,
                "symbol":       clean_sym,
                "quantity":     clean_qty,
                "stopLossPrice": clean_new_sl,
            }
            status, res = await self.shark_post(session, "/v1/order/place-order", sl_payload)

            # ── Error 3023 Auto-Recovery: force-purge any hidden old SL and retry ──
            if status == 400 and isinstance(res, dict) and res.get("error") == "3023":
                logger.warning(f"Error 3023 for {clean_sym}: Old SL still active. Force-purging open SL orders...")
                all_ops = await self.get_open_orders(session, symbol=clean_sym)
                for o in all_ops:
                    if o.get("linkType") == "ORDER_SL" or o.get("subType") == "STOP_LOSS" or o.get("type", "").upper() in ("STOP_MARKET", "STOP_LIMIT", "STOP"):
                        cid_sl = o.get("clientOrderId")
                        if cid_sl:
                            await self.shark_delete(session, "/v1/order/delete-order", {"clientOrderId": str(cid_sl)})
                # Retry placing new SL
                status, res = await self.shark_post(session, "/v1/order/place-order", sl_payload)

            if status in (200, 201):
                logger.info(f"Placed trailed SL for {clean_sym} at {clean_new_sl}.")
                return True

            # ── Handle error 3011: SL would activate instantly (price at/near trigger) ──
            err_str = str(res)
            if "3011" in err_str or "activated instantly" in err_str:
                logger.warning(f"SL at {clean_new_sl} would activate instantly for {clean_sym}. Retrying 0.3% away...")
                if exit_side == "SELL":   # LONG — SL is below price
                    fallback_sl = round_price(clean_sym, clean_new_sl * 0.997)
                else:                      # SHORT — SL is above price
                    fallback_sl = round_price(clean_sym, clean_new_sl * 1.003)
                sl_payload["stopLossPrice"] = fallback_sl
                status2, res2 = await self.shark_post(session, "/v1/order/place-order", sl_payload)
                if status2 in (200, 201):
                    logger.info(f"Placed fallback SL for {clean_sym} at {fallback_sl} (0.3% buffer).")
                    return True
                else:
                    logger.warning(f"Fallback SL also failed for {clean_sym}: {res2}")
            else:
                logger.warning(f"Could not place trailed SL for {clean_sym}: {res}")

        except Exception as e:
            logger.error(f"Error modifying stop loss for {clean_sym}: {e}")

        return False

    async def modify_take_profit(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        direction: str,
        new_tp: float,
        qty: float = None
    ) -> bool:
        """
        Modifies/replaces the take-profit LIMIT order on Shark Exchange:
        1. Queries open orders to find the old TP limit order(s).
        2. Queries open position to get positionId and actual quantity.
        3. Cancels old TP limit order(s) via DELETE /v1/order/delete-order.
        4. Places new TP order with placeType: 'POSITION' at new_tp.
        """
        clean_sym = clean_contract_symbol(symbol)
        exit_side = "SELL" if direction.upper() in ("BUY", "LONG") else "BUY"
        clean_new_tp = round_price(clean_sym, new_tp)

        if not self.is_configured() or not session:
            return False

        try:
            # 1. Fetch current open orders to locate existing TP order(s)
            open_orders = await self.get_open_orders(session, symbol=clean_sym)
            tp_orders = [
                o for o in open_orders
                if (
                    str(o.get("type") or "").upper() == "LIMIT"
                    and (o.get("placeType") == "POSITION" or o.get("reduceOnly") is True or o.get("subType") == "PRIMARY")
                    and str(o.get("side") or "").upper() == exit_side
                )
            ]

            # 2. Get active position for positionId and actual qty
            pos = await self.get_open_position(session, clean_sym)
            if not pos:
                logger.error(f"CRITICAL: Cannot modify TP for {clean_sym} — position not found. Aborting.")
                return False

            pos_id = pos.get("positionId")
            if not pos_id:
                logger.error(f"CRITICAL: positionId is None for {clean_sym}. Aborting TP update.")
                return False

            actual_qty = abs(float(pos.get("quantity") or pos.get("positionAmount") or (qty or 0)))
            clean_qty = round_coin_qty(clean_sym, actual_qty, price=clean_new_tp)

            # 3. Cancel old TP orders
            for o in tp_orders:
                cid = o.get("clientOrderId")
                if cid:
                    del_status, del_res = await self.shark_delete(session, "/v1/order/delete-order", {"clientOrderId": str(cid)})
                    if del_status in (200, 201):
                        logger.info(f"Cancelled old TP clientOrderId {cid} for {clean_sym} before placing new one.")
                    else:
                        logger.warning(f"Failed to cancel old TP order {cid} for {clean_sym}: {del_res}")

            # 4. Place new TP order
            tp_payload = {
                "placeType": "POSITION",
                "positionId": pos_id,
                "reduceOnly": True,
                "type": "LIMIT",
                "side": exit_side,
                "symbol": clean_sym,
                "quantity": clean_qty,
                "price": clean_new_tp
            }
            st, res = await self.shark_post(session, "/v1/order/place-order", tp_payload)
            if st in (200, 201):
                logger.info(f"Successfully placed updated TP for {clean_sym} at {clean_new_tp} qty={clean_qty}")
                return True
            else:
                logger.warning(f"Could not place updated TP for {clean_sym}: {res}")
                return False

        except Exception as e:
            logger.error(f"Error modifying take profit for {clean_sym}: {e}")
            return False

    async def cancel_all_open_orders(self, session: aiohttp.ClientSession, symbol: str) -> bool:
        """Cancels open ENTRY orders. Protects SL/TPs if a position is active."""
        clean_sym = clean_contract_symbol(symbol)
        try:
            # SAFETY CHECK: Is there a live position?
            pos = await self.get_open_position(session, clean_sym)
            has_position = False
            if pos and abs(float(pos.get("quantity") or pos.get("positionAmount") or 0)) > 0:
                has_position = True

            open_orders = await self.get_open_orders(session, symbol=clean_sym)
            if not open_orders:
                return True
                
            success = True
            for o in open_orders:
                # SAFETY FILTER: If a position is open, DO NOT cancel its SL or TPs
                if has_position and o.get("placeType") == "POSITION":
                    continue
                
                cid = o.get("clientOrderId")
                if not cid:
                    sym_c = clean_contract_symbol(o.get("symbol") or clean_sym)
                    cid = self._pending_entry_orders.get(sym_c, {}).get("clientOrderId")
                if cid:
                    param = {"clientOrderId": str(cid)}
                    status, res = await self.shark_delete(session, "/v1/order/delete-order", param)
                    if status not in (200, 201):
                        logger.warning(f"Failed to cancel order {param} for {clean_sym}: {res}")
                        success = False
                    else:
                        logger.info(f"Successfully cancelled order {param} for {clean_sym}.")
            return success
        except Exception as e:
            logger.error(f"Error cancelling open orders for {clean_sym}: {e}")
            return False

    cancel_open_orders_for_symbol = cancel_all_open_orders

    async def close_position_market(self, session: aiohttp.ClientSession, symbol: str, reason: str = "TIMEOUT") -> bool:
        """
        Closes an open position at market price on Shark Exchange and cancels all attached open orders.
        Used for 20-Hour Position Timeout and 12-Hour Stale Runner rule.
        """
        clean_sym = clean_contract_symbol(symbol)
        if not self.is_configured() or not session:
            return False
        try:
            pos = await self.get_open_position(session, clean_sym)
            if not pos:
                return False

            pos_id = pos.get("positionId")
            qty = abs(float(pos.get("quantity") or pos.get("positionAmount") or pos.get("amount") or 0))
            if qty <= 0:
                return False

            pos_dir_raw = str(pos.get("positionType") or pos.get("positionSide") or pos.get("side") or "").upper()
            if pos_dir_raw in ("LONG", "BUY"):
                exit_side = "SELL"
            elif pos_dir_raw in ("SHORT", "SELL"):
                exit_side = "BUY"
            else:
                # Fallback: use quantity sign (negative = SHORT, positive = LONG)
                raw_qty_signed = float(pos.get("quantity") or pos.get("positionAmount") or pos.get("amount") or 0)
                exit_side = "SELL" if raw_qty_signed > 0 else "BUY"

            clean_qty = round_coin_qty(clean_sym, qty)

            # FIRST cancel resting open orders so Shark Exchange releases the reserved reduce-only quantity lock
            await self.cancel_all_open_orders(session, clean_sym)
            await asyncio.sleep(0.5)

            payload = {
                "placeType": "POSITION",
                "positionId": pos_id,
                "reduceOnly": True,
                "type": "MARKET",
                "side": exit_side,
                "symbol": clean_sym,
                "quantity": clean_qty
            }
            st, res = await self.shark_post(session, "/v1/order/place-order", payload)
            if st in (200, 201):
                logger.info(f"Position for {clean_sym} closed at MARKET on Shark Exchange ({reason}).")
                
                exit_p = float(pos.get("markPrice") or pos.get("lastPrice") or pos.get("entryPrice") or 0)
                entry_p = float(pos.get("entryPrice") or pos.get("avgPrice") or 0)
                pos_qty_real = abs(float(pos.get("quantity") or pos.get("positionAmount") or pos.get("amount") or 0))
                if exit_side == "SELL":  # Was LONG position
                    pnl_usdt_close = (exit_p - entry_p) * pos_qty_real
                else:                     # Was SHORT position
                    pnl_usdt_close = (entry_p - exit_p) * pos_qty_real
                pnl_inr = pnl_usdt_close * self.usd_inr_rate
                pnl_str = f"+₹{pnl_inr:,.2f} INR" if pnl_inr >= 0 else f"-₹{abs(pnl_inr):,.2f} INR"
                
                from telegram_bot import send_text_alert
                if "TP1" in reason.upper() or "TARGET" in reason.upper() or "TAKE PROFIT" in reason.upper():
                    sign = "+" if pnl_inr >= 0 else ""
                    msg = (
                        f"🎯 <b>REAL LIVE TRADE: TAKE PROFIT HIT ✅ (100% PROFIT SECURED)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<b>Asset:</b> {clean_sym} [{pos_dir_raw}]\n"
                        f"<b>Entry Price:</b> ${format_price(entry_p, clean_sym)}\n"
                        f"<b>Exit Price (Market Target):</b> ${format_price(exit_p, clean_sym)}\n"
                        f"<b>Quantity:</b> {pos_qty_real}\n"
                        f"<b>Total Position PnL:</b> <b>{sign}₹{pnl_inr:,.2f} INR</b> ({sign}${pnl_usdt_close:.2f} USDT) 💰\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<i>100% Position Closed at 1.5R Target on Shark Exchange! Full Cash Profit Locked in Wallet.</i>"
                    )
                elif "12-HR" in reason.upper() or "STALE" in reason.upper():
                    msg = (
                        f"⏳ <b>SHARK RUNNER CLOSED (12-HR STALE TRADE RULE)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<b>Asset:</b> {clean_sym}\n"
                        f"<b>Reason:</b> 12 hours elapsed after TP1 hit without hitting TP2 or Breakeven SL.\n"
                        f"🎯 <b>Exit Price:</b> {format_price(exit_p, clean_sym)}\n"
                        f"💰 <b>Estimated PnL:</b> {pnl_str}\n"
                        f"💡 <i>Freed broker margin for fresh high-momentum setups!</i>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                else:
                    msg = (
                        f"⏱️ <b>SHARK POSITION CLOSED (20-HOUR TIMEOUT RULE)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<b>Asset:</b> {clean_sym}\n"
                        f"<b>Reason:</b> Position active for 20 hours. Squared off at live market price!\n"
                        f"🎯 <b>Entry Price:</b> {format_price(entry_p, clean_sym)}\n"
                        f"🏁 <b>Exit Price (Market):</b> {format_price(exit_p, clean_sym)}\n"
                        f"💰 <b>Estimated PnL:</b> {pnl_str}\n"
                        f"💼 <i>Margin released safely on exchange!</i>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                await self.send_alert(msg)
                return True
            else:
                logger.error(f"Failed to close position at market for {clean_sym}: {res}")
                if "exceeds remaining position size" in str(res).lower() or "3025" in str(res):
                    logger.warning(f"Position size already reduced or closed on exchange for {clean_sym}. Purging resting orders.")
                    await self.cancel_all_open_orders(session, clean_sym)
                return False
        except Exception as e:
            logger.error(f"Error in close_position_market for {clean_sym}: {e}")
            return False

    async def sweep_expired_entry_orders(self, session: aiohttp.ClientSession, max_minutes: float = 30.0) -> None:
        """
        EXCHANGE-NATIVE 30-MIN ORDER SWEEPER:
        Directly queries Shark Exchange for ALL open orders across the account.
        Identifies any non-position entry limit orders created > 30 minutes ago
        and cancels them directly via DELETE /v1/order/delete-order.
        Completely independent of local JSON state files!
        """
        if not self.is_configured() or not session:
            return

        try:
            all_orders = await self.get_open_orders(session)
            if not all_orders:
                return

            now_utc = datetime.now(timezone.utc)
            for o in all_orders:
                # Skip POSITION bracket orders (SL/TP) — we ONLY sweep un-filled ENTRY orders
                place_type = str(o.get("placeType") or "").upper()
                is_reduce_only = o.get("reduceOnly", False)
                sub_type = str(o.get("subType") or "").upper()
                order_type = str(o.get("type") or "").upper()

                # NOTE: Never check linkType in ('ORDER_SL', 'ORDER_TP') here! Primary ENTRY limit orders submitted
                # with an attached SL carry linkType='ORDER_SL'. Checking linkType caused sweeper to skip all entry orders!
                if place_type == "POSITION" or is_reduce_only or sub_type in ("STOP_LOSS", "TAKE_PROFIT") or order_type in ("STOP_MARKET", "STOP_LIMIT", "STOP"):
                    continue

                # Parse order creation time (handles numeric ms/s timestamps, ISO strings, and state fallback)
                order_time = None
                raw_time = o.get("time") or o.get("createdAt") or o.get("timestamp") or o.get("created_at")
                if raw_time is not None:
                    try:
                        raw_s = str(raw_time).strip()
                        if raw_s.replace(".", "", 1).isdigit():
                            ts_num = float(raw_s)
                            if ts_num > 1e11:  # Milliseconds
                                ts_num /= 1000.0
                            order_time = datetime.fromtimestamp(ts_num, tz=timezone.utc)
                        else:
                            clean_t_str = raw_s.replace("Z", "+00:00").replace(" ", "T")
                            order_time = datetime.fromisoformat(clean_t_str)
                            if order_time.tzinfo is None:
                                order_time = order_time.replace(tzinfo=timezone.utc)
                    except Exception as parse_err:
                        logger.debug(f"Order time parse error for raw {raw_time}: {parse_err}")

                # Fallback to state limit_placed_at if exchange order payload lacks parseable timestamp
                if order_time is None:
                    sym_fallback = clean_contract_symbol(o.get("symbol") or "")
                    if sym_fallback:
                        from state import get_latest_symbol_state
                        st = get_latest_symbol_state(sym_fallback)
                        placed_str = st.get("limit_placed_at") or st.get("sent_at")
                        if placed_str:
                            try:
                                order_time = datetime.fromisoformat(str(placed_str).replace("Z", "+00:00"))
                                if order_time.tzinfo is None:
                                    order_time = order_time.replace(tzinfo=timezone.utc)
                            except Exception:
                                pass

                if order_time is not None:
                    elapsed_mins = (now_utc - order_time).total_seconds() / 60.0
                    # USER DIRECTIVE: Strict 30-Minute Expiry on Shark Exchange
                    if elapsed_mins >= 30.0:
                        sym = clean_contract_symbol(o.get("symbol") or "")
                        cid = o.get("clientOrderId")
                        if not cid and sym in self._pending_entry_orders:
                            cid = self._pending_entry_orders[sym].get("clientOrderId")
                        
                        if cid:
                            st, res = await self.shark_delete(session, "/v1/order/delete-order", {"clientOrderId": str(cid)})
                            if st in (200, 201):
                                logger.info(f"EXCHANGE SWEEPER: Cancelled expired entry order {cid} for {sym} (Age: {elapsed_mins:.1f} mins)")
                                self._pending_entry_orders.pop(sym, None)
                                self._session_active_symbols.pop(sym, None)
                                from telegram_bot import send_text_alert
                                msg = (
                                    f"⏳ <b>SHARK AUTO-ORDER EXPIRED & CANCELLED (30-MIN LIMIT EXPIRATION)</b>\n\n"
                                    f"<b>Asset:</b> {sym}\n"
                                    f"<b>Status:</b> Entry limit not filled within {int(max_minutes)} minutes.\n"
                                    f"<b>Action:</b> ✅ Order cancelled directly on exchange with ₹0 INR loss."
                                )
                                await self.send_alert(msg)
                            else:
                                logger.warning(f"EXCHANGE SWEEPER: Shark returned {st} when cancelling {cid} for {sym}: {res}")
        except Exception as e:
            logger.error(f"Error in sweep_expired_entry_orders: {e}")

    async def sync_and_manage_live_orders(self, session: aiohttp.ClientSession) -> None:
        """
        UNIFIED LIVE EXCHANGE LIFECYCLE MONITOR:
        1. Cancels any over-cap pending entry orders (safe cleanup — never touches TP/SL).
        2. Sweeps expired entry limit orders (> 30 min).
        3. Monitors live positions & TP order counts to trigger step-trailing SL on genuine TP hits.
        4. Auto-heals missing Stop-Loss or missing TP limit orders on Shark Exchange.
        Consolidates exchange state management into a single robust method.
        """
        if not self.is_configured() or not session:
            return

        from state import get_latest_symbol_state

        try:
            # 0. Cancel any over-cap pending entry orders (SAFE — never touches TP/SL or live positions)
            await self.cancel_over_limit_pending_orders(session)

            # 1. Sweep expired entry orders
            await self.sweep_expired_entry_orders(session, max_minutes=30.0)

            # 2. Fetch live positions and orders
            positions  = await self.get_open_positions(session)
            all_orders = await self.get_open_orders(session)

            if not self._last_positions_fetch_ok or not self._last_orders_fetch_ok:
                logger.warning(
                    f"sync_and_manage_live_orders: API query failed (positions_ok={self._last_positions_fetch_ok}, "
                    f"orders_ok={self._last_orders_fetch_ok}). Skipping iteration to protect state integrity."
                )
                return

            # Sync local in-memory active symbols with live exchange positions + open orders + recent local entries
            live_syms = {clean_contract_symbol(self.clean_sym(p)) for p in positions}
            entry_syms = {clean_contract_symbol(self.clean_sym(o)) for o in all_orders if not o.get("reduceOnly") and o.get("placeType") != "POSITION"}
            live_syms.discard("")
            entry_syms.discard("")

            now_ts = time.time()
            for s_live in (live_syms | entry_syms):
                if s_live not in self._session_active_symbols:
                    self._session_active_symbols[s_live] = now_ts
            # Prune symbols older than 120s that are no longer in positions or entry orders
            for s_local, ts_val in list(self._session_active_symbols.items()):
                if s_local not in live_syms and s_local not in entry_syms and (now_ts - ts_val >= 120.0):
                    self._session_active_symbols.pop(s_local, None)

            if not positions and not all_orders:
                return

            orders_by_sym = {}
            for o in all_orders:
                s = clean_contract_symbol(self.clean_sym(o))
                if s not in orders_by_sym:
                    orders_by_sym[s] = []
                orders_by_sym[s].append(o)

            # REAL LIVE POSITION CLOSE MONITOR & NOTIFICATION
            current_syms = {clean_contract_symbol(self.clean_sym(p)) for p in positions if clean_contract_symbol(self.clean_sym(p))}
            just_closed = set(self._known_live_positions.keys()) - current_syms
            if just_closed:
                st_cl, closed_list = await self.shark_get(session, "/v1/positions/CLOSED")
                if st_cl == 200 and isinstance(closed_list, list):
                    for c_sym in just_closed:
                        matched = next((cp for cp in closed_list if clean_contract_symbol(self.clean_sym(cp)) == c_sym), None)
                        pos_id = str(matched.get("positionId") or matched.get("id") or "") if matched else ""
                        if matched and pos_id not in self._notified_closed_ids:
                            self._notified_closed_ids.add(pos_id)
                            pnl_inr = float(matched.get("realizedProfitInMarginAsset") or 0.0)
                            pnl_usd = float(matched.get("realizedProfit") or 0.0)
                            pos_dir = str(matched.get("positionType") or "SHORT").upper()
                            entry_p = float(matched.get("entryPrice") or 0.0)
                            qty = abs(float(matched.get("quantity") or matched.get("positionAmount") or 0.0))
                            from state import get_latest_symbol_state
                            st_sym = get_latest_symbol_state(c_sym)
                            tp1_was_hit = bool(st_sym and st_sym.get("tp1_hit"))
                            tp2_was_hit = bool(st_sym and st_sym.get("tp2_hit"))
                            be_shield_hit = bool(st_sym and (st_sym.get("be_hit_time_shield") or st_sym.get("be_hit")))

                            pnl_note = ""
                            is_sl_outcome = False
                            if pnl_inr < -15.0:
                                status_txt = "🛑 <b>REAL LIVE TRADE: STOP-LOSS HIT ❌</b>"
                                is_sl_outcome = True
                            elif abs(pnl_inr) <= 15.0:
                                status_txt = "🛡️ <b>REAL LIVE TRADE: BREAKEVEN EXIT (CTC) 🤝</b>"
                            elif tp2_was_hit or pnl_inr > 15.0:
                                if tp1_was_hit:
                                    status_txt = "🛡️ <b>REAL LIVE TRADE: RUNNER CLOSED (TP1 PROFIT SECURED) 🤝</b>"
                                    pnl_note = f"\n💡 <i>Breakdown: 70% profit was credited at TP1 earlier. The remaining runner closed at entry CTC. Total net profit: +₹{pnl_inr:,.2f} INR.</i>"
                                elif tp2_was_hit:
                                    status_txt = "🎯 <b>REAL LIVE TRADE: FULL TAKE PROFIT (TP2) HIT 🏆</b>"
                                else:
                                    status_txt = "🎯 <b>REAL LIVE TRADE: TAKE PROFIT HIT ✅</b>"
                            else:
                                status_txt = "🛑 <b>REAL LIVE TRADE: STOP-LOSS HIT ❌</b>"
                                is_sl_outcome = True

                            # DAILY RISK CIRCUIT BREAKER: 5 consecutive SLs -> 60-min pause
                            from state import record_trade_result
                            cb_triggered, cb_state = record_trade_result(is_sl=is_sl_outcome, symbol=c_sym, pnl=pnl_inr, pause_minutes=60, max_consecutive_sl=5)
                            if cb_triggered:
                                from telegram_bot import send_circuit_breaker_alert
                                await send_circuit_breaker_alert(cb_state["paused_until"], cb_state["consecutive_sl_count"], c_sym)

                            sign = "+" if pnl_inr >= 0 else ""
                            alert_msg = (
                                f"{status_txt}\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"<b>Asset:</b> {c_sym} [{pos_dir}]\n"
                                f"<b>Entry Price:</b> ${entry_p}\n"
                                f"<b>Quantity:</b> {qty}\n"
                                f"<b>Total Position PnL:</b> <b>{sign}₹{pnl_inr:,.2f} INR</b> ({sign}${pnl_usd:.2f} USDT)\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"<i>Broker Position Closed on Shark Exchange</i>{pnl_note}"
                            )
                            try:
                                await self.send_alert(alert_msg)
                            except Exception as e_alert:
                                logger.error(f"Failed to send close alert for {c_sym}: {e_alert}")
                        self._known_live_positions.pop(c_sym, None)
            
            self._known_live_positions = {clean_contract_symbol(self.clean_sym(p)): p for p in positions if clean_contract_symbol(self.clean_sym(p))}

            # SAFETY SHIELD: Clean up any orphan position-bound orders if position is already closed
            for s_ord, ord_list in orders_by_sym.items():
                if s_ord and s_ord not in live_syms:
                    for o_orph in ord_list:
                        if o_orph.get("placeType") == "POSITION" or o_orph.get("reduceOnly"):
                            cid = o_orph.get("clientOrderId")
                            if cid:
                                logger.info(f"Purging orphan position-bound order for closed symbol {s_ord}: {cid}")
                                await self.shark_delete(session, "/v1/order/delete-order", {"clientOrderId": str(cid)})

            for pos in positions:
                sym = clean_contract_symbol(self.clean_sym(pos))
                if not sym:
                    continue

                entry_price = float(pos.get("entryPrice") or pos.get("avgPrice") or 0)
                pos_qty     = float(pos.get("quantity")   or pos.get("positionAmount") or pos.get("amount") or 0)

                # Load state entry early for fallback entry price and metadata
                state_entry = get_latest_symbol_state(sym)

                if entry_price <= 0:
                    pos_sz = abs(float(pos.get("positionSize") or pos.get("size") or 0))
                    pos_amt = abs(pos_qty)
                    if pos_sz > 0 and pos_amt > 0:
                        entry_price = round(pos_sz / pos_amt, 6)
                    elif state_entry:
                        entry_price = float(state_entry.get("entry_price") or state_entry.get("price") or 0)
                
                # Authoritative direction detection: Exchange position fields > Qty sign > State fallback
                pos_dir_raw = str(pos.get("positionType") or pos.get("positionSide") or pos.get("side") or pos.get("type") or "").upper()
                if pos_dir_raw in ("LONG", "BUY"):
                    direction = "BUY"
                elif pos_dir_raw in ("SHORT", "SELL"):
                    direction = "SELL"
                elif pos_qty < 0:
                    direction = "SELL"
                elif pos_qty > 0:
                    direction = "BUY"
                else:
                    st_heal = state_entry or get_latest_symbol_state(sym)
                    st_sig = str(st_heal.get("direction") or st_heal.get("signal") or "").upper()
                    direction = "SELL" if st_sig in ("SHORT", "SELL") else "BUY"

                if entry_price <= 0 or pos_qty == 0:
                    continue

                sym_orders = orders_by_sym.get(sym, [])

                # Check 20-Hour Position Timeout and 12-Hour Stale Runner on Real Exchange
                now_utc = datetime.now(timezone.utc)
                pos_time = None
                raw_pos_t = pos.get("createdAt") or pos.get("time") or (state_entry.get("limit_filled_at") if state_entry else None) or (state_entry.get("created_at") if state_entry else None)
                if raw_pos_t:
                    try:
                        raw_s = str(raw_pos_t).strip().replace("Z", "+00:00").replace(" ", "T")
                        if raw_s.replace(".", "", 1).isdigit():
                            ts_n = float(raw_s)
                            if ts_n > 1e11: ts_n /= 1000.0
                            pos_time = datetime.fromtimestamp(ts_n, tz=timezone.utc)
                        else:
                            pos_time = datetime.fromisoformat(raw_s)
                            if pos_time.tzinfo is None: pos_time = pos_time.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                # 1. 20-Hour Position Timeout
                if pos_time and (now_utc - pos_time).total_seconds() >= 20 * 3600:
                    logger.warning(f"⏱️ 20-Hour Timeout reached for {sym} on Shark Exchange. Squaring off position at market.")
                    await self.close_position_market(session, sym, reason="20-HOUR TIMEOUT")
                    continue

                # 2. 12-Hour Stale Runner Rule (if TP1 was hit > 12 hours ago)
                if state_entry and state_entry.get("tp1_hit") and state_entry.get("tp1_hit_at"):
                    try:
                        tp1_t = datetime.fromisoformat(str(state_entry.get("tp1_hit_at")).replace("Z", "+00:00"))
                        if tp1_t.tzinfo is None: tp1_t = tp1_t.replace(tzinfo=timezone.utc)
                        if (now_utc - tp1_t).total_seconds() >= 12 * 3600:
                            logger.warning(f"⏳ 12-Hour Stale Runner reached for {sym} on Shark Exchange. Squaring off runner at market.")
                            await self.close_position_market(session, sym, reason="12-HR STALE RUNNER")
                            continue
                    except Exception:
                        pass

                # Count TP orders (Native position TP brackets & reduceOnly position limit orders)
                tp_orders = [
                    o for o in sym_orders
                    if (
                        (o.get("type", "").upper() == "LIMIT" and o.get("placeType") == "POSITION")
                        or o.get("subType") == "TAKE_PROFIT"
                        or (o.get("reduceOnly") and o.get("type", "").upper() == "LIMIT")
                    ) and str(o.get("placeType") or "").upper() != "ORDER_FORM"
                ]

                # Count SL orders (Native position SL brackets)
                sl_orders = [
                    o for o in sym_orders
                    if (
                        o.get("type", "").upper() in ("STOP_MARKET", "STOP_LIMIT", "STOP")
                        or o.get("subType") == "STOP_LOSS"
                        or (o.get("placeType") == "POSITION" and o.get("stopLossPrice"))
                    ) and not (
                        str(o.get("placeType") or "").upper() == "ORDER_FORM"
                        and str(o.get("type") or "").upper() == "LIMIT"
                    )
                ]
                tp_qty_field = float(pos.get("takeProfitOrdersQty") or 0)
                current_tp_count = len(tp_orders)

                # Always define pos_qty_abs and live mark_p here to avoid UnboundLocalError in SL/TP guard sections
                pos_qty_abs = abs(pos_qty)

                mark_p = 0.0
                for o_m in sym_orders:
                    mp_cand = float(o_m.get("curMarketPrice") or 0)
                    if mp_cand > 0:
                        mark_p = mp_cand
                        break
                if mark_p <= 0:
                    try:
                        from web_dashboard import fetch_live_market_price
                        mark_p = await fetch_live_market_price(session, sym)
                    except Exception:
                        pass
                if mark_p <= 0:
                    mark_p = float(pos.get("markPrice") or pos.get("lastPrice") or entry_price)

                # 3. 2-Hour Time Shield: DISABLED per user directive
                # Previously, moving SL to Breakeven after 2 hours suffocated 1H/4H swing positions before they had time to reach TP1.
                # Now, structural SL (below swing low / EMA 26) is kept intact until TP1 (70%) is genuinely hit!

                # Ensure state entry is loaded
                if not state_entry:
                    state_entry = get_latest_symbol_state(sym)

                # Seed cache on first run
                is_first_run = sym not in self._tp_order_count_cache
                if is_first_run:
                    self._tp_order_count_cache[sym] = current_tp_count
                    prev_tp_count = current_tp_count
                    logger.info(f"TP Monitor [INIT]: {sym} seeded cleanly at {current_tp_count} TPs live.")

                    # SL Protection on reboot/startup: ensure SL is moved ONLY if state explicitly recorded a TP hit
                    if pos_qty_abs > 0 and state_entry:
                        if state_entry.get("tp2_hit") and state_entry.get("tp1"):
                            target_sl = float(state_entry.get("tp1"))
                            if (direction == "BUY" and target_sl < entry_price) or (direction == "SELL" and target_sl > entry_price):
                                logger.info(f"TP Monitor [INIT]: {sym} TP2 hit in state. Ensuring SL is at TP1 ({target_sl}) on exchange.")
                                await self.modify_stop_loss(session, sym, direction, target_sl, pos_qty_abs, is_trailed=True)
                        elif state_entry.get("tp1_hit"):
                            target_sl = float(state_entry.get("initial_sl") or entry_price)
                            if (direction == "BUY" and target_sl <= entry_price) or (direction == "SELL" and target_sl >= entry_price):
                                logger.info(f"TP Monitor [INIT]: {sym} TP1 hit in state. Ensuring SL is at CTC ({target_sl}) on exchange.")
                                await self.modify_stop_loss(session, sym, direction, target_sl, pos_qty_abs, is_trailed=True)
                else:
                    prev_tp_count = self._tp_order_count_cache[sym]

                tps_hit = max(0, prev_tp_count - current_tp_count)

                # Load TP prices from state or fallback estimation
                tp1 = tp2 = tp3 = None
                if state_entry:
                    tp1 = state_entry.get("tp1")
                    tp2 = state_entry.get("tp2")
                    tp3 = state_entry.get("tp3")

                live_sl_order_p = float(sl_orders[0].get("stopLossPrice") or sl_orders[0].get("stopPrice") or sl_orders[0].get("price") or 0) if sl_orders else 0
                initial_sl_val = live_sl_order_p or (float(state_entry.get("initial_sl") or 0) if state_entry else 0)
                risk_step = abs(entry_price - initial_sl_val) if (initial_sl_val > 0 and initial_sl_val != entry_price) else (entry_price * 0.015)

                if not tp1 or not tp2 or not tp3:
                    if direction == "BUY":
                        tp1 = tp1 or round_price(sym, entry_price + 1.5 * risk_step)
                        tp2 = tp2 or round_price(sym, entry_price + 5.0 * risk_step)
                        tp3 = tp2
                    else:
                        tp1 = tp1 or round_price(sym, entry_price - 1.5 * risk_step)
                        tp2 = tp2 or round_price(sym, entry_price - 5.0 * risk_step)
                        tp3 = tp2

                # Active Trade TP1 Re-Alignment: Ensure TP1 has minimum target floor (2.2R for Breakdown continuation, 1.5R standard)
                target_r = 1.5
                if state_entry:
                    target_r = float(state_entry.get("target_r") or (2.2 if "1H_RangeBreak" in str(state_entry.get("unique_key", "")) or "Squeeze" in str(state_entry.get("unique_key", "")) else 1.5))

                if not (state_entry and state_entry.get("tp1_hit")):
                    if direction == "BUY":
                        if not tp1 or tp1 < (entry_price + (target_r - 0.02) * risk_step):
                            new_tp1 = round_price(sym, entry_price + target_r * risk_step)
                            logger.info(f"🎯 MINIMUM {target_r}R ENFORCED [BUY]: {sym} TP1 {tp1} brought to minimum {target_r}R: {new_tp1}")
                            tp1 = new_tp1
                            from state import _load, _save
                            st_map = _load()
                            for k_st in list(st_map.keys()):
                                if to_shark_symbol(k_st.split("|")[0]) == sym and st_map[k_st].get("shark_order_placed"):
                                    st_map[k_st]["tp1"] = tp1
                                    _save(st_map)
                                    break
                    else:
                        if not tp1 or tp1 > (entry_price - (target_r - 0.02) * risk_step):
                            new_tp1 = round_price(sym, entry_price - target_r * risk_step)
                            logger.info(f"🎯 MINIMUM {target_r}R ENFORCED [SELL]: {sym} TP1 {tp1} brought to minimum {target_r}R: {new_tp1}")
                            tp1 = new_tp1
                            from state import _load, _save
                            st_map = _load()
                            for k_st in list(st_map.keys()):
                                if to_shark_symbol(k_st.split("|")[0]) == sym and st_map[k_st].get("shark_order_placed"):
                                    st_map[k_st]["tp1"] = tp1
                                    _save(st_map)
                                    break

                # ── Strict Fill Verification & False TP1 Auto-Correction Shield ───────────────
                orig_qty = float(pos.get("primaryOrdersQty") or (state_entry.get("total_qty") if state_entry else 0) or pos_qty_abs)
                realized_pnl_inr = float(pos.get("realizedProfitInMarginAsset") or 0.0)
                price_at_tp1 = False
                if tp1 and mark_p > 0:
                    price_at_tp1 = (direction == "BUY" and mark_p >= tp1 * 0.999) or (direction == "SELL" and mark_p <= tp1 * 1.001)

                # ── SMART MAKER-FIRST HYBRID TP EXECUTION (FEES MINIMIZATION) ────────
                # 1. Primary: Native LIMIT order sits on the exchange orderbook for lowest MAKER fees!
                # 2. Maker Grace Window (30s): When price reaches TP1, we give the exchange orderbook
                #    up to 30 seconds to fill natively as Maker, saving commissions.
                # 3. Reversal Protection: If price reaches TP1 and then starts reversing back towards entry,
                #    OR if 30s elapse without the LIMIT order filling, we trigger Market Fallback to lock profit.
                if tp1 and mark_p > 0 and pos_qty_abs > 0:
                    price_reached_tp = (direction == "BUY" and mark_p >= tp1 * 0.999) or (direction == "SELL" and mark_p <= tp1 * 1.001)
                    if price_reached_tp:
                        if sym not in self._tp_touch_timer:
                            self._tp_touch_timer[sym] = time.time()
                            logger.info(f"🎯 MAKER TP WATCH: {sym} mark price {mark_p} reached TP1 {tp1}. Waiting for Maker LIMIT fill...")

                        elapsed = time.time() - self._tp_touch_timer[sym]
                        should_fallback_market = False

                        # Condition A: 30s Grace Period expired without LIMIT fill
                        if elapsed >= 30.0:
                            logger.warning(f"⚠️ {sym} LIMIT TP order did not fill after {elapsed:.1f}s on exchange. Activating Market Fallback to lock profit!")
                            should_fallback_market = True
                        
                        if should_fallback_market:
                            logger.info(
                                f"🎯 TP GUARD FALLBACK: Executing 100% MARKET profit booking for {sym} [{direction}] (qty={pos_qty_abs})."
                            )
                            closed_ok = await self.close_position_market(session, sym, reason="TP1 HIT (1.5R TARGET)")
                            if closed_ok:
                                self._tp_touch_timer.pop(sym, None)
                                if state_entry:
                                    state_entry["tp1_hit"] = True
                                    state_entry["outcome"] = "TP1_HIT_100_PERCENT"
                                    from state import _load, _save
                                    st_map = _load()
                                    for k_st in list(st_map.keys()):
                                        if to_shark_symbol(k_st.split("|")[0]) == sym and st_map[k_st].get("shark_order_placed"):
                                            st_map[k_st]["tp1_hit"] = True
                                            st_map[k_st]["outcome"] = "TP1_HIT_100_PERCENT"
                                            _save(st_map)
                                            break
                                self._tp_order_count_cache.pop(sym, None)
                                self._known_live_positions.pop(sym, None)
                                continue
                    else:
                        # Price is not at TP1 — if it previously touched and is now pulling back > 0.4%, close to protect profit
                        if sym in self._tp_touch_timer:
                            touch_age = time.time() - self._tp_touch_timer[sym]
                            is_reversing = (direction == "BUY" and mark_p < tp1 * 0.994) or (direction == "SELL" and mark_p > tp1 * 1.006)
                            if is_reversing and touch_age > 10.0:
                                logger.warning(f"⚠️ {sym} touched TP1 earlier but is now reversing back! Protecting profit via Market Exit.")
                                closed_ok = await self.close_position_market(session, sym, reason="TP1 REVERSAL PROTECTION")
                                if closed_ok:
                                    self._tp_touch_timer.pop(sym, None)
                                    self._tp_order_count_cache.pop(sym, None)
                                    self._known_live_positions.pop(sym, None)
                                    continue
                            elif not is_reversing and touch_age > 120.0:
                                self._tp_touch_timer.pop(sym, None)

                # Self-Healing False TP1 State Correction:
                # If state mistakenly claims tp1_hit=True, but position on exchange is still full size (pos_qty_abs >= orig_qty * 0.85)
                # and price has not reached TP1 and no profit realized, reset tp1_hit to False!
                if state_entry and state_entry.get("tp1_hit"):
                    if pos_qty_abs >= (orig_qty * 0.85) and realized_pnl_inr <= 0 and not price_at_tp1:
                        logger.warning(
                            f"🛡️ FALSE TP1 RECOVERY: {sym} had tp1_hit=True in state, but exchange position is still full size "
                            f"({pos_qty_abs}/{orig_qty}) and mark price ({mark_p}) has not reached TP1 ({tp1}). Resetting tp1_hit=False!"
                        )
                        state_entry["tp1_hit"] = False
                        state_entry.pop("tp1_hit_at", None)
                        from state import _load, _save
                        st_map = _load()
                        for k_st in list(st_map.keys()):
                            if to_shark_symbol(k_st.split("|")[0]) == sym and st_map[k_st].get("shark_order_placed"):
                                st_map[k_st]["tp1_hit"] = False
                                st_map[k_st].pop("tp1_hit_at", None)
                                _save(st_map)
                                break

                # Fire step-trailing SL on genuine TP hit transitions (supports both 70/30 2-TP and legacy 3-TP)
                if tps_hit > 0:
                    logger.info(f"🎯 TP MONITOR: {sym} [{direction}] TPs {prev_tp_count}→{current_tp_count}")

                    # STRICT TRIPLE-VERIFICATION FOR TP1:
                    # 1. Price MUST have reached TP1
                    # 2. Position quantity on exchange MUST have reduced (pos_qty_abs <= orig_qty * 0.60) OR broker confirmed realized profit > 0
                    is_genuine_tp1_fill = (price_at_tp1 and (pos_qty_abs <= orig_qty * 0.60 or realized_pnl_inr > 0))

                    # 1. TP1 Hit: 70% Booked (transition 2→1 in 70/30 model, or 3→2 in legacy)
                    if (not state_entry or not state_entry.get("tp1_hit")) and current_tp_count >= 1:
                        if is_genuine_tp1_fill:
                            logger.info(f"  → TP1 HIT CONFIRMED (70% Booked on Exchange, Qty {orig_qty}→{pos_qty_abs}): trailing SL to CTC")
                            await self.update_trailing_sl_on_tp_hit(
                                session, sym, "TP1", entry_price, tp1, tp2, tp3)
                        else:
                            logger.info(
                                f"  → TP order count shifted for {sym} ({prev_tp_count}→{current_tp_count}) "
                                f"but NOT confirmed by exchange (Pos: {pos_qty_abs}/{orig_qty}, Mark: {mark_p}, TP1: {tp1}, RealizedPnL: {realized_pnl_inr}). "
                                f"Ignoring false trigger (orders refreshing/re-healing)."
                            )

                    # 2. Final Exit: 0 TPs remaining
                    elif current_tp_count == 0:
                        if pos_qty_abs == 0:
                            logger.info(f"  → FULL WIN: position closed on exchange, full trade complete")
                            await self.update_trailing_sl_on_tp_hit(
                                session, sym, "TP2", entry_price, tp1, tp2, tp2)
                        else:
                            logger.warning(f"⚠️ {sym}: 0 TPs while position is STILL OPEN (qty={pos_qty_abs}) — auto-healing TP limit order now!")
                            st_heal = get_latest_symbol_state(sym)
                            sl_val = float(st_heal.get("cur_sl") or st_heal.get("initial_sl") or entry_price)
                            await self.place_tp_limits(
                                session, sym, direction, entry_price, tp1, tp2, tp3, initial_sl=sl_val)

                # ── USER DIRECTIVE: Fixed 1.5R TP based on SL. Trailing ONLY occurs if a NEW continuation signal arrives for the same coin (handled in trail_position_on_new_signal).
                # No artificial automated price-ladder pushing TP away, ensuring clean 100% profit booking at target!

                # SL GUARD: Ensure SL exists and enforces safe liquidation floor & distance for live positions
                pos_qty_abs = abs(pos_qty)
                live_liq = float(pos.get("liquidationPrice") or pos.get("liqPrice") or pos.get("forceClosePrice") or 0)
                from indicators import get_min_sl_percent
                min_sl_pct = get_min_sl_percent(sym)

                # Default fallback threshold
                safe_sl_threshold = enforce_sl_above_liquidation(sym, entry_price, entry_price * (0.97 if direction == "BUY" else 1.03), direction, self.leverage, live_liq)

                # 1. Check if SL order is completely MISSING on exchange for an active position:
                if len(sl_orders) == 0 and pos_qty_abs > 0:
                    last_heal = self._tp_sl_heal_lock.get(sym + "_sl", 0)
                    if time.time() - last_heal > 15:  # immediate protection, max once per 15s
                        logger.warning(f"🚨 SL MISSING on Shark for active position {sym} (Qty: {pos_qty_abs}) — placing emergency SL immediately!")
                        st_heal = get_latest_symbol_state(sym)
                        raw_sl = float(st_heal.get("cur_sl") or st_heal.get("initial_sl") or safe_sl_threshold)
                        safe_sl = enforce_sl_above_liquidation(sym, entry_price, raw_sl, direction, self.leverage, live_liq)
                        await self.modify_stop_loss(session, sym, direction, safe_sl, pos_qty_abs)
                        self._tp_sl_heal_lock[sym + "_sl"] = time.time()

                # 2. Check if existing SL order is beyond liquidation or too tight:
                elif sl_orders and pos_qty_abs > 0:
                    # Check if existing SL order quantity is misaligned with live position (scale-in or partial TP fills)
                    total_sl_qty = sum(self.extract_order_qty(o) for o in sl_orders)
                    if total_sl_qty > 0 and (total_sl_qty < (pos_qty_abs * 0.90) or total_sl_qty > (pos_qty_abs * 1.10)):
                        last_heal = self._tp_sl_heal_lock.get(sym + "_sl_scalein", 0)
                        if time.time() - last_heal > 60:
                            logger.warning(f"🛡️ POSITION SL RESIZE: Live position {sym} is {pos_qty_abs} but SL order covers {total_sl_qty}. Resizing SL to match exact remaining position size!")
                            st_heal = get_latest_symbol_state(sym)
                            sl_p_cur = float(sl_orders[0].get("stopLossPrice") or sl_orders[0].get("price") or st_heal.get("cur_sl") or safe_sl_threshold)
                            safe_sl = enforce_sl_above_liquidation(sym, entry_price, sl_p_cur, direction, self.leverage, live_liq)
                            await self.modify_stop_loss(session, sym, direction, safe_sl, pos_qty_abs)
                            self._tp_sl_heal_lock[sym + "_sl_scalein"] = time.time()

                    for o_sl in sl_orders:
                        sl_p_raw = float(o_sl.get("stopLossPrice") or o_sl.get("stopPrice") or o_sl.get("price") or 0)
                        if sl_p_raw > 0:
                            # Strict Liquidation Check: Is SL on or beyond liquidation price?
                            is_beyond_liq = (direction == "BUY" and live_liq > 0 and sl_p_raw <= live_liq) or                                             (direction == "SELL" and live_liq > 0 and sl_p_raw >= live_liq)
                            # Noise check: Is SL too tight on an UNTRAILED / FRESH trade?
                            # IMPORTANT: If TP1 has already hit, or trade is at Cost-to-Cost / Breakeven, do NOT touch SL!
                            is_trailed = bool(state_entry and (state_entry.get("tp1_hit") or (state_entry.get("trail_stage") or 0) > 0 or state_entry.get("be_hit_time_shield")))
                            is_at_or_in_profit_sl = (direction == "BUY" and sl_p_raw >= entry_price) or (direction == "SELL" and sl_p_raw <= entry_price)
                            is_protected = is_trailed or is_at_or_in_profit_sl
                            min_dist_threshold = round_price(sym, entry_price * (1.0 - min_sl_pct if direction == "BUY" else 1.0 + min_sl_pct))
                            is_too_tight = not is_protected and ((direction == "BUY" and sl_p_raw > min_dist_threshold) or (direction == "SELL" and sl_p_raw < min_dist_threshold))

                            if is_beyond_liq:
                                logger.critical(f"🚨 DANGER: SL {sl_p_raw} for {sym} [{direction}] is beyond liquidation {live_liq}! Upgrading SL above liquidation now!")
                                fixed_sl = enforce_sl_above_liquidation(sym, entry_price, sl_p_raw, direction, self.leverage, live_liq)
                                await self.modify_stop_loss(session, sym, direction, fixed_sl, pos_qty_abs)
                                break
                            elif is_too_tight:
                                last_heal = self._tp_sl_heal_lock.get(sym + "_sl_adjust", 0)
                                if time.time() - last_heal > 60:
                                    logger.info(f"🛡️ AUTO-HEAL: Upgrading SL for active position {sym} [{direction}] from {sl_p_raw} to safe floor {min_dist_threshold}")
                                    await self.modify_stop_loss(session, sym, direction, min_dist_threshold, pos_qty_abs)
                                    self._tp_sl_heal_lock[sym + "_sl_adjust"] = time.time()
                                    break

                # TP HEAL & SCALE-IN: Re-place 100% 1-TP if missing or undersized
                total_tp_qty = sum(self.extract_order_qty(o) for o in tp_orders)
                tp1_already_hit = bool(state_entry and state_entry.get("tp1_hit"))
                is_tp_undersized = (not tp1_already_hit and pos_qty_abs > 0 and current_tp_count > 0 and total_tp_qty > 0 and total_tp_qty < (pos_qty_abs * 0.95))

                # Realign distant TP strictly ONCE per position to avoid infinite cancellation/replacement loops
                # Target floor: 2.2R for Breakdown continuation, 1.5R for standard
                realigned_key = sym + "_tp1_realigned"
                has_distant_tp1 = False
                st_heal = get_latest_symbol_state(sym)
                is_continuation_trailed = bool(
                    (st_heal and st_heal.get("continuation_trailed"))
                    or self._tp_sl_heal_lock.get(sym + "_continuation_trail")
                )
                target_r = 1.5
                if st_heal:
                    target_r = _resolve_target_r(st_heal)

                if not tp1_already_hit and pos_qty_abs > 0 and tp_orders and not is_continuation_trailed and not self._tp_sl_heal_lock.get(realigned_key):
                    valid_tp_prices = [float(o.get("price") or 0) for o in tp_orders if float(o.get("price") or 0) > 0]
                    if valid_tp_prices:
                        if direction == "BUY":
                            nearest_tp = min(valid_tp_prices)
                            if nearest_tp > (entry_price + (target_r + 0.15) * risk_step):
                                has_distant_tp1 = True
                        else:
                            nearest_tp = max(valid_tp_prices)
                            if nearest_tp < (entry_price - (target_r + 0.15) * risk_step):
                                has_distant_tp1 = True

                should_heal_tp = (current_tp_count == 0 or is_tp_undersized or has_distant_tp1) and pos_qty_abs > 0 and tps_hit == 0 and not tp1_already_hit
                if should_heal_tp:
                    last_heal = self._tp_sl_heal_lock.get(sym + "_tp", 0)
                    never_healed = last_heal == 0  # Fresh fill — first time we see 0 TPs
                    if never_healed or (time.time() - last_heal > 60):
                        trailed_tp_val = float((st_heal.get("tp1") if st_heal else 0) or 0)
                        if is_continuation_trailed and trailed_tp_val > 0:
                            calc_tp1 = round_price(sym, trailed_tp_val)
                        else:
                            calc_tp1 = round_price(sym, entry_price + target_r * risk_step if direction == "BUY" else entry_price - target_r * risk_step)
                        reason_lbl = f"{target_r}R realignment" if has_distant_tp1 else ("scale-in expansion" if is_tp_undersized else ("missing TP heal" if current_tp_count == 0 else "re-heal"))
                        logger.warning(f"⚠️ {sym}: {reason_lbl} for qty={pos_qty_abs} — enforcing TP limits now (TP1={calc_tp1})")
                        st_heal = get_latest_symbol_state(sym)
                        sl_from_order = float(sl_orders[0].get("stopLossPrice") or sl_orders[0].get("stopPrice") or sl_orders[0].get("price") or 0) if sl_orders else 0
                        sl_val = sl_from_order or float((st_heal.get("cur_sl") or st_heal.get("initial_sl") or entry_price) if st_heal else entry_price)
                        await self.place_tp_limits(
                            session, sym, direction, entry_price, calc_tp1, tp2, tp3, initial_sl=sl_val)
                        self._tp_sl_heal_lock[sym + "_tp"] = time.time()
                        if has_distant_tp1:
                            self._tp_sl_heal_lock[realigned_key] = True
                        if is_tp_undersized:
                            from state import update_latest_shark_state
                            update_latest_shark_state(sym, {"tp1_hit": False, "total_qty": pos_qty_abs})

                self._tp_order_count_cache[sym] = current_tp_count

        except Exception as e:
            logger.error(f"Error in sync_and_manage_live_orders: {e}")

    async def update_trailing_sl_on_tp_hit(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        tp_stage: str,
        entry: float,
        tp1: float,
        tp2: float,
        tp3: float = None
    ) -> None:
        """
        Elite 70/30 Profit Booking Engine:
        - TP1 Hit: 70% Profit Booked. Moves SL to +0.4R Guaranteed Profit Floor for remaining 30% runner.
        - TP3 Hit: Remaining 30% Runner Booked (Full Win). Cancels remaining SL.
        Uses REAL Shark Exchange position data for accurate PnL reporting.
        """
        clean_sym = clean_contract_symbol(symbol)

        # --- Fetch REAL position data from Shark Exchange ---
        real_qty        = 0.0
        real_entry      = entry
        real_unreal_pnl = 0.0
        live_liq        = 0.0
        try:
            pos = await self.get_open_position(session, clean_sym)
            if pos:
                real_qty        = abs(float(pos.get("quantity") or pos.get("positionAmount") or pos.get("amount") or 0))
                real_entry      = float(pos.get("entryPrice") or pos.get("avgPrice") or entry)
                real_unreal_pnl = float(pos.get("unrealizedPnl") or pos.get("unrealizedProfit") or 0)
                live_liq        = float(pos.get("liquidationPrice") or pos.get("liqPrice") or pos.get("forceClosePrice") or 0)
                # Authoritative direction from LIVE position (not from tp1/entry comparison which is fragile)
                pos_dir_raw = str(pos.get("positionType") or pos.get("positionSide") or pos.get("side") or pos.get("type") or "").upper()
                if pos_dir_raw in ("LONG", "BUY"):
                    direction = "BUY"
                elif pos_dir_raw in ("SHORT", "SELL"):
                    direction = "SELL"
                else:
                    # Fallback: quantity sign or tp1/entry comparison
                    raw_qty_signed = float(pos.get("quantity") or pos.get("positionAmount") or 0)
                    direction = "BUY" if raw_qty_signed > 0 else ("SELL" if raw_qty_signed < 0 else ("BUY" if tp1 > entry else "SELL"))
        except Exception as e:
            logger.warning(f"Could not fetch live position for {clean_sym}: {e}")
            direction = "BUY" if tp1 > entry else "SELL"

        from state import get_latest_symbol_state, _load, _save
        state_entry = get_latest_symbol_state(clean_sym)
        initial_sl = float(state_entry.get("initial_sl") or 0) if state_entry else 0

        initial_qty, tp1_qty, _, tp3_qty, _, _ = self.calculate_position_size(clean_sym, real_entry, sl_price=initial_sl)
        if initial_qty <= 0:
            initial_qty = real_qty if real_qty > 0 else 1.0

        # USER DIRECTIVE: 100% Full Profit Booking at TP1 (No 70/30, No Runner Retracement)
        tp1_qty = initial_qty
        tp3_qty = 0.0

        def calc_pnl(qty, exit_px, entry_px, side):
            """Real PnL = qty × (exit - entry) for LONG, qty × (entry - exit) for SHORT"""
            if side == "BUY":
                return qty * (exit_px - entry_px)
            else:
                return qty * (entry_px - exit_px)

        target_tp3 = tp3 if tp3 and tp3 > 0 else (tp2 if tp2 and tp2 > 0 else tp1)

        if tp_stage == "TP1":
            # 1. 70% Profit Booked at TP1
            realized_inr = float(pos.get("realizedProfitInMarginAsset") or 0.0) if pos else 0.0
            realized_usd = float(pos.get("realizedProfit") or 0.0) if pos else 0.0
            if realized_inr > 0:
                pnl_inr = realized_inr
                pnl_usdt = realized_usd if realized_usd > 0 else (realized_inr / self.usd_inr_rate)
            else:
                pnl_usdt = calc_pnl(tp1_qty, tp1, real_entry, direction)
                pnl_inr  = pnl_usdt * self.usd_inr_rate
            runner_qty = real_qty if (real_qty > 0 and real_qty < initial_qty * 0.85) else tp3_qty

            # 2. Shift SL to Cost-to-Cost (+0.4% / +0.4R Guaranteed Profit Floor)
            risk = abs(real_entry - initial_sl) if (initial_sl and initial_sl != real_entry) else (real_entry * 0.012)
            profit_lock_dist = max(real_entry * 0.0040, 0.40 * risk)
            if direction == "BUY":
                # Entry + 0.40% / +0.4R Guaranteed Profit Floor (covers exchange taker fees and locks profit)
                target_sl = real_entry + profit_lock_dist
                trailed_sl = round_price(clean_sym, min(target_sl, tp1 * 0.998))
            else:
                # Entry - 0.40% / +0.4R Guaranteed Profit Floor (covers exchange taker fees and locks profit)
                target_sl = real_entry - profit_lock_dist
                trailed_sl = round_price(clean_sym, max(target_sl, tp1 * 1.002))

            # Clamp with Liquidation Shield
            safe_trailed_sl = enforce_sl_above_liquidation(clean_sym, real_entry, trailed_sl, direction, self.leverage, live_liq, is_trailed=True)

            # 3. Modify Stop-Loss order on Shark Exchange
            if runner_qty > 0:
                logger.info(f"🎯 TP1 HIT: Modifying SL for {clean_sym} to CTC (+0.4% Profit Floor) {safe_trailed_sl} (Runner qty={runner_qty})")
                await self.modify_stop_loss(session, clean_sym, direction, safe_trailed_sl, runner_qty, is_trailed=True)
            else:
                logger.info(f"🎯 100% TP HIT: Position fully closed for {clean_sym}. Cleaning up resting SL orders.")
                try:
                    open_orders = await self.get_open_orders(session, symbol=clean_sym)
                    for o in open_orders:
                        cid = o.get("clientOrderId")
                        if cid and ("sl" in cid.lower() or "stop" in cid.lower()):
                            await self.cancel_order(session, symbol=clean_sym, client_order_id=cid)
                except Exception as e_c:
                    logger.warning(f"Error cleaning SL after 100% TP hit for {clean_sym}: {e_c}")

            # 4. Update persistent state
            st_map = _load()
            for k_st in list(st_map.keys()):
                if to_shark_symbol(k_st.split("|")[0]) == clean_sym and st_map[k_st].get("shark_order_placed"):
                    st_map[k_st]["tp1_hit"] = True
                    st_map[k_st]["cur_sl"] = safe_trailed_sl
                    st_map[k_st]["trail_stage"] = 0
                    st_map[k_st]["tp1_hit_at"] = datetime.now(timezone.utc).isoformat()
                    _save(st_map)
                    break

            bal_summary = await self.get_wallet_balance_summary(session)
            bal_block = f"\n\n{bal_summary}" if bal_summary else ""
            conf_tag = " [Exchange Verified]" if realized_inr > 0 else ""

            target_r = 1.5
            if state_entry:
                target_r = float(state_entry.get("target_r") or (2.2 if "1H_RangeBreak" in str(state_entry.get("unique_key", "")) or "Squeeze" in str(state_entry.get("unique_key", "")) else 1.5))
            target_r_str = f"{target_r:.1f}R" if abs(target_r - 1.5) > 0.1 else "1.5R"

            if runner_qty <= 0:
                msg = (
                    f"🎯 <b>REAL LIVE TRADE: TARGET ({target_r_str}) HIT ✅ (100% PROFIT BOOKED)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {clean_sym} [{direction}]\n"
                    f"<b>Entry:</b> {format_price(real_entry)}  →  <b>Target:</b> {format_price(tp1)}\n"
                    f"<b>Realized Profit:</b> +${pnl_usdt:.2f} USDT (+₹{pnl_inr:,.0f} INR){conf_tag} 💰\n"
                    f"<b>Status:</b> 100% position closed. Lingering SL cancelled.{bal_block}"
                )
            else:
                msg = (
                    f"🎯 <b>REAL LIVE TRADE: TP1 HIT ✅ (70% PROFIT SECURED)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> {clean_sym} [{direction}]\n"
                    f"<b>Entry:</b> {format_price(real_entry)}  →  <b>TP1:</b> {format_price(tp1)}\n"
                    f"<b>Realized 70% Profit:</b> +${pnl_usdt:.2f} USDT (+₹{pnl_inr:,.0f} INR){conf_tag} 💰\n"
                    f"🛡️ <b>SL Trailed to Cost-to-Cost (+0.4% Profit Floor):</b> {format_price(safe_trailed_sl)}\n"
                    f"🚀 <b>Remaining 30% Runner ({runner_qty} qty):</b> Aiming for TP2 {format_price(target_tp3)}!\n"
                    f"<i>Trade is now 100% Zero-Risk. Runner trailing for full trend capture!</i>{bal_block}"
                )
            await self.send_alert(msg)

        elif tp_stage in ("TP2", "TP3", "FULL_WIN"):
            pnl_tp3_usdt = calc_pnl(tp3_qty, target_tp3, real_entry, direction)
            pnl_tp1_usdt = calc_pnl(tp1_qty, tp1, real_entry, direction)
            total_pnl_usdt = pnl_tp1_usdt + pnl_tp3_usdt
            total_pnl_inr = total_pnl_usdt * self.usd_inr_rate

            bal_summary = await self.get_wallet_balance_summary(session)
            bal_block = f"\n\n{bal_summary}" if bal_summary else ""
            msg = (
                f"🏆 <b>REAL LIVE TRADE: TP2 RUNNER HIT — FULL WIN! 🚀</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Asset:</b> {clean_sym} [{direction}]\n"
                f"<b>Entry:</b> {format_price(real_entry)}  →  <b>Target:</b> {format_price(target_tp3)}\n"
                f"<b>Total Trade Realized Profit:</b> +${total_pnl_usdt:.2f} USDT (+₹{total_pnl_inr:,.0f} INR) 💰🚀\n"
                f"<b>Status:</b> 100% position closed. Lingering SL cancelled.{bal_block}"
            )
            logger.info(f"FULL WIN {clean_sym}: TP2 runner hit. Total PnL +${total_pnl_usdt:.2f}")

            # Cancel remaining open orders (SL) — Shark DELETE only accepts clientOrderId, never orderId
            try:
                open_orders = await self.get_open_orders(session, symbol=clean_sym)
                for o in open_orders:
                    cid = o.get("clientOrderId")
                    if cid:
                        await self.shark_delete(session, "/v1/order/delete-order", {"clientOrderId": str(cid)})
                    else:
                        logger.warning(f"TP2 cleanup: skipping order with no clientOrderId for {clean_sym}: {o.get('orderId')}")
            except Exception as ce:
                logger.warning(f"Error cleaning up orders for {clean_sym} after TP3: {ce}")

            await self.send_alert(msg)


class MultiAccountExecutor:
    """
    Multi-Account Copy-Trading Coordinator for Shark Exchange.
    Wraps multiple SharkExchangeExecutor instances (Account 1, Account 2, etc.)
    and executes trades, bracket orders, and live order management concurrently.
    """
    def __init__(self):
        self.accounts: list[SharkExchangeExecutor] = []
        self._init_accounts()

    def _init_accounts(self):
        from config import get_shark_accounts
        configs = get_shark_accounts()
        self.accounts = [
            SharkExchangeExecutor(
                name=cfg.get("name", f"Account_{i+1}"),
                api_key=cfg.get("api_key"),
                api_secret=cfg.get("api_secret"),
                telegram_chat_id=cfg.get("telegram_chat_id")
            )
            for i, cfg in enumerate(configs)
        ]
        logger.info(f"🚀 MultiAccountExecutor initialized with {len(self.accounts)} active account(s): {[a.name for a in self.accounts]}")

    @property
    def primary(self) -> SharkExchangeExecutor:
        return self.accounts[0] if self.accounts else SharkExchangeExecutor()

    def __getattr__(self, name):
        """Transparently delegates any non-overridden attribute/method to the primary account."""
        if self.accounts:
            return getattr(self.primary, name)
        raise AttributeError(f"'MultiAccountExecutor' has no attribute '{name}'")

    def is_configured(self) -> bool:
        return any(acc.is_configured() for acc in self.accounts)

    def get_account(self, identifier: str = None) -> SharkExchangeExecutor:
        """Finds account by index (1-based), name, or returns primary if default."""
        if not self.accounts:
            return None
        if not identifier:
            return self.primary
        ident = str(identifier).strip().lower()
        if ident in ("1", "main", "himanshu", "primary", "mine"):
            return self.accounts[0]
        if ident in ("2", "anuj", "anuj sharma", "sharma"):
            return self.accounts[1] if len(self.accounts) > 1 else self.accounts[0]
        if ident.isdigit():
            idx = int(ident) - 1
            if 0 <= idx < len(self.accounts):
                return self.accounts[idx]
        for acc in self.accounts:
            if ident in acc.name.lower():
                return acc
        return self.primary

    def get_all_configured_accounts(self) -> list[SharkExchangeExecutor]:
        return [acc for acc in self.accounts if acc.is_configured()]

    def clean_sym(self, p) -> str:
        return self.primary.clean_sym(p) if self.primary else ""

    def extract_order_qty(self, obj: dict) -> float:
        return self.primary.extract_order_qty(obj) if self.primary else 0.0

    async def get_available_margin_inr(self, session: aiohttp.ClientSession) -> float:
        return await self.primary.get_available_margin_inr(session)

    async def get_open_positions(self, session: aiohttp.ClientSession) -> list:
        return await self.primary.get_open_positions(session)

    async def get_open_position(self, session: aiohttp.ClientSession, symbol: str) -> dict:
        # Check primary first, then secondary configured accounts
        p = await self.primary.get_open_position(session, symbol)
        if p:
            return p
        for acc in self.accounts:
            if acc != self.primary and acc.is_configured():
                p_sub = await acc.get_open_position(session, symbol)
                if p_sub:
                    return p_sub
        return None

    async def has_active_trade_on_exchange(self, session: aiohttp.ClientSession, symbol: str, signal: str = None) -> bool:
        """
        Multi-Account Single Order Shield:
        Only returns True if ALL configured accounts already hold an active position or resting order for this symbol.
        If ANY account lacks the trade, returns False so execute_signal_trade() can place the order on missing accounts
        while existing accounts automatically trail their SL/TP!
        """
        configured_accs = [acc for acc in self.accounts if acc.is_configured()]
        if not configured_accs:
            return False
        checks = await asyncio.gather(*[
            acc.has_active_trade_on_exchange(session, symbol, signal=signal)
            for acc in configured_accs
        ], return_exceptions=True)
        return all(isinstance(c, bool) and c for c in checks)

    async def get_directional_risk_slots(
        self,
        session: aiohttp.ClientSession,
        signal: str,
        max_at_risk: int = 5,
        batch_submitted_syms: list = None,
    ) -> dict:
        return await self.primary.get_directional_risk_slots(
            session,
            signal=signal,
            max_at_risk=max_at_risk,
            batch_submitted_syms=batch_submitted_syms,
        )

    async def execute_signal_trade(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        signal: str,
        entry: float,
        sl: float,
        tp1: float,
        tp2: float = None,
        tp3: float = None,
        aligned_count: int = 3,
        adx: float = None,
        allow_during_maintenance: bool = False
    ) -> dict:
        """
        Executes trade across ALL configured accounts concurrently (Copy Trading)!
        Each account sizes independently based on its own free margin on Shark Exchange.
        Reports per-account success, trailing, skipped, and error outcomes explicitly so
        failures on secondary accounts are never swallowed silently.
        """
        configured_accs = [acc for acc in self.accounts if acc.is_configured()]
        if not configured_accs:
            logger.warning("No configured Shark Exchange accounts available for execution.")
            return {"status": "skipped", "reason": "No accounts configured", "failed_accounts": [], "account_results": []}

        if not allow_during_maintenance:
            from config import is_night_maintenance_window
            if is_night_maintenance_window():
                msg = "Night Maintenance Mode active (12:30 AM - 05:30 AM IST) — Auto-execution paused."
                logger.info(f"🌙 MULTI-ACCOUNT MAINTENANCE SHIELD: Blocked {symbol} [{signal}] ({msg})")
                return {"status": "skipped", "reason": msg, "failed_accounts": [], "account_results": []}

        logger.info(f"⚡ MULTI-ACCOUNT DISPATCH: Executing {symbol} [{signal}] across {len(configured_accs)} account(s): {[a.name for a in configured_accs]}")

        tasks = [
            acc.execute_signal_trade(
                session, symbol, signal, entry, sl, tp1, tp2,
                tp3=tp3, aligned_count=aligned_count, adx=adx,
                allow_during_maintenance=allow_during_maintenance,
            )
            for acc in configured_accs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        account_results = []
        succeeded = []
        trailed = []
        failed = []
        primary_result = None

        for i, res in enumerate(results):
            acc_name = configured_accs[i].name
            if isinstance(res, Exception):
                err_txt = str(res)
                logger.error(f"❌ Execution exception on [{acc_name}]: {err_txt}")
                rec = {"account": acc_name, "status": "error", "reason": err_txt}
                account_results.append(rec)
                failed.append(rec)
            elif isinstance(res, dict):
                st_val = res.get("status", "skipped")
                rsn_val = res.get("reason", "OK")
                logger.info(f"✅ Execution result for [{acc_name}]: status={st_val} reason={rsn_val}")
                rec = {"account": acc_name, "status": st_val, "reason": rsn_val, "total_qty": res.get("total_qty", 0)}
                account_results.append(rec)
                if st_val == "success":
                    succeeded.append(res)
                elif st_val == "trailed":
                    trailed.append(res)
                else:
                    failed.append(rec)
                if i == 0:
                    primary_result = res
            else:
                logger.info(f"Execution result for [{acc_name}]: {res}")
                rec = {"account": acc_name, "status": "error", "reason": str(res)}
                account_results.append(rec)
                failed.append(rec)

        if succeeded:
            base = dict(succeeded[0])
            base["status"] = "partial_success" if failed else "success"
            base["failed_accounts"] = failed
            base["account_results"] = account_results
            return base

        if trailed:
            base = dict(trailed[0])
            base["status"] = "partial_success" if failed else "trailed"
            base["failed_accounts"] = failed
            base["account_results"] = account_results
            return base

        combined_reasons = " | ".join([f"{f['account']}: {f['reason']}" for f in failed]) if failed else (primary_result.get("reason", "Execution failed") if isinstance(primary_result, dict) else "Execution failed")
        overall_status = "error" if any(f["status"] == "error" for f in failed) else "skipped"
        return {
            "status": overall_status,
            "reason": combined_reasons,
            "failed_accounts": failed,
            "account_results": account_results,
        }

    async def sync_and_manage_live_orders(self, session: aiohttp.ClientSession) -> None:
        """Monitors, heals, and trails orders across ALL accounts concurrently."""
        tasks = [
            acc.sync_and_manage_live_orders(session)
            for acc in self.accounts if acc.is_configured()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def get_wallet_balance_summary(self, session: aiohttp.ClientSession) -> str:
        summaries = []
        for acc in self.accounts:
            if acc.is_configured():
                s = await acc.get_wallet_balance_summary(session)
                if s:
                    summaries.append(f"<b>[{acc.name}]</b>\n{s}")
        return "\n\n".join(summaries)


# Global Multi-Account Singleton Instance
executor = MultiAccountExecutor()


async def check_and_apply_retest_trailing_sl(
    session: aiohttp.ClientSession,
    symbol: str,
    direction: str,
    new_sl: float,
    tf_label: str
) -> bool:
    # User Explicit Directive: "no trailing needed" — Fixed 1 TP 1 SL mode active
    logger.debug(f"Retest trailing SL is disabled for {symbol} (Fixed 1 TP 1 SL mode active).")
    return False

    for key, val in state.items():
        if not isinstance(val, dict):
            continue
        parts = key.split("|")
        if len(parts) < 3:
            continue

        k_sym = clean_contract_symbol(parts[0])
        k_dir = parts[2]
        outcome = val.get("outcome", "PENDING")

        # Must be same symbol, same direction, and currently active (PENDING)
        if k_sym == clean_sym and k_dir == direction and outcome == "PENDING":
            cur_sl = float(val.get("cur_sl") or val.get("sl") or 0.0)
            entry = float(val.get("entry") or 0.0)

            should_trail = False
            if direction == "BUY":
                # For Longs: Only trail UP (higher stop = tighter risk / locked profit)
                if new_sl > cur_sl:
                    should_trail = True
            elif direction == "SELL":
                # For Shorts: Only trail DOWN (lower stop = tighter risk / locked profit)
                if cur_sl > 0 and new_sl < cur_sl:
                    should_trail = True


            if should_trail:
                val["cur_sl"] = new_sl
                val["retest_trailed"] = True
                val["retest_trailed_at"] = datetime.now(timezone.utc).isoformat()
                state[key] = val
                _save(state)

                # GUARD: Only hit Shark Exchange API & send Telegram if a real order was placed
                shark_order_placed = val.get("shark_order_placed", False)
                if shark_order_placed:
                    # Update live bracket order on Shark Exchange
                    total_qty = float(val.get("total_qty") or 1.0)
                    success = await executor.modify_stop_loss(session, clean_sym, direction, new_sl, total_qty)

                    if not success:
                        logger.warning(f"Retest trail: modify_stop_loss failed for {clean_sym}. SL may be closed. Skipping Telegram.")
                        continue

                    # Send Telegram notification ONLY if SL was successfully updated on exchange
                    pnl_lock_str = ""
                    if direction == "BUY" and entry > 0 and new_sl > entry:
                        pnl_lock_str = f" 💰 (Locked in +{((new_sl - entry)/entry*100):.2f}% profit!)"
                    elif direction == "SELL" and entry > 0 and new_sl < entry:
                        pnl_lock_str = f" 💰 (Locked in +{((entry - new_sl)/entry*100):.2f}% profit!)"

                    msg = (
                        f"🛡️ <b>DYNAMIC RETEST TRAIL ACTIVATED (SHARK EXCHANGE)</b>\n\n"
                        f"<b>Asset:</b> {clean_sym} [{direction}]\n"
                        f"<b>Trigger:</b> Confirmed {tf_label} {'Support' if direction == 'BUY' else 'Resistance'} Retest\n"
                        f"<b>Stop-Loss Trailed:</b> <s>{format_price(cur_sl)}</s> ➔ <b>{format_price(new_sl)}</b>{pnl_lock_str}\n"
                        f"<b>Protection Shield:</b> One-way structural ratchet active on exchange. Risk reduced!"
                    )
                    from telegram_bot import send_text_alert as _send_alert
                    await _send_alert(msg)
                    logger.info(f"Retest SL Trailed for {clean_sym}: {cur_sl} -> {new_sl} ({tf_label})")
                else:
                    logger.debug(f"Retest detected for {clean_sym} but no Shark order placed — state updated silently.")

                trailed = True
                break

    return trailed
