import requests
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

# Load deduplicated unique signals
with open('evaluated_real_trajectories.json', 'r', encoding='utf-8') as f:
    signals = json.load(f)

session = requests.Session()
kline_cache = {}

def get_binance_klines(symbol, start_dt):
    clean_sym = symbol.replace('/', '').replace('-', '').upper()
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
    except Exception:
        pass
    return []

strict_evaluated = []

for s in signals:
    sym = s['symbol']
    direction = s['direction']
    tf = s['tf']
    entry = s['entry']
    sl = s['sl']
    tp1 = s['tp1']
    tp2 = s['tp2']
    tp3 = s['tp3']
    ts_str = s['timestamp']

    dt = None
    try:
        clean_ts = ts_str.replace('T', ' ').split('.')[0].strip()
        dt = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime(2026, 9, 26, 5, 0, 0, tzinfo=timezone.utc)

    klines = get_binance_klines(sym, dt)

    if not klines or not entry or entry <= 0:
        outcome = s.get('norm_outcome', 'UNAVAILABLE')
    else:
        filled = False
        tp1_hit = False
        tp2_hit = False
        tp3_hit = False
        sl_hit = False
        max_pnl = 0.0

        for candle in klines:
            c_high  = float(candle[2])
            c_low   = float(candle[3])

            # Limit Order Entry Fill:
            # BUY limit fills when market price drops to or below entry price
            # SELL limit fills when market price rises to or above entry price
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
                        if not tp1_hit:
                            sl_hit = True
                        break
                else: # SELL
                    pnl = (entry - c_low) / entry * 100.0
                    if pnl > max_pnl: max_pnl = pnl

                    if c_low <= tp3: tp3_hit = True
                    if c_low <= tp2: tp2_hit = True
                    if c_low <= tp1: tp1_hit = True

                    if c_high >= sl:
                        if not tp1_hit:
                            sl_hit = True
                        break

        if not filled:
            outcome = 'RESTING LIMIT UNFILLED'
        elif tp3_hit:
            outcome = 'TP3 HIT (Full Win)'
        elif tp2_hit:
            outcome = 'TP2 HIT'
        elif tp1_hit:
            outcome = 'TP1 HIT (CTC Shield)'
        elif sl_hit:
            outcome = 'SL HIT'
        else:
            outcome = 'ACTIVE / RUNNING'

    s['strict_outcome'] = outcome
    strict_evaluated.append(s)

with open('evaluated_strict_trajectories.json', 'w', encoding='utf-8') as f:
    json.dump(strict_evaluated, f, indent=2)

print("=== STRICT TRAJECTORY EVALUATION COMPLETE ===", flush=True)

# Summary per category
outcomes_summary = {}
star_summary = {'4-STAR': {}, '3-STAR': {}}

for s in strict_evaluated:
    out = s['strict_outcome']
    outcomes_summary[out] = outcomes_summary.get(out, 0) + 1
    
    stars = s.get('aligned_count') or s.get('stars')
    tf = s['tf']
    setup = s['setup'].upper()
    is_4star = (stars == 4) or ('4H' in tf or '1D' in tf or '4-STAR' in setup or '4/4' in setup or 'VOLUME' in setup or 'RETEST' in setup)
    star_cat = '4-STAR' if is_4star else '3-STAR'
    
    star_summary[star_cat][out] = star_summary[star_cat].get(out, 0) + 1

print("\nOverall Strict Outcomes:", outcomes_summary, flush=True)
print("4-Star Strict Outcomes:", star_summary['4-STAR'], flush=True)
print("3-Star Strict Outcomes:", star_summary['3-STAR'], flush=True)

# BTC Specific Inspection
btc_strict = [s for s in strict_evaluated if 'BTC' in s['symbol']]
print(f"\nTotal BTC Signals Evaluated: {len(btc_strict)}", flush=True)
for b in btc_strict:
    print(f"  [{b['timestamp']}] BTC [{b['direction']}] {b['tf']} Entry={b['entry']} -> {b['strict_outcome']}", flush=True)

wins = sum(1 for s in strict_evaluated if 'TP' in s['strict_outcome'])
losses = sum(1 for s in strict_evaluated if s['strict_outcome'] == 'SL HIT')
unfilled = sum(1 for s in strict_evaluated if s['strict_outcome'] == 'RESTING LIMIT UNFILLED')
running = sum(1 for s in strict_evaluated if 'RUNNING' in s['strict_outcome'])

closed = wins + losses
wr = (wins / closed * 100) if closed > 0 else 0
print(f"\nSUMMARY METRICS: Total Signals={len(strict_evaluated)} | Closed Trades={closed} | Wins={wins} | Losses={losses} | Unfilled={unfilled} | Running={running} | Win Rate={wr:.1f}%", flush=True)
