"""
admin_panel.py — Unified Admin Control Panel for:
- EMA Signal Scanner (-1004332146822)
- EMA Cross (-1004463573705)
- AlphaCryptoSignals (user_prefs)

Provides unified user listing, 1-tap command links, and interactive inline action buttons:
  • Remove EMA Scanner
  • Remove EMA Cross
  • Remove Alpha
  • Ban EMA Scanner
  • Ban EMA Cross
  • Ban Alpha
  • Unban EMA Scanner
  • Unban EMA Cross
  • Unban Alpha
"""

import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
import user_prefs
import channel_manager
from config import TELEGRAM_CHAT_ID

logger = logging.getLogger("admin_panel")

ADMIN_CHAT_ID = str(TELEGRAM_CHAT_ID or "771305593").strip()
SCANNER_CHAT_ID = "-1004332146822"
CROSS_CHAT_ID = "-1004463573705"


def get_all_users_combined() -> dict:
    """Aggregates all unique users across Alpha, Scanner, and Cross."""
    combined = {}

    # 1. Alpha subscribers
    alpha_users = user_prefs._load_prefs()
    for uid, u in alpha_users.items():
        if uid == ADMIN_CHAT_ID:
            continue
        combined[uid] = {
            "name": u.get("first_name") or u.get("username") or f"User {uid[-4:]}",
            "username": u.get("username") or "",
            "in_alpha": True,
            "in_scanner": False,
            "in_cross": False,
        }

    # 2. Alpha banned users
    banned_alpha = user_prefs.get_banned_users()
    for uid, b in banned_alpha.items():
        if uid == ADMIN_CHAT_ID:
            continue
        if uid not in combined:
            combined[uid] = {
                "name": b.get("first_name") or b.get("username") or f"User {uid[-4:]}",
                "username": b.get("username") or "",
                "in_alpha": False,
                "in_scanner": False,
                "in_cross": False,
            }
        combined[uid]["banned_alpha"] = True

    # 3. Channel members
    ch_data = channel_manager.get_all_channel_data()
    for ch_id, ch_info in ch_data.items():
        is_scanner = "4332146822" in str(ch_id)
        members = ch_info.get("members", {})
        for uid, m in members.items():
            if str(uid) == ADMIN_CHAT_ID:
                continue
            if uid not in combined:
                combined[uid] = {
                    "name": m.get("first_name") or m.get("username") or f"Member {uid[-4:]}",
                    "username": m.get("username") or "",
                    "in_alpha": False,
                    "in_scanner": False,
                    "in_cross": False,
                }
            if is_scanner:
                combined[uid]["in_scanner"] = True
            else:
                combined[uid]["in_cross"] = True

    return combined


def build_admin_overview() -> tuple[str, InlineKeyboardMarkup]:
    """Builds the main /admin overview text and user selection keyboard."""
    users = get_all_users_combined()

    msg = "👑 <b>UNIFIED ADMIN CONTROL PANEL</b>\n"
    msg += f"Active Tracked Users: <b>{len(users)}</b>\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    if not users:
        msg += "<i>No subscribers or members found yet. When users interact with the bots or channels, they will automatically appear here.</i>\n\n"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh List", callback_data="act_refresh")]])
        return msg, kb

    buttons = []
    for idx, (uid, u) in enumerate(users.items(), 1):
        uname = f"@{u['username']}" if u["username"] else "No Username"
        name = u["name"]

        # Exactly the format requested by user:
        # name
        # remove ema scanner
        # remove ema cross
        # remove alpha
        # ban ema scanner
        # ban ema cross
        # ban alpha
        # and same for unban
        msg += f"👤 <b>{idx}. {name}</b> ({uname} | <code>{uid}</code>)\n"
        msg += f"❌ <b>Remove:</b>\n"
        msg += f"  • Remove EMA Scanner: /rm_scan_{uid}\n"
        msg += f"  • Remove EMA Cross: /rm_cross_{uid}\n"
        msg += f"  • Remove Alpha: /rm_alpha_{uid}\n"
        msg += f"🚫 <b>Ban:</b>\n"
        msg += f"  • Ban EMA Scanner: /ban_scan_{uid}\n"
        msg += f"  • Ban EMA Cross: /ban_cross_{uid}\n"
        msg += f"  • Ban Alpha: /ban_alpha_{uid}\n"
        msg += f"✅ <b>Unban:</b>\n"
        msg += f"  • Unban EMA Scanner: /unban_scan_{uid}\n"
        msg += f"  • Unban EMA Cross: /unban_cross_{uid}\n"
        msg += f"  • Unban Alpha: /unban_alpha_{uid}\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

        buttons.append([InlineKeyboardButton(f"⚙️ Manage {name[:14]} ({uid[-4:]})", callback_data=f"act_user_{uid}")])

    buttons.append([InlineKeyboardButton("🔄 Refresh List", callback_data="act_refresh")])
    return msg, InlineKeyboardMarkup(buttons)


def build_user_action_menu(user_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the 9-action interactive button layout for a specific user."""
    users = get_all_users_combined()
    u = users.get(str(user_id), {})
    name = u.get("name", f"User {user_id[-4:]}")
    uname = f"@{u.get('username')}" if u.get("username") else "None"

    text = (
        f"⚙️ <b>ACTION CONTROLS FOR USER</b>\n\n"
        f"👤 <b>Name:</b> {name}\n"
        f"🔗 <b>Username:</b> {uname}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
        f"👇 <i>Tap an action below to execute immediately:</i>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Remove EMA Scanner", callback_data=f"act_rm_scan_{user_id}")],
        [InlineKeyboardButton("❌ Remove EMA Cross", callback_data=f"act_rm_cross_{user_id}")],
        [InlineKeyboardButton("❌ Remove Alpha", callback_data=f"act_rm_alpha_{user_id}")],
        [InlineKeyboardButton("🚫 Ban EMA Scanner", callback_data=f"act_ban_scan_{user_id}")],
        [InlineKeyboardButton("🚫 Ban EMA Cross", callback_data=f"act_ban_cross_{user_id}")],
        [InlineKeyboardButton("🚫 Ban Alpha", callback_data=f"act_ban_alpha_{user_id}")],
        [InlineKeyboardButton("✅ Unban EMA Scanner", callback_data=f"act_unban_scan_{user_id}")],
        [InlineKeyboardButton("✅ Unban EMA Cross", callback_data=f"act_unban_cross_{user_id}")],
        [InlineKeyboardButton("✅ Unban Alpha", callback_data=f"act_unban_alpha_{user_id}")],
        [InlineKeyboardButton("🔙 Back to Users List", callback_data="act_back_users")],
    ])

    return text, kb


async def execute_action(bot, action_key: str, target_uid: str) -> tuple[bool, str]:
    """Executes any of the 9 actions across the 3 platforms."""
    target_uid = str(target_uid).strip()

    try:
        # 1. Remove Actions (Kick)
        if action_key == "rm_scan":
            ok, res = await channel_manager.kick_channel_member(bot, SCANNER_CHAT_ID, target_uid)
            return ok, f"User {target_uid} REMOVED from EMA Scanner." if ok else f"Error: {res}"

        elif action_key == "rm_cross":
            ok, res = await channel_manager.kick_channel_member(bot, CROSS_CHAT_ID, target_uid)
            return ok, f"User {target_uid} REMOVED from EMA Cross." if ok else f"Error: {res}"

        elif action_key == "rm_alpha":
            ok = user_prefs.remove_user(target_uid)
            return ok, f"User {target_uid} REMOVED from AlphaCryptoSignals." if ok else "User was not in active subscribers."

        # 2. Ban Actions
        elif action_key == "ban_scan":
            ok, res = await channel_manager.ban_channel_member(bot, SCANNER_CHAT_ID, target_uid)
            return ok, f"User {target_uid} PERMANENTLY BANNED from EMA Scanner." if ok else f"Error: {res}"

        elif action_key == "ban_cross":
            ok, res = await channel_manager.ban_channel_member(bot, CROSS_CHAT_ID, target_uid)
            return ok, f"User {target_uid} PERMANENTLY BANNED from EMA Cross." if ok else f"Error: {res}"

        elif action_key == "ban_alpha":
            ok = user_prefs.ban_user(target_uid)
            return ok, f"User {target_uid} PERMANENTLY BANNED from AlphaCryptoSignals."

        # 3. Unban Actions
        elif action_key == "unban_scan":
            ok, res = await channel_manager.unban_channel_member(bot, SCANNER_CHAT_ID, target_uid)
            return ok, f"User {target_uid} UNBANNED in EMA Scanner." if ok else f"Error: {res}"

        elif action_key == "unban_cross":
            ok, res = await channel_manager.unban_channel_member(bot, CROSS_CHAT_ID, target_uid)
            return ok, f"User {target_uid} UNBANNED in EMA Cross." if ok else f"Error: {res}"

        elif action_key == "unban_alpha":
            ok = user_prefs.unban_user(target_uid)
            return ok, f"User {target_uid} UNBANNED in AlphaCryptoSignals." if ok else "User was not banned in Alpha."

        else:
            return False, f"Unknown action: {action_key}"

    except Exception as e:
        logger.error(f"Error executing {action_key} for {target_uid}: {e}")
        return False, str(e)
