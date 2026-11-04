import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

for fname in ['evaluated_since_yesterday.json', 'evaluated_full_day.json', 'evaluated_today_summary.json']:
    if os.path.exists(fname):
        with open(fname, 'r', encoding='utf-8') as f:
            d = json.load(f)
            t_list = d.get('trades') or d.get('signals') or []
            btc_items = [t for t in t_list if 'BTC' in str(t.get('symbol'))]
            print(f"=== {fname} BTC SIGNALS === (Count: {len(btc_items)})")
            for b in btc_items:
                max_p = b.get('max_pnl') if b.get('max_pnl') is not None else b.get('max_pnl_pct')
                print(f"  {b.get('symbol')} [{b.get('direction')}] {b.get('tf')} Entry={b.get('entry')} max_pnl={max_p}% -> Outcome={b.get('outcome')}")
