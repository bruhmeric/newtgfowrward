"""Telethon user-session manager.

Used to:
  * Enumerate forum topics in the destination group (requires the user account
    to be a member of that group)
  * Fetch messages from locked / private channels the user account is a member
    of, including channels where forwarding is disabled by the admin

Login flow is handled by `login.py` separately so that bot.py never needs to
prompt interactively.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import ipaddress
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient, helpers as _tg_helpers
from telethon.errors import ChannelPrivateError, InviteHashInvalidError
from telethon.tl import types as tl

# FIX: GetForumTopicsRequest moved from telethon.tl.functions.messages to
# telethon.tl.functions.channels in newer Telethon releases. The old import
# crashes at startup on Telethon >= 1.37 (and the unpinned >=1.36.0 in
# requirements.txt installed the latest version on every fresh deploy).
# Import from either location, whichever this Telethon version provides.
try:
    from telethon.tl.functions.channels import GetForumTopicsRequest  # Telethon >= 1.37
except ImportError:  # pragma: no cover — very old Telethon
    from telethon.tl.functions.messages import GetForumTopicsRequest  # type: ignore

logger = logging.getLogger(__name__)

# Progress formatters (repo-root module; no project-internal imports ->
# no circularity with handlers/* or bot.py, which all import this module).
from progress import format_scrape_progress


# ---------- Telegram datacenter reachability diagnostics ----------

# Official production DC IPv4 endpoints (same list Telethon ships with).
# Used ONLY for reachability probing — the session itself decides which DC
# the client actually connects to.
TELEGRAM_DC_IPS: dict[int, str] = {
    1: "149.154.175.53",    # DC1 — Miami
    2: "149.154.167.51",    # DC2 — Amsterdam (Telethon default)
    3: "149.154.175.100",   # DC3 — Miami
    4: "149.154.167.91",    # DC4 — Amsterdam
    5: "91.108.56.130",     # DC5 — Singapore
}

# Telegram's officially routed IPv4 space. A session string may embed ANY
# address in these ranges — not just the five canonical IPs above (e.g.
# 149.154.175.58 is a perfectly valid DC1 front door). Range-match instead
# of exact-matching so genuine Telegram addresses aren't misclassified as
# "suspicious public IPs".
TELEGRAM_NETWORKS = [
    ipaddress.ip_network(n) for n in (
        "149.154.160.0/20",    # DC1-DC4 (Miami / Amsterdam)
        "91.105.192.0/23",     # DC2 Amsterdam
        "91.108.4.0/22",       # DC3 Miami
        "91.108.8.0/21",       # DC4 Amsterdam
        "91.108.16.0/21",
        "91.108.56.0/22",      # DC5 Singapore
    )
]

# Process-wide cache for the all-DC sweep so a retrying loop doesn't hammer
# five datacenters on every attempt — once per process is plenty.
_DC_SWEEP_CACHE: dict | None = None


def classify_session_address(addr: str) -> str:
    """Classify the server address embedded in a session string.

    Returns 'telegram' (an IP inside Telegram's routed ranges), 'private'
    (RFC1918/loopback — the string was generated behind a VPN/proxy and can
    NEVER work from a public host like Render), 'public' (some other global
    IP — suspicious but still attempted), or 'hostname'."""
    if not addr:
        return "unknown"
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "hostname"
    if ip.version == 4 and any(ip in net for net in TELEGRAM_NETWORKS):
        return "telegram"
    return "public" if ip.is_global else "private"


def _compat_parse_session_string(session_string: str):
    """Raw-decode a Telethon StringSession, handling TWO address dialects.

    Native Telethon 1.40 layout:
        dc_id (1B) + packed IP (4 or 16 B) + port (2B) + auth key (256B)
    Older / third-party generator dialect:
        dc_id (1B) + ip_len (2B) + ASCII IP + port (2B) + auth key (256B)

    Telethon 1.40 only understands the first. A string in the second
    dialect still parses WITHOUT error — but the length prefix + ASCII
    bytes get read as the IP itself, producing a garbage IPv6 like
    'e:3134:392e:31:35:...' that the client then dials forever. Observed in
    the wild as "connect timed out after 30s" on every host, with a
    perfectly valid session.

    Returns (dc_id, address, port, dialect) or None if nothing sensible
    could be extracted. Never raises.
    """
    s = session_string
    if not s or s[0] != "1":
        return None
    try:
        raw = base64.urlsafe_b64decode(s[1:] + "=" * (-len(s[1:]) % 4))
    except Exception:
        return None
    if len(raw) < 260:  # 1 (dc) + 4 (min ip) + 2 (port) + 256 (key) - 3 slack
        return None
    dc_id = raw[0]
    # Dialect B first — its exact total-length self-check makes a false
    # positive on a native string effectively impossible (the "length"
    # would have to be the first two bytes of a packed IP AND make the
    # total come out exactly right).
    n = int.from_bytes(raw[1:3], "big")
    if 3 <= n <= 253 and len(raw) == 3 + n + 2 + 256:
        ip_bytes = raw[3:3 + n]
        if all(33 <= b <= 126 for b in ip_bytes):  # printable ASCII host
            host = ip_bytes.decode("ascii", errors="replace")
            port = int.from_bytes(raw[3 + n:3 + n + 2], "big")
            if port > 0:
                return dc_id, host, port, "length-prefixed-ascii"
    # Dialect A: native packed-binary IP
    k = len(raw) - 259
    if k in (4, 16):
        try:
            host = str(ipaddress.ip_address(raw[1:1 + k]))
        except ValueError:
            host = ""
        port = int.from_bytes(raw[1 + k:3 + k], "big")
        if host and port > 0:
            return dc_id, host, port, "native"
    return None


# ---------- link parsing ----------

# Private channel link: https://t.me/c/1234567890/42
PRIVATE_LINK_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/c/(\d+)/(\d+)(?:/(\d+))?",
    re.IGNORECASE,
)
# Public channel link: https://t.me/channelname/42
PUBLIC_LINK_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/([A-Za-z][A-Za-z0-9_]{3,})/(\d+)(?:/(\d+))?",
    re.IGNORECASE,
)


@dataclass
class ParsedLink:
    kind: str  # 'private' | 'public' | 'invite'
    chat_ref: str  # '-1001234567890' or 'channelname' or invite hash
    message_id: int
    # For albums: a list of message ids, else None
    extra_message_ids: Optional[list[int]] = None


def parse_telegram_link(url: str) -> Optional[ParsedLink]:
    """Return a ParsedLink if `url` looks like a t.me deep link to a post."""
    url = url.strip()
    m = PRIVATE_LINK_RE.search(url)
    if m:
        # Telegram private channel link format: t.me/c/<raw_id>/<msg_id>
        # Bot API chat_id = -100 concatenated with raw_id, i.e. -1e12 - raw_id
        raw_id = int(m.group(1))
        chat_id = -1_000_000_000_000 - raw_id
        msg_id = int(m.group(2))
        return ParsedLink(kind="private", chat_ref=str(chat_id), message_id=msg_id)
    m = PUBLIC_LINK_RE.search(url)
    if m:
        # Skip if this is actually an invite link like t.me/+abc...; the regex
        # already requires the first char to be a letter, so 'abc' style invites
        # won't match — but we still need to reject '+' and 'joinchat' links.
        username = m.group(1)
        if username.lower() in ("joinchat", "share", "addstickers", "setlanguage"):
            return None
        msg_id = int(m.group(2))
        return ParsedLink(kind="public", chat_ref=username, message_id=msg_id)
    # t.me/+abc... (private invite, no message_id) — we don't auto-join, skip
    return None


# Regex for channel-only links (no message_id):
# https://t.me/c/1234567890  (private channel, no msg_id)
# https://t.me/channelname   (public channel, no msg_id)
PRIVATE_CHANNEL_ONLY_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/c/(\d+)/?$",
    re.IGNORECASE,
)
PUBLIC_CHANNEL_ONLY_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$",
    re.IGNORECASE,
)


def parse_channel_link(url: str) -> Optional[ParsedLink]:
    """Parse a channel-only link (no message_id) — used by /scrape.

    Returns a ParsedLink with message_id=0 (signaling "no specific message")
    or None if the URL doesn't match.
    """
    url = url.strip()
    # Strip any trailing query string or fragment
    url = url.split("?")[0].split("#")[0]

    m = PRIVATE_CHANNEL_ONLY_RE.match(url)
    if m:
        raw_id = int(m.group(1))
        chat_id = -1_000_000_000_000 - raw_id
        return ParsedLink(kind="private", chat_ref=str(chat_id), message_id=0)

    m = PUBLIC_CHANNEL_ONLY_RE.match(url)
    if m:
        username = m.group(1)
        if username.lower() in ("joinchat", "share", "addstickers", "setlanguage"):
            return None
        return ParsedLink(kind="public", chat_ref=username, message_id=0)

    return None


# ---------- link stripping (the /strip feature) ----------

# Raw URLs in plain text — catches links even when the message carries no
# URL entities at all (plain-text posts, some clients). Matches http(s)
# links, www. links, and scheme-less t.me / telegram.me deep links.
URL_TEXT_RE = re.compile(
    r"(?:https?://\S+|www\.\S+|\bt\.me/\S+|\btelegram\.me/\S+)",
    re.IGNORECASE,
)


def _merge_ranges(ranges: list) -> list:
    """Sort + merge overlapping (start, end) ranges."""
    merged: list = []
    for s, e in sorted(ranges):
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def _remove_ranges(s: str, ranges: list) -> str:
    out, cur = [], 0
    for s_, e_ in ranges:
        out.append(s[cur:s_])
        cur = e_
    out.append(s[cur:])
    return "".join(out)


def _post_removal_cleanup_ranges(orig: str, inter: str) -> list:
    """Extra ranges to remove AFTER link removal so the remaining text reads
    naturally instead of showing the holes where links used to be:

      * runs of 2+ spaces collapse to one (a URL between two words leaves
        "word  word" otherwise)
      * spaces directly before a newline / at the end of the text vanish
      * a line that BECAME empty because its only content was a link is
        deleted entirely (link-on-its-own-line posts)
      * runs of intentionally-blank lines collapse to a single blank line

    `orig` is the text BEFORE link removal, `inter` AFTER — both in the same
    surrogate-aware index space. No removed range ever contains a newline,
    so the two texts have identical line structure and we can compare line
    by line. Returns extra (start, end) ranges to remove.
    """
    extra: list = []

    # 1) collapse runs of 2+ spaces/tabs into one
    for m in re.finditer(r"[ \t]{2,}", inter):
        extra.append((m.start() + 1, m.end()))

    # 2) spaces hanging right before a newline or at the very end
    for m in re.finditer(r"[ \t]+(?:\n|$)", inter):
        ends_with_nl = m.group().endswith("\n")
        extra.append((m.start(), m.end() - (1 if ends_with_nl else 0)))

    if "\n" in inter:
        # 3) line-level cleanup (line indexes are identical in orig/inter)
        starts, pos = [], 0
        for line in inter.split("\n"):
            starts.append(pos)
            pos += len(line) + 1
        orig_lines = orig.split("\n")
        inter_lines = inter.split("\n")
        n = len(inter_lines)
        to_delete: list[int] = []
        blank_run: list[int] = []

        def _flush_run() -> None:
            # Keep the FIRST blank of a run only when it was blank in the
            # original too (an intentional separator, not a hole) and it is
            # not the first/last line of the text.
            for k, idx in enumerate(blank_run):
                keep = (k == 0 and 0 < idx < n - 1
                        and orig_lines[idx].strip() == "")
                if not keep:
                    to_delete.append(idx)
            blank_run.clear()

        for i, (o_line, n_line) in enumerate(zip(orig_lines, inter_lines)):
            if n_line.strip() == "":
                if o_line.strip() != "":
                    to_delete.append(i)   # became empty — its content was a link
                else:
                    blank_run.append(i)   # was already blank — intentional
            else:
                _flush_run()
        _flush_run()

        for i in to_delete:
            if i + 1 < n:
                # the line plus its trailing newline
                extra.append((starts[i], starts[i] + len(inter_lines[i]) + 1))
            else:
                # last line — eat the preceding newline instead
                extra.append((max(0, starts[i] - 1), len(inter)))

    return extra


def strip_links_from_text(text: str, entities=None) -> tuple[str, object, bool]:
    """Remove every link from `text`, keeping everything else intact.

    Handles all three ways links appear in Telegram messages:
      * raw URL text (https://…, www.…, t.me/…) — the URL substring is
        deleted, even when the message has no entities at all
      * MessageEntityUrl — same, using the entity's exact range
      * MessageEntityTextUrl ("click [here]" hyperlinks) — the visible text
        is KEPT, only the link behind it is dropped

    Non-link entities (bold, italic, hashtags, mentions…) survive with
    their offsets shifted to match the new text. Entity offsets are UTF-16
    based (Telegram convention), so all range math runs on the surrogate-
    aware form of the text via Telethon's helpers.

    Returns (new_text, new_entities_or_None, changed). `changed` is False
    only when nothing needed stripping (text and entities come back as-is)
    — callers use it to keep Telegram's fast true-forward path when there
    is nothing to clean.
    """
    if not text:
        return text, entities, False
    s16 = _tg_helpers.add_surrogate(text)
    removals: list = []
    has_text_url = False
    for e in entities or []:
        if isinstance(e, tl.MessageEntityUrl):
            removals.append((e.offset, e.offset + e.length))
        elif isinstance(e, tl.MessageEntityTextUrl):
            has_text_url = True   # visible text stays; the hyperlink is dropped
    for m in URL_TEXT_RE.finditer(s16):
        removals.append((m.start(), m.end()))
    if not removals and not has_text_url:
        return text, entities, False

    # Two-pass removal: the URL ranges live in the ORIGINAL index space, the
    # cleanup ranges (collapsed spaces, deleted lines) are computed on the
    # text AFTER pass 1 — so pass 2 must apply them to `inter`, not to the
    # original. Entity offsets shift once per pass.
    removals = _merge_ranges(removals)
    inter = _remove_ranges(s16, removals)
    extra = _post_removal_cleanup_ranges(s16, inter)
    out = _remove_ranges(inter, extra)

    kept_entities = []
    for e in entities or []:
        if isinstance(e, (tl.MessageEntityUrl, tl.MessageEntityTextUrl)):
            continue
        s_, e_ = e.offset, e.offset + e.length
        # pass 1: does the entity overlap a removed URL span?
        if any(s_ < r[1] and r[0] < e_ for r in removals):
            continue
        inter_off = s_ - sum(r[1] - r[0] for r in removals if r[1] <= s_)
        inter_end = inter_off + e.length
        # pass 2: does it overlap a cleanup span in the intermediate text?
        if any(inter_off < r[1] and r[0] < inter_end for r in extra):
            continue
        final_off = inter_off - sum(r[1] - r[0] for r in extra
                                    if r[1] <= inter_off)
        try:
            ne = copy.copy(e)
            ne.offset = final_off
            kept_entities.append(ne)
        except Exception:
            logger.debug("entity copy failed during link strip", exc_info=True)

    new_text = _tg_helpers.del_surrogate(out)
    # If we got here at least one link was found, so the message content DID
    # change (even when the visible text is identical — a hyperlink was
    # removed from behind it).
    return new_text, (kept_entities or None), True


def text_has_links(text: str, entities=None) -> bool:
    """True when `text` contains any link (URL entity, text-url entity, or a
    raw URL in the text itself)."""
    if not text:
        return False
    if any(isinstance(e, (tl.MessageEntityUrl, tl.MessageEntityTextUrl))
           for e in (entities or [])):
        return True
    return bool(URL_TEXT_RE.search(text))


# ---------- from-point scraping (post link + #old / #new) ----------

# Matches the direction hashtag in a plain message: #old / #new with an
# optional count baked in (#new120) or as a separate number after it.
SCRAPE_POINT_TAG_RE = re.compile(r"#(old|new)(\d{1,4})?\b", re.IGNORECASE)


def parse_scrape_point_request(text: str) -> Optional[tuple[ParsedLink, str, int]]:
    """Detect a from-point scrape request in a plain (non-command) message.

    Triggers when the message contains BOTH:
      * a post link — t.me/<channel>/<msg_id> or t.me/c/<id>/<msg_id>
      * a direction hashtag — #old (walk back in history from that post) or
        #new (walk forward), with an optional count: "#new", "#new120" or
        "#new 120" (default 50, clamped to 500)

    This is the "send a post link with a flag" flow: the admin forwards a
    message from the source channel with #old/#new appended and the bot
    scrapes from exactly that point. Returns (ParsedLink, direction, count)
    or None when the message is not a from-point request.
    """
    if not text:
        return None
    m = SCRAPE_POINT_TAG_RE.search(text)
    if not m:
        return None
    link = parse_telegram_link(text)
    if not link:
        return None   # hashtag but no post link (e.g. a channel-only link)
    direction = m.group(1).lower()
    count = int(m.group(2)) if m.group(2) else 50
    if not m.group(2):
        # optional separate number right after the hashtag ("#new 120")
        rest = text[m.end():m.end() + 12].lstrip()
        num = re.match(r"(\d{1,4})\b", rest)
        if num:
            count = int(num.group(1))
    return link, direction, max(1, min(count, 500))


def order_messages_for_delivery(msgs: list, direction: str) -> list:
    """Order messages fetched around an anchor point for delivery.

    'old'  — fetched newest→oldest (iter_messages walked backward from the
             anchor), so the list is reversed into chronological order and
             the destination reads like the source channel does
    'new'  — already fetched chronologically (reverse=True), pass-through

    Album runs (consecutive messages sharing a grouped_id) are re-sorted
    ascending by id in both cases — reversing the whole batch would flip
    the items inside each album, and forward_messages/send_file send album
    items in the order they're given.
    """
    out = list(msgs)
    if direction == "old":
        out.reverse()
    fixed: list = []
    i = 0
    while i < len(out):
        gid = getattr(out[i], "grouped_id", None)
        if not gid:
            fixed.append(out[i])
            i += 1
            continue
        j = i
        while j + 1 < len(out) and getattr(out[j + 1], "grouped_id", None) == gid:
            j += 1
        fixed.extend(sorted(out[i:j + 1], key=lambda m: m.id))
        i = j + 1
    return fixed


# ---------- session manager ----------

class UserSession:
    # Telethon's connect()/is_user_authorized() have NO built-in timeout — a
    # session string pointing at an unreachable data center (or a network
    # black hole) hangs FOREVER. These ceilings turn a hang into a clean,
    # explainable failure that the bot can show on its dashboard.
    # 60 s (up from 30): on a cold 0.1-CPU free-tier instance the first
    # MTProto handshake can legitimately take tens of seconds; a timeout
    # that fires too early looks identical to a broken session.
    CONNECT_TIMEOUT = int(os.environ.get("TELETHON_CONNECT_TIMEOUT", "60"))
    AUTH_CHECK_TIMEOUT = 30
    RPC_TIMEOUT = 20
    PROBE_TIMEOUT = 8  # raw TCP probe of the DC before Telethon connects

    def __init__(self, session_name: str, api_id: int, api_hash: str,
                 session_string: Optional[str] = None) -> None:
        """Create a Telethon client backed by either a file-based session
        (session_name) or a StringSession (session_string). StringSession is
        preferred for ephemeral filesystems like Render's free tier."""
        self._init_error: Optional[str] = None
        self._dialogs_warmed = 0
        # Non-secret facts about the parsed session (dc/server/port) + the
        # result of the last TCP reachability probe — shown on the dashboard
        # so "why won't Telethon connect" is answerable WITHOUT log access.
        self.session_info: dict = {}
        self.last_probe: Optional[dict] = None
        if session_string:
            from telethon.sessions import StringSession
            try:
                session_obj = StringSession(session_string)
            except Exception as e:
                # A malformed SESSION_STRING used to blow up here, OUTSIDE any
                # try/except, crashing the whole startup. Now it becomes a
                # clear dashboard message instead.
                self._init_error = (
                    f"SESSION_STRING is malformed ({type(e).__name__}) — copy the "
                    "ENTIRE string printed by: python login.py --string")
                logger.error("%s", self._init_error)
                # Build the client with a blank StringSession so self.client is
                # always a valid TelegramClient; start() refuses to use it.
                session_obj = StringSession()
            else:
                # Parse succeeded. FIRST repair address dialects Telethon
                # 1.40 misparses (see _compat_parse_session_string) — a
                # valid session in the older dialect otherwise dials a
                # garbage IPv6 and times out on every host, forever.
                compat = _compat_parse_session_string(session_string)
                if compat is not None:
                    dc_c, addr_c, port_c, dialect = compat
                    loaded_addr = str(session_obj.server_address or "")
                    if (addr_c, port_c) != (loaded_addr, session_obj.port):
                        key_backup = session_obj.auth_key
                        try:
                            session_obj.set_dc(dc_c, addr_c, port_c)
                            if (session_obj.auth_key is None
                                    and key_backup is not None):
                                # set_dc must never eat the auth key
                                session_obj._auth_key = key_backup
                            logger.info(
                                "SESSION_STRING dialect repaired ('%s'): "
                                "%r -> DC%s %s:%s — Telethon misread the "
                                "address field; the auth key is untouched",
                                dialect, loaded_addr, dc_c, addr_c, port_c)
                        except Exception:
                            logger.debug("dialect repair failed (ignored)",
                                         exc_info=True)
                # Then introspect what the string actually points at. NONE
                # of this is secret (the auth key itself never leaves the
                # process), and it answers the #1 mystery of "connect timed
                # out": WHICH address was being dialed.
                try:
                    key_len = 0
                    if session_obj.auth_key is not None:
                        key_len = len(session_obj.auth_key.key or b"")
                    self.session_info = {
                        "dc_id": session_obj.dc_id,
                        "server": session_obj.server_address,
                        "port": session_obj.port,
                        "auth_key_bytes": key_len,
                    }
                    logger.info(
                        "SESSION_STRING parsed: DC%s -> %s:%s (auth key: %d bytes)",
                        session_obj.dc_id, session_obj.server_address,
                        session_obj.port, key_len)
                except Exception:
                    logger.debug("session introspection failed", exc_info=True)
                kind = classify_session_address(
                    str(self.session_info.get("server") or ""))
                # The embedded address is NOT a usable Telegram endpoint — a
                # VPN/proxy artifact, a misparse, or some random IP. The auth
                # key is bound to the DATA CENTER, not to the front-door
                # address, so dialing the canonical DC address is safe and
                # repairs the session instead of failing.
                if kind not in ("telegram", "hostname"):
                    dc_id = self.session_info.get("dc_id")
                    try:
                        canonical = TELEGRAM_DC_IPS.get(int(dc_id)) if dc_id else None
                    except (TypeError, ValueError):
                        canonical = None
                    if canonical:
                        old_addr = self.session_info.get("server")
                        port = int(self.session_info.get("port") or 443)
                        key_backup = session_obj.auth_key
                        try:
                            session_obj.set_dc(int(dc_id), canonical, port)
                            if (session_obj.auth_key is None
                                    and key_backup is not None):
                                session_obj._auth_key = key_backup
                            self.session_info["server"] = canonical
                            kind = "telegram"
                            logger.warning(
                                "SESSION_STRING address %r is not a Telegram "
                                "DC (VPN/proxy artifact?) — dialing canonical "
                                "DC%s %s:%s instead (the auth key is "
                                "DC-bound, so this is safe)",
                                old_addr, dc_id, canonical, port)
                        except Exception:
                            logger.debug("canonical DC override failed",
                                         exc_info=True)
                self.session_info["address_kind"] = kind
                if kind == "private":
                    # No canonical DC to fall back to either — a private
                    # address with an unknown DC can never connect from a
                    # public host. Fail fast with instructions instead of
                    # burning a 60 s timeout on every retry forever.
                    self._init_error = (
                        f"SESSION_STRING embeds a private address "
                        f"({self.session_info.get('server')}) and no known DC "
                        "to fall back to — it was generated behind a "
                        "VPN/proxy. Regenerate WITHOUT the proxy: "
                        "python login.py --string (same API_ID/API_HASH)")
                    logger.error("%s", self._init_error)
                    session_obj = StringSession()
            self.client = TelegramClient(
                session_obj, api_id, api_hash,
                # The background loop in bot.py owns the retry policy (backoff
                # + dashboard state) — keep Telethon's internal retries small
                # so failures surface quickly instead of hanging inside
                # attempt #1 of 5 while the outer wait_for() fires.
                connection_retries=3, retry_delay=2,
                auto_reconnect=True,
                # A StringSession embeds an IPv4 DC address; never let Telethon
                # try IPv6 (blackholed AAAA routes hang the connect).
                use_ipv6=False,
            )
            self._uses_string_session = True
        else:
            self.client = TelegramClient(session_name, api_id, api_hash)
            self._uses_string_session = False
        self._started = False
        # Human-readable reason for the last failed start() — shown on the
        # web dashboard so a bad SESSION_STRING is obvious without log access.
        self.last_error: Optional[str] = None
        # "@username (id)" after a successful connect — for the dashboard.
        self.connected_as: Optional[str] = None

    def _session_target_desc(self) -> str:
        """Human description of the endpoint this session dials, e.g.
        'DC4 (149.154.167.91:443)' — used in error messages and logs so a
        timeout is never a mystery about WHERE it was trying to go."""
        si = self.session_info or {}
        if si.get("server"):
            dc = f"DC{si.get('dc_id', '?')} " if si.get("dc_id") else ""
            return f"{dc}({si['server']}:{si.get('port', '?')})"
        return "the session's data center"

    @staticmethod
    async def _probe_tcp(host: str, port: int, timeout: float = 8.0) -> dict:
        """Raw TCP connect probe, independent of Telethon.

        Answers the question a Telethon timeout cannot: is the datacenter
        reachable from THIS host at all? Returns a small dict with
        ok / ms / error — never raises."""
        t0 = time.monotonic()
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout)
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=5)
            except Exception:
                pass
            return {"ok": True, "ms": int((time.monotonic() - t0) * 1000),
                    "error": None}
        except asyncio.TimeoutError:
            return {"ok": False, "ms": int((time.monotonic() - t0) * 1000),
                    "error": f"no TCP answer in {timeout:.0f}s (blackholed/filtered)"}
        except Exception as e:
            return {"ok": False, "ms": int((time.monotonic() - t0) * 1000),
                    "error": f"{type(e).__name__}: {e}"}

    async def _probe_session_dc(self) -> None:
        """Probe the session's own DC, and if it fails, sweep ALL official
        DCs (once per process). Populates self.last_probe with a verdict that
        the dashboard shows — turning 'timed out' into an actionable cause:
        either 'this host cannot reach MTProto at all' or 'the string's DC
        specifically is unreachable (regenerate it)'."""
        global _DC_SWEEP_CACHE
        si = self.session_info or {}
        host, port = si.get("server"), int(si.get("port") or 443)
        if not host:
            return
        probe = await self._probe_tcp(host, port, timeout=self.PROBE_TIMEOUT)
        self.last_probe = {"host": f"{host}:{port}", **probe}
        if probe["ok"]:
            logger.info("MTProto probe: %s:%s reachable in %dms — handing over "
                        "to Telethon", host, port, probe["ms"])
            return
        logger.warning("MTProto probe FAILED: %s:%s — %s. Sweeping all "
                       "Telegram DCs to localize the problem...",
                       host, port, probe["error"])
        if _DC_SWEEP_CACHE is None:
            ips = list(TELEGRAM_DC_IPS.items())
            results = await asyncio.gather(
                *(self._probe_tcp(ip, 443, timeout=6) for _dc, ip in ips))
            _DC_SWEEP_CACHE = {dc: r for (dc, _ip), r in zip(ips, results)}
        sweep = _DC_SWEEP_CACHE
        reachable = sorted(dc for dc, r in sweep.items() if r["ok"])
        self.last_probe["sweep"] = {
            str(dc): (f"OK ({r['ms']}ms)" if r["ok"] else r["error"])
            for dc, r in sweep.items()}
        if reachable:
            self.last_probe["verdict"] = (
                f"DCs {reachable} ARE reachable, but this session's DC "
                f"({host}) is NOT — the SESSION_STRING points somewhere this "
                "host can't reach. Regenerate it: python login.py --string")
        else:
            self.last_probe["verdict"] = (
                "NO Telegram DC accepts TCP connections from this host — "
                "MTProto egress is blocked (the Bot API over HTTPS still "
                "works, which is why the bot itself is fine). Telethon can "
                "never connect from this network.")
        logger.warning("DC sweep: %s | verdict: %s",
                       self.last_probe["sweep"], self.last_probe["verdict"])

    async def start(self) -> bool:
        """Connect using an existing session. Returns False if no session
        exists or the session is invalid. Callers should NOT call interactive
        login here — that's done by login.py.

        Every network step is wrapped in a timeout so this method ALWAYS
        returns (True/False) within ~CONNECT_TIMEOUT+AUTH_CHECK_TIMEOUT
        seconds — it can no longer hang a caller forever (which used to
        freeze the whole bot startup)."""
        self.last_error = None
        if self._init_error:
            self.last_error = self._init_error
            return False
        if not self._uses_string_session and not os.path.exists(self.session_filename):
            logger.warning("Telethon session file missing — run python login.py first.")
            self.last_error = "no session file and no SESSION_STRING set — run login.py"
            return False
        # Reachability check FIRST: if raw TCP to the DC can't even complete,
        # Telethon's connect() would just hang until the timeout — the probe
        # turns that into a concrete, dashboard-visible cause instead.
        if self._uses_string_session and self.session_info.get("server"):
            try:
                await self._probe_session_dc()
            except Exception:
                logger.debug("DC probe crashed (ignored — connect proceeds)",
                             exc_info=True)
        try:
            await asyncio.wait_for(self.client.connect(), timeout=self.CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            await self._force_disconnect()
            probe_note = ""
            if self.last_probe:
                if self.last_probe.get("verdict"):
                    probe_note = f" Probe verdict: {self.last_probe['verdict']}"
                else:
                    probe_note = (f" (raw TCP probe reached "
                                  f"{self.last_probe.get('host')} in "
                                  f"{self.last_probe.get('ms')}ms — the TCP path "
                                  "works, so the hang is in the MTProto "
                                  "handshake: likely a corrupt auth key)")
            self.last_error = (
                f"connection to {self._session_target_desc()} timed out after "
                f"{self.CONNECT_TIMEOUT}s — Telegram MTProto is unreachable "
                f"from this host, or the SESSION_STRING is corrupt.{probe_note}")
            logger.warning("Telethon connect timed out after %ss (target: %s)",
                           self.CONNECT_TIMEOUT, self._session_target_desc())
            return False
        except Exception as e:
            await self._force_disconnect()
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning("Telethon connect failed: %s", self.last_error)
            return False
        try:
            authorized = await asyncio.wait_for(
                self.client.is_user_authorized(), timeout=self.AUTH_CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            await self._force_disconnect()
            self.last_error = (
                f"authorization check timed out after {self.AUTH_CHECK_TIMEOUT}s — "
                "the SESSION_STRING is probably invalid or revoked")
            logger.warning("Telethon auth check timed out")
            return False
        except Exception as e:
            await self._force_disconnect()
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning("Telethon auth check failed: %s", self.last_error)
            return False
        if not authorized:
            await self._force_disconnect()
            self.last_error = ("SESSION_STRING is invalid, expired, or was "
                               "revoked — generate a fresh one with: python login.py --string")
            logger.warning("Telethon session exists but is not authorized.")
            return False
        self._started = True
        self.connected_as = await self._safe_get_me()
        logger.info("Telethon user session connected as %s (mode=%s)",
                    self.connected_as,
                    "string" if self._uses_string_session else "file")
        # Warm the entity cache in the background — see _warm_entity_cache.
        # Non-blocking: the session is usable immediately; numeric IDs get
        # resolvable a couple of seconds later.
        asyncio.get_running_loop().create_task(self._warmup_task())
        return True

    async def _warmup_task(self) -> None:
        """Background dialog warmup launched by start(); failures are
        non-fatal (the lazy fallback in _get_entity_with_warmup retries)."""
        try:
            await self._warm_entity_cache()
        except Exception:
            logger.debug("background dialog warmup failed", exc_info=True)

    async def _warm_entity_cache(self) -> int:
        """Fetch the account's dialogs once so numeric chat IDs resolve.

        StringSession — the only session type that survives Render's
        ephemeral disk — does NOT persist Telethon's entity cache. On every
        fresh boot, resolving a numeric ID like -1004372538320 fails with
        "Could not find the input entity" until the dialogs are fetched
        once. Without this warmup, topic discovery AND forwarding to the
        destination group silently fail after every restart.
        """
        if self._dialogs_warmed:
            return self._dialogs_warmed
        count = 0
        async for _dialog in self.client.iter_dialogs(limit=200):
            count += 1
        self._dialogs_warmed = count
        logger.info("Entity cache warmed with %d dialog(s)", count)
        return count

    async def _get_entity_with_warmup(self, ref):
        """get_entity() with a one-shot dialog warmup for fresh sessions.

        Raises the second failure if warmup doesn't fix resolution — callers
        already have good error reporting around their get_entity calls."""
        try:
            return await self.client.get_entity(ref)
        except Exception as first_err:
            if self._dialogs_warmed:
                raise
            logger.info("Entity %r not in cache (%s) — warming dialogs once...",
                        ref, type(first_err).__name__)
            try:
                warmed = await asyncio.wait_for(
                    self._warm_entity_cache(), timeout=self.RPC_TIMEOUT + 15)
            except asyncio.TimeoutError:
                raise first_err
            if not warmed:
                raise first_err
            return await self.client.get_entity(ref)

    async def _safe_get_me(self) -> str:
        try:
            me = await asyncio.wait_for(self.client.get_me(), timeout=self.RPC_TIMEOUT)
            return f"@{me.username} ({me.id})" if me else "?"
        except Exception:
            return "?"

    async def _force_disconnect(self) -> None:
        """Best-effort disconnect that NEVER hangs and never raises — used on
        failure paths so a half-open connection is not leaked."""
        try:
            await asyncio.wait_for(self.client.disconnect(), timeout=10)
        except Exception:
            logger.debug("force disconnect failed (ignored)", exc_info=True)

    @property
    def session_filename(self) -> str:
        if self._uses_string_session:
            return "<string-session>"
        # Telethon stores session in <session_name>.session
        try:
            return f"{self.client.session.filename}.session" if (
                hasattr(self.client, "session") and self.client.session
            ) else f"{self.client.api_id}.session"
        except Exception:
            return "user_session.session"

    async def stop(self) -> None:
        """Disconnect unconditionally. The old `if self._started` guard leaked
        connections whenever start() was interrupted mid-handshake (e.g. a
        cancelled connect after a timeout) — _started was never set but the
        socket was half-open."""
        try:
            if self.client.is_connected():
                await asyncio.wait_for(self.client.disconnect(), timeout=10)
        except Exception:
            logger.debug("stop(): disconnect failed (ignored)", exc_info=True)
        self._started = False

    @property
    def available(self) -> bool:
        return self._started and self.client.is_connected()

    # ---------- forum topic enumeration ----------

    async def list_forum_topics(self, chat_id: int) -> list[dict]:
        """Return list of {'id': int, 'title': str} for all forum topics except
        the General topic (id=1)."""
        try:
            # Resolve via get_entity (network) with the fresh-session warmup
            # fallback — get_input_entity alone only reads the cache, which is
            # EMPTY on every fresh StringSession boot (see _warm_entity_cache).
            await self._get_entity_with_warmup(chat_id)
            peer = await self.client.get_input_entity(chat_id)
        except Exception:
            logger.exception("could not resolve chat_id=%s", chat_id)
            return []

        topics: list[dict] = []
        offset_id = 0
        # Paginate up to 500 topics
        for _ in range(10):
            try:
                # NOTE: the parameter is called `channel` in newer Telethon
                # (was `peer` in old versions) — pass it positionally so the
                # call works on both.
                result = await self.client(GetForumTopicsRequest(
                    peer,
                    offset_date=0,
                    offset_id=offset_id,
                    offset_topic=0,
                    limit=100,
                ))
            except Exception:
                logger.exception("GetForumTopicsRequest failed")
                break
            for t in result.topics:
                # Skip General topic (id=1)
                if getattr(t, "id", 0) == 1:
                    continue
                title = getattr(t, "title", None) or f"Topic {t.id}"
                topics.append({"id": t.id, "title": title})
            # GetForumTopics returns topics ordered by creation date; we use
            # offset_id = last topic's top_message id, but Telethon's wrapper
            # already handles pagination internally in most cases. For
            # simplicity, we break if fewer than 100 returned.
            if len(result.topics) < 100:
                break
            # advance offset — use the last topic's top_message id
            offset_id = getattr(result.topics[-1], "top_message", 0) or 0
        return topics

    # ---------- message fetching for locked channels ----------

    async def fetch_message(self, parsed: ParsedLink) -> tuple[Optional[dict], list[str]]:
        """Fetch a single message (and possibly its album siblings) from a
        private / public channel the user account is a member of.

        Returns a tuple of (result_or_none, diagnostics_log) where:
          - result_or_none is a dict with keys:
              chat_id: int (negative for channels)
              message: telethon Message object (single)
              album: list[Message] | None  (album siblings if any)
            ...or None if fetch failed
          - diagnostics_log is a list of human-readable strings describing
            each step that was attempted (for surfacing to the user when
            something fails)

        The diagnostics_log is what made the difference — previously the bot
        would say "Couldn't fetch that message" with no detail. Now the user
        gets the full step-by-step trace so they can pinpoint the failure
        (e.g. "Step 2 failed: ChannelPrivateError - you're not a member").
        """
        diag: list[str] = []
        diag.append(f"Step 1: Parsed link → kind={parsed.kind}, chat_ref={parsed.chat_ref}, msg_id={parsed.message_id}")

        # Step 2: resolve entity
        try:
            if parsed.kind == "private":
                chat_id = int(parsed.chat_ref)
                diag.append(f"Step 2: Resolving entity for chat_id={chat_id} via Telethon get_entity()...")
                # Use get_entity instead of get_input_entity — it makes a
                # network call to resolve unknown entities (e.g. channels the
                # user just joined that aren't in Telethon's session cache).
                # get_input_entity only uses the cache and can fail for new
                # channels. _get_entity_with_warmup additionally fetches the
                # dialog list once on fresh StringSessions (whose entity cache
                # starts EMPTY on every deploy).
                entity = await self._get_entity_with_warmup(chat_id)
            else:
                diag.append(f"Step 2: Resolving entity for username={parsed.chat_ref} via Telethon get_entity()...")
                entity = await self._get_entity_with_warmup(parsed.chat_ref)
            diag.append(f"Step 2: ✓ Entity resolved → {type(entity).__name__} (id={getattr(entity, 'id', '?')})")
        except ChannelPrivateError as e:
            diag.append(f"Step 2: ✗ FAILED — ChannelPrivateError: {e}")
            diag.append("  → Your user account is NOT a member of this chat, or the chat doesn't exist.")
            return None, diag
        except Exception as e:
            diag.append(f"Step 2: ✗ FAILED — {type(e).__name__}: {e}")
            return None, diag

        # Step 3: fetch the message
        try:
            diag.append(f"Step 3: Fetching message id={parsed.message_id} via get_messages()...")
            messages = await self.client.get_messages(entity, ids=parsed.message_id)
        except Exception as e:
            diag.append(f"Step 3: ✗ FAILED — {type(e).__name__}: {e}")
            return None, diag

        if not messages:
            diag.append(f"Step 3: ✗ No message returned — message_id={parsed.message_id} may not exist in this chat.")
            return None, diag

        msg = messages[0] if isinstance(messages, list) else messages
        if not msg:
            diag.append("Step 3: ✗ Message object is empty.")
            return None, diag

        diag.append(f"Step 3: ✓ Message fetched (id={msg.id}, has_media={bool(getattr(msg, 'media', None))})")

        # Step 4: detect album
        #
        # FIX (important): the old code fetched `get_messages(entity,
        # limit=20)` — the 20 MOST RECENT messages of the channel — and
        # filtered them by grouped_id. For any post older than ~20 messages
        # from the top of the channel, the siblings were never found, so
        # albums silently degraded to "forward only the first item".
        #
        # Album message ids are consecutive, so we fetch a window AROUND the
        # target message id instead (ids may skip when the channel mixes in
        # service messages, hence a window of +/-10 which covers Telegram's
        # 10-item album limit).
        album: list = []
        if getattr(msg, "grouped_id", None):
            try:
                diag.append(f"Step 4: Message is part of an album (grouped_id={msg.grouped_id}). Fetching siblings...")
                lo = max(1, parsed.message_id - 10)
                hi = parsed.message_id + 10
                window_ids = list(range(lo, hi + 1))
                all_msgs = await self.client.get_messages(entity, ids=window_ids)
                album = [m for m in (all_msgs or [])
                         if m is not None and getattr(m, "grouped_id", None) == msg.grouped_id]
                album.sort(key=lambda m: m.id)
                if msg.id not in [m.id for m in album]:
                    album.append(msg)
                    album.sort(key=lambda m: m.id)
                diag.append(f"Step 4: ✓ Found {len(album)} item(s) in the album")
            except Exception as e:
                diag.append(f"Step 4: ✗ Album fetch failed (continuing with single message) — {type(e).__name__}: {e}")
                album = [msg]
        else:
            diag.append("Step 4: Message is NOT part of an album (single message).")

        # Step 5: compute chat_id in Bot API format
        chat_real_id = msg.peer_id.channel_id if isinstance(msg.peer_id, tl.PeerChannel) else msg.chat_id
        bot_api_chat_id = -1_000_000_000_000 - chat_real_id
        diag.append(f"Step 5: ✓ Chat resolved to Bot API chat_id={bot_api_chat_id}")

        return {
            "chat_id": bot_api_chat_id,
            "message": msg,
            "album": album or None,
        }, diag


    async def test_link(self, parsed: ParsedLink) -> dict:
        """Diagnostic method — try to resolve and fetch the link, return
        a structured result with all the info. Used by /test_link command.

        Returns a dict with keys:
          parsed: dict — what was parsed from the URL
          steps: list[str] — step-by-step diagnostic log
          success: bool — whether fetch succeeded
          error: str | None — top-level error message if any
          has_media: bool — whether the message has downloadable media
          media_type: str | None — 'photo' | 'video' | 'document' | etc.
          chat_title: str | None — resolved chat title (if entity was resolved)
        """
        result = {
            "parsed": {
                "kind": parsed.kind,
                "chat_ref": parsed.chat_ref,
                "message_id": parsed.message_id,
            },
            "steps": [],
            "success": False,
            "error": None,
            "has_media": False,
            "media_type": None,
            "chat_title": None,
        }

        fetched, diag = await self.fetch_message(parsed)
        result["steps"] = diag

        if fetched is None:
            result["error"] = "Fetch failed — see steps above for the cause."
            return result

        msg = fetched["message"]
        result["success"] = True
        result["has_media"] = bool(getattr(msg, "media", None))

        # Determine media type
        from telethon.tl import types as tl_types
        if isinstance(msg.media, tl_types.MessageMediaPhoto):
            result["media_type"] = "photo"
        elif isinstance(msg.media, tl_types.MessageMediaDocument):
            doc = msg.media.document
            if doc and doc.mime_type:
                if doc.mime_type.startswith("video/"):
                    result["media_type"] = "video"
                elif doc.mime_type.startswith("audio/"):
                    result["media_type"] = "audio"
                else:
                    result["media_type"] = "document"
            else:
                result["media_type"] = "document"
        elif isinstance(msg.media, tl_types.MessageMediaWebPage):
            result["media_type"] = "web_page"
        else:
            result["media_type"] = type(msg.media).__name__ if msg.media else None

        # Try to get chat title (best effort)
        try:
            entity = await self.client.get_entity(int(fetched["chat_id"]))
            result["chat_title"] = getattr(entity, "title", None)
        except Exception:
            pass

        return result

    # ---------- direct send to destination (NEW — fast path) ----------

    async def send_to_destination(
        self,
        source_chat_id: int,
        source_message_ids: list[int],
        dest_chat_id: int,
        topic_id: int | None = None,
        progress_callback=None,
        custom_caption: str | None = None,
        pre_messages: list | None = None,
        source_entity=None,
        dest_entity=None,
        strip_captions: bool = False,
        strip_links: bool = False,
    ) -> tuple[bool, list[str]]:
        """Send message(s) from a source chat to the destination chat using
        the user account (Telethon). This is the fast path that avoids
        downloading media to disk and re-uploading via Bot API.

        OPTIMIZED: `pre_messages`, `source_entity` and `dest_entity` let
        callers that ALREADY hold the Message objects / entities (e.g. the
        channel scraper, which just iterated them) skip 2 entity lookups +
        1 get_messages RPC per message — a ~3x reduction in API calls, which
        matters both for the 0.1-CPU free instance and for staying under
        Telegram's flood limits.

        Strategy:
          1. Try `user_client.forward_messages(dest, source_msg_ids, source_chat)`
             — this is the TRUE Telegram forward, fastest, preserves original
             sender info, and works for albums natively.
          2. If forward_messages fails (because the source has noforwards
             restriction), fall back to `user_client.send_message(dest,
             file=msg.media, formatting_entities=msg.entities)` for each
             message — this re-uploads from Telegram's servers WITHOUT
             downloading to disk. For protected albums, items are sent
             individually (loses album grouping in destination, but works).
          3. If (2) also fails (fully-protected content), download to
             disk + send_file(file=path) — uploads as a brand-new file with
             no link to the protected source. We PRESERVE the original
             document attributes (DocumentAttributeVideo, duration, dims,
             supports_streaming) and mime_type so the file is sent as a
             PLAYABLE VIDEO (not a generic document).

        Args:
          progress_callback: optional callable(sent_bytes, total_bytes) called
            during downloads and uploads so the caller can show progress.
            Currently used by the third (download+upload) path only.
          pre_messages: optional list of already-fetched Telethon Message
            objects (skips the get_messages RPC).
          source_entity / dest_entity: optional pre-resolved entities
            (skip the get_entity RPCs).

        Requirements:
          - The user account must be a member of BOTH source and destination
          - For topics, pass topic_id (the topic's top message ID)

        Returns:
          (success: bool, diagnostics_log: list[str])
        """
        diag: list[str] = []
        diag.append(f"send_to_destination: {len(source_message_ids)} message(s) "
                    f"from {source_chat_id} to {dest_chat_id}"
                    f"{f' topic {topic_id}' if topic_id else ''}")

        # Helper: compute the effective caption based on the custom_caption
        # policy AND the /strip settings:
        # - custom_caption=None: original caption, then optionally cleaned
        #     by strip_captions (media captions removed) / strip_links
        #     (URLs deleted from whatever text survives)
        # - custom_caption="" (empty string): strip ALL captions
        # - custom_caption="<text>": use this string for the first item only
        #   (albums only show the first item's caption in Telegram)
        def _caption_for(msg, item_index: int = 0) -> str:
            """Return the caption to send for this message."""
            if custom_caption is not None:
                if custom_caption == "":
                    # Strip captions entirely
                    return ""
                # Use custom caption, but only on the first item of an album
                return custom_caption if item_index == 0 else ""
            text = msg.message or ""
            if item_index > 0:
                # Album non-first items never carry text in a re-sent album
                # (Telegram shows only the first item's caption)
                return ""
            if strip_captions and msg.media:
                # /strip captions — media posts lose their caption entirely.
                # Text-only posts are NOT affected: the text IS the content.
                return ""
            if strip_links and text:
                new_text, _ents, _chg = strip_links_from_text(text, msg.entities)
                return new_text
            return text

        def _formatting_entities_for(msg, item_index: int = 0):
            """Return formatting entities to preserve. Custom captions lose
            entities entirely; link-stripped text keeps its surviving
            entities (offsets already shifted by strip_links_from_text)."""
            if custom_caption is not None:
                return None
            if item_index > 0:
                return None
            text = msg.message or ""
            if strip_captions and msg.media:
                return None
            if strip_links and text:
                _t, ents, changed = strip_links_from_text(text, msg.entities)
                return ents if changed else msg.entities
            return msg.entities

        try:
            if source_entity is None:
                source_entity = await self._get_entity_with_warmup(source_chat_id)
            if dest_entity is None:
                dest_entity = await self._get_entity_with_warmup(dest_chat_id)
            diag.append(f"✓ Resolved source ({type(source_entity).__name__}) and "
                        f"destination ({type(dest_entity).__name__})")
        except Exception as e:
            diag.append(f"✗ Failed to resolve entities: {type(e).__name__}: {e}")
            return False, diag

        # Fetch the messages first so we have them ready (also confirms access).
        # Skip the RPC entirely when the caller already passed the Message
        # objects (scraper fast path).
        if pre_messages:
            messages = [m for m in pre_messages if m is not None]
            if not messages:
                diag.append("✗ pre_messages was empty after filtering")
                return False, diag
            diag.append(f"✓ Using {len(messages)} pre-fetched message(s) (skipped get_messages)")
        else:
            try:
                messages = await self.client.get_messages(source_entity, ids=source_message_ids)
                # Telethon returns None for missing messages; filter those out
                if isinstance(messages, list):
                    messages = [m for m in messages if m is not None]
                else:
                    messages = [messages] if messages else []
                if not messages:
                    diag.append(f"✗ No messages found with IDs {source_message_ids}")
                    return False, diag
                diag.append(f"✓ Fetched {len(messages)} message(s) from source")
            except Exception as e:
                diag.append(f"✗ Failed to fetch messages: {type(e).__name__}: {e}")
                return False, diag

        # ---- Step 1: try true forward (works for non-protected content) ----
        # Skip this step when the text of ANY message would change under the
        # caption/link settings — forward_messages copies the source text
        # verbatim and cannot be edited afterwards, so caption control
        # (custom_caption, strip_captions, strip_links) needs the re-send
        # path below (which DOES let us set the caption).
        def _text_would_change(msg, item_index: int) -> bool:
            if item_index > 0:
                # Album non-first items carry no text after a re-send either
                # way — they can't force the slow path on their own.
                return False
            if custom_caption is not None:
                return (msg.message or "") != (""
                                                if custom_caption == ""
                                                else custom_caption)
            text = msg.message or ""
            if strip_captions and msg.media and text:
                return True
            if strip_links and text and text_has_links(text, msg.entities):
                return True
            return False

        if not any(_text_would_change(m, i) for i, m in enumerate(messages)):
            try:
                # Build reply_to for topics if specified
                kwargs: dict = {}
                if topic_id:
                    from telethon.tl.types import MessageReplyHeader
                    kwargs["reply_to"] = MessageReplyHeader(
                        reply_to_top_id=topic_id,
                        reply_to_msg_id=topic_id,
                    )

                await self.client.forward_messages(
                    dest_entity,
                    [m.id for m in messages],
                    source_entity,
                )
                diag.append(f"✓ True forward succeeded — {len(messages)} message(s) "
                            f"forwarded to destination"
                            f"{f' (topic {topic_id})' if topic_id else ''}")
                return True, diag
            except Exception as forward_err:
                diag.append(f"⚠ True forward failed: {type(forward_err).__name__}: {forward_err}")
                diag.append("  → Falling back to copy-and-resend (for protected content)")
        else:
            diag.append("ℹ Skipping true forward (caption/link settings modify "
                        "the text — using the re-send path)")

        # ---- Step 2: fallback — re-upload via send_message(file=msg.media) ----
        # This bypasses the noforwards restriction by re-uploading from
        # Telegram's servers directly (no disk download).
        sent_count = 0
        skipped_no_text = 0
        last_error = None
        for i, msg in enumerate(messages):
            # A text-only message whose text vanished entirely after link
            # stripping has nothing left to forward — count it as skipped
            # instead of trying to send an empty message (Telegram would
            # reject it anyway).
            if not msg.media and not (_caption_for(msg, i) or "").strip():
                skipped_no_text += 1
                diag.append(f"↷ Message {i+1}/{len(messages)} skipped — "
                            f"text-only and empty after link stripping")
                continue
            try:
                send_kwargs = dict(
                    message=_caption_for(msg, i),
                    file=msg.media,
                    formatting_entities=_formatting_entities_for(msg, i),
                    link_preview=False,
                )
                if topic_id:
                    from telethon.tl.types import MessageReplyHeader
                    send_kwargs["reply_to"] = MessageReplyHeader(
                        reply_to_top_id=topic_id,
                        reply_to_msg_id=topic_id,
                    )
                # For multi-item albums, only the first item gets a caption
                # (already handled by _caption_for which returns "" for i>0
                # when custom_caption is set; for None it returns "" for i>0
                # since the original captions of album items >0 don't make
                # sense to repeat — only the first item's caption matters in
                # a Telegram album).
                if i > 0 and custom_caption is None:
                    send_kwargs["message"] = ""

                await self.client.send_message(dest_entity, **send_kwargs)
                sent_count += 1
                diag.append(f"✓ Re-sent message {i+1}/{len(messages)} "
                            f"(type: {type(msg.media).__name__ if msg.media else 'text'})")
            except Exception as e:
                last_error = e
                diag.append(f"✗ Failed to send message {i+1}/{len(messages)} "
                            f"via send_message(file=msg.media): "
                            f"{type(e).__name__}: {e}")

        if sent_count == len(messages):
            diag.append(f"✓ All {sent_count} message(s) re-sent to destination")
            return True, diag
        elif sent_count > 0:
            diag.append(f"⚠ Partial success: {sent_count}/{len(messages)} sent")
            return True, diag
        if skipped_no_text == len(messages):
            diag.append("✓ Nothing to send — every message was text-only and "
                        "empty after link stripping")
            return True, diag

        # ---- Step 3: third fallback — download to disk + send_file ----
        # The "send_message(file=msg.media)" path fails with
        # ChatForwardsRestrictedError because Telethon detects that the file
        # object references an existing message and treats it as a forward.
        # The only reliable way to send protected content is to:
        #   1. Download the media bytes to disk (Telethon allows this — you
        #      have view access as a member)
        #   2. Upload as a brand new file via send_file(file=path) — no link
        #      to the protected source, Telegram can't tell it's a "forward"
        #   3. PRESERVE the original document attributes (DocumentAttributeVideo
        #      with duration, dimensions) and mime_type so the file is sent
        #      as a PLAYABLE VIDEO (not a generic document). Pass
        #      supports_streaming=True to send_file for videos.
        diag.append("⚠ Falling back to download-to-disk + send_file (third path)")
        diag.append("  → This is slower but works for fully protected content")

        import tempfile
        import shutil
        from telethon.tl import types as tl_types
        tmp_dir = tempfile.mkdtemp(prefix="forwarder_protected_")
        try:
            sent_count = 0
            for i, msg in enumerate(messages):
                if not msg.media:
                    # Text-only message — just send the text (unless link
                    # stripping left it completely empty)
                    if not (_caption_for(msg, i) or "").strip():
                        skipped_no_text += 1
                        diag.append(f"↷ Message {i+1}/{len(messages)} skipped — "
                                    f"text-only and empty after link stripping")
                        continue
                    try:
                        send_kwargs = dict(
                            message=_caption_for(msg, i),
                            link_preview=False,
                        )
                        if topic_id:
                            from telethon.tl.types import MessageReplyHeader
                            send_kwargs["reply_to"] = MessageReplyHeader(
                                reply_to_top_id=topic_id,
                                reply_to_msg_id=topic_id,
                            )
                        # For albums, only first item has caption (already
                        # handled by _caption_for returning "" for i>0 when
                        # custom_caption is set; for None, the original caption
                        # of subsequent items would be repetitive in an album,
                        # so we strip them too).
                        if i > 0 and custom_caption is None:
                            send_kwargs["message"] = ""
                        await self.client.send_message(dest_entity, **send_kwargs)
                        sent_count += 1
                        diag.append(f"✓ Sent text-only message {i+1}/{len(messages)}")
                    except Exception as e:
                        diag.append(f"✗ Failed to send text message {i+1}: {type(e).__name__}: {e}")
                    continue

                # ----- Build the progress callback for this iteration -----
                # IMPORTANT: Python closure bug — `i` and `phase` would all
                # resolve to the last loop iteration's value if we just used
                # them in a closure. We bind them as default args to make
                # each callback capture its own values.
                #
                # Also: Telethon's progress_callback can be either sync OR
                # async — Telethon awaits it via _maybe_await. So we can use
                # async def. But we keep it sync and call the outer
                # progress_callback (which is async) — _maybe_await will
                # await the returned coroutine.
                def make_progress_cb(item_index: int, total_items: int,
                                      phase: str, filename: str):
                    """Build a progress callback bound to the given iteration.
                    `phase` is 'Downloading' or 'Uploading'."""
                    def _cb(sent_bytes: int, total_bytes: int):
                        if progress_callback:
                            label = f"{phase} {filename} ({item_index+1}/{total_items})"
                            # progress_callback is async — _maybe_await will
                            # await the returned coroutine
                            return progress_callback(sent_bytes, total_bytes, label)
                    return _cb

                # ----- Helper: download thumbnail (if available) -----
                async def _download_thumbnail(msg_media, thumb_dir: str, idx: int):
                    """Download the thumbnail for a video/document as a JPEG.
                    Returns the path to the .jpg file, or None if no thumb.

                    Telegram documents have a `thumbs` list (PhotoSize
                    objects). The largest is typically a small JPEG used as
                    the video poster / preview. We download it via Telethon's
                    `download_media(msg, file=bytes, thumb=-1)` which uses
                    Telethon's _get_thumb to pick the largest size.
                    """
                    try:
                        thumb_bytes = await self.client.download_media(
                            msg_media, file=bytes, thumb=-1,
                        )
                        if not thumb_bytes:
                            return None
                        thumb_path = os.path.join(thumb_dir, f"thumb_{idx}.jpg")
                        with open(thumb_path, "wb") as f:
                            f.write(thumb_bytes)
                        return thumb_path
                    except Exception as e:
                        diag.append(f"  • (thumbnail download skipped: {type(e).__name__})")
                        return None

                # ----- Photos: MessageMediaPhoto -----
                if isinstance(msg.media, tl_types.MessageMediaPhoto):
                    out_path = os.path.join(tmp_dir, f"photo_{i+1}_{int(time.time())}.jpg")
                    dl_cb = make_progress_cb(i, len(messages), "Downloading", f"photo_{i+1}.jpg")
                    try:
                        result = await self.client.download_media(
                            msg, file=out_path, progress_callback=dl_cb,
                        )
                        if not result:
                            diag.append(f"✗ Could not download photo for message {i+1}")
                            continue
                        if isinstance(result, bytes):
                            with open(out_path, "wb") as f:
                                f.write(result)
                        else:
                            out_path = str(result)
                        sz = os.path.getsize(out_path)
                        diag.append(f"  • Downloaded photo_{i+1}.jpg ({sz/1024:.1f} KB)")
                    except Exception as e:
                        diag.append(f"✗ Failed to download photo {i+1}: {type(e).__name__}: {e}")
                        continue
                    # Send as photo — force_document=False lets Telethon treat
                    # the .jpg file as a photo (InputMediaUploadedPhoto)
                    try:
                        send_kwargs = dict(
                            file=out_path,
                            caption=_caption_for(msg, i),
                            formatting_entities=_formatting_entities_for(msg, i),
                            force_document=False,
                        )
                        if topic_id:
                            from telethon.tl.types import MessageReplyHeader
                            send_kwargs["reply_to"] = MessageReplyHeader(
                                reply_to_top_id=topic_id,
                                reply_to_msg_id=topic_id,
                            )
                        ul_cb = make_progress_cb(i, len(messages), "Uploading", f"photo_{i+1}.jpg")
                        await self.client.send_file(
                            dest_entity, progress_callback=ul_cb, **send_kwargs,
                        )
                        sent_count += 1
                        diag.append(f"✓ Sent re-uploaded photo {i+1}/{len(messages)}")
                    except Exception as e:
                        diag.append(f"✗ Failed to send photo {i+1}: {type(e).__name__}: {e}")
                        last_error = e
                    continue

                # ----- Documents (video, audio, animation, generic document) -----
                if isinstance(msg.media, tl_types.MessageMediaDocument):
                    doc = msg.media.document
                    if not doc:
                        diag.append(f"✗ No document in message {i+1}")
                        continue

                    # Extract original mime_type
                    original_mime = doc.mime_type or "application/octet-stream"

                    # Determine media type from mime + attributes
                    is_video = original_mime.startswith("video/")
                    is_audio = original_mime.startswith("audio/")
                    is_animation = any(isinstance(a, tl_types.DocumentAttributeAnimated)
                                       for a in (doc.attributes or []))
                    is_image_doc = original_mime.startswith("image/")

                    # Find original filename
                    original_filename = None
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl_types.DocumentAttributeFilename):
                            original_filename = attr.file_name
                            break
                    if not original_filename:
                        # Generate based on mime type
                        ext_map = {
                            "video/mp4": "mp4", "video/quicktime": "mov",
                            "video/x-matroska": "mkv",
                            "audio/mpeg": "mp3", "audio/ogg": "ogg",
                            "audio/x-wav": "wav",
                            "image/jpeg": "jpg", "image/png": "png",
                        }
                        ext = ext_map.get(original_mime, "bin")
                        original_filename = f"media_{i+1}.{ext}"

                    # Build the attributes list to pass to send_file.
                    #
                    # KEY INSIGHT: We must NOT pass DocumentAttributeFilename
                    # because Telethon's get_attributes() regenerates it from
                    # the local file path. If we pass both, Telethon may
                    # override the regenerated one with our (potentially
                    # inconsistent) one — and that's actually fine.
                    # But we MUST keep DocumentAttributeVideo (with duration,
                    # w, h, supports_streaming) and DocumentAttributeAudio
                    # so Telegram knows it's a video/audio.
                    #
                    # We filter out DocumentAttributeFilename to avoid the
                    # conflict; Telethon will use the local file's basename.
                    original_attributes = [
                        attr for attr in (doc.attributes or [])
                        if not isinstance(attr, tl_types.DocumentAttributeFilename)
                    ]

                    # Decide force_document — True for generic files (pdf, zip)
                    force_document = not (is_video or is_audio or
                                          is_animation or is_image_doc)

                    # Download to disk with LARGER CHUNK SIZE for speed.
                    # Telethon's auto-picker uses 128KB for <100MB files,
                    # which is 4x smaller than the 512KB max. Each chunk is a
                    # separate network round-trip, so 4x smaller = 4x slower.
                    # We bypass the auto-picker by calling _download_file with
                    # part_size_kb=512 (4x speedup).
                    out_path = os.path.join(tmp_dir, original_filename)
                    dl_cb = make_progress_cb(i, len(messages), "Downloading", original_filename)
                    try:
                        # Use Telethon's _download_file directly with a large
                        # chunk size for faster download. The InputDocumentFileLocation
                        # is what Telethon would use internally for documents.
                        from telethon.tl.types import InputDocumentFileLocation
                        thumb_size_type = ""  # main file, not a thumb
                        file_location = InputDocumentFileLocation(
                            id=doc.id,
                            access_hash=doc.access_hash,
                            file_reference=doc.file_reference,
                            thumb_size=thumb_size_type,
                        )
                        await self.client._download_file(
                            file_location,
                            out_path,
                            part_size_kb=512,  # 4x larger than auto-picked 128KB
                            file_size=doc.size,
                            progress_callback=dl_cb,
                        )
                        sz = os.path.getsize(out_path)
                        diag.append(f"  • Downloaded {original_filename} "
                                    f"({sz/1024/1024:.1f} MB, mime={original_mime})")
                    except Exception as e:
                        diag.append(f"✗ Failed to download media {i+1}: "
                                    f"{type(e).__name__}: {e}")
                        # Fallback: try the regular download_media
                        try:
                            diag.append(f"  → Retrying with regular download_media()...")
                            result = await self.client.download_media(
                                msg, file=out_path, progress_callback=dl_cb,
                            )
                            if not result:
                                continue
                            sz = os.path.getsize(out_path)
                            diag.append(f"  • Downloaded (fallback) {original_filename} "
                                        f"({sz/1024/1024:.1f} MB)")
                        except Exception as e2:
                            diag.append(f"✗ Fallback download also failed: {type(e2).__name__}: {e2}")
                            continue

                    # Download the thumbnail (for videos). The thumb is a small
                    # JPEG that Telegram shows as the video poster before play.
                    # Without it, the destination video has no preview/thumbnail.
                    thumb_path = None
                    if is_video and getattr(doc, "thumbs", None):
                        thumb_path = await _download_thumbnail(msg, tmp_dir, i)
                        if thumb_path:
                            try:
                                tsize = os.path.getsize(thumb_path)
                                diag.append(f"  • Downloaded thumbnail ({tsize/1024:.1f} KB)")
                            except OSError:
                                pass

                    # Send the file with preserved attributes + thumbnail +
                    # supports_streaming. We PRE-UPLOAD the file with a large
                    # chunk size (512KB max) for 4x speedup vs Telethon's
                    # auto-picker, then pass the resulting InputFile to send_file.
                    #
                    # For files >10MB, we use a PARALLEL uploader that splits
                    # the file into chunks and uploads them concurrently. This
                    # gives ~4x speedup for large files (the sequential
                    # bottleneck was each chunk waiting for the previous
                    # chunk's response before sending).
                    try:
                        ul_cb = make_progress_cb(i, len(messages), "Uploading", original_filename)
                        file_size_bytes = os.path.getsize(out_path)

                        if file_size_bytes > 10 * 1024 * 1024:
                            # Large file — use parallel upload (4 concurrent chunks)
                            diag.append(f"  • Using parallel upload (4 chunks) for {file_size_bytes/1024/1024:.1f} MB file")
                            file_handle = await self._parallel_upload_file(
                                out_path,
                                file_size_bytes,
                                part_size_kb=512,
                                parallel=4,
                                progress_callback=ul_cb,
                            )
                        else:
                            # Small file — sequential upload is fast enough
                            file_handle = await self.client.upload_file(
                                out_path,
                                part_size_kb=512,
                                file_size=file_size_bytes,
                                progress_callback=ul_cb,
                            )

                        send_kwargs = dict(
                            file=file_handle,  # already-uploaded InputFile — send_file skips re-upload
                            caption=_caption_for(msg, i),
                            formatting_entities=_formatting_entities_for(msg, i),
                            # Pass the original attributes (minus Filename)
                            # so Telegram sees the video duration/dims.
                            attributes=original_attributes,
                            mime_type=original_mime,
                            force_document=force_document,
                        )
                        # KEY FIX: Pass the downloaded thumbnail so the
                        # destination video shows a poster/preview image.
                        if thumb_path:
                            send_kwargs["thumb"] = thumb_path
                        # KEY FIX: Pass supports_streaming=True for videos
                        # so Telegram shows it as a streamable video.
                        if is_video:
                            send_kwargs["supports_streaming"] = True
                        # voice_note for voice messages
                        if is_audio:
                            for attr in (doc.attributes or []):
                                if isinstance(attr, tl_types.DocumentAttributeAudio) and getattr(attr, "voice", False):
                                    send_kwargs["voice_note"] = True
                                    break

                        if topic_id:
                            from telethon.tl.types import MessageReplyHeader
                            send_kwargs["reply_to"] = MessageReplyHeader(
                                reply_to_top_id=topic_id,
                                reply_to_msg_id=topic_id,
                            )
                        await self.client.send_file(dest_entity, **send_kwargs)
                        sent_count += 1
                        if is_video:
                            thumb_msg = " with thumbnail" if thumb_path else " (no thumbnail)"
                            diag.append(f"✓ Sent re-uploaded video {i+1}/{len(messages)} "
                                        f"(playable, streaming{thumb_msg}, "
                                        f"duration/dims preserved)")
                        elif is_audio:
                            diag.append(f"✓ Sent re-uploaded audio {i+1}/{len(messages)}")
                        elif is_animation:
                            diag.append(f"✓ Sent re-uploaded animation {i+1}/{len(messages)}")
                        else:
                            diag.append(f"✓ Sent re-uploaded document {i+1}/{len(messages)}")
                    except Exception as e:
                        diag.append(f"✗ Failed to send re-uploaded media {i+1}: "
                                    f"{type(e).__name__}: {e}")
                        last_error = e
        finally:
            # Cleanup tmp dir
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

        if sent_count == len(messages):
            diag.append(f"✓ All {sent_count} message(s) re-uploaded (third path)")
            return True, diag
        elif sent_count > 0:
            diag.append(f"⚠ Partial success (third path): {sent_count}/{len(messages)} sent")
            return True, diag
        else:
            diag.append(f"✗ All sends failed. Last error: {last_error}")
            return False, diag

    # ---------- parallel upload (for large files) ----------

    async def _parallel_upload_file(
        self,
        file_path: str,
        file_size: int,
        part_size_kb: int = 512,
        parallel: int = 4,
        progress_callback=None,
    ):
        """Upload a file in PARALLEL chunks for faster throughput.

        This replaces Telethon's sequential upload_file for large files.
        The sequential approach sends one 512KB chunk, waits for the
        response, then sends the next. For a 50MB file that's 100 chunks
        × ~200ms round-trip = ~20 seconds.

        This parallel approach splits the file into chunks and uploads
        `parallel` chunks concurrently. For 4x parallel, a 50MB file
        takes ~5 seconds instead of 20.

        Telegram's SaveBigFilePartRequest is stateless — each part is
        stored independently, associated by file_id. So parallel uploads
        work: we just need to use the same file_id for all parts and
        different file_part indices.

        Args:
          file_path: Path to the file to upload.
          file_size: Size of the file in bytes.
          part_size_kb: Chunk size in KB (max 512, Telethon's limit).
          parallel: Number of concurrent chunk uploads (default 4).
          progress_callback: Sync callable(sent, total) — called after
            each chunk is uploaded.

        Returns:
          InputFileBig (for files >10MB) or InputFile (for smaller) —
          ready to pass to send_file.
        """
        import asyncio as _asyncio
        import hashlib
        from telethon.tl.functions.upload import (
            SaveBigFilePartRequest, SaveFilePartRequest,
        )
        from telethon.tl.types import InputFile, InputFileBig
        from telethon import helpers

        part_size = part_size_kb * 1024
        part_count = (file_size + part_size - 1) // part_size
        file_id = helpers.generate_random_long()
        file_name = os.path.basename(file_path)

        is_big = file_size > 10 * 1024 * 1024
        logger.info("Parallel upload: %s (%d bytes, %d chunks, %d parallel)",
                    file_name, file_size, part_count, parallel)

        # Track progress across parallel tasks
        bytes_uploaded = [0]  # mutable container for closure
        progress_lock = _asyncio.Lock()

        async def upload_one_part(part_index: int):
            """Read and upload a single chunk of the file."""
            offset = part_index * part_size
            # Read this specific part from disk
            with open(file_path, "rb") as f:
                f.seek(offset)
                data = f.read(part_size)

            # Use the correct request type based on file size
            if is_big:
                request = SaveBigFilePartRequest(
                    file_id, part_index, part_count, data,
                )
            else:
                request = SaveFilePartRequest(
                    file_id, part_index, data,
                )

            result = await self.client(request)
            if not result:
                raise RuntimeError(
                    f"Telegram rejected part {part_index}/{part_count}"
                )

            # Update progress (thread-safe via lock)
            async with progress_lock:
                bytes_uploaded[0] += len(data)
                if progress_callback:
                    try:
                        r = progress_callback(bytes_uploaded[0], file_size)
                        if inspect.isawaitable(r):
                            await r
                    except Exception:
                        pass

        # Use a semaphore to limit concurrency
        sem = _asyncio.Semaphore(parallel)

        async def bounded_upload(part_index: int):
            async with sem:
                return await upload_one_part(part_index)

        # Launch all upload tasks
        tasks = [_asyncio.create_task(bounded_upload(i)) for i in range(part_count)]
        results = await _asyncio.gather(*tasks, return_exceptions=True)

        # Check for errors
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                # Cancel any remaining tasks (shouldn't be any since gather waits)
                raise RuntimeError(
                    f"Parallel upload failed at part {i}/{part_count}: "
                    f"{type(r).__name__}: {r}"
                )

        # Build the InputFile / InputFileBig to pass to send_file
        if is_big:
            return InputFileBig(id=file_id, parts=part_count, name=file_name)
        else:
            # For small files, compute MD5 (needed for InputFile dedup)
            md5 = hashlib.md5()
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    md5.update(chunk)
            return InputFile(
                id=file_id,
                parts=part_count,
                name=file_name,
                md5_digest=md5.digest(),
            )


    # ---------- legacy: download media to disk (fallback path) ----------

    async def download_message_media(
        self,
        source_chat_id: int,
        source_message_ids: list[int],
        tmp_dir: str,
        max_bytes: int = 50 * 1024 * 1024,
    ) -> tuple[list[dict], str | None, str | None, list[str]]:
        """Legacy fallback: download media from source chat to disk for
        re-upload via Bot API. Used when send_to_destination fails (e.g.,
        user account is not a member of the destination).

        Returns:
          (media_paths, caption, text_only, diagnostics_log)

        media_paths is a list of {'path': str, 'type': str}
        caption is the original caption
        text_only is set if no media was downloaded (text-only message)
        """
        diag: list[str] = []
        media_paths: list[dict] = []
        caption: str | None = None
        text_only: str | None = None

        try:
            source_entity = await self.client.get_entity(source_chat_id)
        except Exception as e:
            diag.append(f"✗ Failed to resolve source entity: {type(e).__name__}: {e}")
            return [], None, None, diag

        messages = await self.client.get_messages(source_entity, ids=source_message_ids)
        if isinstance(messages, list):
            messages = [m for m in messages if m is not None]
        else:
            messages = [messages] if messages else []

        if not messages:
            diag.append(f"✗ No messages found with IDs {source_message_ids}")
            return [], None, None, diag

        from telethon.tl import types as tl

        for idx, msg in enumerate(messages):
            if idx == 0:
                caption = msg.message

            if not msg.media:
                if msg.message and not media_paths:
                    text_only = msg.message
                continue

            # Skip non-downloadable media types
            if isinstance(msg.media, (tl.MessageMediaWebPage, tl.MessageMediaContact,
                                       tl.MessageMediaGeo, tl.MessageMediaVenue,
                                       tl.MessageMediaGame, tl.MessageMediaPoll,
                                       tl.MessageMediaUnsupported)):
                continue

            # Determine type
            media_type = "document"
            if isinstance(msg.media, tl.MessageMediaPhoto):
                media_type = "photo"
            elif isinstance(msg.media, tl.MessageMediaDocument):
                doc = msg.media.document
                if doc and doc.mime_type:
                    mt = doc.mime_type
                    if mt.startswith("video/"):
                        for attr in doc.attributes:
                            if isinstance(attr, tl.DocumentAttributeAnimated):
                                media_type = "animation"
                                break
                            if isinstance(attr, tl.DocumentAttributeVideo):
                                media_type = "video_note" if getattr(attr, "round_message", False) else "video"
                                break
                    elif mt.startswith("audio/"):
                        media_type = "voice" if "ogg" in mt else "audio"

            # Check size before download
            try:
                if isinstance(msg.media, tl.MessageMediaDocument) and msg.media.document:
                    sz = msg.media.document.size or 0
                    if sz > max_bytes:
                        diag.append(f"⚠ Skipping item {idx}: file too large "
                                    f"({sz/1024/1024:.1f} MB > {max_bytes/1024/1024:.0f} MB)")
                        continue
            except Exception:
                pass

            # Download
            out_path = os.path.join(tmp_dir, f"media_{idx}_{int(time.time())}")
            try:
                result = await self.client.download_media(msg, file=out_path)
                if not result:
                    continue
                if isinstance(result, bytes):
                    with open(out_path, "wb") as f:
                        f.write(result)
                else:
                    out_path = str(result)
                sz = os.path.getsize(out_path)
                if sz > max_bytes:
                    diag.append(f"⚠ Skipping item {idx}: downloaded {sz/1024/1024:.1f} MB "
                                f"(> {max_bytes/1024/1024:.0f} MB Bot API limit)")
                    os.remove(out_path)
                    continue
                media_paths.append({"path": out_path, "type": media_type})
                diag.append(f"✓ Downloaded item {idx} ({media_type}, {sz/1024:.1f} KB)")
            except Exception as e:
                diag.append(f"✗ Failed to download item {idx}: {type(e).__name__}: {e}")

        return media_paths, caption, text_only, diag

    # ---------- channel scraping ----------

    async def scrape_channel(
        self,
        source_chat_ref,
        dest_chat_id,
        topic_id: int | None = None,
        reverse: bool = False,
        min_id: int = 0,
        max_id: int = 0,
        cancel_event=None,
        progress_callback=None,
        status_callback=None,
        stats_callback=None,
        media_types: list[str] | None = None,
        parallel: int = 3,
        custom_caption: str | None = None,
    ) -> dict:
        """Iterate all messages in a channel and forward each media message
        to the destination chat. Used by the /scrape command.

        Args:
          source_chat_ref: chat_id (int) or username (str) of the source channel
          dest_chat_id: destination chat_id (int) or "me" for Saved Messages
          topic_id: optional topic thread (for forum destinations)
          reverse: if True, oldest first (default: newest first)
          min_id: skip messages with id <= min_id
          max_id: skip messages with id > max_id (or 0 for no upper bound)
          cancel_event: asyncio.Event — set to cancel scraping
          progress_callback: async callable(sent, total_seen, last_msg_id, label)
          status_callback: async callable(status_text) — for status updates
          media_types: list of types to include. None means all media.
            Valid values: 'photo', 'video', 'animation', 'document', 'audio',
            'voice'. If specified, only matching media is sent; others are
            skipped (counted in skipped_count).
          parallel: number of concurrent sends. Default 3 (safe for Telegram).
            Higher values risk FloodWait. Each task uses its own asyncio task.

        Returns:
          dict with keys: sent_count, failed_count, skipped_count,
                          total_seen, last_message_id, cancelled (bool),
                          flood_waits (int)

        Rate limiting:
          - Per-task 0.3 sec delay between sends (safe even with parallel=3)
          - On FloodWaitError, sleep for the requested seconds + 5s buffer
            and retry the message once

        Notes:
          - parallel sends are independent — they don't share rate-limit state.
            The actual bottleneck is Telegram's per-account rate limit, not
            our concurrency. Setting parallel=5+ risks FloodWait.
          - Each task holds its own network connection (no shared TCP overhead)
          - The order of sent messages in the destination may differ from the
            source order when parallel > 1 (out-of-order arrival)
        """
        from telethon.errors import FloodWaitError
        import asyncio as _asyncio

        # Normalize media_types: lowercase, validate, default to None (all)
        if media_types is not None:
            media_types = [t.lower() for t in media_types]
            valid = {"photo", "video", "animation", "document", "audio", "voice"}
            invalid = set(media_types) - valid
            if invalid:
                if status_callback:
                    await status_callback(f"❌ Invalid media types: {invalid}. "
                                          f"Valid: {valid}")
                return {
                    "sent_count": 0, "failed_count": -1, "skipped_count": 0,
                    "total_seen": 0, "last_message_id": 0,
                    "cancelled": False, "flood_waits": 0,
                }

        result = {
            "sent_count": 0,
            "failed_count": 0,
            "skipped_count": 0,  # text-only or no-media messages
            "total_seen": 0,
            "last_message_id": 0,
            "cancelled": False,
            "flood_waits": 0,
        }

        try:
            source_entity = await self._get_entity_with_warmup(source_chat_ref)
            dest_entity = await self._get_entity_with_warmup(dest_chat_id)
        except Exception as e:
            if status_callback:
                await status_callback(f"❌ Failed to resolve entities: {type(e).__name__}: {e}")
            result["failed_count"] = -1  # signal error
            return result

        if status_callback:
            await status_callback(
                f"🔍 Scraping channel: {getattr(source_entity, 'title', source_chat_ref)}\n"
                f"   → destination: {getattr(dest_entity, 'title', dest_chat_id)}"
                f"{f' (topic {topic_id})' if topic_id else ''}\n"
                f"   order: {'oldest first' if reverse else 'newest first'}"
            )

        # Build kwargs for iter_messages
        iter_kwargs = {
            "reverse": reverse,
            "limit": None,  # iterate ALL messages (use async iterator)
        }
        if min_id > 0:
            iter_kwargs["min_id"] = min_id
        if max_id > 0:
            iter_kwargs["max_id"] = max_id

        # Statistics for status updates
        last_status_time = 0
        status_interval = 5.0  # update status every 5 seconds
        send_started = time.time()  # for the msg/s rate in progress updates

        # Helper: classify a message's media type
        def _classify_media(msg) -> str | None:
            """Return 'photo', 'video', 'animation', 'document', 'audio',
            'voice', or None (for non-media messages)."""
            if not msg.media:
                return None
            if isinstance(msg.media, tl.MessageMediaPhoto):
                return "photo"
            if isinstance(msg.media, tl.MessageMediaDocument):
                doc = msg.media.document
                if not doc or not doc.mime_type:
                    return "document"
                mt = doc.mime_type
                if mt.startswith("video/"):
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl.DocumentAttributeAnimated):
                            return "animation"
                        if isinstance(attr, tl.DocumentAttributeVideo):
                            # round_message=True means round video (video note)
                            return "video_note" if getattr(attr, "round_message", False) else "video"
                    return "video"
                if mt.startswith("audio/"):
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl.DocumentAttributeAudio) and getattr(attr, "voice", False):
                            return "voice"
                    return "audio" if "ogg" not in mt else "voice"
                return "document"
            # Non-downloadable types: web pages, contacts, geos, polls, etc.
            return None

        # Helper: send ONE BATCH (a single message, or all items of one
        # album) via send_to_destination, with FloodWait retry.
        #
        # OPTIMIZED vs the old per-message sender:
        #   * passes the pre-fetched Message objects + resolved entities, so
        #     each send skips 2 get_entity + 1 get_messages RPC
        #   * albums (consecutive grouped_id messages) are dispatched as ONE
        #     batch — the destination gets a real grouped album and we make
        #     1 API call instead of N (much friendlier to Telegram's flood
        #     limits and to the 0.1-CPU free instance).
        async def _send_batch(msgs: list):
            """Send a batch of message(s). Updates `result` in place.
            Returns True if sent."""
            ids = [m.id for m in msgs]
            kwargs = dict(
                source_chat_id=source_entity.id,
                source_message_ids=ids,
                dest_chat_id=dest_chat_id,
                topic_id=topic_id,
                progress_callback=None,
                custom_caption=custom_caption,
                pre_messages=msgs,
                source_entity=source_entity,
                dest_entity=dest_entity,
            )
            try:
                success, _ = await self.send_to_destination(**kwargs)
                if success:
                    result["sent_count"] += len(msgs)
                    return True
                result["failed_count"] += len(msgs)
                return False
            except FloodWaitError as e:
                result["flood_waits"] += 1
                wait_seconds = e.seconds + 5
                if status_callback:
                    await status_callback(
                        f"⏳ Flood wait: sleeping {wait_seconds}s before retrying...\n"
                        f"   Sent so far: {result['sent_count']}"
                    )
                await _asyncio.sleep(wait_seconds)
                # Retry once
                try:
                    success, _ = await self.send_to_destination(**kwargs)
                    if success:
                        result["sent_count"] += len(msgs)
                        return True
                except Exception:
                    pass
                result["failed_count"] += len(msgs)
                return False
            except Exception as e:
                logger.warning("scrape: failed to send msgs %s: %s", ids, e)
                result["failed_count"] += len(msgs)
                return False

        # Semaphore for parallel sends (limits concurrent in-flight sends)
        # to avoid hitting Telegram's rate limit. Default 3 = ~9 msgs/sec total.
        sem = _asyncio.Semaphore(parallel)
        pending_send_tasks: list = []  # asyncio tasks for in-flight sends

        async def _send_with_semaphore(msgs: list):
            """Wrap _send_batch with the semaphore. Awaits the send."""
            async with sem:
                # Check cancellation before sending
                if cancel_event and cancel_event.is_set():
                    return False
                await _send_batch(msgs)
                # Small delay between sends within a single task slot
                await _asyncio.sleep(0.3)

        # ---- album grouping state ----
        # iter_messages yields album items consecutively; buffer items that
        # share a grouped_id and dispatch them together when the group ends.
        album_buffer: list = []
        album_grouped_id = None

        def _flush_album():
            nonlocal album_buffer, album_grouped_id
            if album_buffer:
                batch, album_buffer = album_buffer, []
                album_grouped_id = None
                pending_send_tasks.append(
                    _asyncio.create_task(_send_with_semaphore(batch))
                )

        try:
            async for msg in self.client.iter_messages(source_entity, **iter_kwargs):
                # Check for cancellation
                if cancel_event and cancel_event.is_set():
                    result["cancelled"] = True
                    # Wait for in-flight sends to complete (don't abandon them)
                    if pending_send_tasks:
                        if status_callback:
                            await status_callback(
                                f"🛑 Cancel signal received. Waiting for "
                                f"{len(pending_send_tasks)} in-flight send(s) to complete..."
                            )
                        await _asyncio.gather(*pending_send_tasks, return_exceptions=True)
                    if status_callback:
                        await status_callback(
                            f"🛑 Scraping cancelled by user.\n"
                            f"   Sent: {result['sent_count']}, "
                            f"Failed: {result['failed_count']}, "
                            f"Skipped: {result['skipped_count']}"
                        )
                    break

                result["total_seen"] += 1
                result["last_message_id"] = msg.id

                # Classify the message's media type
                m_type = _classify_media(msg)

                # Skip if no media
                if m_type is None:
                    result["skipped_count"] += 1
                    continue

                # Apply user's media type filter
                if media_types is not None:
                    # Normalize: 'voice' and 'video_note' both count as their
                    # respective primary types. 'video_note' is treated as 'video'.
                    type_check = m_type
                    if type_check == "video_note":
                        type_check = "video"
                    if type_check not in media_types:
                        result["skipped_count"] += 1
                        continue

                # ---- dispatch with album grouping ----
                gid = getattr(msg, "grouped_id", None)
                if gid:
                    if album_grouped_id == gid:
                        # Same album as the previous item — keep buffering.
                        album_buffer.append(msg)
                    else:
                        # A new album started — flush the previous one first.
                        _flush_album()
                        album_grouped_id = gid
                        album_buffer = [msg]
                else:
                    # Standalone message — flush any buffered album, then
                    # schedule this message on its own.
                    _flush_album()
                    pending_send_tasks.append(
                        _asyncio.create_task(_send_with_semaphore([msg]))
                    )

                # Periodically drain completed tasks to avoid the list growing
                # unboundedly for huge channels.
                if len(pending_send_tasks) >= parallel * 4:
                    # Wait for at least one to complete before continuing.
                    # NOTE: asyncio.wait returns (done, pending) as SETS, not
                    # lists. We must convert back to a list to keep using
                    # .append() later. (Bug: was assigning the set directly,
                    # causing "AttributeError: 'set' object has no attribute
                    # 'append'" on the next iteration.)
                    done, pending = await _asyncio.wait(
                        pending_send_tasks, return_when=_asyncio.FIRST_COMPLETED,
                    )
                    # Drain all done tasks (their results were already applied
                    # via _send_one updating `result`)
                    for t in done:
                        try:
                            t.result()  # raise exceptions if any
                        except Exception:
                            pass
                    # Convert pending set back to list for .append() compatibility
                    pending_send_tasks = list(pending)

                # Progress callback
                if progress_callback:
                    try:
                        await progress_callback(
                            result["sent_count"],
                            result["total_seen"],
                            result["last_message_id"],
                            f"Sent {result['sent_count']} / seen {result['total_seen']}",
                        )
                    except Exception:
                        pass

                # Periodic status update — rich format (bar + rate).
                # Whole-channel runs do NOT know their total up front, so
                # this honestly shows counts + rate instead of a fake %.
                import time as _time
                now = _time.time()
                if status_callback and now - last_status_time > status_interval:
                    last_status_time = now
                    elapsed = now - send_started
                    rate = (result["sent_count"] / elapsed
                            if elapsed > 0.5 and result["sent_count"] else None)
                    await status_callback(format_scrape_progress(
                        title=(getattr(source_entity, "title", None)
                                or str(source_chat_ref)),
                        sent=result["sent_count"],
                        total=None,
                        failed=result["failed_count"],
                        skipped=result["skipped_count"],
                        flood_waits=result["flood_waits"],
                        rate=rate,
                        last_msg_id=result["last_message_id"],
                    ))

                # Send stats to stats_callback (for bot_data updates so
                # /scrape_status shows current progress, not stale data)
                if stats_callback:
                    try:
                        await stats_callback(result)
                    except Exception:
                        pass

        except Exception as e:
            logger.exception("scrape_channel: iter_messages failed")
            if status_callback:
                await status_callback(f"❌ Scrape error: {type(e).__name__}: {e}")
            result["failed_count"] = -1
            return result

        # Flush the final album (if the channel ends mid-album)
        _flush_album()

        # Wait for any remaining in-flight send tasks to complete
        if pending_send_tasks:
            try:
                await _asyncio.gather(*pending_send_tasks, return_exceptions=True)
            except Exception:
                pass

        # Final status
        if status_callback and not result["cancelled"]:
            await status_callback(
                f"✅ Scraping complete!\n\n"
                f"Total seen: {result['total_seen']}\n"
                f"Sent: {result['sent_count']}\n"
                f"Failed: {result['failed_count']}\n"
                f"Skipped (filtered/no media): {result['skipped_count']}\n"
                f"Flood waits: {result['flood_waits']}\n"
                f"Last msg ID: {result['last_message_id']}\n"
                f"Parallel sends: {parallel}"
            )

        return result

    # ---------- from-point scraping (post link + old/new) ----------

    async def scrape_from_point(
        self,
        source_chat_ref,
        anchor_message_id: int,
        direction: str,
        dest_chat_id,
        count: int = 50,
        topic_id: int | None = None,
        cancel_event=None,
        status_callback=None,
        stats_callback=None,
        media_types: list[str] | None = None,
        parallel: int = 3,
        custom_caption: str | None = None,
        strip_captions: bool = False,
        strip_links: bool = False,
    ) -> dict:
        """Scrape a channel/group FROM a specific post, in a specific
        direction — the "send a post link with a flag" flow.

        Direction:
          'old' — the anchor post plus the `count`-1 posts BEFORE it
                  (walking back in history). Fetched with
                  offset_id=anchor+1, then reversed so the destination
                  receives them chronologically (reads like the source).
          'new' — the anchor post plus the `count`-1 posts AFTER it
                  (walking toward the present). Fetched with
                  min_id=anchor-1, reverse=True (already chronological).

        Differences from scrape_channel (whole-channel):
          * bounded — exactly `count` messages max (default 50, clamp 500),
            not the entire history
          * media_types=None means EVERYTHING (text posts included — this is
            a point mirror, not a media dump); pass a type list to filter
          * the anchor post itself IS included in both directions

        Returns the same dict as scrape_channel, plus anchor_id, direction,
        fetched_count, first_sent_id and last_sent_id (so the status message
        can tell the user exactly which range was covered and where to
        continue from).
        """
        from telethon.errors import FloodWaitError
        import asyncio as _asyncio

        direction = (direction or "old").lower()
        if direction not in ("old", "new"):
            if status_callback:
                await status_callback(f"❌ Unknown direction {direction!r} — "
                                      "use 'old' or 'new'.")
            return {"sent_count": 0, "failed_count": -1, "skipped_count": 0,
                    "total_seen": 0, "last_message_id": 0, "cancelled": False,
                    "flood_waits": 0, "anchor_id": anchor_message_id,
                    "direction": direction, "fetched_count": 0,
                    "first_sent_id": 0, "last_sent_id": 0}
        count = max(1, min(int(count or 50), 500))

        result = {
            "sent_count": 0, "failed_count": 0, "skipped_count": 0,
            "total_seen": 0, "last_message_id": 0, "cancelled": False,
            "flood_waits": 0,
            "anchor_id": anchor_message_id, "direction": direction,
            "fetched_count": 0, "first_sent_id": 0, "last_sent_id": 0,
            "expected_total": 0,
        }

        # Normalize media filters (same rules as scrape_channel)
        if media_types is not None:
            media_types = [t.lower() for t in media_types]
            valid = {"photo", "video", "animation", "document", "audio", "voice"}
            invalid = set(media_types) - valid
            if invalid:
                if status_callback:
                    await status_callback(f"❌ Invalid media types: {invalid}")
                result["failed_count"] = -1
                return result

        try:
            source_entity = await self._get_entity_with_warmup(source_chat_ref)
            dest_entity = await self._get_entity_with_warmup(dest_chat_id)
        except Exception as e:
            if status_callback:
                await status_callback(
                    f"❌ Failed to resolve entities: {type(e).__name__}: {e}")
            result["failed_count"] = -1
            return result

        src_title = getattr(source_entity, "title", None) or str(source_chat_ref)
        if status_callback:
            arrow = "⬅ older posts" if direction == "old" else "➡ newer posts"
            await status_callback(
                f"🔍 Scraping “{src_title}” from post #{anchor_message_id} "
                f"({arrow}, up to {count} messages)…"
            )

        # ---- fetch phase ----
        try:
            if direction == "old":
                # Messages at/below the anchor, walking back, newest first.
                # offset_id is exclusive, so anchor+1 includes the anchor.
                it = self.client.iter_messages(
                    source_entity,
                    offset_id=anchor_message_id + 1,
                    limit=count,
                )
            else:
                kw = {"reverse": True, "limit": count}
                if anchor_message_id - 1 > 0:
                    # min_id is exclusive — anchor-1 includes the anchor
                    kw["min_id"] = anchor_message_id - 1
                it = self.client.iter_messages(source_entity, **kw)
            fetched: list = []
            async for m in it:
                if cancel_event and cancel_event.is_set():
                    break
                fetched.append(m)
        except Exception as e:
            logger.exception("scrape_from_point: fetch failed")
            if status_callback:
                await status_callback(f"❌ Fetch failed: {type(e).__name__}: {e}")
            result["failed_count"] = -1
            return result

        result["fetched_count"] = len(fetched)

        if not fetched:
            if status_callback:
                if direction == "new":
                    await status_callback(
                        f"✅ No messages at or after #{anchor_message_id} — "
                        f"the anchor is the newest post (or was deleted).")
                else:
                    await status_callback(
                        f"✅ No messages at or before #{anchor_message_id} — "
                        f"nothing to scrape in that direction.")
            return result

        # Chronological delivery order + album items sorted by id
        ordered = order_messages_for_delivery(fetched, direction)

        # The count is KNOWN up front for from-point runs — the progress
        # display can show a real bar + percentage + ETA (unlike
        # whole-channel scrapes, where the total is unknowable).
        result["expected_total"] = len(ordered)
        send_started = time.time()

        if status_callback:
            lo, hi = ordered[0].id, ordered[-1].id
            await status_callback(
                f"📥 Fetched {len(ordered)} message(s) "
                f"(#{lo} … #{hi}). Sending to destination…"
            )

        # ---- send phase (same dispatch pattern as scrape_channel) ----
        def _classify_media(msg) -> str | None:
            if not msg.media:
                return None
            if isinstance(msg.media, tl.MessageMediaPhoto):
                return "photo"
            if isinstance(msg.media, tl.MessageMediaDocument):
                doc = msg.media.document
                if not doc or not doc.mime_type:
                    return "document"
                mt = doc.mime_type
                if mt.startswith("video/"):
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl.DocumentAttributeAnimated):
                            return "animation"
                        if isinstance(attr, tl.DocumentAttributeVideo):
                            return ("video_note" if getattr(attr, "round_message", False)
                                    else "video")
                    return "video"
                if mt.startswith("audio/"):
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl.DocumentAttributeAudio) and getattr(attr, "voice", False):
                            return "voice"
                    return "audio" if "ogg" not in mt else "voice"
                return "document"
            return None   # web pages, contacts, geos, polls, ...

        async def _send_batch(msgs: list) -> bool:
            ids = [m.id for m in msgs]
            kwargs = dict(
                source_chat_id=source_entity.id,
                source_message_ids=ids,
                dest_chat_id=dest_chat_id,
                topic_id=topic_id,
                custom_caption=custom_caption,
                strip_captions=strip_captions,
                strip_links=strip_links,
                pre_messages=msgs,
                source_entity=source_entity,
                dest_entity=dest_entity,
            )
            try:
                success, _ = await self.send_to_destination(**kwargs)
                if success:
                    result["sent_count"] += len(msgs)
                    if not result["first_sent_id"]:
                        result["first_sent_id"] = min(ids)
                    result["last_sent_id"] = max(ids)
                    return True
                result["failed_count"] += len(msgs)
                return False
            except FloodWaitError as e:
                result["flood_waits"] += 1
                wait_seconds = e.seconds + 5
                if status_callback:
                    await status_callback(
                        f"⏳ Flood wait: sleeping {wait_seconds}s before retrying…\n"
                        f"   Sent so far: {result['sent_count']}")
                await _asyncio.sleep(wait_seconds)
                try:
                    success, _ = await self.send_to_destination(**kwargs)
                    if success:
                        result["sent_count"] += len(msgs)
                        if not result["first_sent_id"]:
                            result["first_sent_id"] = min(ids)
                        result["last_sent_id"] = max(ids)
                        return True
                except Exception:
                    pass
                result["failed_count"] += len(msgs)
                return False
            except Exception as e:
                logger.warning("scrape_from_point: failed to send msgs %s: %s",
                               ids, e)
                result["failed_count"] += len(msgs)
                return False

        sem = _asyncio.Semaphore(parallel)
        pending_send_tasks: list = []

        async def _send_with_semaphore(msgs: list):
            async with sem:
                if cancel_event and cancel_event.is_set():
                    return False
                await _send_batch(msgs)
                await _asyncio.sleep(0.3)

        album_buffer: list = []
        album_grouped_id = None

        def _flush_album():
            nonlocal album_buffer, album_grouped_id
            if album_buffer:
                batch, album_buffer = album_buffer, []
                album_grouped_id = None
                pending_send_tasks.append(
                    _asyncio.create_task(_send_with_semaphore(batch)))

        last_status_time = 0.0
        for msg in ordered:
            if cancel_event and cancel_event.is_set():
                result["cancelled"] = True
                break
            result["total_seen"] += 1
            result["last_message_id"] = msg.id

            # Service messages (joins, pins, …) carry no forwardable content
            if getattr(msg, "action", None) or getattr(msg, "service", False):
                result["skipped_count"] += 1
                continue

            m_type = _classify_media(msg)

            if media_types is not None:
                # media filter active: same rules as scrape_channel
                if m_type is None:
                    result["skipped_count"] += 1
                    continue
                type_check = "video" if m_type == "video_note" else m_type
                if type_check not in media_types:
                    result["skipped_count"] += 1
                    continue
            else:
                # No filter: forward EVERYTHING. A text-only post whose text
                # vanishes entirely under /strip links is skipped BEFORE the
                # send (send_to_destination guards it too, but skipping here
                # saves the API round-trip).
                if (m_type is None and strip_links and custom_caption is None
                        and not msg.media):
                    stripped, _e, _c = strip_links_from_text(
                        msg.message or "", msg.entities)
                    if not (stripped or "").strip():
                        result["skipped_count"] += 1
                        continue

            gid = getattr(msg, "grouped_id", None)
            if gid:
                if album_grouped_id == gid:
                    album_buffer.append(msg)
                else:
                    _flush_album()
                    album_grouped_id = gid
                    album_buffer = [msg]
            else:
                _flush_album()
                pending_send_tasks.append(
                    _asyncio.create_task(_send_with_semaphore([msg])))

            if len(pending_send_tasks) >= parallel * 4:
                done, pending = await _asyncio.wait(
                    pending_send_tasks, return_when=_asyncio.FIRST_COMPLETED)
                for t in done:
                    try:
                        t.result()
                    except Exception:
                        pass
                pending_send_tasks = list(pending)

            import time as _time
            now = _time.time()
            if status_callback and now - last_status_time > 5.0:
                last_status_time = now
                elapsed = now - send_started
                rate = (result["sent_count"] / elapsed
                        if elapsed > 0.5 and result["sent_count"] else None)
                await status_callback(format_scrape_progress(
                    title=f"from #{anchor_message_id} {direction}",
                    sent=result["sent_count"],
                    total=result.get("expected_total"),
                    failed=result["failed_count"],
                    skipped=result["skipped_count"],
                    flood_waits=result["flood_waits"],
                    rate=rate,
                    last_msg_id=result["last_message_id"],
                ))

            if stats_callback:
                try:
                    await stats_callback(result)
                except Exception:
                    pass

        _flush_album()
        if pending_send_tasks:
            try:
                await _asyncio.gather(*pending_send_tasks, return_exceptions=True)
            except Exception:
                pass

        if status_callback:
            if result["cancelled"]:
                await status_callback(
                    f"🛑 Scraping cancelled.\n"
                    f"   Sent: {result['sent_count']}, "
                    f"Failed: {result['failed_count']}, "
                    f"Skipped: {result['skipped_count']}")
            else:
                ran = (f"#{result['first_sent_id']} … #{result['last_sent_id']}"
                       if result["first_sent_id"] else "(none)")
                elapsed = max(time.time() - send_started, 0.001)
                took = f"{elapsed:.0f}s"
                pace = (f" · {result['sent_count'] / elapsed:.1f} msg/s"
                        if result["sent_count"] else "")
                expected = result.get("expected_total") or 0
                sent_line = (f"Sent: {result['sent_count']} / {expected}"
                             if expected else f"Sent: {result['sent_count']}")
                await status_callback(
                    f"✅ From-point scrape complete!\n\n"
                    f"Range: {ran}\n"
                    f"Fetched: {result['fetched_count']}\n"
                    f"{sent_line}\n"
                    f"Failed: {result['failed_count']}\n"
                    f"Skipped (filtered/service): {result['skipped_count']}\n"
                    f"Flood waits: {result['flood_waits']}\n"
                    f"Took: {took}{pace}\n\n"
                    f"To continue exactly where this stopped, send the link "
                    f"of the last message with #old / #new again.")
        return result


__all__ = [
    "UserSession",
    "ParsedLink",
    "parse_telegram_link",
    "parse_channel_link",
    "strip_links_from_text",
    "text_has_links",
    "parse_scrape_point_request",
    "order_messages_for_delivery",
]
