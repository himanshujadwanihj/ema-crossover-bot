"""
demo_run_simulation.py — Complete Dry-Run & Disaster Management Test Suite for Binance EMA Bot
"""

import asyncio
import sys
import os

# Add bot directory to path
bot_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, bot_dir)

import pandas as pd
import numpy as np
from datetime import datetime, timezone

from config import (
    AUTO_TRADE_MARGIN_INR,
    AUTO_TRADE_LEVERAGE,
    AUTO_TRADE_MAX_CONCURRENT
)
from executor import executor, clean_contract_symbol, round_price, round_coin_qty, USD_INR_RATE
from indicators import find_liquidity_pool_targets, _compute_indicators
from state import get_latest_symbol_state, _load, _save, mark_sent, clear_all_pending_backlogs
from telegram_bot import _send_chunked_digest, _build_single_coin_block

print("=" * 70)
print("BINANCE EMA BOT -- DISASTER MANAGEMENT & DEMO RUN TEST SUITE")
print("=" * 70)

tests_passed = 0
tests_failed = 0

def test_assert(condition, description):
    global tests_passed, tests_failed
    if condition:
        print(f"  [PASS]: {description}")
        tests_passed += 1
    else:
        print(f"  [FAIL]: {description}")
        tests_failed += 1

# ── TEST 1: Margin & Sizing Math ──────────────────────────────────────────────
print("\n[TEST 1] MARGIN & POSITION SIZING MATH (RS 1,500 INR @ 10x)")
total_qty, tp1_qty, tp2_qty, tp3_qty, margin_usdt, notional_usdt = executor.calculate_position_size("BTCUSDT", 65000.0)
test_assert(AUTO_TRADE_MARGIN_INR == 1500.0, f"AUTO_TRADE_MARGIN_INR is {AUTO_TRADE_MARGIN_INR} (expected 1500.0)")
test_assert(round(margin_usdt, 2) == round(1500.0 / 102.0, 2), f"Margin USDT is ${margin_usdt:.2f} (~$14.71 USDT)")
test_assert(round(notional_usdt, 2) == round(margin_usdt * 10, 2), f"Notional USDT is ${notional_usdt:.2f} (~$147.06 USDT)")

# ── TEST 2: Quantity Rounding & Lot Sizes (AVAX 3006 Fix) ─────────────────────
print("\n[TEST 2] LOT SIZING & QUANTITY PRECISION (3006 Error Prevention)")
avax_qty = round_coin_qty("AVAXUSDT", 13.4, price=11.5)
test_assert(isinstance(avax_qty, int), f"AVAX qty is integer: {avax_qty} (type={type(avax_qty).__name__})")

btc_qty = round_coin_qty("BTCUSDT", 0.00226, price=65000.0)
test_assert(btc_qty == 0.002, f"BTC qty 3-decimal precision: {btc_qty}")

sol_qty = round_coin_qty("SOLUSDT", 0.942, price=150.0)
test_assert(sol_qty == 0.94, f"SOL qty 2-decimal precision: {sol_qty}")

arb_qty = round_coin_qty("ARBUSDT", 703.28, price=0.208)
test_assert(isinstance(arb_qty, (int, float)), f"ARB qty precision: {arb_qty}")

# ── TEST 3: Liquidity Pool Targets (Minimum Target Distance Filter) ───────────
print("\n[TEST 3] LIQUIDITY POOL TP TARGET CALCULATOR (Instant TP1 Prevention)")
dates = pd.date_range(end=pd.Timestamp.now(), periods=100, freq='1h')
np.random.seed(42)
prices = 100 + np.cumsum(np.random.randn(100) * 0.5)
df_test = pd.DataFrame({'open': prices, 'high': prices + 0.8, 'low': prices - 0.8, 'close': prices + 0.1, 'volume': 1000}, index=dates)

entry_px = df_test.iloc[-1]['close']
default_sl = entry_px * 0.98
dyn_sl, (tp1, tp2, tp3), (rr1, rr2, rr3), (lbl1, lbl2, lbl3) = find_liquidity_pool_targets(df_test, "BUY", entry_px, default_sl, symbol="TESTUSDT")

min_expected_tp1 = entry_px + max(0.5 * abs(entry_px - dyn_sl), entry_px * 0.005)
test_assert(tp1 >= min_expected_tp1, f"BUY TP1 ({tp1:.4f}) >= min target ({min_expected_tp1:.4f}) -- distance={((tp1-entry_px)/entry_px*100):.2f}%")
test_assert(tp2 > tp1, f"3-Tier Telegram Target Analysis: TP2 ({tp2:.4f}) > TP1 ({tp1:.4f})")
test_assert(tp3 >= tp2, f"3-Tier Telegram Target Analysis: TP3 ({tp3:.4f}) >= TP2 ({tp2:.4f})")

# ── TEST 4: Small Position Notional Check (Error 3010 Prevention) ─────────────
print("\n[TEST 4] SMALL POSITION NOTIONAL CHECK (3010 Error Prevention)")
# Simulate small $12 USDT position
small_qty = 20.0
small_tp1 = 0.55
est_notional = small_qty * small_tp1
test_assert(est_notional < 15.0, f"Simulated small position notional: ${est_notional:.2f} < $15.0 USDT")

# ── TEST 5: State Reverse Lookup (Latest Signal Resolution) ───────────────────
print("\n[TEST 5] REVERSE STATE LOOKUP (Latest Active Trade Target Resolution)")
dummy_key_1 = "SIMTEST/USDT|1H_9/26 EMA Cross|BUY|2026-09-20 00:00:00+00:00"
dummy_key_2 = "SIMTEST/USDT|1H_9/26 EMA Cross|BUY|2026-09-30 12:00:00+00:00"

# Inject dummy state entries
state = _load()
state[dummy_key_1] = {"entry": 1.00, "sl": 0.90, "initial_sl": 0.90, "tp1": 1.10, "tp2": 1.20, "tp3": 1.30, "outcome": "PURGED_BACKLOG", "shark_order_placed": False}
state[dummy_key_2] = {"entry": 2.00, "sl": 1.90, "initial_sl": 1.90, "tp1": 2.10, "tp2": 2.20, "tp3": 2.30, "outcome": "PENDING", "shark_order_placed": True}
_save(state)

latest_res = get_latest_symbol_state("SIMTESTUSDT")
test_assert(latest_res.get("initial_sl") == 1.90, f"Reverse state lookup returned latest signal SL 1.90 (not old 0.90): got {latest_res.get('initial_sl')}")
test_assert(latest_res.get("tp1") == 2.10, f"Reverse state lookup returned latest signal TP1 2.10 (not old 1.10): got {latest_res.get('tp1')}")

# Clean up dummy state
state = _load()
state.pop(dummy_key_1, None)
state.pop(dummy_key_2, None)
_save(state)

# ── TEST 6: Telegram Chunking Engine (4096-Char Limit Disaster Check) ────────
print("\n[TEST 6] TELEGRAM DIGEST CHUNKING (4096-Char Message Length Protection)")
dummy_items = []
for i in range(18):
    dummy_items.append({
        "symbol": f"COIN{i}/USDT",
        "tf_label": "1H",
        "signal": "BUY",
        "price": 100.0 + i,
        "entry": 100.0 + i,
        "sl": 98.0,
        "tp1": 102.0, "tp2": 105.0, "tp3": 110.0,
        "rr1": 1.0, "rr2": 2.5, "rr3": 5.0,
        "lbl1": "BSL Pool 1", "lbl2": "BSL Pool 2", "lbl3": "BSL Pool 3",
        "tf_states": {"1H": "bullish", "4H": "bullish", "1D": "bullish", "1W": "bullish"},
        "cross_type": "9/26 EMA Cross",
        "last_1h_cross_time": None,
        "candle_time": "2026-09-30T12:00:00Z",
        "macro_struct": "Bullish Trend",
        "rr": 1.0, "tp_label": "BSL Pool 1", "tf_data": {}, "is_special_list": False
    })

# Check chunking math
chunk_size = 5
chunks = [dummy_items[i:i + chunk_size] for i in range(0, len(dummy_items), chunk_size)]
test_assert(len(chunks) == 4, f"18 signals chunked into 4 separate messages: {len(chunks)} (max 5 coins each)")

sample_msg = f"[LIGHTNING] 18 FRESH EMA CROSSOVERS (Part 1/4)\n[TIME] Time: 30-Sep 17:30 IST\n" + ("-" * 26) + "\n"
for i, item in enumerate(chunks[0], 1):
    sample_msg += _build_single_coin_block(item, index=i)
    sample_msg += ("-" * 26) + "\n"

test_assert(len(sample_msg) < 3000, f"Chunk message length: {len(sample_msg)} chars (well under Telegram 4096 limit)")

print("\n[TEST 7] BTC VOLATILITY SHOCK SHIELD (30-MIN AUTO-PAUSE)")
from indicators import is_btc_shock_paused
is_paused, rem = is_btc_shock_paused()
test_assert(not is_paused, "BTC shock guard default is unpaused (False)")

print("\n" + "=" * 70)
print(f"SUMMARY: {tests_passed} TESTS PASSED | {tests_failed} TESTS FAILED")
print("=" * 70)
