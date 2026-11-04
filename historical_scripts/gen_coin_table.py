import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

def safe_float(v, default=0.0):
    if v is None: return default
    try: return float(v)
    except: return default

def load_all_trades():
    all_raw = []
    for fname in ['evaluated_since_yesterday.json', 'evaluated_full_day.json', 'evaluated_today_summary.json']:
        if os.path.exists(fname):
            with open(fname, 'r', encoding='utf-8') as f:
                d = json.load(f)
                items = d.get('trades') or d.get('signals') or []
                for t in items:
                    raw_sym = str(t.get('symbol', '')).replace('/', '').replace('-', '').upper()
                    if raw_sym:
                        t['clean_sym'] = raw_sym
                        all_raw.append(t)
    return all_raw

all_trades = load_all_trades()

unique_trades = {}
for t in all_trades:
    sym = t['clean_sym']
    direction = str(t.get('direction', '')).upper()
    tf = str(t.get('tf', '')).upper()
    entry = safe_float(t.get('entry'))
    k = f"{sym}_{direction}_{tf}_{round(entry, 4)}"
    if k not in unique_trades:
        unique_trades[k] = t
    else:
        out_new = str(t.get('outcome', '')).upper()
        out_old = str(unique_trades[k].get('outcome', '')).upper()
        if ('TP' in out_new or 'SL' in out_new or 'WIN' in out_new) and not ('TP' in out_old or 'SL' in out_old):
            unique_trades[k] = t

coin_stats = {}
for k, t in unique_trades.items():
    sym = t['clean_sym']
    out = str(t.get('outcome', '')).upper()
    tp1_h = t.get('tp1_hit', False)
    tp2_h = t.get('tp2_hit', False)
    tp3_h = t.get('tp3_hit', False)
    sl_h  = t.get('sl_hit', False)

    if sym not in coin_stats:
        coin_stats[sym] = {'total': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0, 'sl': 0, 'running': 0}

    coin_stats[sym]['total'] += 1

    if tp3_h or 'TP3' in out or 'FULL WIN' in out or 'TARGET HIT' in out:
        coin_stats[sym]['tp3'] += 1
    elif tp2_h or 'TP2' in out:
        coin_stats[sym]['tp2'] += 1
    elif tp1_h or 'TP1' in out:
        coin_stats[sym]['tp1'] += 1
    elif sl_h or 'SL' in out or 'STOP' in out or 'LOSS' in out:
        coin_stats[sym]['sl'] += 1
    else:
        coin_stats[sym]['running'] += 1

sorted_coins = sorted(coin_stats.items(), key=lambda x: x[1]['total'], reverse=True)

print(f"Total Unique Coins Traded: {len(sorted_coins)}")
print(f"{'Asset':<14} | {'Total':<5} | {'TP1':<4} | {'TP2':<4} | {'TP3 (Full)':<10} | {'SL':<4} | {'Running':<7} | {'Win Rate':<8}")
print("-" * 75)

for sym, stat in sorted_coins:
    wins = stat['tp1'] + stat['tp2'] + stat['tp3']
    closed = wins + stat['sl']
    wr = (wins / closed * 100) if closed > 0 else 0.0
    print(f"{sym:<14} | {stat['total']:<5} | {stat['tp1']:<4} | {stat['tp2']:<4} | {stat['tp3']:<10} | {stat['sl']:<4} | {stat['running']:<7} | {wr:<7.1f}%")
