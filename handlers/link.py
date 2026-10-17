"""Link-forward flow.

When the admin sends a t.me/c/<id>/<msg_id> or t.me/<channel>/<msg_id> URL:
  * Parse it
  * Use the Telethon user session to fetch the message(s) — including
    album siblings — WITHOUT downloading anything (we store just the source
    chat_id + message ids in a pending forward)
  * Show the inline topic picker
  * When the user taps a topic, Telethon's three-tier fallback sends the
    content (true forward → server-side re-upload → download + re-upload),
    which works even on noforwards (protected) channels.

The old download-to-disk helpers were removed — the direct-send path made
them dead code. The legacy payload format is still honored by
handlers/direct._forward_link for pending forwards created before an upgrade.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters

from user_session import parse_telegram_link, parse_channel_link, UserSession
from db import Database

logger = logging.getLogger(__name__)

LINK_FILTER = filters.Regex(r"(https?://)?t(?:elegram)?\.me/")


def _link_error_help(text: str) -> str:
    """User-friendly message when a t.me link couldn't be handled.

    Detects channel-only links (no message id) and points the user at
    /scrape instead of showing a generic parse error.
    """
    first_token = text.split()[0] if text.split() else text
    if parse_channel_link(first_token):
        return (
            "That looks like a whole-channel link (no message id).\n"
            "Use /scrape <url> to pull ALL media from the channel, or link a "
            "specific post like:\n"
            "• https://t.me/c/1234567890/42  (private channel post)\n"
            "• https://t.me/channelname/42   (public channel post)"
        )
    return (
        "Couldn't parse that link. Supported formats:\n"
        "• https://t.me/c/1234567890/42  (private channel post)\n"
        "• https://t.me/channelname/42   (public channel post)\n"
        "\nNote: invite-only links like t.me/+abc... can't be auto-fetched "
        "because we can't join chats for you.\n"
        "Tip: /scrape <channel_url> pulls an entire channel."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect a t.me link in the user's message and pull the content via
    the Telethon user session."""
    if not update.effective_message or not update.effective_user:
        return
    cfg = context.bot_data["config"]
    user_id = update.effective_user.id
    if not cfg.is_admin(user_id):
        return

    text = update.effective_message.text or update.effective_message.caption or ""
    user_session: UserSession = context.bot_data.get("user_session")
    if not user_session or not user_session.available:
        await update.effective_message.reply_text(
            "Locked-channel forwarding needs a Telethon user session, but "
            "none is configured. Run `python login.py` first."
        )
        return

    parsed = parse_telegram_link(text)
    if not parsed:
        await update.effective_message.reply_text(_link_error_help(text))
        return

    status = await update.effective_message.reply_text(
        f"🔗 Fetching message from `{parsed.chat_ref}` / msg `{parsed.message_id}`...",
        parse_mode="Markdown",
    )
    try:
        fetched, diag = await user_session.fetch_message(parsed)
    except Exception as e:
        logger.exception("fetch_message failed")
        await status.edit_text(
            f"❌ Failed to fetch: `{type(e).__name__}: {e}`\n\n"
            f"This is unexpected — the fetch_message method should have caught this. "
            f"Check the bot logs.",
            parse_mode="Markdown",
        )
        return

    if not fetched:
        # Show the full step-by-step diagnostic log to the user so they can
        # pinpoint exactly where the fetch failed.
        diag_text = "\n".join(diag)
        # Telegram messages have a 4096-char limit. Truncate if needed.
        if len(diag_text) > 3800:
            diag_text = diag_text[:3800] + "\n... (truncated)"
        await status.edit_text(
            f"❌ Couldn't fetch that message.\n\n"
            f"📋 Step-by-step diagnostic:\n```\n{diag_text}\n```",
            parse_mode="Markdown",
        )
        return

    msg = fetched["message"]
    album = fetched.get("album") or []

    # Hybrid approach (GetRestrictedMessages research):
    # Instead of downloading media to disk now and re-uploading via Bot API
    # when the user taps a topic, we store the source chat_id and message
    # IDs. When the user taps a topic, Telethon's send_to_destination:
    #   1. Tries forward_messages (true Telegram forward — fastest)
    #   2. Falls back to send_message(file=msg.media) which re-uploads
    #      from Telegram's servers WITHOUT downloading to disk first.
    #   3. Falls back to download + re-upload for fully protected content.
    #
    # The trade-off: the message appears as sent by the user account (since
    # it's the user account sending), not by the bot. For most users this is
    # fine — they're sending to their own group with their own account.

    source_chat_id = fetched["chat_id"]
    source_message_ids = [m.id for m in (album if album else [msg])]

    # Determine media type for display (without downloading)
    from telethon.tl import types as tl
    has_media = bool(getattr(msg, "media", None))
    media_types: list[str] = []
    if album:
        for m in album:
            if isinstance(getattr(m, "media", None), tl.MessageMediaPhoto):
                media_types.append("photo")
            elif isinstance(getattr(m, "media", None), tl.MessageMediaDocument):
                doc = m.media.document
                if doc and doc.mime_type and doc.mime_type.startswith("video/"):
                    media_types.append("video")
                else:
                    media_types.append("document")
            else:
                media_types.append("text")
    else:
        if isinstance(getattr(msg, "media", None), tl.MessageMediaPhoto):
            media_types.append("photo")
        elif isinstance(getattr(msg, "media", None), tl.MessageMediaDocument):
            doc = msg.media.document
            if doc and doc.mime_type and doc.mime_type.startswith("video/"):
                media_types.append("video")
            else:
                media_types.append("document")
        elif msg.message:
            media_types.append("text")
        else:
            media_types.append("unknown")

    caption_preview = (msg.message or "")[:50]

    payload = {
        "direct_send": True,  # signals to use the fast path
        "source_chat_id": source_chat_id,
        "source_message_ids": source_message_ids,
        "has_media": has_media,
        "media_types": media_types,
        "caption_preview": caption_preview,
    }

    db: Database = context.bot_data["db"]
    pending_id = await db.create_pending(
        user_id=user_id,
        source_chat_id=source_chat_id,
        source_message_id=parsed.message_id,
        payload=payload,
        kind="link",
    )

    # Show topic picker (or single Forward button for non-forum)
    topics_mgr = context.bot_data["topics"]
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await status.edit_text("No destination group set. Use /setgroup <group_id>.")
        return

    # Build the label showing what was fetched
    n = len(source_message_ids)
    if n == 1:
        label = f"Fetched 1 {media_types[0]}"
        if caption_preview:
            label += f" (\"{caption_preview}...\")"
        label += " — pick a destination:"
    elif n > 1:
        type_counts: dict[str, int] = {}
        for t in media_types:
            type_counts[t] = type_counts.get(t, 0) + 1
        summary = ", ".join(f"{c} {t}" for t, c in type_counts.items())
        label = f"Fetched {n} items ({summary}) — pick a destination:"
    else:
        label = "Fetched message — pick a destination:"

    # Use the same picker logic as direct forward — handles both forum and non-forum.
    # The user sent the link in a private chat with the bot, so chat_id == user_id.
    # _send_picker_new sends a NEW message (doesn't reply to the original link)
    from handlers.direct import _send_picker_new
    await _send_picker_new(context, update.effective_chat.id, pending_id, label)
