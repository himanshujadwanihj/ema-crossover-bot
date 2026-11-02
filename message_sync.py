"""
message_sync.py — Unified Real-Time Message Deletion Sync Engine.
Tracks all messages sent to admin, channels, and subscribers.
When the admin deletes a message in their bot chat (or uses /del),
this engine automatically propagates the deletion to ALL users and channels.
"""

import os
import json
import time
import asyncio
import logging
from typing import Dict, List, Any
import httpx
from config import TELEGRAM_BOT_TOKEN, FILTER_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("message_sync")

REGISTRY_FILE = "/data/broadcast_messages.json" if os.path.isdir("/data") else "broadcast_messages.json"
ADMIN_CHAT_ID = str(TELEGRAM_CHAT_ID or "771305593")

_registry: List[Dict[str, Any]] = []
_loaded = False

def _load_registry():
    global _registry, _loaded
    if _loaded:
        return _registry
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
                _registry = json.load(f)
        except Exception as e:
            logger.error(f"Error loading {REGISTRY_FILE}: {e}")
            _registry = []
    _loaded = True
    return _registry

def _save_registry():
    try:
        os.makedirs(os.path.dirname(REGISTRY_FILE) if os.path.dirname(REGISTRY_FILE) else ".", exist_ok=True)
        # Keep only the last 200 records to prevent memory/disk bloat
        trimmed = _registry[-200:]
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving {REGISTRY_FILE}: {e}")

def record_broadcast_group(admin_bot_type: str, admin_msg_id: int, recipients: List[Dict[str, Any]], desc: str = ""):
    """
    admin_bot_type: 'main' or 'filter'
    admin_msg_id: message_id in admin's chat
    recipients: list of {'bot_type': 'main'|'filter', 'chat_id': str, 'message_id': int}
    """
    _load_registry()
    entry = {
        "id": f"{int(time.time())}_{admin_msg_id}",
        "timestamp": time.time(),
        "admin_bot_type": admin_bot_type,
        "admin_chat_id": ADMIN_CHAT_ID,
        "admin_msg_id": admin_msg_id,
        "recipients": recipients,
        "desc": desc[:80],
        "deleted": False
    }
    _registry.append(entry)
    _save_registry()
    logger.info(f"Registered broadcast group (admin_msg_id={admin_msg_id}) with {len(recipients)} recipients.")

async def is_message_alive(bot_token: str, chat_id: str, message_id: int) -> bool:
    """Checks if a message still exists without sending notifications or modifying text."""
    url = f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup"
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 429:
                retry_after = 3.0
                try:
                    retry_after = float(resp.json().get("parameters", {}).get("retry_after", 3.0))
                except Exception:
                    pass
                logger.debug(f"Telegram 429 rate limit in is_message_alive. Sleeping {retry_after}s...")
                await asyncio.sleep(retry_after)
                return True

            data = resp.json()
            if data.get("ok"):
                return True
            desc = data.get("description", "").lower()
            if "message is not modified" in desc:
                return True
            if "message to edit not found" in desc or "message can't be found" in desc:
                return False
            return True
    except Exception:
        return True

async def delete_remote_message(bot_token: str, chat_id: str, message_id: int) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "message_id": message_id})
            return resp.json().get("ok", False)
    except Exception as e:
        logger.debug(f"Failed to delete {chat_id}:{message_id}: {e}")
        return False

async def propagate_deletion_for_entry(entry: Dict[str, Any]):
    if entry.get("deleted"):
        return
    recipients = entry.get("recipients", [])
    logger.info(f"🗑️ Propagating deletion for group {entry['id']} (admin_msg_id={entry.get('admin_msg_id')}) to {len(recipients)} recipients...")
    tokens = {
        "main": TELEGRAM_BOT_TOKEN,
        "filter": FILTER_BOT_TOKEN
    }
    for r in recipients:
        btype = r.get("bot_type", "filter")
        token = tokens.get(btype, FILTER_BOT_TOKEN)
        if token:
            await delete_remote_message(token, str(r["chat_id"]), int(r["message_id"]))
    entry["deleted"] = True
    _save_registry()

async def delete_by_admin_message_id(admin_msg_id: int) -> bool:
    _load_registry()
    found = False
    for entry in reversed(_registry):
        if not entry.get("deleted") and entry.get("admin_msg_id") == admin_msg_id:
            await propagate_deletion_for_entry(entry)
            found = True
            break
    return found

async def delete_last_n_broadcasts(count: int = 1) -> int:
    _load_registry()
    deleted_count = 0
    tokens = {"main": TELEGRAM_BOT_TOKEN, "filter": FILTER_BOT_TOKEN}
    for entry in reversed(_registry):
        if not entry.get("deleted"):
            # Also delete admin's copy if still exists
            admin_token = tokens.get(entry.get("admin_bot_type", "filter"), FILTER_BOT_TOKEN)
            if admin_token:
                await delete_remote_message(admin_token, entry["admin_chat_id"], entry["admin_msg_id"])
            await propagate_deletion_for_entry(entry)
            deleted_count += 1
            if deleted_count >= count:
                break
    return deleted_count

async def start_deletion_sync_watcher():
    """Continuous background loop running every 15 seconds to detect if Admin deleted any message."""
    logger.info("🛡️ Deletion Sync Watcher started (paced rate-limited monitoring every 15s)...")
    tokens = {
        "main": TELEGRAM_BOT_TOKEN,
        "filter": FILTER_BOT_TOKEN
    }
    while True:
        try:
            await asyncio.sleep(15.0)
            _load_registry()
            now = time.time()
            active_entries = [
                e for e in _registry
                if not e.get("deleted") and (now - e.get("timestamp", 0) < 86400)
            ]
            for entry in active_entries[-5:]:
                btype = entry.get("admin_bot_type", "filter")
                token = tokens.get(btype, FILTER_BOT_TOKEN)
                if not token:
                    continue
                alive = await is_message_alive(token, entry["admin_chat_id"], entry["admin_msg_id"])
                if not alive:
                    logger.info(f"⚡ Admin deleted message {entry['admin_msg_id']} in chat {entry['admin_chat_id']}! Automatically deleting across all users...")
                    await propagate_deletion_for_entry(entry)
                await asyncio.sleep(1.2)  # Strict Telegram compliance: 1 request/sec per chat
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in deletion sync watcher loop: {e}")
            await asyncio.sleep(10.0)
