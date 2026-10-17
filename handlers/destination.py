"""Destination resolution — where forwarded content goes.

Historically the destination was one group id (DESTINATION_GROUP_ID env or
/setgroup), and EVERY path (link picker, direct sends, /scrape) hard-failed
with "No destination group set" when it was missing.

New behavior requested by the user:
  1. When NO destination group is set, forwards go to Saved Messages
     (Telethon "me") instead of erroring out.
  2. A persistent toggle makes Saved Messages the DEFAULT destination even
     when a group IS set (/dest saved on|off, stored in the DB so it
     survives restarts; env default SAVED_MESSAGES_DEFAULT).
  3. Commands can still force one side per-run: the `saved` flag means
     "this run goes to Saved Messages" and the new `group` flag means
     "this run goes to the group even if the default is Saved Messages".

Resolution order (resolve_destination):
  prefer="saved" -> Saved Messages (explicit flag)
  prefer="group" -> the configured group; None (caller must error) if unset
  prefer=None    -> toggle ON  -> Saved Messages (it IS the default)
                 -> toggle OFF -> group if set
                                -> Saved Messages fallback (with a note)

Saved Messages is only reachable through the Telethon user session — the
Bot API cannot send to a user's self-chat. Callers that need to actually
DELIVER there must use user_session.send_to_destination(dest_chat_id="me").
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SAVED_ID = "me"                    # Telethon's entity for the user's own chat
SAVED_LABEL = "Saved Messages"
DB_KEY = "saved_messages_default"  # runtime_config key ("1"/"0")

_ENV_TRUE = ("1", "true", "yes", "on")


@dataclass
class DestSpec:
    """One resolved destination. `dest_id` is either an int (group/chat id)
    or the string "me" (Saved Messages)."""
    dest_id: object          # int | "me"
    label: str
    is_saved: bool
    topic_id: int | None     # Saved Messages has no topics -> always None
    note: str | None = None  # one-line why (shown on the start message once)

    @property
    def dest_line(self) -> str:
        base = f"Destination: {self.label}"
        if self.note:
            base += f"\nℹ️ {self.note}"
        return base


async def get_saved_default(context) -> bool:
    """Is Saved Messages the DEFAULT destination?

    Precedence: bot_data cache (set by /dest) > DB runtime_config (set on a
    previous boot) > env default (SAVED_MESSAGES_DEFAULT). Same pattern as
    the /strip settings."""
    cached = context.bot_data.get("saved_default")
    if cached is not None:
        return bool(cached)
    db = context.bot_data["db"]
    cfg = context.bot_data["config"]
    raw = await db.get_runtime(DB_KEY)
    if raw is not None:
        return raw in _ENV_TRUE
    import os
    return os.environ.get("SAVED_MESSAGES_DEFAULT", "").strip().lower() in _ENV_TRUE


async def set_saved_default(context, on: bool) -> None:
    """Persist the toggle (DB), cache it on bot_data, and mirror it into the
    dashboard state — same pattern _sync_strip_state uses."""
    db = context.bot_data["db"]
    await db.set_runtime(DB_KEY, "1" if on else "0")
    context.bot_data["saved_default"] = bool(on)
    try:
        import bot as _bot
        _bot._STATE["dest"] = _dest_state_payload(context, on)
    except Exception:
        logger.debug("could not sync saved-default state to dashboard",
                     exc_info=True)


def _dest_state_payload(context, on: bool) -> dict:
    cfg = context.bot_data["config"]
    return {
        "saved_default": bool(on),
        "group_id": cfg.destination_group_id,
    }


async def _configured_group(context):
    """The group from env /setgroup — DB value wins (same rule as
    everywhere else in the bot)."""
    cfg = context.bot_data["config"]
    gid = cfg.destination_group_id
    if gid is None:
        db = context.bot_data["db"]
        raw = await db.get_runtime("destination_group_id")
        if raw:
            try:
                gid = int(raw)
            except ValueError:
                gid = None
    return gid


async def resolve_destination(context, prefer: str | None = None):
    """Resolve the destination for a forward/scrape run.

    Args:
      prefer: None (auto) | "saved" | "group"

    Returns:
      DestSpec, or None ONLY when prefer="group" was explicitly requested
      but no group is configured (the caller shows an error and aborts).
      The auto path NEVER returns None — with nothing configured it falls
      back to Saved Messages.
    """
    prefer = (prefer or "").lower() or None

    if prefer in ("saved", "me", "self"):
        return DestSpec(
            dest_id=SAVED_ID, label=SAVED_LABEL, is_saved=True, topic_id=None,
            note=None)

    group = await _configured_group(context)

    if prefer == "group":
        if group is None:
            return None
        return DestSpec(dest_id=group, label=_group_label(context, group),
                        is_saved=False, topic_id=None, note=None)

    # ---- auto ----
    toggle = await get_saved_default(context)
    if toggle:
        return DestSpec(
            dest_id=SAVED_ID, label=SAVED_LABEL, is_saved=True, topic_id=None,
            note="Saved Messages is the default destination (/dest saved off "
                 "to use the group instead)")
    if group is not None:
        return DestSpec(dest_id=group, label=_group_label(context, group),
                        is_saved=False, topic_id=None, note=None)
    return DestSpec(
        dest_id=SAVED_ID, label=SAVED_LABEL, is_saved=True, topic_id=None,
        note="No destination group set — sending to Saved Messages "
             "(/setgroup to set one, or /dest saved on to keep this)")


def _group_label(context, group: int) -> str:
    """Human label for a group destination; reuse the cached chat title when
    it matches this group (populated by /setgroup and the picker checks)."""
    bd = context.bot_data
    if (bd.get("destination_is_forum_group_id") == group
            and bd.get("destination_chat_title")):
        return f"{bd['destination_chat_title']} ({group})"
    return f"chat {group}"


__all__ = [
    "SAVED_ID", "SAVED_LABEL", "DB_KEY", "DestSpec",
    "get_saved_default", "set_saved_default", "resolve_destination",
]
