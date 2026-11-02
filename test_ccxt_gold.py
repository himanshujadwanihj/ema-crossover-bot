import ccxt

for ex_id in ['binance', 'bybit', 'gateio', 'mexc', 'okx']:
    try:
        ex_class = getattr(ccxt, ex_id)
        ex = ex_class({'enableRateLimit': True})
        markets = ex.load_markets()
        gold_symbols = [s for s in markets.keys() if 'XAU' in s or 'PAXG' in s or 'GOLD' in s]
        print(f"{ex_id} gold symbols: {gold_symbols[:10]}")
    except Exception as e:
        print(f"{ex_id} error: {e}")
