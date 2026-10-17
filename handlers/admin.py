"""Admin commands: /setgroup, /refresh, /addtopic, /help, /status."""
from __future__ import annotations

import logging
import re
import time
import unicodedata

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from progress import ProgressReporter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Telegram clients (mobile, desktop) aggressively auto-correct hyphens to
# "nicer-looking" Unicode variants when the user types a leading minus sign.
# This breaks int() parsing silently from the user's perspective because the
# ValueError handler used to just say "group_id must be an integer" — and if
# the message delivery failed or Render was asleep, the user saw nothing.
#
# Map all common hyphen/dash variants back to ASCII '-' so int() works.
_HYPHEN_REPLACEMENTS = {
    "\u2010": "-",  # HYPHEN
    "\u2011": "-",  # NON-BREAKING HYPHEN
    "\u2012": "-",  # FIGURE DASH
    "\u2013": "-",  # EN DASH
    "\u2014": "-",  # EM DASH
    "\u2015": "-",  # HORIZONTAL BAR
    "\u2212": "-",  # MINUS SIGN (math)
    "\uFE63": "-",  # SMALL HYPHEN-MINUS
    "\uFF0D": "-",  # FULL-WIDTH HYPHEN-MINUS
}


def _normalize_int_str(s: str) -> str:
    """Normalize a string that should represent an integer — strips whitespace,
    replaces Unicode hyphen variants with ASCII '-', and removes any
    thousands separators (commas, spaces between digits)."""
    if not s:
        return s
    # Replace Unicode hyphens
    for bad, good in _HYPHEN_REPLACEMENTS.items():
        s = s.replace(bad, good)
    # Strip whitespace
    s = s.strip()
    # Remove thousand separators ONLY between digits (e.g. "-100 123 456" -> "-100123456")
    s = re.sub(r"(?<=\d)[\s,](?=\d)", "", s)
    return s


def _safe_int(value: str) -> int | None:
    """Try to parse `value` as an int, normalizing Unicode hyphens first.
    Returns None if parsing fails."""
    if value is None:
        return None
    try:
        return int(_normalize_int_str(value))
    except (ValueError, TypeError):
        return None


def _is_authorized(cfg, user_id: int) -> bool:
    """Check if user_id is in the admin whitelist. If the whitelist is empty,
    everyone is allowed (single-user self-hosted bot)."""
    return cfg.is_admin(user_id)


async def _deny_silent(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        reason: str) -> None:
    """Log a denied command attempt and (optionally) notify the user.

    For commands like /setgroup that the user EXPECTS to work, we want to
    give them feedback so they don't think the bot is broken. But for
    security, we don't want to leak "you are not authorized" to random
    users probing the bot — we just stay silent.

    Compromise: if ADMIN_IDS is set AND the user is NOT in it, log the
    attempt but don't reply. This is what the original code did.
    If ADMIN_IDS is empty (everyone allowed), this function is never called.
    """
    user = update.effective_user
    logger.warning("Unauthorized /%s attempt by user_id=%s username=%s: %s",
                    reason, user.id if user else '?',
                    getattr(user, 'username', None) if user else '?', reason)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Hi! I forward whatever you send me to a topic in your group.\n\n"
        "Setup:\n"
        "1. /setgroup <group_id>  — set the destination group\n"
        "2. /refresh              — discover forum topics (needs Telethon session)\n"
        "   or /addtopic <title> <topic_id>  — add a topic manually\n"
        "\nThen just send me anything — I'll show you a topic picker."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "*Commands*\n"
        "/setgroup <id>  — set destination group/channel (forum OR non-forum)\n"
        "/info           — show destination chat info (is it a forum? title?)\n"
        "/refresh        — re-fetch forum topics via Telethon (forum only)\n"
        "/topics         — list currently-known topics (forum only)\n"
        "/addtopic <title> <id>  — add a topic manually (forum only)\n"
        "/deltopic <id>  — remove a manually-added topic\n"
        "/status         — show bot status (Telethon, group, topics)\n"
        "/whoami         — show your Telegram user ID + admin status\n"
        "/test_link <url>  — diagnostic: test fetching a t.me link\n"
        "/saved <url>    — 🚀 FAST: send t.me link content to Saved Messages\n"
        "/scrape <url> [flags]  — 🤖 AUTO: scrape the WHOLE channel. Works\n"
        "     with any link — a POST link identifies the same channel.\n"
        "     `old`=oldest first · `new`=newest first · `N`=cap at N msgs\n"
        "/scrape <post_link> from old|new [N]  — 🎯 scrape from that post,\n"
        "     back in history (old) or toward the newest posts (new), N msgs\n"
        "/dest           — 📌 view / change the DEFAULT destination\n"
        "     `/dest saved on` — Saved Messages is the default (even if a\n"
        "       group is set) · `/dest saved off` — back to the group\n"
        "/stop_scrape    — 🛑 stop the active scrape\n"
        "/scrape_status  — 📊 check scrape progress\n"
        "/caption <text>  — 📝 set a custom caption (replaces original)\n"
        "/caption strip   — 📝 strip ALL captions from forwarded media\n"
        "/caption clear   — 📝 restore original captions\n"
        "/strip          — 🧹 toggle stripping captions/links on forward\n"
        "/cancel         — cancel the latest pending forward\n"
        "\n*Sending content*\n"
        "• Send me a photo / video / text / file -> I show topics (if forum) "
        "or a single Forward button (if not) -> tap to forward\n"
        "• Send me a t.me/c/<id>/<msg> link -> I fetch via your Telethon "
        "session and let you pick a destination\n"
        "• `/saved <url>` -> skip the picker, send directly to Saved Messages "
        "(fastest path)\n"
        "• `/scrape <url>` -> scrape the entire channel and auto-send all "
        "media to your destination (a post link works too — it names the "
        "same channel; the post itself is only the anchor with `from`)\n"
        "• Send a post link with `#old` / `#new` appended (e.g. "
        "`https://t.me/c/1234567890/42 #new`) -> scrape from that post "
        "backward or forward — same as `/scrape <link> from old|new`\n"
        "\n*Scrape flags*\n"
        "  `old` / `new` — delivery order: oldest / newest first\n"
        "  `N` / `count=N` — cap the run at N messages\n"
        "  `from` — with a post link: scrape FROM that post (needs old/new)\n"
        "  `saved` — send to Saved Messages (default: destination group)\n"
        "  `photo` / `video` / `doc` / `audio` / `voice` / `animation` — filter by media type\n"
        "  `parallel=N` — set parallel sends (default 3, max 10)\n"
        "  Example: `/scrape https://t.me/c/123/456 old 100 videos parallel=5`\n"
        "     (whole channel: oldest first, first 100, videos only)\n"
        "  Example: `/scrape https://t.me/c/123/456 from new 100`\n"
        "     (from post #456 toward the newest, max 100)\n"
        "\n*Captions & links (/strip)*\n"
        "  `/strip captions on|off` — forward media WITHOUT its caption\n"
        "  `/strip links on|off` — remove URLs/hyperlinks from forwarded text\n"
        "  `/strip on` / `/strip off` — both at once\n"
        "  Applies to link forwards, /saved and all scraping. A post that is "
        "just a link is skipped entirely when link stripping is on.\n"
        "\n*Destination types*\n"
        "• No destination set? Everything goes to your Saved Messages —\n"
        "  the bot works out of the box, before any /setgroup\n"
        "• `/dest saved on` — make Saved Messages the default permanently\n"
        "• Forum groups: pick a topic from the picker\n"
        "• Regular groups/channels: single Forward button (no topic picker)\n"
        "• Saved Messages: /saved, the `saved` flag, or the automatic fallback",
        parse_mode="Markdown",
    )


async def cmd_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        # User is not in ADMIN_IDS whitelist — log and silently return.
        # This is the "nothing happens" symptom: bot received /setgroup but
        # ignored it because the user is not authorized.
        user = update.effective_user
        logger.warning("/setgroup DENIED — user_id=%s username=%s not in ADMIN_IDS=%s",
                       user.id if user else '?',
                       getattr(user, 'username', None) if user else '?',
                       cfg.admin_ids)
        return
    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text(
            "Usage: /setgroup <group_id>\n"
            "Example: /setgroup -1001234567890\n\n"
            "To find the group ID, add @RawDataBot to the group, read its "
            "reply, then remove it.\n\n"
            "Tip: if the bot says 'group_id must be an integer', your "
            "Telegram client may have auto-corrected the minus sign to a "
            "Unicode dash. Try copying the ID directly from @RawDataBot's "
            "message (long-press -> Copy)."
        )
        return

    raw_arg = context.args[0]
    gid = _safe_int(raw_arg)
    if gid is None:
        # Show the user EXACTLY what we received — this is the #1 diagnostic
        # for the "nothing happens" bug. Telegram auto-corrects hyphens.
        await update.effective_message.reply_text(
            f"Could not parse '{raw_arg}' as an integer.\n\n"
            f"Most likely cause: Telegram auto-corrected your '-' to a "
            f"Unicode dash character. Try this instead:\n"
            f"  1. Long-press the group ID from @RawDataBot's message\n"
            f"  2. Paste it directly (don't retype the '-')\n\n"
            f"Alternatively, send:\n"
            f"  /setgroup {raw_arg!r}\n...and I'll show what I received."
        )
        return

    await context.bot_data["db"].set_runtime("destination_group_id", str(gid))
    # Also stash on the config object so handlers don't need to re-read each time
    cfg.destination_group_id = gid

    # Invalidate the cached "is_forum" check so it gets re-evaluated for the
    # new destination on next use
    context.bot_data.pop("destination_is_forum", None)
    context.bot_data.pop("destination_is_forum_group_id", None)
    context.bot_data.pop("destination_chat_title", None)

    # Try to fetch chat info immediately so we can tell the user whether
    # it's a forum or not (and update the cache at the same time)
    chat_type_info = ""
    try:
        chat = await context.bot.get_chat(chat_id=gid)
        is_forum = bool(getattr(chat, "is_forum", False))
        title = getattr(chat, "title", None) or "(no title)"
        # Cache it
        context.bot_data["destination_is_forum"] = is_forum
        context.bot_data["destination_is_forum_group_id"] = gid
        context.bot_data["destination_chat_title"] = title
        chat_type_info = (
            f"\n\n📊 Chat info:\n"
            f"  • Title: {title}\n"
            f"  • Type: {getattr(chat, 'type', '?')}\n"
            f"  • Is forum: {'✅ YES (use /refresh to discover topics)' if is_forum else '❌ NO (single Forward button)'}"
        )
    except Exception as e:
        chat_type_info = (
            f"\n\n⚠️ Couldn't fetch chat info: {type(e).__name__}: {e}\n"
            f"Make sure the bot is a member of this chat."
        )

    await update.effective_message.reply_text(
        f"✅ Destination group set to {gid}.{chat_type_info}"
    )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id> first.")
        return
    topics_mgr = context.bot_data["topics"]
    if not topics_mgr.user_session or not topics_mgr.user_session.available:
        await update.effective_message.reply_text(
            "Telethon user session not available. Run `python login.py` first "
            "(and configure API_ID/API_HASH)."
        )
        return
    await update.effective_message.reply_text("Refreshing topics...")
    topics = await topics_mgr.refresh(group_id)
    if not topics:
        await update.effective_message.reply_text(
            "No topics found. Make sure your user account is a member of the "
            "destination group and that the group is a forum (topics enabled)."
        )
        return
    listing = "\n".join(f"• `{t['id']}` — {t['title']}" for t in topics)
    await update.effective_message.reply_text(
        f"Found {len(topics)} topic(s):\n{listing}", parse_mode="Markdown"
    )


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id>.")
        return
    topics = await context.bot_data["topics"].get_topics(group_id)
    if not topics:
        await update.effective_message.reply_text("No topics known. /refresh or /addtopic first.")
        return
    listing = "\n".join(f"• `{t['id']}` — {t['title']}" for t in topics)
    await update.effective_message.reply_text(f"Known topics:\n{listing}", parse_mode="Markdown")


async def cmd_addtopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/addtopic DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /addtopic <title> <topic_id>\nExample: /addtopic Videos 12"
        )
        return
    topic_id_str = context.args[-1]
    title = " ".join(context.args[:-1])
    topic_id = _safe_int(topic_id_str)
    if topic_id is None:
        await update.effective_message.reply_text(
            f"Could not parse '{topic_id_str}' as an integer (topic_id)."
        )
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id> first.")
        return
    await db.add_topic_override(group_id, topic_id, title)
    await update.effective_message.reply_text(
        f"Added topic override: {title} -> {topic_id} for group {group_id}"
    )


async def cmd_deltopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/deltopic DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /deltopic <topic_id>")
        return
    topic_id = _safe_int(context.args[0])
    if topic_id is None:
        await update.effective_message.reply_text(
            f"Could not parse '{context.args[0]}' as an integer (topic_id)."
        )
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        return
    await db.delete_topic_override(group_id, topic_id)
    await update.effective_message.reply_text(f"Removed topic override {topic_id}.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/status DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    telethon_status = "n/a"
    if user_session:
        telethon_status = "connected" if user_session.available else "disconnected"
    elif not cfg.has_user_session:
        telethon_status = "not configured"
    else:
        # Configured but the background connect loop hasn't succeeded yet —
        # the reason (bad string, timeout, retrying) is on the web dashboard.
        telethon_status = "connecting/retrying — check the web dashboard at the service URL"

    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = v if v else None

    from handlers.destination import (
        get_saved_default, set_saved_default, resolve_destination)
    saved_default = await get_saved_default(context)
    eff_spec = await resolve_destination(context)

    topics_count = 0
    if group_id is not None:
        topics_count = len(await context.bot_data["topics"].get_topics(int(group_id)))

    # Uptime / memory / mode — useful for spotting Render restarts and
    # memory pressure on the free tier (512 MB).
    import time as _time
    try:
        with open("/proc/self/status") as f:
            rss_kb = next((int(l.split()[1]) for l in f if l.startswith("VmRSS:")), 0)
        rss = f"{rss_kb / 1024:.0f} MB"
    except Exception:
        rss = "n/a"
    uptime_s = _time.time() - context.bot_data.get("started_at", _time.time())
    uptime = f"{int(uptime_s // 3600)}h {int((uptime_s % 3600) // 60)}m"

    scrape_task = context.bot_data.get("scrape_task")
    scrape_state = (
        "running" if scrape_task and not scrape_task.done() else "idle"
    )
    strip_c, strip_l = await _get_strip_settings(context)

    msg = (
        f"Bot status:\n"
        f"• Mode: {cfg.mode}\n"
        f"• Uptime: {uptime} (resets on every Render restart)\n"
        f"• Memory (RSS): {rss} / 512 MB free-tier limit\n"
        f"• Telethon user session: {telethon_status}\n"
        f"• Saved Messages default: {'ON' if saved_default else 'off'}\n"
        f"• Destination group: {group_id or '(not set)'}\n"
        f"• Effective destination: {eff_spec.label}\n"
        f"• Known topics: {topics_count}\n"
        f"• Admin whitelist: {len(cfg.admin_ids)} user(s)\n"
        f"• Keep-alive: {'on' if cfg.keep_alive else 'off'}"
        f" (every {cfg.keep_alive_interval}s)\n"
        f"• Scrape: {scrape_state}\n"
        f"• Strip captions: {'ON' if strip_c else 'off'}\n"
        f"• Strip links: {'ON' if strip_l else 'off'}"
    )
    await update.effective_message.reply_text(msg)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic command — shows the user their Telegram user ID and whether
    they are in the admin whitelist. Useful for debugging 'nothing happens'
    when ADMIN_IDS is misconfigured."""
    cfg = context.bot_data["config"]
    user = update.effective_user
    if not user:
        return
    is_admin = cfg.is_admin(user.id)
    admin_list = cfg.admin_ids if cfg.admin_ids else "(empty — everyone allowed)"
    await update.effective_message.reply_text(
        f"Your Telegram user ID: `{user.id}`\n"
        f"Your username: @{getattr(user, 'username', None)}\n"
        f"Your name: {getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}\n\n"
        f"Admin whitelist (ADMIN_IDS env var): {admin_list}\n"
        f"You are{' ' if is_admin else ' NOT '}authorized to use admin commands.",
        parse_mode="Markdown",
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show destination chat info — is it a forum? what's the title?
    Useful for debugging "is my destination a forum or not?"""
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/info DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id> first.")
        return

    status_msg = await update.effective_message.reply_text(f"Fetching info for chat `{group_id}`...", parse_mode="Markdown")

    try:
        chat = await context.bot.get_chat(chat_id=group_id)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Failed to fetch chat info: `{type(e).__name__}: {e}`\n\n"
            f"Make sure the bot is a member of the chat with permission to view chat info.",
            parse_mode="Markdown",
        )
        return

    is_forum = bool(getattr(chat, "is_forum", False))
    title = getattr(chat, "title", None) or "(no title)"
    chat_type = getattr(chat, "type", "?")
    username = getattr(chat, "username", None)
    member_count = "?"
    try:
        member_count = await context.bot.get_chat_member_count(chat_id=group_id)
    except Exception:
        pass

    # If it's a forum, list known topics
    topics_info = ""
    if is_forum:
        topics_mgr = context.bot_data.get("topics")
        if topics_mgr:
            topics = await topics_mgr.get_topics(group_id)
            if topics:
                topics_info = f"\n\n📋 Known topics ({len(topics)}):"
                for t in topics[:20]:  # show up to 20
                    topics_info += f"\n  • `{t['id']}` — {t['title']}"
                if len(topics) > 20:
                    topics_info += f"\n  ... and {len(topics) - 20} more"
            else:
                topics_info = "\n\n📋 No topics cached. Run /refresh to fetch them."
        else:
            topics_info = "\n\n📋 Topics manager not initialized."

    # Update the cache
    context.bot_data["destination_is_forum"] = is_forum
    context.bot_data["destination_is_forum_group_id"] = group_id
    context.bot_data["destination_chat_title"] = title

    await status_msg.edit_text(
        f"📊 Destination chat info:\n\n"
        f"• ID: `{group_id}`\n"
        f"• Title: {title}\n"
        f"• Type: {chat_type}\n"
        f"• Username: @{username}\n"
        f"• Is forum (has topics): {'✅ YES' if is_forum else '❌ NO'}\n"
        f"• Member count: {member_count}"
        f"{topics_info}",
        parse_mode="Markdown",
    )


async def cmd_test_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic command — try to fetch a t.me link via Telethon and show
    the user a detailed report. Useful for debugging "forwarding from locked
    private channels doesn't work".

    Usage: /test_link https://t.me/c/1234567890/42
    """
    from user_session import parse_telegram_link
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/test_link DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    if not user_session or not user_session.available:
        await update.effective_message.reply_text(
            "❌ Telethon user session not available.\n"
            "Run `python login.py --string` locally and set SESSION_STRING env var."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/test_link <t.me URL>`\n\n"
            "Example: `/test_link https://t.me/c/1234567890/42`\n"
            "         `/test_link https://t.me/somechannel/42`",
            parse_mode="Markdown",
        )
        return

    url = " ".join(context.args)
    parsed = parse_telegram_link(url)
    if not parsed:
        await update.effective_message.reply_text(
            f"❌ Could not parse URL: `{url}`\n\n"
            f"Supported formats:\n"
            f"  • `https://t.me/c/1234567890/42`  (private channel post)\n"
            f"  • `https://t.me/channelname/42`   (public channel post)",
            parse_mode="Markdown",
        )
        return

    status_msg = await update.effective_message.reply_text(
        f"🔍 Testing link...\n\n"
        f"Parsed:\n"
        f"  kind: `{parsed.kind}`\n"
        f"  chat_ref: `{parsed.chat_ref}`\n"
        f"  message_id: `{parsed.message_id}`\n\n"
        f"Fetching via Telethon...",
        parse_mode="Markdown",
    )

    try:
        result = await user_session.test_link(parsed)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Exception: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    # Build report
    steps_text = "\n".join(result.get("steps", []))
    if len(steps_text) > 3000:
        steps_text = steps_text[:3000] + "\n... (truncated)"

    report = (
        f"📋 Test link report:\n\n"
        f"Parsed:\n"
        f"  kind: `{result['parsed']['kind']}`\n"
        f"  chat_ref: `{result['parsed']['chat_ref']}`\n"
        f"  message_id: `{result['parsed']['message_id']}`\n\n"
        f"Result:\n"
        f"  success: {'✅ YES' if result['success'] else '❌ NO'}\n"
        f"  has_media: {'✅' if result['has_media'] else '❌'}\n"
        f"  media_type: `{result['media_type']}`\n"
        f"  chat_title: {result['chat_title'] or '(unknown)'}\n"
        f"  error: {result['error'] or '(none)'}\n\n"
        f"Steps:\n```\n{steps_text}\n```"
    )

    await status_msg.edit_text(report, parse_mode="Markdown")


async def cmd_saved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a t.me link directly to Saved Messages via Telethon — FAST PATH.

    This bypasses the topic picker entirely. The user account sends the
    message to its own Saved Messages ("me" in Telethon), which is the
    fastest destination because:
      1. The user account is ALWAYS a member of its own Saved Messages
      2. forward_messages or send_message works directly (no topic thread)
      3. No need to wait for the user to tap a button

    Usage: /saved https://t.me/c/1234567890/42
    """
    from user_session import parse_telegram_link
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/saved DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    if not user_session or not user_session.available:
        await update.effective_message.reply_text(
            "❌ Telethon user session not available.\n"
            "Run `python login.py --string` locally and set SESSION_STRING env var."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/saved <t.me URL>`\n\n"
            "Sends the content directly to your Saved Messages (fastest path — "
            "no topic picker needed).\n\n"
            "Example: `/saved https://t.me/c/1234567890/42`",
            parse_mode="Markdown",
        )
        return

    url = " ".join(context.args)
    parsed = parse_telegram_link(url)
    if not parsed:
        await update.effective_message.reply_text(
            f"❌ Could not parse URL: `{url}`\n\n"
            f"Supported formats:\n"
            f"  • `https://t.me/c/1234567890/42`  (private channel post)\n"
            f"  • `https://t.me/channelname/42`   (public channel post)",
            parse_mode="Markdown",
        )
        return

    status_msg = await update.effective_message.reply_text(
        f"📩 Sending to Saved Messages...\n\n"
        f"Source: `{parsed.chat_ref}` / msg `{parsed.message_id}`",
        parse_mode="Markdown",
    )

    # Build a progress callback that updates the status message.
    # Rich format (bar + MB/s), throttled edits with the first one immediate.
    reporter = ProgressReporter(
        lambda text: status_msg.edit_text(text), min_interval=0.8)

    async def progress_cb(sent_bytes: int, total_bytes: int, label: str):
        # label arrives as "Downloading photo_2.jpg" / "Uploading video.mp4"
        await reporter.bytes_progress(
            label=label, sent_bytes=sent_bytes, total_bytes=total_bytes)

    try:
        # Send to "me" — Telethon's special entity for Saved Messages.
        # This is a direct call to send_to_destination with dest_chat_id="me"
        # which Telethon resolves to the user's own Saved Messages chat.
        custom_caption = await _get_custom_caption(context)
        strip_captions, strip_links = await _get_strip_settings(context)
        success, diag = await user_session.send_to_destination(
            source_chat_id=int(parsed.chat_ref) if parsed.kind == "private" else parsed.chat_ref,
            source_message_ids=[parsed.message_id],
            dest_chat_id="me",  # Saved Messages
            topic_id=None,
            progress_callback=progress_cb,
            custom_caption=custom_caption,
            strip_captions=strip_captions,
            strip_links=strip_links,
        )
    except Exception as e:
        logger.exception("/saved failed")
        await status_msg.edit_text(
            f"❌ Exception: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    if success:
        await status_msg.edit_text("✅ Sent to Saved Messages!")
    else:
        err_lines = "\n".join(diag[-7:])
        await status_msg.edit_text(
            f"❌ Failed to send to Saved Messages.\n\n"
            f"Last diagnostic steps:\n{err_lines}",
        )


# ---------------------------------------------------------------------------
# /scrape flag parsing — ONE routing engine, unit-tested in smoke_test.py
#
# THE ROUTING RULE (what users expect):
#   * /scrape <ANY link> [flags] → scrape the WHOLE channel/group.
#     A POST link identifies the channel exactly like a channel link does;
#     its message id is ignored except in explicit `from` mode.
#       - old/oldest → oldest first · new/newest → newest first (default)
#       - N / count=N → cap the run at N delivered messages (0 = all)
#       - saved/group, media filters, parallel=N, min=N/max=N as before
#   * /scrape <post_link> from old|new [N] → from-point scrape (explicit
#     `from` flag — plain old/new NEVER trigger from-point on /scrape)
#   * a bare post link with NO /scrape → forward ONLY that post (link.py);
#     appending #old / #new to a bare link selects from-point there.
# ---------------------------------------------------------------------------

# Order/direction aliases. On a whole-channel run they pick the delivery
# order; with the explicit `from` flag on a post link they pick the direction.
_SCRAPE_NEW = {"new", "newer", "newest", "fwd"}
_SCRAPE_OLD = {"old", "older", "oldest", "back"}
_SCRAPE_MEDIA = {"photo", "video", "animation", "document", "audio", "voice"}
_SCRAPE_MEDIA_ALIAS = {"photos": "photo", "videos": "video", "docs": "document"}
_SCRAPE_POINT_DEFAULT = 50   # from-point count when none given
_SCRAPE_LIMIT_MAX = 100_000  # whole-channel cap (effectively unlimited)


def _parse_scrape_flags(raw_flags: list[str], url: str) -> dict:
    """Parse /scrape arguments into a single routing decision.

    Returns a dict:
      mode           'whole-channel' | 'from-point' | 'error'
      error          None | user-facing message (mode='error')
      direction      'old' | 'new' | None — order (whole-channel) /
                     direction (from-point)
      limit          int — whole-channel: max delivered (0=all);
                     from-point: count (default 50, max 500)
      media_types    list | None (None = all media)
      parallel       int 1..10
      min_id/max_id  int (resume bounds)
      saved/group    bool
      parsed_post    ParsedLink | None (url is a post link)
      parsed_channel ParsedLink | None (channel-only link, or a post link
                     demoted to message_id=0 — the post id only matters in
                     `from` mode)
    """
    from user_session import parse_telegram_link, parse_channel_link

    out: dict = {
        "mode": "whole-channel",
        "error": None,
        "direction": None,
        "limit": 0,
        "media_types": None,
        "parallel": 3,
        "min_id": 0,
        "max_id": 0,
        "saved": False,
        "group": False,
        "parsed_post": None,
        "parsed_channel": None,
    }

    for flag in raw_flags:
        actual = _SCRAPE_MEDIA_ALIAS.get(flag, flag)
        if actual in _SCRAPE_MEDIA:
            if out["media_types"] is None:
                out["media_types"] = []
            if actual not in out["media_types"]:
                out["media_types"].append(actual)
        elif flag == "saved":
            out["saved"] = True
        elif flag == "group":
            out["group"] = True
        elif flag.startswith("parallel="):
            try:
                out["parallel"] = max(1, min(int(flag.split("=", 1)[1] or 3), 10))
            except ValueError:
                pass
        elif flag.startswith(("min=", "max=")):
            try:
                v = max(0, int(flag.split("=", 1)[1]))
                if flag.startswith("min="):
                    out["min_id"] = v
                else:
                    out["max_id"] = v
            except ValueError:
                pass
        elif flag.startswith("count="):
            try:
                out["limit"] = int(flag.split("=", 1)[1])
            except ValueError:
                pass
        elif flag.isdigit():
            # bare number: whole-channel cap (or the from-point count with `from`)
            out["limit"] = int(flag)
        # 'from' / 'old' / 'new' and their aliases are resolved below —
        # they are never media/destination/knob flags

    if any(f in _SCRAPE_NEW for f in raw_flags):
        out["direction"] = "new"
    elif any(f in _SCRAPE_OLD for f in raw_flags):
        out["direction"] = "old"

    parsed_post = parse_telegram_link(url)
    out["parsed_post"] = parsed_post

    if "from" in raw_flags:
        # EXPLICIT from-point request — plain old/new never trigger this
        if parsed_post is None:
            out["mode"] = "error"
            out["error"] = (
                "`from` needs a POST link — the message id is the anchor.\n"
                "Try: `/scrape https://t.me/c/1234567890/42 from new 100`\n"
                "(a channel-only link has no anchor post — for the whole "
                "channel just drop `from`)")
        elif out["direction"] is None:
            out["mode"] = "error"
            out["error"] = (
                "`from` needs a direction — add `old` (back in history) or "
                "`new` (toward the newest posts).\n"
                "Try: `/scrape <post_link> from old 100`")
        else:
            out["mode"] = "from-point"
            if not out["limit"]:
                out["limit"] = _SCRAPE_POINT_DEFAULT
            out["limit"] = max(1, min(out["limit"], 500))

    # The channel the URL points at. A post link is demoted to
    # message_id=0 — the post id only matters in `from` mode.
    parsed_channel = parse_channel_link(url)
    if not parsed_channel and parsed_post:
        parsed_channel = type(parsed_post)(kind=parsed_post.kind,
                                           chat_ref=parsed_post.chat_ref,
                                           message_id=0)
    out["parsed_channel"] = parsed_channel

    if out["mode"] == "whole-channel" and out["limit"]:
        out["limit"] = max(1, min(out["limit"], _SCRAPE_LIMIT_MAX))
    return out


async def cmd_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scrape a channel — send all media (photos/videos) to destination.

    Usage:
      /scrape <url> [flags]

    ANY link works — a channel link OR a post link (the post just
    identifies the channel) — and /scrape ALWAYS scrapes the WHOLE
    channel/group according to the flags:
      old / new     — delivery order: oldest / newest first (default newest)
      N / count=N   — cap the run at N delivered messages (0 = all)
      from          — with a post link + old/new: scrape FROM that post
      saved         — send to Saved Messages (default: destination group)
      photo/video/doc/audio/voice/animation — media type filter
      photos/videos/docs — aliases
      parallel=N    — parallel sends (default 3, max 10)
      min=N / max=N — resume bounds for whole-channel runs

    Examples:
      /scrape https://t.me/publicchannel
      /scrape https://t.me/c/1234567890/42 old 100  — whole channel, oldest first, first 100
      /scrape https://t.me/c/1234567890 photo video — whole channel, only photos and videos
      /scrape https://t.me/c/1234567890/42 from new — FROM post #42 toward the newest
      /scrape https://t.me/c/1234567890 saved old videos parallel=5

    Notes:
      - If no media type filter is given, ALL media is forwarded
      - Text-only messages are always skipped (they have no media)
      - Sending just the link WITHOUT /scrape forwards ONLY that post
      - Rate limit: 0.3 sec delay between sends (per parallel slot)
      - On FloodWait, the bot sleeps and retries automatically
      - Protected (noforwards) channels use the same three-tier fallback
        as /saved — forward → send_message(file=) → download+send_file
    """
    import asyncio
    from user_session import parse_channel_link, parse_telegram_link
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/scrape DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    if not user_session or not user_session.available:
        await update.effective_message.reply_text(
            "❌ Telethon user session not available.\n"
            "Run `python login.py --string` locally and set SESSION_STRING env var."
        )
        return

    # Check if there's already an active scrape
    if context.bot_data.get("scrape_task") and not context.bot_data["scrape_task"].done():
        await update.effective_message.reply_text(
            "⚠️ A scrape is already running. Use /stop_scrape to stop it first, "
            "or /scrape_status to check progress."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/scrape <url> [flags]`\n\n"
            "🤖 Whole channel — works with ANY link (a post link just\n"
            "identifies the channel, same as a channel link):\n"
            "  `/scrape https://t.me/publicchannel`\n"
            "  `/scrape https://t.me/c/1234567890/42 old 100`\n"
            "     → whole channel, oldest first, first 100 messages\n"
            "  `/scrape https://t.me/c/1234567890 saved`\n"
            "     → whole channel → Saved Messages\n\n"
            "🎯 From a specific post (add `from` + old/new):\n"
            "  `/scrape https://t.me/c/1234567890/42 from new 100`\n"
            "     → from post #42 toward the newest, max 100\n"
            "  (or just send the link with #old / #new appended — no command)\n\n"
            "Flags:\n"
            "  `old` / `new` — delivery order: oldest / newest first\n"
            "  `N` or `count=N` — cap the run at N messages\n"
            "  `from` — with a post link: scrape FROM that post (needs old/new)\n"
            "  `saved` — send to Saved Messages (default: destination group)\n"
            "  `photo` / `video` / `doc` / `audio` / `voice` / `animation` — media filter\n"
            "  `parallel=N` — parallel sends (default 3, max 10)\n"
            "  `min=N` / `max=N` — resume bounds for whole-channel runs\n\n"
            "Notes:\n"
            "  • `/scrape` + ANY link = the WHOLE channel per the flags\n"
            "  • a bare link WITHOUT /scrape forwards ONLY that post\n"
            "  • from-point runs include text posts unless a media filter\n"
            "    is given; the linked post itself is always included",
            parse_mode="Markdown",
        )
        return

    # Parse args: first arg is URL, rest are flags — ONE routing engine
    # (_parse_scrape_flags above, unit-tested in smoke_test.py)
    url = context.args[0]
    raw_flags = [a.lower() for a in context.args[1:]]
    flags = _parse_scrape_flags(raw_flags, url)

    if flags["error"]:
        await update.effective_message.reply_text(
            f"❌ {flags['error']}", parse_mode="Markdown")
        return

    # ---- from-point mode: EXPLICIT `from` flag on a post link ----
    # /scrape https://t.me/c/123/456 from new 100 → scrape from post #456.
    # Plain old/new (without `from`) do NOT trigger this — with a post
    # link they mean "whole channel, oldest/newest first" instead.
    if flags["mode"] == "from-point":
        await run_scrape_from_point(
            update, context, flags["parsed_post"], flags["direction"],
            flags["limit"],
            media_types=flags["media_types"],
            parallel=flags["parallel"],
            prefer=("saved" if flags["saved"] else
                    "group" if flags["group"] else None))
        return

    # ---- whole-channel mode: ANY link (channel OR post) means the WHOLE
    # channel/group; the post id in a post link only identifies WHICH
    # channel. The flags decide order / limit / media / destination. ----
    parsed = flags["parsed_channel"]
    if not parsed:
        await update.effective_message.reply_text(
            f"❌ Could not parse URL: `{url}`\n\n"
            f"Supported formats:\n"
            f"  • `https://t.me/c/1234567890`  (private channel)\n"
            f"  • `https://t.me/channelname`   (public channel)\n"
            f"  • `https://t.me/c/1234567890/42`  (private post — same channel)\n"
            f"  • `https://t.me/channelname/42`   (public post — same channel)",
            parse_mode="Markdown",
        )
        return

    oldest_first = flags["direction"] == "old"
    limit = flags["limit"]
    media_types = flags["media_types"]
    parallel = flags["parallel"]
    min_id = flags["min_id"]
    max_id = flags["max_id"]
    send_to_saved = flags["saved"]
    send_to_group = flags["group"]

    # Determine destination (explicit flags > /dest toggle > group > Saved fallback)
    from handlers.destination import resolve_destination
    prefer = "saved" if send_to_saved else ("group" if send_to_group else None)
    spec = await resolve_destination(context, prefer=prefer)
    if spec is None:
        # Only reachable with an explicit `group` flag and no group configured
        await update.effective_message.reply_text(
            "No destination group set — nothing to send to. Either:\n"
            "  • /setgroup <group_id> first, OR\n"
            "  • drop the `group` flag (or use `saved`) to send to Saved Messages"
        )
        return
    dest_chat_id = spec.dest_id
    dest_label = spec.label

    # Build the filter description for the status message
    filter_desc = "ALL media"
    if media_types:
        filter_desc = "only: " + ", ".join(media_types)
    resume_desc = ""
    if min_id:
        resume_desc += f"\nResume: skipping msg id <= {min_id}"
    if max_id:
        resume_desc += f"\nResume: stopping after msg id > {max_id}"

    # Initial status message
    status_msg = await update.effective_message.reply_text(
        f"🔍 Starting scrape (whole channel)...\n\n"
        f"Source: `{parsed.chat_ref}`\n"
        f"{spec.dest_line}\n"
        f"Order: {'oldest first' if oldest_first else 'newest first'}\n"
        f"Filter: {filter_desc}\n"
        f"Limit: {limit if limit else 'none — all messages'}\n"
        f"Parallel: {parallel} sends{resume_desc}\n\n"
        f"_Use /stop_scrape to cancel, /scrape_status to check progress._",
        parse_mode="Markdown",
    )

    # Set up cancellation event and status storage
    cancel_event = asyncio.Event()
    context.bot_data["scrape_cancel"] = cancel_event
    context.bot_data["scrape_status"] = {
        "sent_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "total_seen": 0,
        "last_message_id": 0,
        "started_at": time.time(),
        "source_ref": parsed.chat_ref,
        "dest_label": dest_label,
        "order": "oldest" if oldest_first else "newest",
        "filter": filter_desc,
        "parallel": parallel,
        "min_id": min_id,
        "max_id": max_id,
        "limit": limit,
        # With a cap the target is known up front → /scrape_status can
        # show the progress bar + % + ETA (unlimited runs show rate only)
        "expected_total": limit,
    }

    # Build the status callback that edits the status message AND updates
    # context.bot_data["scrape_status"]
    last_status_text = {"value": ""}

    async def status_callback(text: str):
        # Throttle status edits to ~1 per 5s (the scrape_channel itself
        # already throttles to 5s, but we double-check here)
        if text == last_status_text["value"]:
            return
        last_status_text["value"] = text
        # Update bot_data status
        context.bot_data["scrape_status"].update({
            "last_update": time.time(),
        })
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass  # rate-limited or message unchanged

    # Stats callback — updates context.bot_data["scrape_status"] with the
    # latest counts from scrape_channel's result dict. This is what makes
    # /scrape_status show CURRENT progress (not stale data from start time).
    async def stats_callback(result_dict: dict):
        context.bot_data["scrape_status"].update({
            "sent_count": result_dict.get("sent_count", 0),
            "failed_count": result_dict.get("failed_count", 0),
            "skipped_count": result_dict.get("skipped_count", 0),
            "total_seen": result_dict.get("total_seen", 0),
            "last_message_id": result_dict.get("last_message_id", 0),
            "flood_waits": result_dict.get("flood_waits", 0),
            "cancelled": result_dict.get("cancelled", False),
            "last_update": time.time(),
        })

    # Run the scrape as a background task
    async def scrape_task():
        try:
            # Load the custom_caption setting (None=original, ""=strip, "<text>"=custom)
            custom_caption = await _get_custom_caption(context)
            # /strip settings apply to /scrape too — whole-channel runs used
            # to silently ignore them (fixed)
            strip_captions, strip_links = await _get_strip_settings(context)
            await user_session.scrape_channel(
                source_chat_ref=int(parsed.chat_ref) if parsed.kind == "private" else parsed.chat_ref,
                dest_chat_id=dest_chat_id,
                topic_id=None,  # scraper doesn't support topics yet (use /saved for that)
                reverse=oldest_first,
                min_id=min_id,
                max_id=max_id,
                limit=limit,
                cancel_event=cancel_event,
                status_callback=status_callback,
                stats_callback=stats_callback,
                media_types=media_types,
                parallel=parallel,
                custom_caption=custom_caption,
                strip_captions=strip_captions,
                strip_links=strip_links,
            )
        except Exception as e:
            logger.exception("/scrape task failed")
            try:
                await status_msg.edit_text(f"❌ Scrape crashed: {type(e).__name__}: {e}")
            except Exception:
                pass
        finally:
            # Clean up the scrape state when done
            context.bot_data.pop("scrape_task", None)
            context.bot_data.pop("scrape_cancel", None)

    context.bot_data["scrape_task"] = asyncio.create_task(scrape_task())
    logger.info("Scrape started for %s -> %s", parsed.chat_ref, dest_label)


async def _get_custom_caption(context) -> str | None:
    """Load the custom_caption setting from DB / bot_data cache.
    Returns:
      None — use original captions (legacy behavior)
      "" — strip all captions
      "<text>" — use this custom caption
    """
    # Check in-memory cache first (set by /caption command)
    if "custom_caption" in context.bot_data:
        return context.bot_data["custom_caption"]
    # Fall back to DB
    db = context.bot_data["db"]
    raw = await db.get_runtime("custom_caption", None)
    if raw is None or raw == "__none__":
        return None  # use original
    return raw  # "" means strip, anything else is custom text


async def _get_strip_settings(context) -> tuple[bool, bool]:
    """Load the /strip settings: (strip_captions, strip_links).

    Precedence: in-memory cache (set by /strip) > DB runtime_config (set by
    /strip on a previous boot) > env defaults (STRIP_CAPTIONS/STRIP_LINKS).
    """
    cached = context.bot_data.get("strip_settings")
    if cached is not None:
        return bool(cached.get("captions")), bool(cached.get("links"))
    db = context.bot_data["db"]
    cfg = context.bot_data["config"]
    cap = await db.get_runtime("strip_captions")
    lnk = await db.get_runtime("strip_links")
    captions = (cap == "1") if cap is not None else bool(
        getattr(cfg, "strip_captions", False))
    links = (lnk == "1") if lnk is not None else bool(
        getattr(cfg, "strip_links", False))
    return captions, links


def _sync_strip_state(captions: bool, links: bool) -> None:
    """Mirror the /strip settings into bot.py's dashboard state (lazy import —
    bot.py imports this module at load time, so a top-level import here would
    be circular)."""
    try:
        import bot as _bot
        _bot._STATE["strip"] = {"captions": bool(captions), "links": bool(links)}
    except Exception:
        logger.debug("could not sync strip state to dashboard", exc_info=True)


async def cmd_strip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle caption/link stripping for ALL forwards.

    Usage:
      /strip               — show current settings
      /strip on|off        — both toggles at once
      /strip captions on|off  — media posts forwarded WITHOUT their caption
      /strip links on|off     — URLs (and hyperlinks) removed from any
                                forwarded text/caption

    Applies to every sourced forward: post links, /saved, /scrape, and
    from-point scraping (#old / #new). Text-only posts keep their text —
    only links are removed from them (a post that is JUST a link is
    skipped entirely). A custom caption set with /caption still wins over
    the captions toggle until you /caption clear.
    """
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/strip DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return

    cur_cap, cur_lnk = await _get_strip_settings(context)
    args = [a.lower() for a in (context.args or [])]

    def _fmt(on: bool) -> str:
        return "✅ ON" if on else "⚪ off"

    if not args:
        cap_note = ""
        custom = await _get_custom_caption(context)
        if custom not in (None, ""):
            cap_note = ("\n⚠️ A custom caption is set (/caption) — it "
                        "overrides the captions toggle until you "
                        "`/caption clear`.")
        await update.effective_message.reply_text(
            f"🧹 Forward cleaning:\n\n"
            f"• Captions: {_fmt(cur_cap)} — media posts forwarded "
            f"{'WITHOUT' if cur_cap else 'with'} their caption\n"
            f"• Links: {_fmt(cur_lnk)} — links are "
            f"{'removed from' if cur_lnk else 'kept in'} forwarded text"
            f"{cap_note}\n\n"
            f"Usage:\n"
            f"  `/strip on` / `/strip off` — both at once\n"
            f"  `/strip captions on|off`\n"
            f"  `/strip links on|off`",
            parse_mode="Markdown",
        )
        return

    def _parse_on_off(token: str | None) -> bool | None:
        if token in ("on", "true", "1", "yes"):
            return True
        if token in ("off", "false", "0", "no"):
            return False
        return None

    new_cap: bool | None = None
    new_lnk: bool | None = None
    a0 = args[0]
    if a0 in ("on", "off", "all", "none"):
        val = a0 not in ("off", "none")
        new_cap, new_lnk = val, val
    elif a0 in ("captions", "caption", "cap"):
        val = _parse_on_off(args[1] if len(args) > 1 else None)
        if val is None:
            await update.effective_message.reply_text(
                "Usage: `/strip captions on|off`", parse_mode="Markdown")
            return
        new_cap = val
    elif a0 in ("links", "link"):
        val = _parse_on_off(args[1] if len(args) > 1 else None)
        if val is None:
            await update.effective_message.reply_text(
                "Usage: `/strip links on|off`", parse_mode="Markdown")
            return
        new_lnk = val
    else:
        await update.effective_message.reply_text(
            "Unknown option. Use `/strip on|off`, `/strip captions on|off` "
            "or `/strip links on|off` (or plain `/strip` to see status).",
            parse_mode="Markdown",
        )
        return

    if new_cap is not None:
        await db.set_runtime("strip_captions", "1" if new_cap else "0")
    if new_lnk is not None:
        await db.set_runtime("strip_links", "1" if new_lnk else "0")
    final_cap = new_cap if new_cap is not None else cur_cap
    final_lnk = new_lnk if new_lnk is not None else cur_lnk
    context.bot_data["strip_settings"] = {
        "captions": final_cap, "links": final_lnk}
    _sync_strip_state(final_cap, final_lnk)

    changed = []
    if new_cap is not None:
        changed.append(f"captions {'ON — media forwards will have no caption' if new_cap else 'OFF — original captions kept'}")
    if new_lnk is not None:
        changed.append(f"links {'ON — links removed from forwarded text' if new_lnk else 'OFF — links kept'}")
    await update.effective_message.reply_text(
        f"✅ Updated: " + "; ".join(changed) +
        f"\n\nNow: captions {_fmt(final_cap)}, links {_fmt(final_lnk)}."
    )


# ---------------------------------------------------------------------------
# Default destination (/dest)
# ---------------------------------------------------------------------------

async def cmd_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View or change the DEFAULT destination.

    Usage:
      /dest               — show the current settings
      /dest saved on      — Saved Messages becomes the default destination
                            (forwards/scrapes go there even if a group is set)
      /dest saved off     — back to the group as default (when one is set)
      /dest group         — same as /dest saved off

    With NO destination group set, everything falls back to Saved Messages
    automatically — the toggle only matters once a group IS set.
    Persisted in the DB, so it survives restarts (and Render redeploys).
    """
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/dest DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return

    from handlers.destination import (
        get_saved_default, set_saved_default, resolve_destination)
    args = [a.lower() for a in (context.args or [])]

    async def _group_id() -> int | None:
        gid = cfg.destination_group_id
        if gid is None:
            v = await db.get_runtime("destination_group_id")
            gid = int(v) if v else None
        return gid

    if not args:
        cur = await get_saved_default(context)
        spec = await resolve_destination(context)
        gid = await _group_id()
        note = ""
        if not gid:
            note = ("\nℹ️ With no group set, everything goes to Saved Messages "
                    "automatically.")
        elif cur:
            note = ("\nℹ️ A group is set but is NOT used — the toggle has "
                    "priority. `/dest saved off` to send to the group.")
        await update.effective_message.reply_text(
            f"📌 Default destination\n\n"
            f"• Saved Messages as default: {'✅ ON' if cur else '⚪ off'}\n"
            f"• Destination group: {gid or '(not set)'}\n"
            f"• Effective destination: {spec.label}{note}\n\n"
            f"Usage:\n"
            f"  `/dest saved on` — Saved Messages is the default\n"
            f"  `/dest saved off` / `/dest group` — use the group\n\n"
            f"Per-run override on /scrape: the `saved` / `group` flags.",
            parse_mode="Markdown",
        )
        return

    a0 = args[0]
    if a0 in ("saved", "me", "self"):
        val = args[1] if len(args) > 1 else None
        if val not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
            await update.effective_message.reply_text(
                "Usage: `/dest saved on` or `/dest saved off`",
                parse_mode="Markdown")
            return
        on = val in ("on", "true", "1", "yes")
        await set_saved_default(context, on)
    elif a0 in ("group", "chat"):
        on = False
        await set_saved_default(context, on)
    else:
        await update.effective_message.reply_text(
            "Unknown option. Use `/dest` to see status, `/dest saved on|off`, "
            "or `/dest group`.", parse_mode="Markdown")
        return

    spec = await resolve_destination(context)
    if on:
        await update.effective_message.reply_text(
            "✅ Saved Messages is now the DEFAULT destination.\n"
            "Links, direct sends and /scrape all go there; use the `group` "
            "flag on /scrape to override per-run.")
    else:
        gid = await _group_id()
        if gid:
            await update.effective_message.reply_text(
                f"✅ Default destination: {spec.label}.")
        else:
            await update.effective_message.reply_text(
                "✅ Default destination: Saved Messages (fallback — no group "
                "is set yet, so nothing changes until you /setgroup).")


# ---------------------------------------------------------------------------
# From-point scraping (post link + old/new direction)
# ---------------------------------------------------------------------------

async def run_scrape_from_point(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parsed,                    # ParsedLink (kind/chat_ref/message_id)
    direction: str,            # 'old' | 'new'
    count: int = 50,
    media_types: list[str] | None = None,
    parallel: int = 3,
    send_to_saved: bool = False,
    prefer: str | None = None,
) -> None:
    """Start a from-point scrape as a background task. Shared by:
      * /scrape <post_link> from old|new [count] [flags]
      * a bare message with a post link + #old / #new hashtag

    Performs the admin/session/destination checks and wires the status
    callbacks + bot_data state that /stop_scrape and /scrape_status read.
    `prefer` mirrors the /scrape flags ("saved"/"group") for destination
    resolution; None = automatic (toggle > group > Saved fallback).
    """
    import asyncio
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("from-point scrape DENIED — user_id=%s not in ADMIN_IDS",
                       update.effective_user.id)
        return
    user_session = context.bot_data.get("user_session")
    if not user_session or not user_session.available:
        await update.effective_message.reply_text(
            "❌ Telethon user session not available.\n"
            "Run `python login.py --string` locally and set SESSION_STRING env var."
        )
        return

    if context.bot_data.get("scrape_task") and not context.bot_data["scrape_task"].done():
        await update.effective_message.reply_text(
            "⚠️ A scrape is already running. Use /stop_scrape to stop it first, "
            "or /scrape_status to check progress."
        )
        return

    count = max(1, min(int(count or 50), 500))
    parallel = max(1, min(int(parallel or 3), 10))

    # Destination: explicit flags > /dest toggle > group > Saved fallback
    from handlers.destination import resolve_destination
    spec = await resolve_destination(context, prefer=prefer)
    if spec is None:
        # Only reachable with an explicit `group` request and no group set
        await update.effective_message.reply_text(
            "No destination group set — nothing to send to. Either:\n"
            "  • /setgroup <group_id> first, OR\n"
            "  • drop the `group` flag (or use `saved`) to send to Saved Messages"
        )
        return
    dest_chat_id = spec.dest_id
    dest_label = spec.label

    arrow = "⬅ back in history" if direction == "old" else "➡ toward the newest posts"
    filter_desc = "everything (incl. text posts)" if not media_types \
        else "only: " + ", ".join(media_types)
    status_msg = await update.effective_message.reply_text(
        f"🎯 From-point scrape starting…\n\n"
        f"Source: `{parsed.chat_ref}` (post #{parsed.message_id})\n"
        f"Direction: {direction} ({arrow})\n"
        f"Count: up to {count} message(s)\n"
        f"{spec.dest_line}\n"
        f"Filter: {filter_desc}\n\n"
        f"_Use /stop_scrape to cancel._",
        parse_mode="Markdown",
    )

    cancel_event = asyncio.Event()
    context.bot_data["scrape_cancel"] = cancel_event
    context.bot_data["scrape_status"] = {
        "sent_count": 0, "failed_count": 0, "skipped_count": 0,
        "total_seen": 0, "last_message_id": 0,
        "started_at": time.time(),
        "source_ref": parsed.chat_ref,
        "dest_label": dest_label,
        "order": f"from #{parsed.message_id} {'old' if direction == 'old' else 'new'}",
        "filter": filter_desc,
        "parallel": parallel,
        "anchor_id": parsed.message_id,
        "direction": direction,
        "expected_total": 0,
    }
    try:
        import bot as _bot
        _bot._STATE["scrape"] = (
            f"running — {parsed.chat_ref} #{parsed.message_id} "
            f"{direction} (sent 0)")
    except Exception:
        pass

    last_status_text = {"value": ""}

    async def status_callback(text: str):
        if text == last_status_text["value"]:
            return
        last_status_text["value"] = text
        context.bot_data["scrape_status"].update({"last_update": time.time()})
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass  # rate-limited or unchanged

    async def stats_callback(result_dict: dict):
        context.bot_data["scrape_status"].update({
            "sent_count": result_dict.get("sent_count", 0),
            "failed_count": result_dict.get("failed_count", 0),
            "skipped_count": result_dict.get("skipped_count", 0),
            "total_seen": result_dict.get("total_seen", 0),
            "last_message_id": result_dict.get("last_message_id", 0),
            "flood_waits": result_dict.get("flood_waits", 0),
            "cancelled": result_dict.get("cancelled", False),
            "expected_total": result_dict.get("expected_total") or 0,
            "last_update": time.time(),
        })
        try:
            import bot as _bot
            _bot._STATE["scrape"] = (
                f"running — {parsed.chat_ref} #{parsed.message_id} "
                f"{direction} (sent {result_dict.get('sent_count', 0)})")
        except Exception:
            pass

    async def scrape_task():
        try:
            custom_caption = await _get_custom_caption(context)
            strip_captions, strip_links = await _get_strip_settings(context)
            await user_session.scrape_from_point(
                source_chat_ref=int(parsed.chat_ref) if parsed.kind == "private"
                                else parsed.chat_ref,
                anchor_message_id=parsed.message_id,
                direction=direction,
                dest_chat_id=dest_chat_id,
                count=count,
                topic_id=None,
                cancel_event=cancel_event,
                status_callback=status_callback,
                stats_callback=stats_callback,
                media_types=media_types,
                parallel=parallel,
                custom_caption=custom_caption,
                strip_captions=strip_captions,
                strip_links=strip_links,
            )
        except Exception as e:
            logger.exception("from-point scrape task failed")
            try:
                await status_msg.edit_text(f"❌ Scrape crashed: {type(e).__name__}: {e}")
            except Exception:
                pass
        finally:
            context.bot_data.pop("scrape_task", None)
            context.bot_data.pop("scrape_cancel", None)
            try:
                import bot as _bot
                _bot._STATE["scrape"] = "idle"
            except Exception:
                pass

    context.bot_data["scrape_task"] = asyncio.create_task(scrape_task())
    logger.info("From-point scrape started: %s #%d %s count=%d -> %s",
                parsed.chat_ref, parsed.message_id, direction, count, dest_label)


async def cmd_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or clear a custom caption applied to all forwarded media.

    Usage:
      /caption <text>      — set a custom caption (applied to all forwards)
      /caption clear       — clear the custom caption (use original captions)
      /caption strip       — always strip captions (no caption at all)
      /caption             — show current setting

    When a custom caption is set:
      - /scrape sends media with your custom caption (no original captions)
      - /saved sends media with your custom caption (no original captions)
      - Direct forwards (links) send with your custom caption (no original)

    Use `/caption clear` to restore original-caption behavior.
    Use `/caption strip` to remove ALL captions (forward media without any text).
    """
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/caption DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return

    if not context.args:
        # Show current setting
        current = await _get_custom_caption(context)
        if current is None:
            current_str = "(not set — using original captions)"
        elif current == "":
            current_str = "(strip mode — all captions removed)"
        else:
            preview = current[:200] + ("..." if len(current) > 200 else "")
            current_str = f"`{preview}`"
        await update.effective_message.reply_text(
            f"📝 Current caption setting:\n\n{current_str}\n\n"
            f"Usage:\n"
            f"  `/caption <text>` — set custom caption\n"
            f"  `/caption clear` — restore original captions\n"
            f"  `/caption strip` — remove all captions\n",
            parse_mode="Markdown",
        )
        return

    arg = " ".join(context.args)
    if arg.lower() == "clear":
        await db.set_runtime("custom_caption", "__none__")  # sentinel for "use original"
        # Also clear from in-memory config cache if present
        context.bot_data.pop("custom_caption", None)
        await update.effective_message.reply_text(
            "✅ Custom caption cleared. Forwards will use original captions."
        )
    elif arg.lower() == "strip":
        await db.set_runtime("custom_caption", "")  # empty string = strip all
        context.bot_data["custom_caption"] = ""
        await update.effective_message.reply_text(
            "✅ Caption mode: STRIP. All forwarded media will have no caption."
        )
    else:
        # Set custom caption (truncate to Telegram's 1024-char caption limit)
        if len(arg) > 1024:
            arg = arg[:1024]
            await update.effective_message.reply_text(
                f"⚠️ Caption truncated to 1024 chars (Telegram's limit)."
            )
        await db.set_runtime("custom_caption", arg)
        context.bot_data["custom_caption"] = arg
        preview = arg[:200] + ("..." if len(arg) > 200 else "")
        await update.effective_message.reply_text(
            f"✅ Custom caption set:\n\n`{preview}`\n\n"
            f"All forwarded media will use this caption instead of the original.",
            parse_mode="Markdown",
        )


async def cmd_stop_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the currently running scrape."""
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        return
    cancel_event = context.bot_data.get("scrape_cancel")
    task = context.bot_data.get("scrape_task")
    if not task or task.done():
        await update.effective_message.reply_text("No active scrape to stop.")
        return
    # Set the cancel event — the scrape loop will pick it up on next iteration
    if cancel_event:
        cancel_event.set()
    await update.effective_message.reply_text(
        "🛑 Stop signal sent. The scrape will stop after the current message "
        "(within a few seconds). Use /scrape_status to see final stats."
    )


async def cmd_scrape_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current scrape status."""
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        return
    status = context.bot_data.get("scrape_status")
    task = context.bot_data.get("scrape_task")
    if not status:
        await update.effective_message.reply_text("No scrape has been started yet.")
        return
    running = task and not task.done()
    elapsed = time.time() - status.get("started_at", 0) if status.get("started_at") else 0
    state_str = "🟢 running" if running else "🔴 finished"

    sent = int(status.get("sent_count", 0) or 0)
    expected = int(status.get("expected_total", 0) or 0)
    rate = (sent / elapsed) if elapsed > 1 and sent > 0 else None

    if expected:
        pct = (sent / expected * 100) if expected else 0.0
        remaining = max(expected - sent, 0)
        eta_str = (f"~{int(remaining / rate)}s" if rate and rate > 0 else "?")
        rate_str = f"{rate:.1f} msg/s" if rate else "n/a"
        progress_str = (f"Progress: {sent}/{expected} ({pct:.0f}%)\n"
                        f"Rate: {rate_str} · ETA: {eta_str}\n")
    else:
        rate_str = f"{rate:.1f} msg/s" if rate else "n/a"
        progress_str = f"Rate: {rate_str}\n"

    await update.effective_message.reply_text(
        f"📊 Scrape status: {state_str}\n\n"
        f"Source: `{status.get('source_ref', '?')}`\n"
        f"Destination: {status.get('dest_label', '?')}\n"
        f"Order: {status.get('order', '?')}\n"
        f"Filter: {status.get('filter', 'ALL media')}\n"
        f"Parallel: {status.get('parallel', 3)}\n"
        f"Elapsed: {elapsed:.0f} sec\n\n"
        f"{progress_str}"
        f"Total seen: {status.get('total_seen', 0)}\n"
        f"Sent: {sent}\n"
        f"Failed: {status.get('failed_count', 0)}\n"
        f"Skipped (filtered/no media): {status.get('skipped_count', 0)}\n"
        f"Flood waits: {status.get('flood_waits', 0)}\n"
        f"Last msg ID: {status.get('last_message_id', 0)}",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        return
    # Remove all pending forwards for this user
    async with db._conn.execute(
        "DELETE FROM pending_forwards WHERE user_id = ?",
        (update.effective_user.id,),
    ) as cur:
        await db._conn.commit()
        n = cur.rowcount or 0
    # Cancel pending batch tasks (time-based batch window)
    # Look for both old and new batch keys for backward compatibility
    batches = context.chat_data.pop("msg_batches", None)
    if batches:
        for batch in batches.values():
            t = batch.get("task")
            if t and not t.done():
                t.cancel()
    # Legacy key (old album_batches)
    legacy_batches = context.chat_data.pop("album_batches", None)
    if legacy_batches:
        for batch in legacy_batches.values():
            t = batch.get("task")
            if t and not t.done():
                t.cancel()
    await update.effective_message.reply_text(f"Cancelled {n} pending forward(s).")


def register_admin_handlers(app) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("addtopic", cmd_addtopic))
    app.add_handler(CommandHandler("deltopic", cmd_deltopic))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("test_link", cmd_test_link))
    app.add_handler(CommandHandler("saved", cmd_saved))
    app.add_handler(CommandHandler("dest", cmd_dest))
    app.add_handler(CommandHandler("scrape", cmd_scrape))
    app.add_handler(CommandHandler("stop_scrape", cmd_stop_scrape))
    app.add_handler(CommandHandler("scrape_status", cmd_scrape_status))
    app.add_handler(CommandHandler("caption", cmd_caption))
    app.add_handler(CommandHandler("strip", cmd_strip))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
