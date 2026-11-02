import asyncio
import aiohttp
from fetcher import get_top_pairs_by_volume, fetch_all_timeframes_async
from indicators import analyze_crossovers, get_trend_state
from telegram_bot import TF_ORDER

async def scan():
    print('Fetching top 200 pairs...')
    pairs = get_top_pairs_by_volume(top_n=200)
    
    semaphore = asyncio.Semaphore(15)
    
    async def process(session, symbol):
        try:
            tf_data = await fetch_all_timeframes_async(session, symbol, semaphore)
            if not tf_data: return None
            
            tf_states = {}
            for l in TF_ORDER:
                tf_states[l] = get_trend_state(tf_data.get(l))
                
            results = []
            for label in ['15M', '1H', '4H', '1D']:
                df = tf_data.get(label)
                if df is None: continue
                
                crossovers = analyze_crossovers(df, symbol=symbol, tf_data=tf_data)
                for cross in crossovers:
                    if 'Cross' in cross['type']:
                        results.append({
                            'symbol': symbol,
                            'tf': label,
                            'signal': cross['signal'],
                            'type': cross['type'],
                            'states': tf_states
                        })
            return results
        except Exception as e:
            return None

    connector = aiohttp.TCPConnector(limit=100)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [process(session, sym) for sym in pairs]
        all_results = await asyncio.gather(*tasks)
        
    final = []
    for r in all_results:
        if r:
            final.extend(r)
            
    with open('scan_results2.txt', 'w', encoding='utf-8') as f:
        f.write('--- PURE FRESH EMA CROSSOVERS ---\n\n')
        if not final:
            f.write('No fresh EMA Crossovers found right this second.\n')
        else:
            for res in final:
                s = res['states']
                conf = f"1H: {s.get('1H').upper()} | 4H: {s.get('4H').upper()} | 1D: {s.get('1D').upper()} | 1W: {s.get('1W').upper()}"
                f.write(f"🔹 {res['symbol']} - {res['tf']} CHART ({res['signal']})\n")
                f.write(f"   Setup: {res['type']}\n")
                f.write(f"   Confluence: {conf}\n\n")

if __name__ == '__main__':
    asyncio.run(scan())
