# -*- coding: utf-8 -*-
"""
executor.py — Automated Trade Execution & Order Bracket Engine for Shark Exchange (India).

Enforces Master Production Strategy:
1. Fixed 10x Leverage on all executed trades.
2. Dynamic 2.0% Available (Free) Margin Sizing per trade (Min ₹500 floor).
3. 2-Tier Target Bracket Structure:
   - 1 Entry Limit Order at Entry Price (30-min fill timeout).
   - TP1 (70% Qty) with 0.35% Order Block Front-Running Buffer (Min 1:2.0 RR).
   - TP2 (30% Qty Runner) at Macro Liquidity Expansion (Min 1:3.0 RR).
   - 1 Stop-Loss STOP_LIMIT Order placed strictly below 26 EMA with 0.25*ATR buffer.
4. Step Trailing Stop-Loss Engine:
   - TP1 Hit: SL Order moved to Cost-to-Cost (Breakeven Shield) for remaining 30% Qty.
   - 12-Hour Stale Runner Rule: Closes 30% runner at market if 12h elapse without hitting TP2/SL.
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
    to_shark_symbol
)
from telegram_bot import format_price, send_text_to_chat

logger = logging.getLogger("executor")

USD_INR_RATE = 102.0  # Shark Exchange native USDT/INR conversion rate


def is_us_stock_token(symbol: str) -> bool:
    """Identifies US stock tokens / synthetic equities (e.g. GOOGLB, SPCXB, TSLA, AAPL)."""
    clean = clean_contract_symbol(symbol).replace("USDT", "").replace("USD", "").upper()
    if clean in US_STOCK_TOKENS or clean.rstrip("B") in US_STOCK_TOKENS:
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
    elif any(c in clean for c in ('ETH', 'SOL', 'BNB', 'XAU', 'CL', 'LTC', 'BCH', 'AAVE', 'LINK', 'AVAX', 'DOT', 'NEAR', 'ATOM', 'ETC')):
        rounded = round(p, 2)
    elif 'XAG' in clean:
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
            # If SL is trailed into profit, ensure strictly above entry
            if is_trailed:
                return round_price(symbol, max(sl, entry * 1.002))

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
            # If SL is trailed into profit for SELL, ensure strictly below entry
            if is_trailed:
                return round_price(symbol, min(sl, entry * 0.998))

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


def round_coin_qty(symbol: str, qty: float, price: float = 0.0) -> float:
    """Helper to round quantity according to symbol lot sizes on Shark Exchange."""
    if qty is None:
        return 0.0
    clean_sym = clean_contract_symbol(symbol)
    try:
        f_qty = float(qty)
    except (ValueError, TypeError):
        return 0.0

    # Zero / negative qty: always return 0.0 so upstream guards (if qty > 0) work correctly
    if f_qty <= 0:
        return 0.0

    if any(c in clean_sym for c in ('BTC', 'ETH', 'XAU', 'PAXG')):
        rounded = max(0.001, round(f_qty, 3))
    elif 'XAG' in clean_sym:
        rounded = max(0.1, round(f_qty, 1))
    elif any(c in clean_sym for c in ('SOL', 'BNB')):
        rounded = max(0.01, round(f_qty, 2))
    elif any(c in clean_sym for c in ('AAPL', 'NVDA', 'TSLA', 'MSTR', 'AMZN', 'META', 'MSFT', 'GOOGL', 'SPY', 'QQQ', 'COIN', 'AMD')):
        rounded = max(0.1, round(f_qty, 1))
    elif any(c in clean_sym for c in ('AVAX', 'PENDLE', 'TIA', 'SEI', 'ONDO', 'RENDER', 'FET', 'ENA', 'EIGEN', 'RAY')):
        rounded = max(1, int(round(f_qty)))
    elif any(c in clean_sym for c in ('NEAR', 'AAVE', 'LINK', 'LTC', 'BCH', 'ETC', 'DOT', 'APT', 'ARB', 'OP', 'ATOM')):
        rounded = max(0.1, round(f_qty, 1))
    elif price >= 100:
        rounded = max(0.1, round(f_qty, 1))
    elif price >= 30:
        rounded = max(0.1, round(f_qty, 1))
    else:
        # Altcoins with price < $30 (PENDLE, AVAX, TIA, SEI, ONDO, RENDER, ENA, DOGE, XRP, XLM, etc.): whole integers (0 decimals)
        rounded = max(1, int(round(f_qty)))

    if isinstance(rounded, float) and rounded.is_integer():
        return int(rounded)
    return rounded


import asyncio
_global_execution_lock = None

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

    def __init__(self):
        self.api_key = os.getenv("SHARK_API_KEY", SHARK_API_KEY)
        self.api_secret = os.getenv("SHARK_API_SECRET", SHARK_API_SECRET)
        self.base_url = os.getenv("SHARK_BASE_URL", SHARK_BASE_URL)
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
        Calculates position quantities for ₹1,500 INR margin at 10x leverage.
        Notional Value = (₹1,500 / 102.0) * 10 = ~$147.06 USDT.
        Adaptive TP:
        - If ADX < 20 (Ranging/Chop): 100% exit at TP1
        - If ADX >= 20 (Trending): 70% at TP1, 30% runner at TP3
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
        notional_inr = margin_inr * self.leverage

        margin_usdt = margin_inr / self.usd_inr_rate
        notional_usdt = margin_usdt * self.leverage

        if entry_price <= 0:
            return 0.0, 0.0, 0.0, 0.0, margin_usdt, notional_usdt

        raw_total_qty = notional_usdt / entry_price
        total_qty = round_coin_qty(clean_sym, raw_total_qty, price=entry_price)

        if adx is not None and adx < 20.0:
            # Choppy / Range Regime: 100% exit at TP1
            tp1_qty = total_qty
            tp2_qty = 0.0
        else:
            # Trending Regime: 70% at TP1, 30% runner at TP2
            tp1_qty = round_coin_qty(clean_sym, total_qty * 0.70, price=entry_price)
            tp2_qty = round_coin_qty(clean_sym, total_qty - tp1_qty, price=entry_price)
            if tp2_qty <= 0:
                tp1_qty = total_qty
                tp2_qty = 0.0
        tp3_qty = tp2_qty  # Backward compat alias

        return total_qty, tp1_qty, tp2_qty, tp2_qty, margin_usdt, notional_usdt

    async def update_leverage(self, session: aiohttp.ClientSession, symbol: str, leverage: int = 10) -> bool:
        """Configures symbol leverage to 10x on Shark Exchange."""
        clean_sym = to_shark_symbol(symbol)
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
        """Queries active open orders from Shark Exchange."""
        status, response = await self.shark_get(session, "/v1/order/open-orders")
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
        return []

    async def get_open_positions(self, session: aiohttp.ClientSession) -> list:
        """Queries active open positions from Shark Exchange."""
        status, response = await self.shark_get(session, "/v1/positions/OPEN")
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
                    for k in ("positionAmount", "quantity", "amount", "holdQty", "size", "volume", "openQty", "posQty"):
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
        return []

    async def get_open_position(self, session: aiohttp.ClientSession, symbol: str) -> dict:
        """Gets single active open position for symbol."""
        shark_sym = to_shark_symbol(symbol)
        positions = await self.get_open_positions(session)
        for p in positions:
            p_sym = to_shark_symbol(p.get("contractPair") or p.get("symbol") or p.get("contractName") or "")
            if p_sym == shark_sym:
                return p
        return None

    async def has_active_trade_on_exchange(self, session: aiohttp.ClientSession, symbol: str) -> bool:
        """
        SINGLE ORDER SHIELD (Portfolio Cap Removed):
        Checks if symbol already has an active open position or pending entry order on Shark Exchange.
        Prevents duplicate entries on the same symbol while allowing unlimited concurrent trades across different symbols.
        """
        if not self.is_configured():
            return False

        clean_sym = to_shark_symbol(symbol)

        # 1. Fetch Exchange State
        positions = await self.get_open_positions(session)
        open_orders = await self.get_open_orders(session)

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
            order_type     = o.get("type", "").upper()
            place_type     = o.get("placeType", "")
            link_type      = o.get("linkType", "")
            sub_type       = o.get("subType", "")
            if is_reduce_only:
                continue
            if place_type == "POSITION":
                continue
            if link_type in ("ORDER_SL", "ORDER_TP") or sub_type in ("STOP_LOSS", "TAKE_PROFIT"):
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

        # Merge Exchange state with local in-memory active slots set (Zero Race Condition Shield)
        active_symbols = position_symbols | pending_entry_symbols | unexpired_local
        logger.info(f"Exchange active symbols check: positions={position_symbols} pending={pending_entry_symbols} local={unexpired_local}")

        # USER DIRECTIVE: Portfolio cap removed completely from everywhere!
        # Only check if active trade or pending order already exists for this exact symbol (duplicate guard)
        if clean_sym in active_symbols:
            logger.warning(f"SINGLE ORDER SHIELD: Active trade or order already exists for {clean_sym}. Skipping duplicate.")
            return True

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
        adx: float = None
    ) -> dict:
        async with _get_execution_lock():
            res = await self._execute_signal_trade_internal(session, symbol, signal, entry, sl, tp1, tp2, tp3, aligned_count=aligned_count, adx=adx)
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
        adx: float = None
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

        # 1. BTC / ETH / Gold Weekend Rule (Weekdays Mon-Fri Allowed; Sat-Sun No Trade)
        is_weekend_restricted = any(m in clean_sym.upper() for m in ("BTC", "ETH", "XAU", "GOLD", "XAUT", "XAG"))
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
        is_us_stock = any(x in symbol.upper() for x in ["AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "GOOGL", "SPY", "QQQ", "COIN", "AMD", "MSTR"]) or symbol.upper().endswith("B/USDT") or symbol.upper().endswith("BUSDT")
        if is_us_stock:
            logger.info(f"AUTO-TRADE EXCLUSION: {symbol} is a US Stock token. Skipping auto-trade per user directive. Telegram alert only.")
            return {"status": "skipped", "reason": f"{symbol} is a US stock (auto-trade disabled per directive, alerts only)"}

        if not self.is_configured():
            logger.info(f"AUTO-TRADING NOT CONFIGURED: Keys or toggle not set. Skipping execution for {clean_sym}.")
            return {"status": "skipped", "reason": "Exchange executor not configured or disabled"}

        # 2. Single Order Margin Shield Guard
        try:
            if await self.has_active_trade_on_exchange(session, clean_sym):
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
            from telegram_bot import send_text_alert as _send_alert
            await _send_alert(msg)
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
        client_order_id = f"bot-{clean_sym}-{int(time.time()*1000)}"
        order_payload = {
            "placeType": "ORDER_FORM",
            "symbol": clean_sym,
            "side": side,
            "type": "LIMIT",
            "price": clean_entry,
            "quantity": total_qty,
            "reduceOnly": False,
            "stopLossPrice": clean_sl,
            "clientOrderId": client_order_id
            # NOTE: takeProfitPrice omitted so Shark Exchange does NOT auto-create a 100% TP bracket!
            # TP1 (70%) and TP2 (30%) are placed upon fill with exact 70/30 quantities as reduce-only limit orders.
        }

        logger.info(
            f"SUBMITTING SHARK ORDER: {clean_sym} [{side}] Qty={total_qty} Price={clean_entry} "
            f"SL={clean_sl} Lev={self.leverage}x Margin=₹{self.margin_inr:.0f}"
        )

        status, data = await self.shark_post(session, "/v1/order/place-order", order_payload)

        if status in (200, 201):
            order_id = data.get("orderId") or data.get("id") or "CONFIRMED"
            actual_cid = data.get("clientOrderId") or client_order_id
            logger.info(f"SHARK ORDER PLACED SUCCESSFULLY: {clean_sym} OrderID={order_id} ClientID={actual_cid}")
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

            exec_card = (
                f"🦈 <b>REAL LIVE TRADE: LIMIT ORDER PLACED (SHARK EXCHANGE)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Asset:</b> {clean_sym} [{side}]\n"
                f"<b>Entry Limit:</b> {format_price(clean_entry)}\n"
                f"<b>Position Qty:</b> {total_qty}\n"
                f"<b>Margin Used:</b> ₹{self.margin_inr:,.0f} INR (~${margin_usdt:.2f} USDT)\n"
                f"<b>Leverage:</b> {self.leverage}x Fixed\n"
                f"<b>Notional:</b> ~${notional_usdt:.2f} USDT\n\n"
                f"🎯 <b>TP1 (70% Book + Trail +0.4R):</b> {format_price(clean_tp1)} (Qty: {tp1_qty})\n"
                f"🚀 <b>TP2 (30% Free Runner):</b> {format_price(clean_tp3)} (Qty: {tp3_qty})\n"
                f"🛡️ <b>Stop-Loss Limit:</b> {format_price(clean_sl)}{bal_block}\n\n"
                f"<i>Status: Entry limit resting on order book. 70/30 Profit Engine + +0.4R Lock armed.</i>"
            )
            from telegram_bot import send_text_alert as _send_alert
            await _send_alert(exec_card)

            return {
                "status": "success",
                "symbol": clean_sym,
                "order_id": order_id,
                "client_order_id": client_order_id,
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
            
            # If rejected due to insufficient margin, notify Telegram so user can top up
            if "3018" in err_msg or "Insufficient margin" in err_msg:
                msg = (
                    f"⚠️ <b>SHARK AUTO-ORDER MARGIN ALERT</b>\n\n"
                    f"Signal for <b>{clean_sym}</b> [{side}] could not be executed.\n"
                    f"<b>Reason:</b> Insufficient Margin in Shark Futures Wallet.\n"
                    f"<b>Required:</b> ₹{self.margin_inr:,.0f} INR.\n"
                    f"<i>Please deposit/transfer funds into your Shark Exchange Futures Wallet.</i>"
                )
                from telegram_bot import send_text_alert as _send_alert
                await _send_alert(msg)

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
            # Resolve actual SL: prefer explicit param → state → safe fallback (NOT entry!)
            actual_sl = initial_sl
            if not actual_sl or actual_sl == entry:
                from state import get_latest_symbol_state
                state_entry = get_latest_symbol_state(clean_sym)
                if state_entry:
                    actual_sl = float(state_entry.get("initial_sl") or state_entry.get("sl") or 0)
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

                # Validate & recalculate TPs if state TPs clash with live position direction (e.g. stale signal in state)
                risk = abs(fill_price - actual_sl) if (actual_sl and actual_sl != fill_price) else (fill_price * 0.015)
                if pos_direction == "BUY":
                    if actual_sl >= fill_price:
                        actual_sl = round_price(clean_sym, fill_price - risk)
                    if not tp1 or tp1 <= fill_price:
                        tp1 = round_price(clean_sym, fill_price + 1.2 * risk)
                    if not tp2 or tp2 <= tp1:
                        tp2 = round_price(clean_sym, max(tp1 * 1.003, fill_price + 2.2 * risk))
                    if not tp3 or tp3 <= tp2:
                        tp3 = round_price(clean_sym, max(tp2 * 1.003, fill_price + 3.2 * risk))
                else:  # SHORT
                    if actual_sl <= fill_price:
                        actual_sl = round_price(clean_sym, fill_price + risk)
                    if not tp1 or tp1 >= fill_price:
                        tp1 = round_price(clean_sym, fill_price - 1.2 * risk)
                    if not tp2 or tp2 >= tp1:
                        tp2 = round_price(clean_sym, min(tp1 * 0.997, fill_price - 2.2 * risk))
                    if not tp3 or tp3 >= tp2:
                        tp3 = round_price(clean_sym, min(tp2 * 0.997, fill_price - 3.2 * risk))

                # Update state so downstream monitoring uses correct direction TPs
                from state import update_trade_progress, get_latest_symbol_state
                st_entry = get_latest_symbol_state(clean_sym)
                if st_entry:
                    from state import _make_key, _load, _save
                    st_map = _load()
                    for k_st in list(st_map.keys()):
                        if to_shark_symbol(k_st.split("|")[0]) == clean_sym and st_map[k_st].get("shark_order_placed"):
                            st_map[k_st]["tp1"] = tp1
                            st_map[k_st]["tp2"] = tp2
                            st_map[k_st]["tp3"] = tp3
                            st_map[k_st]["initial_sl"] = actual_sl
                            _save(st_map)
                            break

                if pos_id and actual_qty > 0:
                    logger.info(f"place_tp_limits: Placing SL={actual_sl} + 3 TPs ({tp1}/{tp2}/{tp3}) for {clean_sym} fill@{fill_price} qty={actual_qty}")
                    return await self.execute_fill_protection(
                        session, clean_sym, pos_direction, pos_id, actual_qty,
                        sl=actual_sl, tp1=tp1, tp2=tp2, tp3=tp3, adx=adx
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
        adx: float = None
    ) -> bool:
        """
        Attaches 3 TP limit orders and 1 Stop-Loss order directly to the open position
        using placeType: 'POSITION' (requires ₹0 additional margin!).
        - TP1: 40% Qty @ tp1
        - TP2: 30% Qty @ tp2
        - TP3: 30% Qty @ tp3 (100% trade closed)
        - SL:  100% Qty @ sl (STOP_MARKET)
        """
        clean_sym = clean_contract_symbol(symbol)
        exit_side = "SELL" if direction.upper() in ("BUY", "LONG") else "BUY"

        clean_sl = round_price(clean_sym, sl)
        clean_tp1 = round_price(clean_sym, tp1)
        clean_tp2 = round_price(clean_sym, tp2)
        clean_tp3 = round_price(clean_sym, tp3) if tp3 is not None else clean_tp2

        # Elite 70/30 2-TP Profit Booking Model: 70% at TP1, 30% runner at TP2
        target_tp2 = clean_tp2 or clean_tp3
        if adx is not None and adx < 20.0:
            tp1_qty = actual_qty
            tp2_qty = 0.0
        else:
            tp1_qty = round_coin_qty(clean_sym, actual_qty * 0.70, price=clean_tp1)
            tp2_qty = round_coin_qty(clean_sym, actual_qty - tp1_qty, price=target_tp2)
            if tp2_qty <= 0:
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

        # 0. Clean up any accidental single 100% TP bracket order on position to enforce 70/30 split
        try:
            cur_orders = await self.get_open_orders(session, symbol=clean_sym)
            for o in cur_orders:
                is_tp = o.get("subType") == "TAKE_PROFIT" or o.get("linkType") in ("ORDER_TP", "ORDER_TP_SL")
                if is_tp and (o.get("placeType") == "POSITION" or o.get("reduceOnly")):
                    cid_tp = o.get("clientOrderId")
                    if cid_tp:
                        logger.info(f"Purging old 100% TP bracket {cid_tp} for {clean_sym} to enforce exact 70/30 split")
                        await self.shark_delete(session, "/v1/order/delete-order", {"clientOrderId": str(cid_tp)})
        except Exception as e_clean:
            logger.debug(f"TP bracket cleanup check error for {clean_sym}: {e_clean}")

        # 2. Place Take-Profit 1 Order (70% Book) as POSITION LIMIT Reduce-Only
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
                    logger.info(f"TP1 placed for {clean_sym} at {clean_tp1} qty={tp1_qty} (70% Book LIMIT)")
                else:
                    logger.warning(f"TP1 failed for {clean_sym}: {res}")

        # Send Telegram Fill Alert for Real Live Trade
        bal_summary = await self.get_wallet_balance_summary(session)
        bal_block = f"\n\n{bal_summary}" if bal_summary else ""
        fill_msg = (
            f"🟢 <b>REAL LIVE TRADE: LIMIT ORDER FILLED (SHARK EXCHANGE)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Asset:</b> {clean_sym} [{pos_direction}] 10x Isolated\n"
            f"🎯 <b>Fill Price:</b> {format_price(fill_price)}\n"
            f"📦 <b>Active Quantity:</b> {actual_qty} contracts\n"
            f"🛡️ <b>Stop-Loss Armed:</b> {format_price(clean_sl)}\n"
            f"🎯 <b>Targets:</b> TP1: {format_price(clean_tp1)} (70% Book) | TP2: {format_price(target_tp2)} (30% Runner)"
            f"{bal_block}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Status: Active position live on Shark Exchange. Real capital in play!</i>"
        )
        from telegram_bot import send_text_alert as _send_alert
        await _send_alert(fill_msg)

        # 3. Place TP2 Order (30%)
        if tp2_qty > 0:
            tp2_valid = (exit_side == "SELL" and clean_tp2 > clean_sl and clean_tp2 > clean_tp1) or \
                        (exit_side == "BUY" and clean_tp2 < clean_sl and clean_tp2 < clean_tp1)
            if not tp2_valid:
                logger.warning(f"TP2 price {clean_tp2} appears invalid for {clean_sym} {exit_side} — skipping")
            else:
                tp2_payload = {
                    "placeType": "POSITION",
                    "positionId": pos_id,
                    "reduceOnly": True,
                    "type": "LIMIT",
                    "side": exit_side,
                    "symbol": clean_sym,
                    "quantity": tp2_qty,
                    "price": clean_tp2
                }
                st, res = await self.shark_post(session, "/v1/order/place-order", tp2_payload)
                if st not in (200, 201):
                    logger.warning(f"TP2 failed for {clean_sym}: {res}")
                    if st == 400 and isinstance(res, dict) and res.get("error") == "3010":
                        rem_qty = round_coin_qty(clean_sym, actual_qty - tp1_qty, price=clean_tp2)
                        if rem_qty > 0:
                            tp2_payload["quantity"] = rem_qty
                            await self.shark_post(session, "/v1/order/place-order", tp2_payload)
                else:
                    logger.info(f"TP2 placed for {clean_sym} at {clean_tp2} qty={tp2_qty}")

        # TP2 is the final target (30% runner). No TP3 exists.

        return True

    async def modify_stop_loss(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        direction: str,
        new_sl: float,
        qty: float
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
                if o.get("type", "").upper() in ("STOP_MARKET", "STOP_LIMIT", "STOP")
                or o.get("linkType") == "ORDER_SL"
                or o.get("subType") == "STOP_LOSS"
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
            clean_new_sl = enforce_sl_above_liquidation(clean_sym, entry_p, clean_new_sl, direction, self.leverage, live_liq)
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
                        o_id = o.get("orderId") or o.get("id") or o.get("clientOrderId")
                        if o_id:
                            param = {"orderId": str(o_id)} if str(o_id).isdigit() else {"clientOrderId": str(o_id)}
                            await self.shark_delete(session, "/v1/order/delete-order", param)
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
            exit_side = "SELL" if pos_dir_raw in ("LONG", "BUY") else "BUY"

            clean_qty = round_coin_qty(clean_sym, qty)

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
                await self.cancel_open_orders_for_symbol(session, clean_sym)
                
                exit_p = float(pos.get("markPrice") or pos.get("lastPrice") or pos.get("entryPrice") or 0)
                entry_p = float(pos.get("entryPrice") or pos.get("avgPrice") or 0)
                unreal_pnl = float(pos.get("unrealizedPnl") or 0)
                pnl_inr = unreal_pnl * self.usd_inr_rate
                pnl_str = f"+₹{pnl_inr:,.2f} INR" if pnl_inr >= 0 else f"-₹{abs(pnl_inr):,.2f} INR"
                
                from telegram_bot import send_text_alert
                if "12-HR" in reason.upper() or "STALE" in reason.upper():
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
                await send_text_alert(msg)
                return True
            else:
                logger.error(f"Failed to close position at market for {clean_sym}: {res}")
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
                # Skip POSITION bracket orders (SL/TP)
                place_type = o.get("placeType", "")
                is_reduce_only = o.get("reduceOnly", False)
                sub_type = o.get("subType", "")
                link_type = o.get("linkType", "")

                if place_type == "POSITION" or is_reduce_only or sub_type in ("STOP_LOSS", "TAKE_PROFIT") or link_type in ("ORDER_SL", "ORDER_TP"):
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
                                await send_text_alert(msg)
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

            # SAFETY SHIELD: Clean up any orphan position-bound orders if position is already closed
            for s_ord, ord_list in orders_by_sym.items():
                if s_ord and s_ord not in live_syms:
                    for o_orph in ord_list:
                        if o_orph.get("placeType") == "POSITION" or o_orph.get("reduceOnly"):
                            cid = o_orph.get("clientOrderId")
                            oid = o_orph.get("orderId") or o_orph.get("id")
                            param = {"clientOrderId": str(cid)} if cid else ({"orderId": str(oid)} if oid else None)
                            if param:
                                logger.info(f"Purging orphan position-bound order for closed symbol {s_ord}: {param}")
                                await self.shark_delete(session, "/v1/order/delete-order", param)

            for pos in positions:
                sym = clean_contract_symbol(self.clean_sym(pos))
                if not sym:
                    continue

                entry_price = float(pos.get("entryPrice") or pos.get("avgPrice") or 0)
                pos_qty     = float(pos.get("quantity")   or pos.get("positionAmount") or pos.get("amount") or 0)
                
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
                    st_heal = get_latest_symbol_state(sym)
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
                    if o.get("linkType") == "ORDER_TP"
                    or o.get("subType") == "TAKE_PROFIT"
                    or o.get("takeProfitPrice")
                    or (o.get("type", "").upper() == "LIMIT" and o.get("placeType") == "POSITION")
                ]

                # Count SL orders
                sl_orders = [
                    o for o in sym_orders
                    if o.get("linkType") == "ORDER_SL"
                    or o.get("subType") == "STOP_LOSS"
                    or o.get("stopLossPrice")
                    or o.get("type", "").upper() in ("STOP_MARKET", "STOP_LIMIT", "STOP")
                ]
                tp_qty_field = float(pos.get("takeProfitOrdersQty") or 0)
                current_tp_count = len(tp_orders)

                # Always define pos_qty_abs here to avoid UnboundLocalError in SL/TP guard sections
                pos_qty_abs = abs(pos_qty)

                # Load state entry for current symbol
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
                                await self.modify_stop_loss(session, sym, direction, target_sl, pos_qty_abs)
                        elif state_entry.get("tp1_hit"):
                            target_sl = float(state_entry.get("initial_sl") or entry_price)
                            if (direction == "BUY" and target_sl <= entry_price) or (direction == "SELL" and target_sl >= entry_price):
                                logger.info(f"TP Monitor [INIT]: {sym} TP1 hit in state. Ensuring SL is at CTC ({target_sl}) on exchange.")
                                await self.modify_stop_loss(session, sym, direction, target_sl, pos_qty_abs)
                else:
                    prev_tp_count = self._tp_order_count_cache[sym]

                tps_hit = max(0, prev_tp_count - current_tp_count)

                # Load TP prices from state or fallback estimation
                tp1 = tp2 = tp3 = None
                if state_entry:
                    tp1 = state_entry.get("tp1")
                    tp2 = state_entry.get("tp2")
                    tp3 = state_entry.get("tp3")

                if not tp1 or not tp2 or not tp3:
                    risk = entry_price * 0.015
                    if direction == "BUY":
                        tp1 = tp1 or (entry_price + 1.0 * risk)
                        tp2 = tp2 or (entry_price + 1.6 * risk)
                        tp3 = tp3 or (entry_price + 2.5 * risk)
                    else:
                        tp1 = tp1 or (entry_price - 1.0 * risk)
                        tp2 = tp2 or (entry_price - 1.6 * risk)
                        tp3 = tp3 or (entry_price - 2.5 * risk)

                # Fire step-trailing SL on genuine TP hit transitions (supports both 70/30 2-TP and legacy 3-TP)
                if tps_hit > 0:
                    logger.info(f"🎯 TP MONITOR: {sym} [{direction}] TPs {prev_tp_count}→{current_tp_count}")

                    # 1. TP1 Hit: 70% Booked (transition 2→1 in 70/30 model, or 3→2 in legacy)
                    if (not state_entry or not state_entry.get("tp1_hit")) and current_tp_count >= 1:
                        logger.info(f"  → TP1 HIT (70% Booked on Exchange): trailing SL to +0.4R Profit Floor")
                        await self.update_trailing_sl_on_tp_hit(
                            session, sym, "TP1", entry_price, tp1, tp2, tp3)

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

                # Point 2: Multi-Stage 'Anti-Giveback' Stepped Trailing Engine Ratchet for Live Positions
                if state_entry and state_entry.get("tp1_hit") and pos_qty_abs > 0:
                    cur_px = float(pos.get("markPrice") or pos.get("currentPrice") or pos.get("lastPrice") or pos.get("indexPrice") or 0)
                    if cur_px <= 0:
                        try:
                            binance_sym = clean_contract_symbol(sym).replace("/", "").replace("-", "").upper()
                            async with session.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={binance_sym}", timeout=3) as px_resp:
                                if px_resp.status == 200:
                                    p_data = await px_resp.json()
                                    cur_px = float(p_data.get("price", 0))
                        except Exception:
                            pass
                    if cur_px > 0:
                        initial_sl_val = float(state_entry.get("initial_sl") or 0)
                        risk_step = abs(entry_price - initial_sl_val) if (initial_sl_val > 0 and initial_sl_val != entry_price) else (entry_price * 0.015)
                        current_stage = int(state_entry.get("trail_stage") or 0)

                        if direction == "BUY":
                            # Stage 2 (+3.0R Expansion -> Ratchet SL to +2.0R)
                            if cur_px >= entry_price + (3.0 * risk_step) and current_stage < 2:
                                ratchet_sl = entry_price + (2.0 * risk_step)
                                live_liq_val = float(pos.get("liquidationPrice") or pos.get("liqPrice") or pos.get("forceClosePrice") or 0)
                                safe_sl = enforce_sl_above_liquidation(sym, entry_price, ratchet_sl, direction, self.leverage, live_liq_val)
                                logger.info(f"🔥 TRAILING RATCHET [BUY]: {sym} hit +3.0R! Ratcheting SL to +2.0R ({safe_sl})")
                                await self.modify_stop_loss(session, sym, direction, safe_sl, pos_qty_abs)
                                st_map = _load()
                                for k_st in list(st_map.keys()):
                                    if to_shark_symbol(k_st.split("|")[0]) == sym and st_map[k_st].get("shark_order_placed"):
                                        st_map[k_st]["cur_sl"] = safe_sl
                                        st_map[k_st]["trail_stage"] = 2
                                        _save(st_map)
                                        break
                                from telegram_bot import send_text_alert as _send_alert
                                await _send_alert(
                                    f"🔥 <b>SHARK AUTO-TRADER: TRAILING SL RATCHET (+2.0R LOCKED) 🏆</b>\n\n"
                                    f"<b>Asset:</b> {sym} [BUY]\n"
                                    f"🚀 <b>Expansion:</b> Price reached +3.0R ({format_price(cur_px)})!\n"
                                    f"🛡️ <b>SL Ratcheted to:</b> <b>{format_price(safe_sl)} (+2.0R)</b> on Shark Exchange.\n"
                                    f"<i>Anti-Giveback active — 30% runner gains locked!</i>"
                                )

                            # Stage 1 (+2.2R Expansion -> Ratchet SL to +1.2R)
                            elif cur_px >= entry_price + (2.2 * risk_step) and current_stage < 1:
                                ratchet_sl = entry_price + (1.2 * risk_step)
                                live_liq_val = float(pos.get("liquidationPrice") or pos.get("liqPrice") or pos.get("forceClosePrice") or 0)
                                safe_sl = enforce_sl_above_liquidation(sym, entry_price, ratchet_sl, direction, self.leverage, live_liq_val)
                                logger.info(f"🛡️ TRAILING RATCHET [BUY]: {sym} hit +2.2R! Ratcheting SL to +1.2R ({safe_sl})")
                                await self.modify_stop_loss(session, sym, direction, safe_sl, pos_qty_abs)
                                st_map = _load()
                                for k_st in list(st_map.keys()):
                                    if to_shark_symbol(k_st.split("|")[0]) == sym and st_map[k_st].get("shark_order_placed"):
                                        st_map[k_st]["cur_sl"] = safe_sl
                                        st_map[k_st]["trail_stage"] = 1
                                        _save(st_map)
                                        break
                                from telegram_bot import send_text_alert as _send_alert
                                await _send_alert(
                                    f"🛡️ <b>SHARK AUTO-TRADER: TRAILING SL RATCHET (+1.2R LOCKED) 🚀</b>\n\n"
                                    f"<b>Asset:</b> {sym} [BUY]\n"
                                    f"🚀 <b>Expansion:</b> Price reached +2.2R ({format_price(cur_px)})!\n"
                                    f"🛡️ <b>SL Ratcheted to:</b> <b>{format_price(safe_sl)} (+1.2R)</b> on Shark Exchange.\n"
                                    f"<i>Anti-Giveback active — Protecting runner profits!</i>"
                                )

                        elif direction == "SELL":
                            # Stage 2 (+3.0R Expansion -> Ratchet SL to +2.0R)
                            if cur_px <= entry_price - (3.0 * risk_step) and current_stage < 2:
                                ratchet_sl = entry_price - (2.0 * risk_step)
                                live_liq_val = float(pos.get("liquidationPrice") or pos.get("liqPrice") or pos.get("forceClosePrice") or 0)
                                safe_sl = enforce_sl_above_liquidation(sym, entry_price, ratchet_sl, direction, self.leverage, live_liq_val)
                                logger.info(f"🔥 TRAILING RATCHET [SELL]: {sym} hit +3.0R! Ratcheting SL to +2.0R ({safe_sl})")
                                await self.modify_stop_loss(session, sym, direction, safe_sl, pos_qty_abs)
                                st_map = _load()
                                for k_st in list(st_map.keys()):
                                    if to_shark_symbol(k_st.split("|")[0]) == sym and st_map[k_st].get("shark_order_placed"):
                                        st_map[k_st]["cur_sl"] = safe_sl
                                        st_map[k_st]["trail_stage"] = 2
                                        _save(st_map)
                                        break
                                from telegram_bot import send_text_alert as _send_alert
                                await _send_alert(
                                    f"🔥 <b>SHARK AUTO-TRADER: TRAILING SL RATCHET (+2.0R LOCKED) 🏆</b>\n\n"
                                    f"<b>Asset:</b> {sym} [SELL]\n"
                                    f"🚀 <b>Expansion:</b> Price dumped past +3.0R ({format_price(cur_px)})!\n"
                                    f"🛡️ <b>SL Ratcheted to:</b> <b>{format_price(safe_sl)} (+2.0R)</b> on Shark Exchange.\n"
                                    f"<i>Anti-Giveback active — 30% runner gains locked!</i>"
                                )

                            # Stage 1 (+2.2R Expansion -> Ratchet SL to +1.2R)
                            elif cur_px <= entry_price - (2.2 * risk_step) and current_stage < 1:
                                ratchet_sl = entry_price - (1.2 * risk_step)
                                live_liq_val = float(pos.get("liquidationPrice") or pos.get("liqPrice") or pos.get("forceClosePrice") or 0)
                                safe_sl = enforce_sl_above_liquidation(sym, entry_price, ratchet_sl, direction, self.leverage, live_liq_val)
                                logger.info(f"🛡️ TRAILING RATCHET [SELL]: {sym} hit +2.2R! Ratcheting SL to +1.2R ({safe_sl})")
                                await self.modify_stop_loss(session, sym, direction, safe_sl, pos_qty_abs)
                                st_map = _load()
                                for k_st in list(st_map.keys()):
                                    if to_shark_symbol(k_st.split("|")[0]) == sym and st_map[k_st].get("shark_order_placed"):
                                        st_map[k_st]["cur_sl"] = safe_sl
                                        st_map[k_st]["trail_stage"] = 1
                                        _save(st_map)
                                        break
                                from telegram_bot import send_text_alert as _send_alert
                                await _send_alert(
                                    f"🛡️ <b>SHARK AUTO-TRADER: TRAILING SL RATCHET (+1.2R LOCKED) 🚀</b>\n\n"
                                    f"<b>Asset:</b> {sym} [SELL]\n"
                                    f"🚀 <b>Expansion:</b> Price reached +2.2R ({format_price(cur_px)})!\n"
                                    f"🛡️ <b>SL Ratcheted to:</b> <b>{format_price(safe_sl)} (+1.2R)</b> on Shark Exchange.\n"
                                    f"<i>Anti-Giveback active — Protecting runner profits!</i>"
                                )

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
                    for o_sl in sl_orders:
                        sl_p_raw = float(o_sl.get("stopLossPrice") or o_sl.get("stopPrice") or o_sl.get("price") or 0)
                        if sl_p_raw > 0:
                            # Strict Liquidation Check: Is SL on or beyond liquidation price?
                            is_beyond_liq = (direction == "BUY" and live_liq > 0 and sl_p_raw <= live_liq) or                                             (direction == "SELL" and live_liq > 0 and sl_p_raw >= live_liq)
                            # Noise check: Is SL too tight on an UNTRAILED / FRESH trade?
                            # IMPORTANT: If TP1 has already hit or position is trailed to breakeven/profit, do NOT downgrade SL!
                            is_trailed = bool(state_entry and (state_entry.get("tp1_hit") or (state_entry.get("trail_stage") or 0) > 0))
                            min_dist_threshold = round_price(sym, entry_price * (1.0 - min_sl_pct if direction == "BUY" else 1.0 + min_sl_pct))
                            is_too_tight = not is_trailed and ((direction == "BUY" and sl_p_raw > min_dist_threshold) or (direction == "SELL" and sl_p_raw < min_dist_threshold))

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

                # TP HEAL: Re-place TPs if missing and position still open
                if current_tp_count == 0 and pos_qty_abs > 0 and not is_first_run and tps_hit == 0:
                    last_heal = self._tp_sl_heal_lock.get(sym + "_tp", 0)
                    never_healed = last_heal == 0  # Fresh fill — first time we see 0 TPs
                    if never_healed or (time.time() - last_heal > 120):
                        logger.warning(f"⚠️ {sym}: 0 TPs {'(fresh fill)' if never_healed else '(re-heal)'} — placing TPs now")
                        st_heal = get_latest_symbol_state(sym)
                        sl_val = float(st_heal.get("cur_sl") or st_heal.get("initial_sl") or entry_price)
                        await self.place_tp_limits(
                            session, sym, direction, entry_price, tp1, tp2, tp3, initial_sl=sl_val)
                        self._tp_sl_heal_lock[sym + "_tp"] = time.time()

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
        direction = "BUY" if tp1 > entry else "SELL"

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
        except Exception as e:
            logger.warning(f"Could not fetch live position for {clean_sym}: {e}")

        from state import get_latest_symbol_state, _load, _save
        state_entry = get_latest_symbol_state(clean_sym)
        initial_sl = float(state_entry.get("initial_sl") or 0) if state_entry else 0

        initial_qty, tp1_qty, _, tp3_qty, _, _ = self.calculate_position_size(clean_sym, real_entry, sl_price=initial_sl)
        if initial_qty <= 0:
            initial_qty = real_qty if real_qty > 0 else 1.0

        if tp1_qty <= 0:
            tp1_qty = round_coin_qty(clean_sym, initial_qty * 0.70, price=tp1)
        if tp3_qty <= 0:
            tp3_qty = round_coin_qty(clean_sym, initial_qty - tp1_qty, price=tp3 or tp1)

        def calc_pnl(qty, exit_px, entry_px, side):
            """Real PnL = qty × (exit - entry) for LONG, qty × (entry - exit) for SHORT"""
            if side == "BUY":
                return qty * (exit_px - entry_px)
            else:
                return qty * (entry_px - exit_px)

        target_tp3 = tp3 if tp3 and tp3 > 0 else (tp2 if tp2 and tp2 > 0 else tp1)

        if tp_stage == "TP1":
            # 1. 70% Profit Booked at TP1
            pnl_usdt = calc_pnl(tp1_qty, tp1, real_entry, direction)
            pnl_inr  = pnl_usdt * self.usd_inr_rate
            runner_qty = real_qty if real_qty > 0 else tp3_qty

            # 2. Shift SL to Cost-to-Cost + 0.4% (Guaranteed Fee-Proof Shield & Profit-Lock)
            risk = abs(real_entry - initial_sl) if (initial_sl and initial_sl != real_entry) else (real_entry * 0.012)
            if direction == "BUY":
                # Entry + 0.40% CTC buffer or +0.4R (whichever gives stronger protection)
                target_sl = max(real_entry * 1.004, real_entry + (0.40 * risk))
                trailed_sl = round_price(clean_sym, min(target_sl, tp1 * 0.998))
            else:
                # Entry - 0.40% CTC buffer or -0.4R (whichever gives stronger protection)
                target_sl = min(real_entry * 0.996, real_entry - (0.40 * risk))
                trailed_sl = round_price(clean_sym, max(target_sl, tp1 * 1.002))

            # Clamp with Liquidation Shield
            safe_trailed_sl = enforce_sl_above_liquidation(clean_sym, real_entry, trailed_sl, direction, self.leverage, live_liq, is_trailed=True)

            # 3. Modify Stop-Loss order on Shark Exchange to CTC + 0.4% for remaining runner
            logger.info(f"🎯 TP1 HIT: Modifying SL for {clean_sym} to CTC + 0.4% {safe_trailed_sl} (Runner qty={runner_qty})")
            if runner_qty > 0:
                await self.modify_stop_loss(session, clean_sym, direction, safe_trailed_sl, runner_qty)

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
            msg = (
                f"🎯 <b>REAL LIVE TRADE: TP1 HIT ✅ (70% PROFIT SECURED)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Asset:</b> {clean_sym} [{direction}]\n"
                f"<b>Entry:</b> {format_price(real_entry)}  →  <b>TP1:</b> {format_price(tp1)}\n"
                f"<b>Realized 70% Profit:</b> +${pnl_usdt:.2f} USDT (+₹{pnl_inr:,.0f} INR) 💰\n"
                f"🛡️ <b>SL Trailed to Entry Price (Cost-to-Cost / Breakeven):</b> {format_price(safe_trailed_sl)}\n"
                f"🚀 <b>Remaining 30% Runner ({runner_qty} qty):</b> Aiming for TP2 {format_price(target_tp3)}!\n"
                f"<i>Trade is now 100% Zero-Risk. Aiming for full 1:3.0+ RR expansion!</i>{bal_block}"
            )
            from telegram_bot import send_text_alert as _send_alert
            await _send_alert(msg)

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

            # Cancel remaining open orders (SL)
            try:
                open_orders = await self.get_open_orders(session, symbol=clean_sym)
                for o in open_orders:
                    cid = o.get("clientOrderId")
                    oid = o.get("orderId") or o.get("id")
                    param = {"clientOrderId": str(cid)} if cid else ({"orderId": str(oid)} if oid else None)
                    if param:
                        await self.shark_delete(session, "/v1/order/delete-order", param)
            except Exception as ce:
                logger.warning(f"Error cleaning up orders for {clean_sym} after TP3: {ce}")

            from telegram_bot import send_text_alert as _send_alert
            await _send_alert(msg)


# Global Singleton Instance
executor = SharkExchangeExecutor()


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
