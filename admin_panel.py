"""
admin_panel.py — Unified Admin Control Panel for:
- EMA Signal Scanner (-1004332146822)
- EMA Cross (-1004463573705)
- AlphaCryptoSignals (user_prefs)

Provides unified user listing, 1-tap command links, filter inspection, and interactive action buttons:
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


def format_user_filter_summary(prefs: dict) -> str:
    """Formats a user's multi-selected filters in a clean, visual summary."""
    if not prefs:
        return "   • <i>Default Alert Filter (Scanner / Cross Channel)</i>"

    status = "🟢 Active" if prefs.get("enabled", True) else "🔴 Paused"

    # Timeframes
    tfs = prefs.get("timeframes", list(user_prefs.DEFAULT_TIMEFRAMES))
    tf_str = ", ".join([t.upper() for t in tfs]) if tfs else "None"

    # Universe / Coin Groups
    groups = prefs.get("coin_groups", list(user_prefs.DEFAULT_COIN_GROUPS))
    group_map = {
        "core_10": "Core 10",
        "alts_25": "Top 25 Alts",
        "commodities": "Commodities",
        "us_stocks": "US Equities",
        "custom": "Custom",
        "all_235": "All 235",
    }
    group_labels = [group_map.get(g, g) for g in groups]
    custom_coins = prefs.get("custom_coins", [])
    if "custom" in groups and custom_coins:
        snippet = ", ".join(custom_coins[:3])
        if len(custom_coins) > 3:
            snippet += f" (+{len(custom_coins)-3})"
        group_labels = [lbl if lbl != "Custom" else f"Custom ({snippet})" for lbl in group_labels]
    group_str = " + ".join(group_labels) if group_labels else "None"

    # Signals
    sigs = prefs.get("signal_types", list(user_prefs.DEFAULT_SIGNAL_TYPES))
    sig_map = {"crossover": "Cross", "retest": "Retest"}
    sig_str = " + ".join([sig_map.get(s, s) for s in sigs]) if sigs else "None"

    # Confluence Stars
    min_stars = prefs.get("min_stars", 3)
    stars_str = "⭐ 3★ & 4★" if min_stars == 3 else "🌟 4★ Only"

    # Scalp & Squeeze
    scalp = "ON (5M/15M)" if prefs.get("gold_scalp_5m_15m") else "OFF"
    squeeze = "ON" if prefs.get("squeeze_alerts", True) else "OFF"
    if prefs.get("squeeze_only"):
        squeeze = "ONLY"

    return (
        f"   • <b>Status:</b> {status} | <b>Stars:</b> {stars_str}\n"
        f"   • <b>Timeframes:</b> <code>{tf_str}</code>\n"
        f"   • <b>Universe:</b> <i>{group_str}</i>\n"
        f"   • <b>Signals:</b> <code>{sig_str}</code> | <b>Gold Scalp:</b> <code>{scalp}</code> | <b>Squeeze:</b> <code>{squeeze}</code>"
    )


def get_all_users_combined() -> dict:
    """Aggregates all unique ACTIVE users across Alpha, Scanner, and Cross.
    Banned users are strictly excluded so only genuine active users are shown."""
    combined = {}
    banned_alpha = user_prefs.get_banned_users()

    # 1. Alpha subscribers
    alpha_users = user_prefs._load_prefs()
    for uid, u in alpha_users.items():
        uid_str = str(uid).strip()
        if uid_str == ADMIN_CHAT_ID:
            continue
        # Exclude banned users
        if uid_str in banned_alpha or user_prefs.is_user_banned(uid_str):
            continue

        combined[uid_str] = {
            "name": u.get("first_name") or u.get("username") or f"User {uid_str[-4:]}",
            "username": u.get("username") or "",
            "in_alpha": True,
            "in_scanner": False,
            "in_cross": False,
            "prefs": u,
        }

    # 2. Channel members
    ch_data = channel_manager.get_all_channel_data()
    for ch_id, ch_info in ch_data.items():
        is_scanner = "4332146822" in str(ch_id)
        members = ch_info.get("members", {})
        for uid, m in members.items():
            uid_str = str(uid).strip()
            if uid_str == ADMIN_CHAT_ID:
                continue
            if uid_str in banned_alpha or user_prefs.is_user_banned(uid_str):
                continue

            if uid_str not in combined:
                combined[uid_str] = {
                    "name": m.get("first_name") or m.get("username") or f"Member {uid_str[-4:]}",
                    "username": m.get("username") or "",
                    "in_alpha": False,
                    "in_scanner": False,
                    "in_cross": False,
                    "prefs": alpha_users.get(uid_str, {}),
                }
            if is_scanner:
                combined[uid_str]["in_scanner"] = True
            else:
                combined[uid_str]["in_cross"] = True

    return combined


def build_admin_overview() -> tuple[str, InlineKeyboardMarkup]:
    """Builds the clean /admin overview — showing active users with their filter preferences."""
    users = get_all_users_combined()
    banned_alpha = user_prefs.get_banned_users()

    active_count = len(users)
    banned_count = len(banned_alpha)

    msg = "👑 <b>UNIFIED ADMIN CONTROL PANEL</b>\n"
    msg += f"Active Users: <b>{active_count}</b> | Banned Users: <b>{banned_count}</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    buttons = []

    # ── Active Users ──────────────────────────────
    if not users:
        msg += "<i>No active subscribers or members found.</i>\n\n"
    else:
        msg += "👥 <b>ACTIVE SUBSCRIBERS & MEMBERS</b>\n\n"
        for idx, (uid, u) in enumerate(users.items(), 1):
            uname = f"@{u['username']}" if u["username"] else "—"
            name = u["name"]
            platforms = []
            if u.get("in_alpha"):
                platforms.append("Alpha")
            if u.get("in_scanner"):
                platforms.append("Scanner")
            if u.get("in_cross"):
                platforms.append("Cross")
            platform_str = " | ".join(platforms) if platforms else "Unknown"

            msg += f"<b>{idx}. {name}</b> ({uname})\n"
            msg += f"   🆔 <code>{uid}</code> · <i>{platform_str}</i>\n"

            # Format selected filters
            prefs = u.get("prefs") or {}
            msg += format_user_filter_summary(prefs) + "\n\n"

            buttons.append([InlineKeyboardButton(
                f"⚙️ Manage {name[:18]}",
                callback_data=f"act_user_{uid}"
            )])

    # Bottom action bar: Dedicated Banned button + Refresh
    banned_btn_text = f"🚫 Banned Users ({banned_count})"
    buttons.append([
        InlineKeyboardButton(banned_btn_text, callback_data="act_view_banned"),
        InlineKeyboardButton("🔄 Refresh", callback_data="act_refresh")
    ])

    return msg, InlineKeyboardMarkup(buttons)


def build_banned_users_overview() -> tuple[str, InlineKeyboardMarkup]:
    """Builds dedicated Banned Users overview screen."""
    banned_alpha = user_prefs.get_banned_users()
    banned_count = len(banned_alpha)

    msg = "🚫 <b>BANNED USERS ARCHIVE</b>\n"
    msg += f"Total Banned: <b>{banned_count}</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    buttons = []
    if not banned_alpha:
        msg += "<i>No users are currently banned. Everything is clean! ✨</i>\n\n"
    else:
        for idx, (bid, binfo) in enumerate(banned_alpha.items(), 1):
            bname = binfo.get("first_name") or binfo.get("username") or f"User {bid[-4:]}"
            buname = f"@{binfo.get('username')}" if binfo.get("username") else "—"
            banned_at = binfo.get("banned_at", "")[:19].replace("T", " ") if binfo.get("banned_at") else "—"
            reason = binfo.get("reason") or "Manual Admin Ban"
            msg += f"<b>{idx}. {bname}</b> ({buname})\n"
            msg += f"   🆔 <code>{bid}</code>\n"
            msg += f"   📅 Banned: <i>{banned_at} UTC</i>\n"
            msg += f"   📝 Reason: <i>{reason}</i>\n\n"

            buttons.append([
                InlineKeyboardButton(f"✅ Unban {bname[:14]}", callback_data=f"act_unban_alpha_{bid}"),
                InlineKeyboardButton("⚙️ Manage", callback_data=f"act_user_{bid}"),
            ])

    buttons.append([
        InlineKeyboardButton("🔙 Back to Active Users", callback_data="act_back_users"),
        InlineKeyboardButton("🔄 Refresh", callback_data="act_view_banned")
    ])
    return msg, InlineKeyboardMarkup(buttons)


def build_user_action_menu(user_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the 9-action interactive button layout for a specific user, displaying their current filter settings."""
    uid = str(user_id).strip()
    alpha_users = user_prefs._load_prefs()
    banned_alpha = user_prefs.get_banned_users()
    is_banned = uid in banned_alpha or user_prefs.is_user_banned(uid)

    prefs = alpha_users.get(uid) or {}
    binfo = banned_alpha.get(uid) or {}

    name = prefs.get("first_name") or binfo.get("first_name") or prefs.get("username") or binfo.get("username") or f"User {uid[-4:]}"
    uname = f"@{prefs.get('username') or binfo.get('username')}" if (prefs.get("username") or binfo.get("username")) else "None"

    status_label = "🔴 BANNED" if is_banned else ("🟢 ACTIVE" if prefs.get("enabled", True) else "🟡 PAUSED")

    text = (
        f"⚙️ <b>ACTION CONTROLS FOR USER</b>\n\n"
        f"👤 <b>Name:</b> {name}\n"
        f"🔗 <b>Username:</b> {uname}\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
        f"📌 <b>Status:</b> <b>{status_label}</b>\n\n"
        f"📋 <b>CURRENT FILTER SELECTIONS:</b>\n"
        f"{format_user_filter_summary(prefs)}\n\n"
        f"👇 <i>Tap an action below to execute immediately:</i>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Remove EMA Scanner", callback_data=f"act_rm_scan_{uid}")],
        [InlineKeyboardButton("❌ Remove EMA Cross", callback_data=f"act_rm_cross_{uid}")],
        [InlineKeyboardButton("❌ Remove Alpha", callback_data=f"act_rm_alpha_{uid}")],
        [InlineKeyboardButton("🚫 Ban EMA Scanner", callback_data=f"act_ban_scan_{uid}")],
        [InlineKeyboardButton("🚫 Ban EMA Cross", callback_data=f"act_ban_cross_{uid}")],
        [InlineKeyboardButton("🚫 Ban Alpha", callback_data=f"act_ban_alpha_{uid}")],
        [InlineKeyboardButton("✅ Unban EMA Scanner", callback_data=f"act_unban_scan_{uid}")],
        [InlineKeyboardButton("✅ Unban EMA Cross", callback_data=f"act_unban_cross_{uid}")],
        [InlineKeyboardButton("✅ Unban Alpha", callback_data=f"act_unban_alpha_{uid}")],
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
