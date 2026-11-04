import json
import requests
import pandas as pd
from datetime import datetime

with open("evaluated_since_yesterday.json", "r", encoding="utf-8") as f:
    data = json.load(f)

trades = [t for t in data["trades"] if "Retest" in t.get("setup", "")]
print(f"Total retest trades in evaluated_since_yesterday: {len(trades)}")

# Let's inspect the wins vs losses
wins = [t for t in trades if t.get("outcome", "").startswith("TP")]
losses = [t for t in trades if "SL" in t.get("outcome", "")]
print(f"Wins: {len(wins)} | Losses: {len(losses)} | Raw Win Rate: {len(wins)/(len(wins)+len(losses))*100:.1f}%")
