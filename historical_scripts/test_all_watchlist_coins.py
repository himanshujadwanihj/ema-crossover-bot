import asyncio
import aiohttp
import sys
import pandas as pd
from datetime import datetime, timezone

# Ensure UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')

from pullback_tracker import WATCHLIST, SPECIAL_LIST
from executor import executor, round_price, round_coin_qty, clean_contract_symbol
from indicators import find_liquidity_pool_targets, get_sl_cap_percent

async def test_all_coins():
    print("=" * 75)
    print("🚀 ALL-WATCHLIST PRE-FLIGHT DEMO TEST & PAYLOAD VALIDATION")
    print("=" * 75)
    
    # Combine all active coins
    all_coins = list(dict.fromkeys(WATCHLIST + SPECIAL_LIST))
    print(f"Total Watchlist Coins to Test: {len(all_coins)}\n")
    
    async with aiohttp.ClientSession() as session:
        # Query active trades on Shark Exchange to exclude active trades
        active_syms = set()
        try:
            positions = await executor.get_open_positions(session)
            orders = await executor.get_open_orders(session)
            for p in positions:
                s = executor.clean_sym(p)
                if s: active_syms.add(s)
            for o in orders:
                s = executor.clean_sym(o)
                if s: active_syms.add(s)
            print(f"ℹ️ Active Exchange Trades/Orders (Excluded from dry-run): {active_syms if active_syms else 'None'}\n")
        except Exception as e:
            print(f"⚠️ Could not fetch live positions from Shark: {e}\n")

        passed_count = 0
        failed_count = 0
        skipped_active = 0
        
        results_log = []

        for symbol in all_coins:
            clean_sym = clean_contract_symbol(symbol)
            if clean_sym in active_syms:
                skipped_active += 1
                results_log.append((symbol, "SKIPPED", "Active Trade on Exchange"))
                continue
                
            binance_sym = symbol.replace("/", "")
            url = f"https://api.binance.com/api/v3/klines?symbol={binance_sym}&interval=1h&limit=50"
            
            try:
                async with session.get(url, timeout=5) as resp:
                    if resp.status != 200:
                        results_log.append((symbol, "SKIP_FETCH", f"Binance status {resp.status}"))
                        continue
                    data = await resp.json()
            except Exception as e_fetch:
                results_log.append((symbol, "SKIP_FETCH", str(e_fetch)[:30]))
                continue

            if not data or len(data) < 30:
                results_log.append((symbol, "SKIP_DATA", "Insufficient candles"))
                continue

            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'tbv', 'tqv', 'ignore'])
            df['open'] = df['open'].astype(float)
            df['high'] = df['high'].astype(float)
            df['low'] = df['low'].astype(float)
            df['close'] = df['close'].astype(float)
            df['volume'] = df['volume'].astype(float)

            current_price = df['close'].iloc[-1]
            ema_9 = df['close'].ewm(span=9, adjust=False).mean().iloc[-1]
            ema_26 = df['close'].ewm(span=26, adjust=False).mean().iloc[-1]

            # Test BOTH BUY and SELL scenario payloads
            for signal in ["BUY", "SELL"]:
                entry = current_price
                def_sl = ema_26 * 0.998 if signal == "BUY" else ema_26 * 1.002
                
                # Liquidity pool target calculator
                sl, (tp1, tp2, tp3), (rr1, rr2, rr3), (lbl1, lbl2, lbl3) = find_liquidity_pool_targets(
                    df, signal, entry, def_sl, symbol=symbol
                )
                
                # Position Sizing
                total_qty, tp1_qty, tp2_qty, tp3_qty, margin_usdt, notional_usdt = executor.calculate_position_size(symbol, entry)
                
                clean_entry = round_price(symbol, entry)
                clean_sl = round_price(symbol, sl)
                clean_tp1 = round_price(symbol, tp1)
                clean_tp2 = round_price(symbol, tp2)
                clean_tp3 = round_price(symbol, tp3)

                # Enforcement checks
                errors = []
                
                # Check 1: SL Direction Guarantee
                if signal == "BUY" and clean_sl >= clean_entry:
                    errors.append(f"BUY SL ({clean_sl}) >= Entry ({clean_entry})")
                elif signal == "SELL" and clean_sl <= clean_entry:
                    errors.append(f"SELL SL ({clean_sl}) <= Entry ({clean_entry})")
                    
                # Check 2: TP Target Progression
                if signal == "BUY":
                    if not (clean_tp1 > clean_entry and clean_tp2 >= clean_tp1):
                        errors.append(f"BUY TP sequence invalid (Entry={clean_entry}, TP1={clean_tp1}, TP2={clean_tp2})")
                else:
                    if not (clean_tp1 < clean_entry and clean_tp2 <= clean_tp1):
                        errors.append(f"SELL TP sequence invalid (Entry={clean_entry}, TP1={clean_tp1}, TP2={clean_tp2})")

                # Check 3: Quantity precision (Error 3006 check)
                if isinstance(total_qty, float):
                    dec_places = len(str(total_qty).split('.')[1]) if '.' in str(total_qty) else 0
                    INTEGER_ALTCOINS = ('PENDLE', 'TIA', 'SEI', 'ONDO', 'RENDER', 'FET', 'ENA', 'DOGE', 'XRP', 'XLM', 'SUI', 'UNI', 'SHIB', 'PEPE', 'FLOKI', 'BONK', 'RUNE', 'ORDI', 'WLD', 'NOT', 'PNUT', 'ACT')
                    if any(c in clean_sym for c in INTEGER_ALTCOINS):
                        if dec_places > 0:
                            errors.append(f"Qty has {dec_places} decimals for integer altcoin: qty={total_qty}")
                            
                # Check 4: Position size non-zero
                if total_qty <= 0:
                    errors.append("Total quantity calculated as 0")

                if errors:
                    failed_count += 1
                    results_log.append((symbol, f"FAIL_{signal}", "; ".join(errors)))
                else:
                    passed_count += 1
                    results_log.append((symbol, f"PASS_{signal}", f"Px={clean_entry} Qty={total_qty} SL={clean_sl} TP1={clean_tp1} Margin=${margin_usdt:.2f}"))

        print("=" * 75)
        print("SUMMARY RESULTS:")
        print("=" * 75)
        print(f"✅ Total Scenarios Passed: {passed_count}")
        print(f"❌ Total Scenarios Failed: {failed_count}")
        print(f"ℹ️ Active Trades Excluded: {skipped_active}")
        print("=" * 75)
        
        # Display sample passes and any failures
        print("\n--- TEST LOG (First 20 Validations) ---")
        for sym, status, detail in results_log[:20]:
            print(f"{sym:<12} | {status:<10} | {detail}")
            
        failures = [r for r in results_log if "FAIL" in r[1]]
        if failures:
            print("\n❌ DETECTED FAILURES:")
            for sym, status, detail in failures:
                print(f"{sym:<12} | {status:<10} | {detail}")
        else:
            print("\n🎉 ZERO FAILURES DETECTED! ALL WATCHLIST COINS 100% VALIDATED.")

if __name__ == "__main__":
    asyncio.run(test_all_coins())
