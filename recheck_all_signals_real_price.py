import json
import os
import re
import sys
import requests
import time
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding='utf-8')

def safe_float(v, default=0.0):
    if v is None: return default
    try: return float(v)
    except: return default

parsed_signals = []

signal_regex = re.compile(
    r'(?:\[\w+\]\s+)?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*?NEW SIGNAL:\s+(BUY|SELL)\s+([A-Z0-9/:-]+)\s+([A-Z0-9]+)\s+\((.*?)\)\s+\|\s+entry=([0-9.]+)\s+sl=([0-9.]+)\s+tp1=([0-9.]+)\s+tp2=([0-9.]+)\s+tp3=([0-9.]+)',
    re.IGNORECASE
)

log_files = ['raw_signals_found.txt', 'raw_signals_since_yesterday.txt', 'fresh_railway_logs.txt', 'railway_logs.txt', 'logs_temp.txt', 'recent_logs.txt', 'log.txt', 'logs.txt']

for fname in log_files:
    if os.path.exists(fname):
        with open(fname, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                match = signal_regex.search(line)
                if match:
                    ts_str, side, sym, tf, setup, entry, sl, tp1, tp2, tp3 = match.groups()
                    clean_sym = sym.replace('/', '').replace('-', '').upper()
                    parsed_signals.append({
                        'timestamp': ts_str,
                        'symbol': clean_sym,
                        'raw_symbol': sym,
                        'direction': side.upper(),
                        'tf': tf.upper(),
                        'setup': setup.strip(),
                        'entry': float(entry),
                        'sl': float(sl),
                        'tp1': float(tp1),
                        'tp2': float(tp2),
                        'tp3': float(tp3),
                        'source': fname
                    })

# Parse JSONs
for fname in ['evaluated_since_yesterday.json', 'evaluated_full_day.json', 'evaluated_today_summary.json', 'logs_36h.json']:
    if os.path.exists(fname):
        try:
            with open(fname, 'r', encoding='utf-8') as f:
                d = json.load(f)
                items = d.get('trades') or d.get('signals') or (d if isinstance(d, list) else [])
                for item in items:
                    if isinstance(item, dict) and item.get('symbol'):
                        clean_sym = str(item.get('symbol', '')).replace('/', '').replace('-', '').upper()
                        parsed_signals.append({
                            'timestamp': item.get('time_utc') or item.get('time_ist') or '2026-09-26 05:00:00',
                            'symbol': clean_sym,
                            'raw_symbol': item.get('symbol'),
                            'direction': str(item.get('direction', '')).upper(),
                            'tf': str(item.get('tf', '')).upper(),
                            'setup': str(item.get('setup', '')),
                            'entry': safe_float(item.get('entry')),
                            'sl': safe_float(item.get('sl')),
                            'tp1': safe_float(item.get('tp1')),
                            'tp2': safe_float(item.get('tp2')),
                            'tp3': safe_float(item.get('tp3')),
                            'outcome': item.get('outcome'),
                            'source': fname
                        })
        except Exception:
            pass

# Deduplicate unique signals
unique_signals = {}
for s in parsed_signals:
    sym = s['symbol']
    direction = s['direction']
    tf = s['tf']
    entry = round(s['entry'], 4)
    k = f"{sym}_{direction}_{tf}_{entry}"
    if k not in unique_signals:
        unique_signals[k] = s

print(f"Total Unique Signals Identified: {len(unique_signals)}")

# Evaluate signals against real Binance 5m kline trajectories
session = requests.Session()
kline_cache = {}

def get_binance_klines(symbol, start_dt):
    clean_sym = symbol.replace("/", "").replace("-", "").upper()
    start_ms = int(start_dt.timestamp() * 1000)
    cache_key = f"{clean_sym}_{start_ms}"
    if cache_key in kline_cache:
        return kline_cache[cache_key]

    url = f"https://api.binance.com/api/v3/klines?symbol={clean_sym}&interval=5m&startTime={start_ms}&limit=1000"
    try:
        r = session.get(url, timeout=3.5)
        if r.status_code == 200:
            data = r.json()
            kline_cache[cache_key] = data
            return data
    except Exception as e:
        pass
    return []

evaluated_signals = []
evaluated_count = 0

for k, s in unique_signals.items():
    sym = s['symbol']
    direction = s['direction']
    tf = s['tf']
    entry = s['entry']
    sl = s['sl']
    tp1 = s['tp1']
    tp2 = s['tp2']
    tp3 = s['tp3']
    ts_str = s['timestamp']

    # Parse datetime
    dt = None
    try:
        clean_ts = ts_str.replace("T", " ").split(".")[0].strip()
        dt = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime(2026, 9, 26, 5, 0, 0, tzinfo=timezone.utc)

    klines = get_binance_klines(sym, dt)

    if not klines or not entry or entry <= 0:
        # Fallback to existing outcome if klines unavailable
        out = str(s.get('outcome', 'SL HIT')).upper()
        if 'TP3' in out: norm_out = 'TP3 HIT (Full Win)'
        elif 'TP2' in out: norm_out = 'TP2 HIT'
        elif 'TP1' in out: norm_out = 'TP1 HIT'
        elif 'SL' in out: norm_out = 'SL HIT'
        else: norm_out = 'ACTIVE / RUNNING'
    else:
        filled = False
        tp1_hit = False
        tp2_hit = False
        tp3_hit = False
        sl_hit = False
        max_pnl = 0.0

        for candle in klines:
            c_open  = float(candle[1])
            c_high  = float(candle[2])
            c_low   = float(candle[3])
            c_close = float(candle[4])

            # Trade entry fill condition
            if not filled:
                if direction == 'BUY' and c_low <= entry:
                    filled = True
                elif direction == 'SELL' and c_high >= entry:
                    filled = True

            if filled:
                if direction == 'BUY':
                    pnl = (c_high - entry) / entry * 100.0
                    if pnl > max_pnl: max_pnl = pnl
                    
                    if c_high >= tp3: tp3_hit = True
                    if c_high >= tp2: tp2_hit = True
                    if c_high >= tp1: tp1_hit = True
                    
                    if c_low <= sl:
                        if not tp1_hit: # If TP1 was already hit, SL was moved to Entry CTC
                            sl_hit = True
                        break
                else: # SELL
                    pnl = (entry - c_low) / entry * 100.0
                    if pnl > max_pnl: max_pnl = pnl
                    
                    if c_low <= tp3: tp3_hit = True
                    if c_low <= tp2: tp2_hit = True
                    if c_low <= tp1: tp1_hit = True
                    
                    if c_high >= sl:
                        if not tp1_hit: # If TP1 was already hit, SL was moved to Entry CTC
                            sl_hit = True
                        break

        if tp3_hit: norm_out = 'TP3 HIT (Full Win)'
        elif tp2_hit: norm_out = 'TP2 HIT'
        elif tp1_hit: norm_out = 'TP1 HIT'
        elif sl_hit: norm_out = 'SL HIT'
        elif filled: norm_out = 'ACTIVE / RUNNING'
        else: norm_out = 'ACTIVE / PENDING'

    s['norm_outcome'] = norm_out
    evaluated_signals.append(s)
    evaluated_count += 1
    if evaluated_count % 50 == 0:
        print(f"Evaluated {evaluated_count}/{len(unique_signals)} signals...")

print(f"Completed trajectory re-evaluation for all {len(evaluated_signals)} unique signals!")

# BTC Specific Inspection
btc_evals = [s for s in evaluated_signals if 'BTC' in s['symbol']]
print("\n=== RE-CHECKED BTC SIGNALS OUTCOMES ===")
btc_wins = 0
btc_sl = 0
btc_running = 0
for b in btc_evals:
    print(f"[{b['timestamp']}] BTC [{b['direction']}] {b['tf']} Entry={b['entry']} -> {b['norm_outcome']}")
    if 'TP' in b['norm_outcome']: btc_wins += 1
    elif b['norm_outcome'] == 'SL HIT': btc_sl += 1
    else: btc_running += 1

btc_closed = btc_wins + btc_sl
btc_wr = (btc_wins / btc_closed * 100) if btc_closed > 0 else 0
print(f"BTC RE-ANALYSIS TOTAL: {len(btc_evals)} Signals | Wins={btc_wins} | SL Losses={btc_sl} | Win Rate={btc_wr:.1f}%\n")

# Save re-evaluated dataset
with open('evaluated_real_trajectories.json', 'w', encoding='utf-8') as f:
    json.dump(evaluated_signals, f, indent=2)

print("Saved re-evaluated signal trajectories to evaluated_real_trajectories.json")
