import asyncio
import aiohttp
import sys
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')

from executor import executor, round_price, round_coin_qty, clean_contract_symbol
from indicators import find_liquidity_pool_targets

async def test_ltc_specific():
    print("=" * 65)
    print("🚀 SPECIFIC LTC/USDT PAYLOAD & PRECISION VALIDATION")
    print("=" * 65)
    
    async with aiohttp.ClientSession() as session:
        url = "https://api.binance.com/api/v3/klines?symbol=LTCUSDT&interval=1h&limit=50"
        async with session.get(url) as resp:
            data = await resp.json()
            
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'tbv', 'tqv', 'ignore'])
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)

        entry = df['close'].iloc[-1]
        ema_26 = df['close'].ewm(span=26, adjust=False).mean().iloc[-1]
        
        for signal in ["BUY", "SELL"]:
            def_sl = ema_26 * 0.998 if signal == "BUY" else ema_26 * 1.002
            sl, (tp1, tp2, tp3), (rr1, rr2, rr3), (lbl1, lbl2, lbl3) = find_liquidity_pool_targets(df, signal, entry, def_sl, symbol="LTC/USDT")
            
            total_qty, tp1_qty, tp2_qty, tp3_qty, margin_usdt, notional_usdt = executor.calculate_position_size("LTC/USDT", entry)
            
            clean_entry = round_price("LTC/USDT", entry)
            clean_sl = round_price("LTC/USDT", sl)
            clean_tp1 = round_price("LTC/USDT", tp1)
            clean_tp2 = round_price("LTC/USDT", tp2)
            
            print(f"\n--- LTC/USDT [{signal}] Payload Test ---")
            print(f"  • Entry Price : {clean_entry} (Type: {type(clean_entry).__name__}, Decimals: {len(str(clean_entry).split('.')[1]) if '.' in str(clean_entry) else 0})")
            print(f"  • Quantity    : {total_qty} (Type: {type(total_qty).__name__})")
            print(f"  • Stop-Loss   : {clean_sl} (Type: {type(clean_sl).__name__})")
            print(f"  • TP1 Price   : {clean_tp1}")
            print(f"  • TP2 Price   : {clean_tp2}")
            print(f"  • Margin Used : ${margin_usdt:.2f} USDT (~₹1,500 INR @ 10x)")
            
            # Validation Assertions
            dec_entry = len(str(clean_entry).split('.')[1]) if '.' in str(clean_entry) else 0
            assert dec_entry <= 2, f"Entry decimals for LTC must be <= 2, got {dec_entry}"
            if signal == "BUY":
                assert clean_sl < clean_entry, "BUY SL must be < Entry"
                assert clean_tp1 > clean_entry, "BUY TP1 must be > Entry"
            else:
                assert clean_sl > clean_entry, "SELL SL must be > Entry"
                assert clean_tp1 < clean_entry, "SELL TP1 must be < Entry"
                
            print(f"  ✅ ALL LTC [{signal}] VALIDATIONS PASSED PERFECTLY!")

if __name__ == "__main__":
    asyncio.run(test_ltc_specific())
