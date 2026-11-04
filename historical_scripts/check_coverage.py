import re
import json
import glob
import os

with open('evaluated_strict_trajectories.json', 'r', encoding='utf-8') as f:
    evaluated = json.load(f)

eval_keys = set()
for s in evaluated:
    k = f"{s['symbol']}_{s['direction']}_{s['tf']}_{round(s['entry'], 4)}"
    eval_keys.add(k)

print(f"Currently evaluated unique signal keys: {len(eval_keys)}")

signal_regex = re.compile(
    r'(?:\[\w+\]\s+)?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*?NEW SIGNAL:\s+(BUY|SELL)\s+([A-Z0-9/:-]+)\s+([A-Z0-9]+)\s+\((.*?)\)\s+\|\s+entry=([0-9.]+)\s+sl=([0-9.]+)\s+tp1=([0-9.]+)\s+tp2=([0-9.]+)\s+tp3=([0-9.]+)',
    re.IGNORECASE
)

log_files = glob.glob('*.txt') + glob.glob('*.log')
all_raw_signals = []

for fname in log_files:
    if os.path.isfile(fname):
        with open(fname, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                match = signal_regex.search(line)
                if match:
                    ts_str, side, sym, tf, setup, entry, sl, tp1, tp2, tp3 = match.groups()
                    clean_sym = sym.replace('/', '').replace('-', '').upper()
                    k = f"{clean_sym}_{side.upper()}_{tf.upper()}_{round(float(entry), 4)}"
                    all_raw_signals.append({
                        'key': k,
                        'symbol': clean_sym,
                        'direction': side.upper(),
                        'tf': tf.upper(),
                        'setup': setup.strip(),
                        'source': fname
                    })

print(f"Total raw signal occurrences found across all log files: {len(all_raw_signals)}")

unique_raw_keys = set(s['key'] for s in all_raw_signals)
print(f"Total unique raw signal keys found across all log files: {len(unique_raw_keys)}")

missing = unique_raw_keys - eval_keys
print(f"Signal keys in log files NOT in evaluated set: {len(missing)}")

crossover_count = 0
for s in evaluated:
    sig_type = str(s.get('type', '')).upper()
    setup = str(s.get('setup', '')).upper()
    if sig_type != 'RETEST' and 'RETEST' not in setup and 'PULLBACK' not in setup:
        crossover_count += 1

print(f"Evaluated Fresh Crossovers Count: {crossover_count}")
