"""
channel_manager.py — Manages members, kicks, and bans for EMA Cross and EMA Signal Scanner channels.
Persists member lists and ban lists under /data/channel_members.json on Railway.
"""

import json
import os
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

logger = logging.getLogger("channel_manager")

if os.path.isdir("/data"):
    CHANNEL_MEMBERS_FILE = "/data/channel_members.json"
    CHANNEL_BANNED_FILE = "/data/channel_banned.json"
else:
    CHANNEL_MEMBERS_FILE = "channel_members.json"
    CHANNEL_BANNED_FILE = "channel_banned.json"

BACKUP_MEMBERS_FILE = "channel_members_backup.json"

CHANNELS = {
    "-1004332146822": "EMA SIGNALS SCANNER",
    "-1004463573705": "EMA CROSS",
}

_members_cache = None

def _load_members() -> Dict[str, Any]:
    global _members_cache
    if _members_cache is not None:
        return _members_cache

    data = {}
    if os.path.exists(CHANNEL_MEMBERS_FILE):
        try:
            with open(CHANNEL_MEMBERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading {CHANNEL_MEMBERS_FILE}: {e}")

    # Dual backup recovery
    if not data and os.path.exists(BACKUP_MEMBERS_FILE):
        try:
            with open(BACKUP_MEMBERS_FILE, "r", encoding="utf-8") as fb:
                data = json.load(fb)
        except Exception:
            pass

    for ch_id, ch_title in CHANNELS.items():
        if ch_id not in data:
            data[ch_id] = {
                "title": ch_title,
                "members": {}
            }

    _members_cache = data
    return _members_cache

def _save_members(data: Dict[str, Any]) -> None:
    global _members_cache
    _members_cache = data
    try:
        os.makedirs(os.path.dirname(CHANNEL_MEMBERS_FILE) if os.path.dirname(CHANNEL_MEMBERS_FILE) else ".", exist_ok=True)
        with open(CHANNEL_MEMBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(_members_cache, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving {CHANNEL_MEMBERS_FILE}: {e}")

    try:
        with open(BACKUP_MEMBERS_FILE, "w", encoding="utf-8") as fb:
            json.dump(_members_cache, fb, indent=2)
    except Exception:
        pass

def track_member_update(chat_id: str, user_id: str, username: str = None, first_name: str = None, status: str = "member"):
    """Tracks member join, leave, or status update in a channel."""
    data = _load_members()
    cid = str(chat_id).strip()
    uid = str(user_id).strip()

    if cid not in data:
        data[cid] = {"title": CHANNELS.get(cid, "Channel"), "members": {}}

    if status in ["member", "administrator", "creator", "restricted"]:
        data[cid]["members"][uid] = {
            "username": username or "",
            "first_name": first_name or "Unknown",
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
    elif status in ["left", "kicked"]:
        if uid in data[cid]["members"]:
            del data[cid]["members"][uid]

    _save_members(data)

def get_channel_members(chat_id: str) -> Dict[str, Any]:
    data = _load_members()
    cid = str(chat_id).strip()
    return data.get(cid, {}).get("members", {})

def get_all_channel_data() -> Dict[str, Any]:
    return _load_members()

async def kick_channel_member(bot, chat_id: str, user_id: str) -> tuple[bool, str]:
    """Kicks a member from a channel (bans then unbans so they are removed but not permanently blocked from re-joining)."""
    try:
        cid = int(chat_id)
        uid = int(user_id)
        # Ban to remove
        await bot.ban_chat_member(chat_id=cid, user_id=uid)
        # Unban to allow them to be re-invited in future if needed
        await bot.unban_chat_member(chat_id=cid, user_id=uid)
        # Remove from local tracking
        track_member_update(str(chat_id), str(user_id), status="kicked")
        return True, "Kicked successfully"
    except Exception as e:
        logger.error(f"Failed to kick user {user_id} from {chat_id}: {e}")
        return False, str(e)

async def ban_channel_member(bot, chat_id: str, user_id: str) -> tuple[bool, str]:
    """Permanently bans a member from a channel."""
    try:
        cid = int(chat_id)
        uid = int(user_id)
        await bot.ban_chat_member(chat_id=cid, user_id=uid)
        track_member_update(str(chat_id), str(user_id), status="kicked")
        return True, "Banned permanently"
    except Exception as e:
        logger.error(f"Failed to ban user {user_id} from {chat_id}: {e}")
        return False, str(e)

async def sync_channel_admins(bot):
    """Auto-discovers and populates channel administrators into member cache."""
    for ch_id in CHANNELS:
        try:
            admins = await bot.get_chat_administrators(ch_id)
            for a in admins:
                u = a.user
                if not u.is_bot:
                    track_member_update(
                        chat_id=ch_id,
                        user_id=str(u.id),
                        username=u.username,
                        first_name=u.first_name,
                        status=a.status
                    )
        except Exception as e:
            logger.debug(f"Could not auto-fetch admins for {ch_id}: {e}")
