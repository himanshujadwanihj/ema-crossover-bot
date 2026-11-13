"""
user_prefs.py — Manages per-user multi-select alert subscription preferences for AlphaCryptoSignals Bot.
Stores preferences persistently under /data/user_subscriptions.json on Railway.

Strategy Timeframe Architecture (User Directive):
  - Fresh Crossovers: 1H and above by default (1H, 4H, 1D, 1W) + 30M early cross for fresh crossovers only.
  - Live Retests: 1H and above by default (1H, 4H, 1D, 1W).
  - Squeeze Breakouts & Coiling Radar: 1H and above (1H institutional coiling).
  - Gold Scalps: 5M & 15M scalps (strictly for Gold when Gold Scalp filter is ON).
"""

import json
import os
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Set

logger = logging.getLogger(__name__)

if os.path.isdir("/data"):
    PREFS_FILE = "/data/user_subscriptions.json"
    BACKUP_PREFS_FILE = "/data/user_subscriptions_backup.json"
    BANNED_FILE = "/data/banned_users.json"
    BACKUP_BANNED_FILE = "/data/banned_users_backup.json"
else:
    PREFS_FILE = "user_subscriptions.json"
    BACKUP_PREFS_FILE = "user_subscriptions_backup.json"
    BANNED_FILE = "banned_users.json"
    BACKUP_BANNED_FILE = "banned_users_backup.json"

DEFAULT_TIMEFRAMES = ["1h", "4h", "1d", "1w"]
DEFAULT_COIN_GROUPS = ["core_10", "alts_25"]
DEFAULT_SIGNAL_TYPES = ["crossover", "retest"]
DEFAULT_MIN_STARS = 3

_prefs_cache = None

def _load_banned() -> Dict[str, Any]:
    banned = {}
    if os.path.exists(BANNED_FILE):
        try:
            with open(BANNED_FILE, "r", encoding="utf-8") as f:
                banned = json.load(f)
        except Exception:
            pass
    if not banned and os.path.exists(BACKUP_BANNED_FILE):
        try:
            with open(BACKUP_BANNED_FILE, "r", encoding="utf-8") as fb:
                banned = json.load(fb)
        except Exception:
            pass
    return banned

def _save_banned(banned: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(BANNED_FILE) if os.path.dirname(BANNED_FILE) else ".", exist_ok=True)
        with open(BANNED_FILE, "w", encoding="utf-8") as f:
            json.dump(banned, f, indent=2)
    except Exception:
        pass
    try:
        with open(BACKUP_BANNED_FILE, "w", encoding="utf-8") as fb:
            json.dump(banned, fb, indent=2)
    except Exception:
        pass

def is_user_banned(chat_id: str) -> bool:
    banned = _load_banned()
    return str(chat_id).strip() in banned

def remove_user(chat_id: str) -> bool:
    """Removes a user completely from active subscriptions and dual backups."""
    cid = str(chat_id).strip()
    prefs = _load_prefs()
    found = False
    if cid in prefs:
        del prefs[cid]
        found = True
        _save_prefs(prefs)
    
    if os.path.exists(BACKUP_PREFS_FILE):
        try:
            with open(BACKUP_PREFS_FILE, "r", encoding="utf-8") as fb:
                bdata = json.load(fb)
            if cid in bdata:
                del bdata[cid]
                found = True
                with open(BACKUP_PREFS_FILE, "w", encoding="utf-8") as fb:
                    json.dump(bdata, fb, indent=2)
        except Exception:
            pass
    return found

def ban_user(chat_id: str, reason: str = "") -> bool:
    """Permanently bans a user so they cannot receive alerts or re-subscribe."""
    cid = str(chat_id).strip()
    prefs = _load_prefs()
    user_info = prefs.get(cid, {})
    uname = user_info.get("username", "")
    fname = user_info.get("first_name", "")
    remove_user(cid)
    banned = _load_banned()
    banned[cid] = {
        "username": uname,
        "first_name": fname,
        "banned_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason or "Manual Admin Ban"
    }
    _save_banned(banned)
    return True

def unban_user(chat_id: str) -> bool:
    """Unbans a user allowing them to use the bot again."""
    cid = str(chat_id).strip()
    banned = _load_banned()
    if cid in banned:
        b_info = banned.pop(cid)
        _save_banned(banned)
        prefs = _load_prefs()
        if cid not in prefs:
            prefs[cid] = {
                "enabled": True,
                "coin_groups": list(DEFAULT_COIN_GROUPS),
                "timeframes": list(DEFAULT_TIMEFRAMES),
                "signal_types": list(DEFAULT_SIGNAL_TYPES),
                "custom_coins": [],
                "min_stars": DEFAULT_MIN_STARS,
                "gold_scalp_5m_15m": False,
                "squeeze_alerts": True,
                "squeeze_only": False,
                "username": b_info.get("username", "") if isinstance(b_info, dict) else "",
                "first_name": b_info.get("first_name", "") if isinstance(b_info, dict) else "",
                "joined_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            }
            _save_prefs(prefs)
        return True
    return False

def get_banned_users() -> Dict[str, Any]:
    return _load_banned()


def _load_prefs() -> Dict[str, Any]:
    global _prefs_cache
    if _prefs_cache is not None:
        return _prefs_cache

    data = {}
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading {PREFS_FILE}: {e}")
            data = {}

    if not data and os.path.exists(BACKUP_PREFS_FILE):
        try:
            with open(BACKUP_PREFS_FILE, "r", encoding="utf-8") as fb:
                data = json.load(fb)
        except Exception as eb:
            logger.error(f"Error loading {BACKUP_PREFS_FILE}: {eb}")

    # Fallback seed only if persistent volume is brand new and empty on first boot
    if not data and PREFS_FILE != "user_subscriptions.json" and os.path.exists("user_subscriptions.json"):
        try:
            with open("user_subscriptions.json", "r", encoding="utf-8") as f_seed:
                data = json.load(f_seed)
                logger.info("Seeded user_subscriptions from local file on first volume boot.")
        except Exception:
            pass

    _prefs_cache = data
    return _prefs_cache

def _save_prefs(data: Dict[str, Any]) -> None:
    global _prefs_cache
    _prefs_cache = data
    try:
        os.makedirs(os.path.dirname(PREFS_FILE) if os.path.dirname(PREFS_FILE) else ".", exist_ok=True)
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(_prefs_cache, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving {PREFS_FILE}: {e}")

    # Synchronous dual backup
    try:
        with open(BACKUP_PREFS_FILE, "w", encoding="utf-8") as fb:
            json.dump(_prefs_cache, fb, indent=2)
    except Exception as eb:
        logger.error(f"Error saving {BACKUP_PREFS_FILE}: {eb}")

def get_user_prefs(chat_id: str, user_obj=None) -> Dict[str, Any]:
    if is_user_banned(chat_id):
        return {"enabled": False, "banned": True}
    prefs = _load_prefs()
    cid = str(chat_id)
    if cid not in prefs:
        prefs[cid] = {
            "enabled": True,
            "coin_groups": list(DEFAULT_COIN_GROUPS),
            "timeframes": list(DEFAULT_TIMEFRAMES),
            "signal_types": list(DEFAULT_SIGNAL_TYPES),
            "custom_coins": [],
            "min_stars": DEFAULT_MIN_STARS,
            "gold_scalp_5m_15m": False,
            "squeeze_alerts": True,
            "squeeze_only": False,
            "username": getattr(user_obj, "username", "") if user_obj else "",
            "first_name": getattr(user_obj, "first_name", "") if user_obj else "",
            "joined_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }
        _save_prefs(prefs)
    else:
        if user_obj:
            uname = getattr(user_obj, "username", "")
            fname = getattr(user_obj, "first_name", "")
            if uname: prefs[cid]["username"] = uname
            if fname: prefs[cid]["first_name"] = fname
            _save_prefs(prefs)
        u = prefs[cid]
        modified = False
        if "coin_groups" not in u:
            u["coin_groups"] = list(DEFAULT_COIN_GROUPS)
            modified = True
        if "signal_types" not in u:
            u["signal_types"] = list(DEFAULT_SIGNAL_TYPES)
            modified = True
        if "timeframes" not in u:
            u["timeframes"] = list(DEFAULT_TIMEFRAMES)
            modified = True
        if "gold_scalp_5m_15m" not in u:
            u["gold_scalp_5m_15m"] = False
            modified = True
        if "squeeze_alerts" not in u:
            u["squeeze_alerts"] = True
            modified = True
        if "squeeze_only" not in u:
            u["squeeze_only"] = False
            modified = True
        if modified:
            _save_prefs(prefs)
    return prefs[cid]

def update_user_pref(chat_id: str, key: str, value: Any) -> Dict[str, Any]:
    cid = str(chat_id)
    prefs = _load_prefs()
    if cid not in prefs:
        get_user_prefs(cid)
        prefs = _load_prefs()
    prefs[cid][key] = value
    _save_prefs(prefs)
    return prefs[cid]

def toggle_timeframe(chat_id: str, tf: str) -> Dict[str, Any]:
    u = get_user_prefs(chat_id)
    tfs = set(u.get("timeframes", list(DEFAULT_TIMEFRAMES)))
    tf_norm = tf.lower().replace("min", "m")
    if tf_norm in tfs:
        tfs.remove(tf_norm)
    else:
        tfs.add(tf_norm)
    order = ["15m", "30m", "1h", "4h", "1d", "1w"]
    sorted_tfs = [t for t in order if t in tfs]
    return update_user_pref(chat_id, "timeframes", sorted_tfs)

def toggle_signal_type(chat_id: str, stype: str) -> Dict[str, Any]:
    u = get_user_prefs(chat_id)
    sigs = set(u.get("signal_types", list(DEFAULT_SIGNAL_TYPES)))
    stype_norm = stype.lower()
    if stype_norm in sigs:
        sigs.remove(stype_norm)
    else:
        sigs.add(stype_norm)
    order = ["crossover", "retest"]
    sorted_sigs = [s for s in order if s in sigs]
    return update_user_pref(chat_id, "signal_types", sorted_sigs)

def toggle_coin_group(chat_id: str, group: str) -> Dict[str, Any]:
    u = get_user_prefs(chat_id)
    groups = set(u.get("coin_groups", list(DEFAULT_COIN_GROUPS)))
    group_norm = group.lower()
    if group_norm in groups:
        groups.remove(group_norm)
    else:
        groups.add(group_norm)
    order = ["core_10", "alts_25", "commodities", "us_stocks", "custom", "all_235"]
    sorted_groups = [g for g in order if g in groups]
    return update_user_pref(chat_id, "coin_groups", sorted_groups)

def toggle_min_stars(chat_id: str) -> Dict[str, Any]:
    u = get_user_prefs(chat_id)
    cur = u.get("min_stars", 3)
    new_val = 4 if cur == 3 else 3
    return update_user_pref(chat_id, "min_stars", new_val)

def toggle_gold_scalp(chat_id: str) -> Dict[str, Any]:
    u = get_user_prefs(chat_id)
    cur = u.get("gold_scalp_5m_15m", False)
    return update_user_pref(chat_id, "gold_scalp_5m_15m", not cur)

def toggle_squeeze_alerts(chat_id: str) -> Dict[str, Any]:
    u = get_user_prefs(chat_id)
    cur = u.get("squeeze_alerts", True)
    return update_user_pref(chat_id, "squeeze_alerts", not cur)

def toggle_squeeze_only(chat_id: str) -> Dict[str, Any]:
    u = get_user_prefs(chat_id)
    val = not u.get("squeeze_only", False)
    return update_user_pref(chat_id, "squeeze_only", val)

def set_min_stars(chat_id: str, stars: int) -> Dict[str, Any]:
    return update_user_pref(chat_id, "min_stars", int(stars))

def add_custom_coins(chat_id: str, coins: List[str]) -> Dict[str, Any]:
    u = get_user_prefs(chat_id)
    current = set(u.get("custom_coins", []))
    for c in coins:
        sym = c.upper().strip().replace("/", "")
        if sym.endswith("USDT"):
            sym = sym[:-4]
        sym = f"{sym}/USDT"
        current.add(sym)
    u["custom_coins"] = sorted(list(current))
    groups = set(u.get("coin_groups", []))
    groups.add("custom")
    u["coin_groups"] = sorted(list(groups))
    _save_prefs(_load_prefs())
    return u

def remove_custom_coin(chat_id: str, coin: str) -> Dict[str, Any]:
    u = get_user_prefs(chat_id)
    current = set(u.get("custom_coins", []))
    sym = coin.upper().strip().replace("/", "")
    if sym.endswith("USDT"):
        sym = sym[:-4]
    sym = f"{sym}/USDT"
    if sym in current:
        current.remove(sym)
    u["custom_coins"] = sorted(list(current))
    _save_prefs(_load_prefs())
    return u

def is_alert_allowed_for_user(chat_id: str, alert_data: dict) -> bool:
    if is_user_banned(chat_id):
        return False
    """
    Checks whether an alert matches this specific user's multi-selected preferences.
    Enforces user timeframe architecture:
      - Fresh Crossovers: 1H and above by default + 30M early cross
      - Live Retests: 1H and above by default (no 30m retests)
      - Squeeze Alerts: 1H and above
      - Gold Scalps: 5M & 15M (when gold scalp filter is enabled)
    """
    u = get_user_prefs(chat_id)
    if not u.get("enabled", True):
        return False

    raw_symbol = str(alert_data.get("symbol", "")).upper().strip()
    clean_sym = raw_symbol.replace("/", "")
    tf_label = str(alert_data.get("tf_label", "")).lower().replace("min", "m")
    aligned_count = alert_data.get("aligned_count", 0)
    cross_type = str(alert_data.get("cross_type", ""))
    is_retest = "Retest" in cross_type
    is_cross = ("Cross" in cross_type or "Crossover" in cross_type) and not is_retest

    # Squeeze identification (Strict separation: Retests and Crossovers are NEVER Squeezes)
    is_squeeze_signal = (alert_data.get("is_dedicated_squeeze", False) or "Squeeze" in cross_type or "Breakout" in cross_type or "Breakdown" in cross_type or "Radar" in cross_type) and not is_retest and not is_cross

    # STRICT USER DIRECTIVE: No trades of commodities, BTC, and ETH on Sat and Sunday in any case
    from config import is_weekend_trading_blocked
    if is_weekend_trading_blocked(raw_symbol):
        return False

    # STRICT SQUEEZE TIMEFRAME RULE (User Directive: '1 hr squeze pr hi raho fir')
    if is_squeeze_signal and tf_label not in ["1h", "1hr", "60m"]:
        return False

    # 1. Squeeze Alerts Control:
    if is_squeeze_signal and not u.get("squeeze_alerts", True):
        return False

    # Squeeze Only Filter: If user toggled squeeze_only, reject all non-squeeze signals
    if u.get("squeeze_only", False) and not is_squeeze_signal:
        return False

    # 2. Gold 5M & 15M Scalp Identification:
    is_gold = any(g in clean_sym for g in ["XAU", "XAUT", "GC=F"]) or raw_symbol in ["XAU/USD", "XAUT/USDT"]
    is_gold_scalp_tf = is_gold and (tf_label in ["5m", "15m"])
    user_groups = set(u.get("coin_groups", list(DEFAULT_COIN_GROUPS)))
    gold_scalp_enabled = u.get("gold_scalp_5m_15m", False)

    # 5M & 15M timeframes: strictly reserved for Gold scalps across the platform!
    # Non-Gold coins NEVER receive 5M or 15M alerts.
    if tf_label in ["5m", "15m"]:
        if not is_gold:
            return False
        if not gold_scalp_enabled:
            return False

    # 3. Timeframe Architecture (Strategy Enforced - No Manual Time Filter Needed):
    # - 30M: Allowed strictly for Fresh Crossovers (Retests NEVER allow 30M).
    # - 1H, 4H, 1D, 1W: Standard timeframes for Crossovers, Retests, and Squeeze setups.
    user_sigs = set(u.get("signal_types", list(DEFAULT_SIGNAL_TYPES)))

    if tf_label in ["30m"]:
        if is_retest:
            return False
        if "crossover" not in user_sigs:
            return False

    # 5. Setup Type Multi-Check (Crossover vs. Retest)
    if not is_squeeze_signal and not (is_gold_scalp_tf and gold_scalp_enabled):
        if is_retest and "retest" not in user_sigs:
            return False
        if not is_retest and "crossover" not in user_sigs:
            return False

    # 6. Confluence Stars (Strict 3★ or 4★ Filter)
    min_stars = int(u.get("min_stars", 3))
    # ONLY Gold 5M/15M scalps and Pre-Squeeze Radar are exempt from star confluence.
    # All Crossovers, Retests, and Squeeze Breakouts MUST strictly satisfy user's min_stars!
    is_exempt_from_min_stars = (is_gold_scalp_tf and gold_scalp_enabled) or ("Radar" in cross_type)
    if not is_exempt_from_min_stars and aligned_count < min_stars:
        return False

    # 7. Coin Groups Multi-Check (Matches if symbol belongs to ANY of user's active groups)
    from pullback_tracker import CORE_10, TOP_25_ALTS, COMMODITIES, US_EQUITIES
    core_10_clean = {c.replace("/", "").upper() for c in CORE_10}
    alts_25_clean = {c.replace("/", "").upper() for c in TOP_25_ALTS}
    commodities_clean = {c.replace("/", "").upper() for c in COMMODITIES}
    us_equities_clean = {c.replace("/", "").upper() for c in US_EQUITIES}
    custom_clean = {c.replace("/", "").upper() for c in u.get("custom_coins", [])}

    matched_group = False
    # Squeeze signals MUST also respect user's selected coin groups!
    # (No more "squeeze = all coins" bypass — only selected coins get squeeze alerts)
    if "core_10" in user_groups and clean_sym in core_10_clean:
        matched_group = True
    elif "alts_25" in user_groups and clean_sym in alts_25_clean:
        matched_group = True
    elif ("commodities" in user_groups or (is_gold and gold_scalp_enabled)) and (clean_sym in commodities_clean or is_gold):
        matched_group = True
    elif "us_stocks" in user_groups and clean_sym in us_equities_clean:
        matched_group = True
    elif "custom" in user_groups and clean_sym in custom_clean:
        matched_group = True
    elif "all_235" in user_groups and (not is_retest):
        matched_group = True

    if not matched_group:
        return False

    return True

def get_all_subscribers() -> List[str]:
    """Returns all chat IDs registered with AlphaCryptoSignals bot."""
    prefs = _load_prefs()
    return [cid for cid, v in prefs.items() if v.get("enabled", True)]
