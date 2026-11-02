import ccxt

for ex_name in ['bybit', 'mexc', 'gateio']:
    try:
        ex = getattr(ccxt, ex_name)()
        for s in ['XAU/USDT:USDT', 'XAUT/USDT', 'PAXG/USDT']:
            try:
                ohlcv = ex.fetch_ohlcv(s, '15m', limit=5)
                if ohlcv:
                    print(f"{ex_name} {s} 15m last close: {ohlcv[-1][4]} at {ohlcv[-1][0]}")
            except Exception as e:
                pass
    except Exception as e2:
        print(ex_name, e2)
