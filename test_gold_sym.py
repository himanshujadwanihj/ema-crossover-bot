import yfinance as yf

tickers = ['GC=F', 'XAU-USD', 'XAUUSD', 'GLD', '^XAU', 'PAXG-USD', 'XAUUSD=X', 'XAU=X']
for sym in tickers:
    try:
        t = yf.Ticker(sym)
        df = t.history(period='2d', interval='1h')
        if df is not None and not df.empty:
            print(f"Symbol {sym} WORKED! Shape: {df.shape}, Last price: {df['Close'].iloc[-1]:.2f} at {df.index[-1]}")
        else:
            print(f"Symbol {sym}: Empty")
    except Exception as e:
        print(f"Symbol {sym}: Error {e}")
