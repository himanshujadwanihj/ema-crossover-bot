import json
import os
import re
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')

def safe_float(v, default=0.0):
    if v is None: return default
    try: return float(v)
    except: return default

parsed_signals = []

# 1. Parse text log files with regex: NEW SIGNAL: (BUY|SELL) (SYMBOL) (TF) (...) | entry=... sl=... tp1=... tp2=... tp3=...
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

# 2. Also parse JSON files
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
                            'timestamp': item.get('time_utc') or item.get('time_ist') or '2026-09-26',
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
        except Exception as e:
            pass

print(f"Total Raw Signals Collected Across All Logs/JSONs: {len(parsed_signals)}")

# Deduplicate
unique_signals = {}
for s in parsed_signals:
    sym = s['symbol']
    direction = s['direction']
    tf = s['tf']
    entry = round(s['entry'], 4)
    # Group by key
    k = f"{sym}_{direction}_{tf}_{entry}"
    if k not in unique_signals:
        unique_signals[k] = s
    else:
        # Prefer signal record with explicitly populated outcome
        if s.get('outcome') and not unique_signals[k].get('outcome'):
            unique_signals[k] = s

print(f"Total Unique Signals Sent (Deduplicated): {len(unique_signals)}")

timestamps = [s['timestamp'] for s in unique_signals.values() if s['timestamp'] and '2026' in s['timestamp']]
timestamps.sort()

if timestamps:
    print(f"Earliest Signal Timestamp: {timestamps[0]}")
    print(f"Latest Signal Timestamp: {timestamps[-1]}")
