import time
"""
state.py — Signal deduplication state manager.

Uses a JSON file to track which signals have already been sent.
On Railway, uses /data/signal_state.json (persistent volume).
Falls back to local file if /data not available.
"""

import json
import os
import logging
from datetime import datetime, timezone, timedelta
from config import clean_contract_symbol

logger = logging.getLogger(__name__)

# Use /data directory on Railway (persistent volume), fallback to local
if os.path.isdir("/data"):
    STATE_FILE = "/data/signal_state_v3.json"
else:
    STATE_FILE = os.getenv("STATE_FILE", "signal_state_v3.json")

logger.info(f"State file: {STATE_FILE}")


_state_cache = None


def _load() -> dict:
    global _state_cache
    if _state_cache is not None:
        return _state_cache

    data = {}
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:
        logger.warning(f"Could not load state file {STATE_FILE}: {e}")

    # Seed fallback: Merge local seed into persistent /data if persistent has fewer entries
    if STATE_FILE != "signal_state_v3.json" and os.path.exists("signal_state_v3.json"):
        try:
            with open("signal_state_v3.json", "r", encoding="utf-8") as f:
                seed = json.load(f)
            if len(seed) > len(data):
                seed.update(data)
                data = seed
                _save(data)
        except Exception:
            pass

    _state_cache = data
    return _state_cache


def _save(state: dict = None) -> None:
    global _state_cache
    if state is not None:
        _state_cache = state
    if _state_cache is None:
        return
    try:
        os.makedirs(os.path.dirname(STATE_FILE) if os.path.dirname(STATE_FILE) else ".", exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state_cache, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save state file: {e}")


def _make_key(symbol: str, tf: str, signal: str, candle_ts: str) -> str:
    return f"{symbol}|{tf}|{signal}|{candle_ts}"


def is_coin_on_cooldown(symbol: str, tf: str = None, max_hours: float = 0.25) -> bool:
    state = _load()
    key = f"COOLDOWN|{symbol}|{tf}" if tf else f"COOLDOWN|{symbol}"
    last_sent_str = state.get(key)
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.fromisoformat(last_sent_str)
        if (datetime.now(timezone.utc) - last_sent) < timedelta(hours=max_hours):
            return True
    except Exception:
        pass
    return False

def set_coin_cooldown(symbol: str, tf: str = None) -> None:
    state = _load()
    key = f"COOLDOWN|{symbol}|{tf}" if tf else f"COOLDOWN|{symbol}"
    state[key] = datetime.now(timezone.utc).isoformat()
    _save(state)

def is_symbol_cooldown(symbol: str, tf: str = None, max_hours: float = 0.25) -> bool:
    return is_coin_on_cooldown(symbol, tf, max_hours)

def is_already_sent(symbol: str, tf: str, signal: str, candle_ts: str) -> bool:
    state = _load()
    exact_key = _make_key(symbol, tf, signal, candle_ts)
    if exact_key in state:
        return True

    target_ts_clean = str(candle_ts).strip()
    prefix = f"{symbol}|{tf}|"
    for key, val in state.items():
        if key.startswith(prefix):
            parts = key.split("|")
            if len(parts) >= 4:
                k_signal = parts[2]
                k_ts = parts[3].strip()
                if k_signal == signal:
                    # Check exact timestamp match or sub-string match (handling ISO formatting variations)
                    if k_ts == target_ts_clean or target_ts_clean.startswith(k_ts[:16]) or k_ts.startswith(target_ts_clean[:16]):
                        return True
    return False


def has_active_opposite_trade(symbol: str, signal: str) -> bool:
    """
    Returns True ONLY if there is an OPEN (PENDING = not yet hit TP or SL) trade
    in the OPPOSITE direction for the same coin.
    
    Logic:
    - LONG is ACTIVE → block SHORT for same coin
    - LONG hit SL    → allow SHORT (trade is closed, fair game)
    - LONG hit TP    → allow SHORT (trade is closed, fair game)
    """
    state = _load()
    opposite_signal = "SELL" if signal == "BUY" else "BUY"
    
    for key, val in state.items():
        if not isinstance(val, dict):
            continue
        parts = key.split("|")
        if len(parts) < 3:
            continue
        key_symbol = parts[0]
        key_signal = parts[2]
        outcome = val.get("outcome", "PENDING")
        
        if key_symbol != symbol:
            continue
        
        # Only block if the OPPOSITE trade is still actively PENDING (not closed)
        if key_signal == opposite_signal and outcome == "PENDING":
            return True
    
    return False
def get_latest_symbol_state(symbol: str) -> dict:
    """
    Finds the LATEST state entry for a given symbol.
    Searches in REVERSE order (newest to oldest) and prioritizes
    active Shark Exchange orders (shark_order_placed=True, outcome=PENDING).
    """
    clean_sym = clean_contract_symbol(symbol)
    state = _load()
    
    def _prepare(key, val):
        res = dict(val)
        parts = key.split("|")
        if len(parts) >= 3:
            res["signal"] = parts[2]
            res["direction"] = parts[2]
        return res

    # Priority 1: Live Shark trade that is PENDING
    for k, v in reversed(list(state.items())):
        if not isinstance(v, dict) or k.startswith("cooldown|"):
            continue
        if clean_contract_symbol(k.split("|")[0]) == clean_sym:
            if v.get("shark_order_placed") and v.get("outcome") == "PENDING":
                return _prepare(k, v)
                
    # Priority 2: Any Shark trade
    for k, v in reversed(list(state.items())):
        if not isinstance(v, dict) or k.startswith("cooldown|"):
            continue
        if clean_contract_symbol(k.split("|")[0]) == clean_sym:
            if v.get("shark_order_placed"):
                return _prepare(k, v)
                
    # Priority 3: Most recent entry for symbol
    for k, v in reversed(list(state.items())):
        if not isinstance(v, dict) or k.startswith("cooldown|"):
            continue
        if clean_contract_symbol(k.split("|")[0]) == clean_sym:
            return _prepare(k, v)
            
    return {}



def mark_sent(symbol: str, tf: str, signal: str, candle_ts: str, entry: float = 0.0, sl: float = 0.0, tp: float = 0.0, tp1: float = 0.0, tp2: float = 0.0, tp3: float = 0.0) -> None:
    state = _load()
    key = _make_key(symbol, tf, signal, candle_ts)
    risk = abs(entry - sl) if (entry and sl) else (entry * 0.01 if entry else 1.0)
    
    eff_tp1 = tp1 if (tp1 and tp1 != entry) else (entry + 1.2 * risk if signal == "BUY" else entry - 1.2 * risk)
    eff_tp2 = tp2 if (tp2 and tp2 != eff_tp1) else (entry + 2.2 * risk if signal == "BUY" else entry - 2.2 * risk)
    eff_tp3 = tp3 if (tp3 and tp3 != eff_tp2) else (entry + 3.2 * risk if signal == "BUY" else entry - 3.2 * risk)

    if signal == "BUY":
        if eff_tp2 <= eff_tp1: eff_tp2 = max(eff_tp1 * 1.005, entry + (2.2 * risk))
        if eff_tp3 <= eff_tp2: eff_tp3 = max(eff_tp2 * 1.005, entry + (3.2 * risk))
    else:
        if eff_tp2 >= eff_tp1: eff_tp2 = min(eff_tp1 * 0.995, entry - (2.2 * risk))
        if eff_tp3 >= eff_tp2: eff_tp3 = min(eff_tp2 * 0.995, entry - (3.2 * risk))

    now_iso = datetime.now(timezone.utc).isoformat()
    state[key] = {
        "sent_at": now_iso,
        "limit_placed_at": now_iso,
        "limit_filled": False,
        "limit_filled_at": None,
        "entry": entry,
        "sl": sl,
        "initial_sl": sl,
        "cur_sl": sl,
        "tp": eff_tp1,
        "tp1": eff_tp1,
        "tp2": eff_tp2,
        "tp3": eff_tp3,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "be_hit": False,
        "tp1_hit_at": None,
        "tp2_hit_at": None,
        "outcome": "PENDING",
        "shark_order_placed": False  # True only when a real Shark Exchange order was placed
    }
    _save(state)

def update_trade_outcome(key: str, outcome: str) -> None:
    state = _load()
    if key in state and isinstance(state[key], dict):
        state[key]["outcome"] = outcome
        _save(state)

def update_trade_sl(key: str, new_sl: float) -> None:
    state = _load()
    if key in state and isinstance(state[key], dict):
        # ONLY update cur_sl — never overwrite 'sl' or 'initial_sl'!
        # 'sl' and 'initial_sl' are the original risk reference — used for TP recalculation.
        # Overwriting them causes risk = 0 which breaks all downstream TP math.
        state[key]["cur_sl"] = new_sl
        state[key]["be_hit"] = True
        _save(state)

def update_trade_progress(key: str, tp1_hit: bool = None, tp2_hit: bool = None, tp3_hit: bool = None, be_hit: bool = None, new_sl: float = None, tp1_hit_at: str = None, tp2_hit_at: str = None, limit_filled: bool = None, limit_filled_at: str = None, be_hit_time_shield: bool = None, **kwargs) -> None:
    state = _load()
    if key in state and isinstance(state[key], dict):
        if tp1_hit is not None: state[key]["tp1_hit"] = tp1_hit
        if tp2_hit is not None: state[key]["tp2_hit"] = tp2_hit
        if tp3_hit is not None: state[key]["tp3_hit"] = tp3_hit
        if be_hit is not None: state[key]["be_hit"] = be_hit
        if be_hit_time_shield is not None: state[key]["be_hit_time_shield"] = be_hit_time_shield
        if new_sl is not None:
            state[key]["cur_sl"] = new_sl
        if tp1_hit_at is not None: state[key]["tp1_hit_at"] = tp1_hit_at
        if tp2_hit_at is not None: state[key]["tp2_hit_at"] = tp2_hit_at
        if limit_filled is not None: state[key]["limit_filled"] = limit_filled
        if limit_filled_at is not None: state[key]["limit_filled_at"] = limit_filled_at
        for k, v in kwargs.items():
            if v is not None:
                state[key][k] = v
        _save(state)

def get_pending_trades(max_hours: float = 24.0) -> dict:
    state = _load()
    pending = {}
    for k, v in list(state.items()):
        if isinstance(v, dict) and v.get("outcome") == "PENDING":
            pending[k] = v
    return pending

def clear_all_pending_backlogs() -> int:
    """
    Purges old historical PENDING backlogs (paper-tracked signals only).
    CRITICAL: Does NOT purge trades with shark_order_placed=True —
    these are REAL live Shark Exchange positions. Their state (initial_sl, tp1/tp2/tp3)
    must be preserved across restarts for TP placement and trailing SL to work.
    """
    state = _load()
    count = 0
    preserved = 0
    for k, v in list(state.items()):
        if isinstance(v, dict) and v.get("outcome") == "PENDING":
            if v.get("shark_order_placed"):
                preserved += 1
                continue  # NEVER purge live Shark trades
            v["outcome"] = "PURGED_BACKLOG"
            count += 1
    if count > 0:
        _save(state)
        logger.info(f"🧹 STATE CLEANUP: Purged {count} old paper backlogs. Preserved {preserved} live Shark trade(s).")
    return count

def get_scoreboard() -> tuple:
    return 0, 0, 0

def get_scoreboard_str() -> str:
    return ""


def _parse_iso_dt(dt_str: str):
    if not dt_str or not isinstance(dt_str, str): return None
    try:
        clean_str = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def purge_stale_pending_trades(max_hours: float = 24.0) -> int:
    """Never force expire pending trades automatically; preserved until TP or SL hit."""
    return 0

def clear_all_cooldowns() -> int:
    state = _load()
    keys_to_del = [k for k in state.keys() if k.startswith("COOLDOWN|")]
    for k in keys_to_del:
        del state[k]
    if keys_to_del:
        _save(state)
        logger.info(f"Cleared {len(keys_to_del)} per-coin cooldown entries for a fresh scan.")
    return len(keys_to_del)

def purge_all_state_fresh() -> None:
    logger.info("STATE RESET BLOCKED: Historical trades and win/loss records are permanently protected.")

def purge_old_entries(days: int = 3650) -> None:
    """Permanent preservation: never delete historical entries so win/loss stats are never lost."""
    return



def get_all_signals() -> list:
    """Return a list of parsed signal objects sorted by time descending."""
    state = _load()
    signals = []
    for key, val in state.items():
        try:
            parts = key.split("|")
            if len(parts) == 4:
                symbol, tf, signal, candle_ts = parts
                sent_at = val["sent_at"] if isinstance(val, dict) else val
                signals.append({
                    "symbol": symbol,
                    "tf": tf,
                    "signal": signal,
                    "candle_ts": candle_ts,
                    "sent_at": sent_at,
                    "entry": val.get("entry") if isinstance(val, dict) else None,
                    "sl": val.get("sl") if isinstance(val, dict) else None,
                    "tp": val.get("tp") if isinstance(val, dict) else None,
                    "outcome": val.get("outcome") if isinstance(val, dict) else None
                })
        except Exception:
            pass
    signals.sort(key=lambda x: x["sent_at"], reverse=True)
    return signals

def is_news_sent(news_id: str) -> bool:
    state = _load()
    key = f"NEWS|{news_id}"
    return key in state

def mark_news_sent(news_id: str) -> None:
    state = _load()
    key = f"NEWS|{news_id}"
    state[key] = {
        "sent_at": datetime.now(timezone.utc).isoformat()
    }
    _save(state)

def purge_old_news(hours: float = 24.0) -> int:
    state = _load()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    news_keys = [k for k in state.keys() if k.startswith("NEWS|")]
    purged = 0
    for k in news_keys:
        val = state[k]
        sent_at_str = val.get("sent_at") if isinstance(val, dict) else None
        if sent_at_str:
            try:
                if datetime.fromisoformat(sent_at_str.replace("Z", "+00:00")) < cutoff:
                    del state[k]
                    purged += 1
            except Exception:
                pass
    if purged > 0:
        _save(state)
        logger.info(f"Purged {purged} old news entries older than {hours}h")
    return purged



# ─── PENDING RETEST TRACKER (58% WIN RATE CONFIRMATION ENGINE) ─────────────

def set_pending_retest(symbol: str, data: dict) -> None:
    state = _load()
    if "_pending_retests" not in state:
        state["_pending_retests"] = {}
    state["_pending_retests"][symbol] = data
    _save(state)

def get_pending_retest(symbol: str) -> dict:
    state = _load()
    pending = state.get("_pending_retests", {})
    return pending.get(symbol)

def remove_pending_retest(symbol: str) -> None:
    state = _load()
    pending = state.get("_pending_retests", {})
    if symbol in pending:
        del pending[symbol]
        state["_pending_retests"] = pending
        _save(state)

def get_all_pending_retests() -> dict:
    state = _load()
    return state.get("_pending_retests", {})


# ── PRE-SQUEEZE RADAR STATE TRACKER ──

def is_global_radar_on_cooldown(cooldown_minutes: float = 10.0) -> bool:
    """Prevents spamming Telegram by enforcing minimum interval between ANY two radar alerts."""
    state = _load()
    last_sent_str = state.get("GLOBAL_RADAR_LAST_SENT")
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.fromisoformat(last_sent_str)
        if (datetime.now(timezone.utc) - last_sent) < timedelta(minutes=cooldown_minutes):
            return True
    except Exception:
        pass
    return False

def record_global_radar_sent() -> None:
    """Records the global timestamp when any radar alert was broadcast."""
    state = _load()
    state["GLOBAL_RADAR_LAST_SENT"] = datetime.now(timezone.utc).isoformat()
    _save(state)

def is_radar_on_cooldown(symbol: str, cooldown_hours: float = 4.0) -> bool:
    """Prevents spamming Pre-Squeeze Radar alerts for the same symbol within cooldown_hours."""
    state = _load()
    key = f"RADAR_COOLDOWN|{symbol.upper()}"
    last_sent_str = state.get(key)
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.fromisoformat(last_sent_str)
        if (datetime.now(timezone.utc) - last_sent) < timedelta(hours=cooldown_hours):
            return True
    except Exception:
        pass
    return False

def record_pre_squeeze_radar(symbol: str, radar_data: dict) -> None:
    """Records a new active Pre-Squeeze Radar alert into state."""
    state = _load()
    sym = symbol.upper()
    now_str = datetime.now(timezone.utc).isoformat()
    state[f"RADAR_COOLDOWN|{sym}"] = now_str
    if "ACTIVE_RADARS" not in state or not isinstance(state["ACTIVE_RADARS"], dict):
        state["ACTIVE_RADARS"] = {}
    state["ACTIVE_RADARS"][sym] = {
        "direction": radar_data.get("direction", "BUY"),
        "squeeze_type": radar_data.get("squeeze_type", "SHORT SQUEEZE"),
        "trigger_entry": radar_data.get("trigger_entry", 0.0),
        "invalidation_sl": radar_data.get("invalidation_sl", 0.0),
        "compression_pct": radar_data.get("compression_pct", 0.0),
        "created_at": now_str,
        "status": "WATCHING"
    }
    _save(state)

def get_active_radar(symbol: str) -> dict:
    """Returns active radar data if symbol is currently on Radar Watchlist and not expired (4h max)."""
    state = _load()
    radars = state.get("ACTIVE_RADARS", {})
    entry = radars.get(symbol.upper())
    if entry and entry.get("status") == "WATCHING":
        created_str = entry.get("created_at")
        if created_str:
            try:
                created = datetime.fromisoformat(created_str)
                if (datetime.now(timezone.utc) - created) > timedelta(hours=4.0):
                    entry["status"] = "EXPIRED"
                    _save(state)
                    return None
            except Exception:
                pass
        return entry
    return None

def mark_radar_triggered(symbol: str) -> None:
    """Marks an active radar as TRIGGERED when Stage 2 entry fires."""
    state = _load()
    radars = state.get("ACTIVE_RADARS", {})
    sym = symbol.upper()
    if sym in radars:
        radars[sym]["status"] = "TRIGGERED"
        radars[sym]["triggered_at"] = datetime.now(timezone.utc).isoformat()
        _save(state)
