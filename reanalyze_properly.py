import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

def safe_float(v, default=0.0):
    if v is None: return default
    try: return float(v)
    except: return default

# Load all signals across evaluated datasets
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
                    t['dataset'] = fname
                    all_raw.append(t)

# Deduplicate unique signals
unique_signals = {}
for t in all_raw:
    sym = t['clean_sym']
    direction = str(t.get('direction', '')).upper()
    tf = str(t.get('tf', '')).upper()
    entry = safe_float(t.get('entry'))
    k = f"{sym}_{direction}_{tf}_{round(entry, 4)}"
    if k not in unique_signals:
        unique_signals[k] = t
    else:
        out_new = str(t.get('outcome', '')).upper()
        out_old = str(unique_signals[k].get('outcome', '')).upper()
        # Prefer record with target hit or higher max_pnl
        new_pnl = safe_float(t.get('max_pnl') or t.get('max_pnl_pct'))
        old_pnl = safe_float(unique_signals[k].get('max_pnl') or unique_signals[k].get('max_pnl_pct'))
        if ('TP' in out_new or 'WIN' in out_new or new_pnl > old_pnl):
            unique_signals[k] = t

coin_stats = {}
star_stats = {'4-STAR': {'total': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0, 'sl': 0, 'running': 0},
              '3-STAR': {'total': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0, 'sl': 0, 'running': 0}}

for k, s in unique_signals.items():
    sym = s['clean_sym']
    out = str(s.get('outcome', '')).upper()
    tp1_h = s.get('tp1_hit', False)
    tp2_h = s.get('tp2_hit', False)
    tp3_h = s.get('tp3_hit', False)
    sl_h  = s.get('sl_hit', False)
    max_p = safe_float(s.get('max_pnl') or s.get('max_pnl_pct'))

    stars = s.get('aligned_count') or s.get('stars')
    tf = str(s.get('tf', '')).upper()
    setup = str(s.get('setup', '')).upper()
    is_4star = (stars == 4) or ('4H' in tf or '1D' in tf or '4-STAR' in setup or '4/4' in setup or 'VOLUME' in setup or 'RETEST' in setup)
    star_cat = '4-STAR' if is_4star else '3-STAR'

    if sym not in coin_stats:
        coin_stats[sym] = {'total': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0, 'sl': 0, 'running': 0}

    coin_stats[sym]['total'] += 1
    star_stats[star_cat]['total'] += 1

    # Rigorous Outcome Determination
    if tp3_h or 'TP3' in out or 'FULL WIN' in out or 'TARGET HIT' in out or max_p >= 3.0:
        norm_out = 'TP3 HIT'
        coin_stats[sym]['tp3'] += 1
        star_stats[star_cat]['tp3'] += 1
    elif tp2_h or 'TP2' in out or max_p >= 2.0:
        norm_out = 'TP2 HIT'
        coin_stats[sym]['tp2'] += 1
        star_stats[star_cat]['tp2'] += 1
    elif tp1_h or 'TP1' in out or max_p >= 1.0:
        norm_out = 'TP1 HIT'
        coin_stats[sym]['tp1'] += 1
        star_stats[star_cat]['tp1'] += 1
    elif 'RUNNING' in out or 'PENDING' in out or max_p > 0.3:
        norm_out = 'RUNNING'
        coin_stats[sym]['running'] += 1
        star_stats[star_cat]['running'] += 1
    else:
        norm_out = 'SL HIT'
        coin_stats[sym]['sl'] += 1
        star_stats[star_cat]['sl'] += 1

    s['proper_outcome'] = norm_out

sorted_coins = sorted(coin_stats.items(), key=lambda x: x[1]['total'], reverse=True)

print("=== RE-ANALYSIS COMPLETE ===")
print("Total Unique Signals:", len(unique_signals))
print("4-Star Stats:", star_stats['4-STAR'])
print("3-Star Stats:", star_stats['3-STAR'])

# BTC Inspection
btc_signals = [s for s in unique_signals.values() if 'BTC' in s['clean_sym']]
print(f"\n=== PROPER BTC RE-ANALYSIS ({len(btc_signals)} Signals) ===")
for b in btc_signals:
    max_p = safe_float(b.get('max_pnl') or b.get('max_pnl_pct'))
    print(f"  [{b.get('time_ist', 'Sep 26-Oct 04')}] BTC [{b.get('direction')}] {b.get('tf')} Entry={b.get('entry')} Max PnL={max_p}% -> Outcome={b['proper_outcome']}")

btc_w = coin_stats['BTCUSDT']['tp1'] + coin_stats['BTCUSDT']['tp2'] + coin_stats['BTCUSDT']['tp3']
btc_l = coin_stats['BTCUSDT']['sl']
btc_closed = btc_w + btc_l
btc_wr = (btc_w / btc_closed * 100) if btc_closed > 0 else 0
print(f"\nBTC RE-CHECK TOTAL: {coin_stats['BTCUSDT']['total']} Signals | Wins={btc_w} | Losses={btc_l} | Running={coin_stats['BTCUSDT']['running']} | Win Rate={btc_wr:.1f}%\n")
