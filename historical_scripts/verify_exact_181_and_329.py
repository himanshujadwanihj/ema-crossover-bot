import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

# Verify exact 181 Fresh Crossover Signals baseline vs retest
with open('evaluated_strict_trajectories.json', 'r', encoding='utf-8') as f:
    all_signals = json.load(f)

crossover_signals = []
for s in all_signals:
    sig_type = str(s.get('type', '')).upper()
    setup = str(s.get('setup', '')).upper()
    if sig_type != 'RETEST' and 'RETEST' not in setup and 'PULLBACK' not in setup:
        crossover_signals.append(s)

print(f"Total Unique Fresh Crossover Signals: {len(crossover_signals)}")

# Count 4-Star vs 3-Star in crossover_signals
c_4star = []
c_3star = []
for s in crossover_signals:
    stars = s.get('aligned_count') or s.get('stars')
    tf = str(s.get('tf', '')).upper()
    setup = str(s.get('setup', '')).upper()
    is_4star = (stars == 4) or ('4H' in tf or '1D' in tf or '4-STAR' in setup or '4/4' in setup or 'VOLUME' in setup)
    if is_4star: c_4star.append(s)
    else: c_3star.append(s)

print(f"Fresh Crossovers 4-Star: {len(c_4star)}")
print(f"Fresh Crossovers 3-Star: {len(c_3star)}")
