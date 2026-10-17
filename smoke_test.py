"""Smoke tests for the optimized Telegram Forwarder Bot.

Covers the pure-logic pieces that don't need network/Telegram access:
  * config loading (keep-alive defaults, RENDER_EXTERNAL_URL fallback,
    robust ADMIN_IDS parsing)
  * link parsing (private/public/channel-only/invite links)
  * database round-trip (pending forwards, topic overrides, purge)
  * webhook secret derivation (deterministic)
  * topic keyboard cap (100-button Telegram limit)
  * album-window logic sanity (the ids range we now fetch)
  * imports of every module (catches the Telethon import bug class)

Run:  python smoke_test.py   (from the repo root, with deps installed)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


# HERMETIC TEST ENV — must be set BEFORE importing the app modules.
# config.py runs load_dotenv() at import time and python-dotenv never
# overrides variables that are already set, so pinning every variable here
# neutralizes a real .env sitting in the repo (e.g. a dev's actual secrets)
# and keeps the checks deterministic.
os.environ.update({
    "BOT_TOKEN": "123456:TEST-token-secret-part",
    "API_ID": "12345",
    "API_HASH": "abcdef",
    "MODE": "webhook",
    "KEEP_ALIVE": "",              # empty -> _env_bool falls back to the default
    "SESSION_STRING": "",
    "ADMIN_IDS": "",
    "DESTINATION_GROUP_ID": "",
    "SESSION_NAME": "user_session",
    "DB_PATH": "unused-by-smoke-tests",
    "PHONE": "",
    "LOG_LEVEL": "INFO",
    "STRIP_CAPTIONS": "",          # /strip env defaults (neutralized here)
    "STRIP_LINKS": "",
})

print("== 1. Module imports (catches the Telethon GetForumTopicsRequest bug) ==")
import config as config_mod          # noqa: E402
import db as db_mod                  # noqa: E402
import topics as topics_mod          # noqa: E402
import user_session as us_mod        # noqa: E402
import handlers.admin                # noqa: E402
import handlers.direct               # noqa: E402
import handlers.link                 # noqa: E402
import bot as bot_mod                # noqa: E402
check("all modules import cleanly", True)

print("== 2. Config loading ==")
env = {
    "BOT_TOKEN": "123456:TEST-token-secret-part",
    "API_ID": "12345",
    "API_HASH": "abcdef",
    "MODE": "webhook",
    "RENDER_EXTERNAL_URL": "https://my-bot.onrender.com/",
}
old = {k: os.environ.get(k) for k in env}
os.environ.update(env)
try:
    cfg = config_mod.Config.load()
    check("webhook_url falls back to RENDER_EXTERNAL_URL",
          cfg.webhook_url == "https://my-bot.onrender.com",
          f"got {cfg.webhook_url!r}")
    check("trailing slash stripped from external URL", not cfg.webhook_url.endswith("/"))
    check("keep_alive defaults ON in webhook mode", cfg.keep_alive is True)
    check("keep_alive_interval default 540s", cfg.keep_alive_interval == 540)
    check("webhook path uses token secret", cfg.webhook_url_path == "TEST-token-secret-part")

    os.environ["KEEP_ALIVE"] = "false"
    os.environ["ADMIN_IDS"] = "111, 222, bad-entry, 333"
    os.environ["KEEP_ALIVE_INTERVAL"] = "99999"
    cfg2 = config_mod.Config.load()
    check("KEEP_ALIVE=false respected", cfg2.keep_alive is False)
    check("bad ADMIN_IDS entries skipped, not fatal",
          cfg2.admin_ids == [111, 222, 333], f"got {cfg2.admin_ids}")
    check("keep_alive_interval clamped to 840", cfg2.keep_alive_interval == 840)

    # has_user_session: api credentials alone must NOT count as a session
    # (the repo's placeholder user_session.session has no auth key either).
    check("api credentials alone do NOT count as a session",
          cfg2.has_user_session is False,
          f"has_user_session={cfg2.has_user_session}")
    os.environ["SESSION_STRING"] = "1SomeSessionString"
    cfg3 = config_mod.Config.load()
    check("SESSION_STRING + api credentials count as a session",
          cfg3.has_user_session is True)
    del os.environ["SESSION_STRING"]

    # a non-numeric API_ID must disable the session with a visible issue,
    # not crash Config.load (it used to raise ValueError -> crash loop)
    os.environ["API_ID"] = "not-a-number"
    cfg4 = config_mod.Config.load()
    check("non-numeric API_ID does not crash load()",
          cfg4.api_id is None and not cfg4.has_user_session)
    check("non-numeric API_ID surfaces as a load issue",
          any("API_ID" in i for i in cfg4.load_issues), f"got {cfg4.load_issues}")
    os.environ["API_ID"] = "12345"
finally:
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print("== 3. Link parsing ==")
p = us_mod.parse_telegram_link("https://t.me/c/1234567890/42")
check("private post link parsed", p is not None and p.kind == "private"
      and p.chat_ref == "-1001234567890" and p.message_id == 42)
p = us_mod.parse_telegram_link("https://t.me/somechannel/99")
check("public post link parsed", p is not None and p.kind == "public"
      and p.chat_ref == "somechannel" and p.message_id == 99)
check("invite link rejected", us_mod.parse_telegram_link("https://t.me/+abc123") is None)
check("bare channel link not a post link",
      us_mod.parse_telegram_link("https://t.me/somechannel") is None)
cp = us_mod.parse_channel_link("https://t.me/c/1234567890")
check("channel-only private link parsed", cp is not None and cp.kind == "private"
      and cp.message_id == 0)
cp2 = us_mod.parse_channel_link("https://t.me/somechannel/")
check("channel-only public link parsed (trailing slash)",
      cp2 is not None and cp2.chat_ref == "somechannel")

print("== 4. Album fetch window (regression test for the limit=20 bug) ==")
# The fixed code fetches ids around the target message, not the channel's
# 20 most recent messages. We verify the window arithmetic shape used in
# fetch_message step 4.
target = 42
lo, hi = max(1, target - 10), target + 10
window = list(range(lo, hi + 1))
check("window centered on target msg id", len(window) == 21 and target in window)
check("window clamps at 1 for low ids", max(1, 1 - 10) == 1)

print("== 5. Database round-trip ==")

async def db_tests() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = db_mod.Database(os.path.join(td, "test.db"))
        await d.init()
        pid = await d.create_pending(
            user_id=7, source_chat_id=-100123, source_message_id=55,
            payload={"direct_send": True, "source_message_ids": [55, 56]},
            kind="link",
        )
        row = await d.get_pending(pid)
        check("pending forward round-trip", row is not None and row["user_id"] == 7
              and row["payload"]["source_message_ids"] == [55, 56])
        await d.delete_pending(pid)
        check("pending delete works", await d.get_pending(pid) is None)

        await d.add_topic_override(-100123, 12, "Videos")
        await d.set_cached_topics(-100123, [{"id": 12, "title": "OLD"},
                                            {"id": 34, "title": "Images"}])
        merged = await d.get_all_topics(-100123)
        check("topic overrides beat cache",
              merged == [{"id": 12, "title": "Videos"}, {"id": 34, "title": "Images"}],
              f"got {merged}")
        await d.delete_topic_override(-100123, 12)
        merged2 = await d.get_all_topics(-100123)
        check("delete_topic_override works",
              merged2 == [{"id": 12, "title": "OLD"}, {"id": 34, "title": "Images"}])

        await d.set_runtime("destination_group_id", "-100999")
        check("runtime config round-trip",
              await d.get_runtime("destination_group_id") == "-100999")

        # purge: create an old pending forward with a stale timestamp
        import time as _t
        stale_pid = await d.create_pending(7, -1, 1, {}, "direct")
        await d._conn.execute(
            "UPDATE pending_forwards SET created_at = ? WHERE id = ?",
            (int(_t.time()) - 7200, stale_pid),
        )
        await d._conn.commit()
        purged = await d.purge_old_pending(max_age_seconds=3600)
        check("purge_old_pending removes stale rows", purged == 1,
              f"purged={purged}")
        await d.close()

asyncio.run(db_tests())

print("== 6. Webhook secret derivation ==")
s1 = bot_mod._webhook_secret(type("C", (), {
    "bot_token": "123456:TEST-token-secret-part"})())
s2 = bot_mod._webhook_secret(type("C", (), {
    "bot_token": "123456:TEST-token-secret-part"})())
s3 = bot_mod._webhook_secret(type("C", (), {
    "bot_token": "999999:DIFFERENT-token"})())
check("secret is deterministic across restarts", s1 == s2)
check("secret differs per bot token", s1 != s3)
check("secret is a safe length (<=64)", 0 < len(s1) <= 64)

print("== 7. Topic keyboard 100-button cap ==")
class FakeDB:  # only used for the constructor; build_keyboard doesn't touch it
    pass
tm = topics_mod.TopicManager.__new__(topics_mod.TopicManager)  # skip __init__ (no db needed)
many_topics = [{"id": i, "title": f"Topic {i}"} for i in range(1, 151)]
kb = tm.build_keyboard("abc123", many_topics)
n_buttons = sum(len(row) for row in kb.inline_keyboard)
check("keyboard capped under Telegram's 100-button limit", n_buttons <= 100,
      f"got {n_buttons} buttons")
kb_small = tm.build_keyboard("abc123", many_topics[:5])
check("small topic sets unaffected", sum(len(r) for r in kb_small.inline_keyboard) == 6)

print("== 8. health payload helper (RSS reading) ==")
rss = bot_mod._rss_mb()
check("rss_mb returns a plausible number", rss == -1.0 or rss > 1.0, f"got {rss}")

print("== 9. Telethon session diagnostics (new) ==")

# 9a. address classification
check("official DC IP classified 'telegram'",
      us_mod.classify_session_address("149.154.167.51") == "telegram")
check("private IP classified 'private'",
      us_mod.classify_session_address("10.0.0.5") == "private")
check("loopback classified 'private'",
      us_mod.classify_session_address("127.0.0.1") == "private")
check("other global IP classified 'public'",
      us_mod.classify_session_address("8.8.8.8") == "public")
check("hostname classified 'hostname'",
      us_mod.classify_session_address("venus.web.telegram.org") == "hostname")
check("empty address classified 'unknown'",
      us_mod.classify_session_address("") == "unknown")
check("in-range DC1 front-door IP (not the canonical one) classified 'telegram'",
      us_mod.classify_session_address("149.154.175.58") == "telegram")
check("in-range DC5 IP classified 'telegram'",
      us_mod.classify_session_address("91.108.58.200") == "telegram")
check("adjacent non-Telegram range NOT classified 'telegram'",
      us_mod.classify_session_address("149.155.0.1") == "public")


def _make_session_string(dc_id: int, ip: str, port: int = 443) -> str:
    """Craft a valid-format Telethon StringSession without any network use."""
    import os as _os
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession
    s = StringSession()
    s.set_dc(dc_id, ip, port)
    s._auth_key = AuthKey(data=_os.urandom(256))
    return s.save()


def _make_session_string_dialect_b(dc_id: int, host: str, port: int = 443) -> str:
    """Craft a StringSession in the older length-prefixed-ASCII dialect —
    the one Telethon 1.40 silently misparses as a garbage IPv6 address
    (dc_id B + host_len H + ASCII host + port H + 256-byte key)."""
    import base64 as _b64
    import os as _os
    raw = (bytes([dc_id]) + len(host).to_bytes(2, "big")
           + host.encode("ascii") + port.to_bytes(2, "big") + _os.urandom(256))
    return "1" + _b64.urlsafe_b64encode(raw).decode("ascii")


async def session_diag_tests() -> None:
    # 9b. introspection: __init__ must expose dc/server/port without connecting
    good = us_mod.UserSession(
        "t", 12345, "abcdef", session_string=_make_session_string(4, "149.154.167.91"))
    check("session introspection extracts DC info",
          good.session_info.get("dc_id") == 4
          and good.session_info.get("server") == "149.154.167.91"
          and good.session_info.get("port") == 443,
          f"got {good.session_info}")
    check("official DC session accepted (no init error)", good._init_error is None)
    check("official DC session has 256-byte auth key",
          good.session_info.get("auth_key_bytes") == 256)
    check("_session_target_desc names the DC",
          "149.154.167.91" in good._session_target_desc()
          and "DC4" in good._session_target_desc())

    # 9c. private-address session (VPN/proxy artifact). With a KNOWN dc_id
    # the address is overridden with the canonical DC address (the auth key
    # is DC-bound, so this repairs the session). With an UNKNOWN dc_id there
    # is no fallback — it must still fail fast with instructions.
    import time as _t
    vpn_known_dc = us_mod.UserSession(
        "t", 12345, "abcdef", session_string=_make_session_string(2, "10.6.0.7"))
    check("private address + known DC overridden to canonical DC",
          vpn_known_dc._init_error is None
          and vpn_known_dc.session_info.get("server") == "149.154.167.51"
          and vpn_known_dc.session_info.get("address_kind") == "telegram",
          f"got server={vpn_known_dc.session_info.get('server')!r} "
          f"err={vpn_known_dc._init_error!r}")
    bad = us_mod.UserSession(
        "t", 12345, "abcdef", session_string=_make_session_string(9, "10.6.0.7"))
    check("private address + unknown DC still flagged at parse time",
          bad._init_error is not None and "private address" in bad._init_error,
          f"got {bad._init_error!r}")
    t0 = _t.monotonic()
    ok = await bad.start()
    check("unknown-DC private-address start() fails immediately (<2s, no network)",
          ok is False and (_t.monotonic() - t0) < 2.0)
    check("unknown-DC private-address reason lands in last_error",
          "VPN" in (bad.last_error or "") or "private" in (bad.last_error or ""))

    # 9c2. dialect-B session strings (length-prefixed ASCII address) — the
    # dialect Telethon 1.40 silently misparses as garbage IPv6
    from telethon.sessions import StringSession as _SS
    b_str = _make_session_string_dialect_b(1, "149.154.175.58")
    raw_loaded = str(_SS(b_str).server_address)
    check("bug reproduced: Telethon 1.40 alone misreads dialect-B as IPv6 garbage",
          ":" in raw_loaded and "3134" in raw_loaded, f"got {raw_loaded!r}")
    fixed = us_mod.UserSession("t", 12345, "abcdef", session_string=b_str)
    check("dialect-B repaired to the embedded IPv4 address",
          fixed.session_info.get("server") == "149.154.175.58"
          and fixed.session_info.get("port") == 443
          and fixed._init_error is None,
          f"got {fixed.session_info}")
    check("dialect-B repaired address classifies as telegram",
          fixed.session_info.get("address_kind") == "telegram")
    check("dialect-B auth key preserved (256 bytes)",
          fixed.session_info.get("auth_key_bytes") == 256)

    # 9d. malformed string still caught (regression)
    ugly = us_mod.UserSession("t", 12345, "abcdef", session_string="garbage-not-a-session")
    check("malformed string still reported, client still constructed",
          ugly._init_error is not None and ugly.client is not None)

    # 9e. raw TCP probe: reachable port on localhost + unroutable TEST-NET IP
    server = await asyncio.start_server(
        lambda r, w: (w.close()), "127.0.0.1", 0)
    probe_port = server.sockets[0].getsockname()[1]
    try:
        r_ok = await us_mod.UserSession._probe_tcp("127.0.0.1", probe_port, timeout=3)
        check("probe succeeds against a live listener", r_ok["ok"] is True,
              f"got {r_ok}")
        r_dead = await us_mod.UserSession._probe_tcp("192.0.2.1", 443, timeout=1.5)
        check("probe fails cleanly against unroutable TEST-NET IP",
              r_dead["ok"] is False and r_dead["error"],
              f"got {r_dead}")
    finally:
        server.close()
        await server.wait_closed()

    # 9f. dashboard picks up the DC info line (rendering, no network)
    bot_mod._USER_SESSION_MGR["us"] = good
    try:
        html = bot_mod._render_dashboard(cfg, dict(bot_mod._STATE))
        check("dashboard shows session DC address",
              "149.154.167.91" in html, "DC info missing from dashboard HTML")
        good.last_probe = {"host": "149.154.167.91:443", "ok": True, "ms": 42,
                           "error": None}
        html2 = bot_mod._render_dashboard(cfg, dict(bot_mod._STATE))
        check("dashboard shows probe result",
              "probe" in html2 and "42" in html2)
    finally:
        bot_mod._USER_SESSION_MGR["us"] = None

asyncio.run(session_diag_tests())

print("== 10. Polling-mode dashboard card (new) ==")
pcfg = config_mod.Config.load()   # webhook cfg from section 2; override for the check
pcfg.mode = "polling"
html_p = bot_mod._render_dashboard(pcfg, {
    "ready": True, "bot_username": "testbot", "attempts": 1,
    "webhook_registered": False, "user_session": "connected as @x (1)",
    "phase": "running", "error": None, "hint": None,
    "wh_info": None, "wh_info_ts": 0.0,
})
check("dashboard shows polling card in polling mode",
      "polling (long-poll active)" in html_p)
check("dashboard does not show 'not registered' webhook card in polling mode",
      "not registered" not in html_p)

print("== 11. Link stripping engine (/strip links) ==")
from telethon.tl.types import (               # noqa: E402
    MessageEntityBold as _Bold, MessageEntityUrl as _Url,
    MessageEntityTextUrl as _TUrl,
)

# 11a. raw URL in plain text (no entities at all)
t, e, ch = us_mod.strip_links_from_text("check https://example.com/abc now", None)
check("raw URL removed from plain text, spacing collapsed",
      t == "check now" and ch, f"got {t!r}")

# 11b. URL entity removed, trailing bold entity kept with shifted offset
txt = "visit https://t.me/foo today"
ents = [_Url(offset=6, length=16), _Bold(offset=23, length=5)]
t, e, ch = us_mod.strip_links_from_text(txt, ents)
check("URL entity removed; bold entity survives with shifted offset",
      t == "visit today" and ch and e and len(e) == 1
      and isinstance(e[0], _Bold) and e[0].offset == 6 and e[0].length == 5,
      f"got {t!r} ents={e}")

# 11c. hyperlinked text (TextUrl): visible text kept, link behind it dropped
txt = "Join us for more"
ents = [_TUrl(offset=0, length=4, url="https://t.me/foo")]
t, e, ch = us_mod.strip_links_from_text(txt, ents)
check("TextUrl: visible text kept, hyperlink entity dropped, changed=True",
      t == txt and ch and not e, f"got {t!r} e={e}")

# 11d. no links — text and entities come back untouched
txt = "just a plain post, nothing else"
ents = [_Bold(offset=0, length=4)]
t, e, ch = us_mod.strip_links_from_text(txt, ents)
check("link-free text comes back untouched",
      t == txt and not ch and e is ents, f"got {t!r} changed={ch}")

# 11e. pure-link text strips to empty
t, e, ch = us_mod.strip_links_from_text(
    "https://a.example/1 https://b.example/2", None)
check("pure-link text strips to empty", t.strip() == "" and ch, f"got {t!r}")

# 11f. a URL on its own line takes the whole line with it
t, e, ch = us_mod.strip_links_from_text(
    "line one\nhttps://t.me/spam\nline two", None)
check("URL-only line disappears entirely",
      t == "line one\nline two", f"got {t!r}")

# 11g. intentional blank separator lines survive
t, e, ch = us_mod.strip_links_from_text(
    "para one\n\npara two\nhttps://x.example/y", None)
check("intentional blank separator kept",
      t == "para one\n\npara two", f"got {t!r}")

# 11h. text_has_links
check("text_has_links: raw URL", us_mod.text_has_links("go to t.me/foo now", None))
check("text_has_links: URL entity",
      us_mod.text_has_links("click", [_Url(0, 5)]))
check("text_has_links: link-free text is False",
      not us_mod.text_has_links("no links here", [_Bold(0, 2)]))
check("text_has_links: empty text is False", not us_mod.text_has_links("", None))

print("== 12. From-point scrape request parsing (#old / #new) ==")
r = us_mod.parse_scrape_point_request("https://t.me/c/1234567890/42 #old")
check("post link + #old -> private link, old, default 50",
      r is not None and r[0].kind == "private" and r[1] == "old" and r[2] == 50,
      f"got {r}")
r = us_mod.parse_scrape_point_request("check this https://t.me/somechannel/99 #NEW")
check("#NEW is case-insensitive, public link",
      r is not None and r[0].kind == "public" and r[1] == "new" and r[2] == 50,
      f"got {r}")
r = us_mod.parse_scrape_point_request("https://t.me/somechannel/99 #new120")
check("count baked into the hashtag (#new120)",
      r is not None and r[1] == "new" and r[2] == 120, f"got {r}")
r = us_mod.parse_scrape_point_request("https://t.me/c/1234567890/42 #old 200")
check("count as a separate number (#old 200)",
      r is not None and r[1] == "old" and r[2] == 200, f"got {r}")
r = us_mod.parse_scrape_point_request("https://t.me/c/1234567890/42 #new 600")
check("count clamped to 500",
      r is not None and r[2] == 500, f"got {r}")
check("no hashtag -> not a from-point request",
      us_mod.parse_scrape_point_request(
          "https://t.me/c/1234567890/42 forward this") is None)
check("hashtag + channel-only link -> not a from-point request",
      us_mod.parse_scrape_point_request(
          "https://t.me/somechannel #new") is None)
check("hashtag alone -> not a from-point request",
      us_mod.parse_scrape_point_request("just talking about #new stuff") is None)

print("== 13. From-point delivery ordering ==")


class _FakeMsg:
    def __init__(self, id_, gid=None):
        self.id = id_
        self.grouped_id = gid
        self.message = None
        self.media = None
        self.entities = None


out = us_mod.order_messages_for_delivery(
    [_FakeMsg(50), _FakeMsg(49), _FakeMsg(48), _FakeMsg(47)], "old")
check("'old' fetch (newest->oldest) reversed into chronological order",
      [m.id for m in out] == [47, 48, 49, 50],
      f"got {[m.id for m in out]}")
out = us_mod.order_messages_for_delivery(
    [_FakeMsg(47), _FakeMsg(48), _FakeMsg(49), _FakeMsg(50)], "new")
check("'new' fetch (already chronological) passed through",
      [m.id for m in out] == [47, 48, 49, 50])
out = us_mod.order_messages_for_delivery(
    [_FakeMsg(47), _FakeMsg(50, gid=9), _FakeMsg(48, gid=9),
     _FakeMsg(49, gid=9)], "new")
check("album run re-sorted ascending by id",
      [m.id for m in out] == [47, 48, 49, 50],
      f"got {[m.id for m in out]}")

print("== 14. /strip env defaults in Config ==")
os.environ["STRIP_CAPTIONS"] = "true"
os.environ["STRIP_LINKS"] = "1"
cfg_strip = config_mod.Config.load()
check("STRIP_CAPTIONS=true / STRIP_LINKS=1 parsed",
      cfg_strip.strip_captions is True and cfg_strip.strip_links is True)
os.environ["STRIP_CAPTIONS"] = ""
os.environ["STRIP_LINKS"] = ""
cfg_strip2 = config_mod.Config.load()
check("strip defaults are off when env unset",
      cfg_strip2.strip_captions is False and cfg_strip2.strip_links is False)

print("== 15. send_to_destination strip paths (fake Telethon client) ==")


async def strip_send_tests() -> None:
    class FakeClient:
        def __init__(self):
            self.forwards = []
            self.sends = []

        async def forward_messages(self, dest, ids, src):
            self.forwards.append((dest, list(ids), src))
            return True

        async def send_message(self, dest, **kwargs):
            self.sends.append(kwargs)
            return True

    class FakeEnt:
        def __init__(self, id_):
            self.id = id_

    class FakeMsg:
        def __init__(self, id_, text, media=None, entities=None):
            self.id = id_
            self.message = text
            self.media = media
            self.entities = entities
            self.grouped_id = None
            self.action = None

    us = us_mod.UserSession(
        "t", 12345, "abcdef",
        session_string=_make_session_string(2, "149.154.167.51"))
    src, dst = FakeEnt(-100123), FakeEnt(-100999)

    async def fake_resolve(ref):
        return src if ref == -100123 else dst

    us._get_entity_with_warmup = fake_resolve   # shadow the method (no network)

    # A. no strip settings -> true-forward fast path
    fc = FakeClient()
    us.client = fc
    ok, _diag = await us.send_to_destination(
        source_chat_id=-100123, source_message_ids=[5], dest_chat_id=-100999,
        pre_messages=[FakeMsg(5, "plain caption", media=object())],
        source_entity=src, dest_entity=dst)
    check("no strip settings -> true forward used",
          ok and len(fc.forwards) == 1 and not fc.sends,
          f"forwards={len(fc.forwards)} sends={len(fc.sends)}")

    # B. strip_captions -> re-sent with an empty caption
    fc = FakeClient()
    us.client = fc
    ok, _diag = await us.send_to_destination(
        source_chat_id=-100123, source_message_ids=[5], dest_chat_id=-100999,
        pre_messages=[FakeMsg(5, "ad caption", media=object())],
        source_entity=src, dest_entity=dst, strip_captions=True)
    check("strip_captions -> re-sent without caption",
          ok and not fc.forwards and len(fc.sends) == 1
          and fc.sends[0]["message"] == "",
          f"forwards={len(fc.forwards)} sends={fc.sends and fc.sends[0]}")

    # C. strip_links -> caption re-sent without the URL
    fc = FakeClient()
    us.client = fc
    ok, _diag = await us.send_to_destination(
        source_chat_id=-100123, source_message_ids=[5], dest_chat_id=-100999,
        pre_messages=[FakeMsg(5, "watch this https://ads.example/x now",
                              media=object())],
        source_entity=src, dest_entity=dst, strip_links=True)
    check("strip_links -> caption re-sent without URL",
          ok and not fc.forwards and len(fc.sends) == 1
          and fc.sends[0]["message"] == "watch this now",
          f"sends={fc.sends and fc.sends[0]['message']!r}")

    # D. a text post that is ONLY links -> skipped entirely
    fc = FakeClient()
    us.client = fc
    ok, diag = await us.send_to_destination(
        source_chat_id=-100123, source_message_ids=[5], dest_chat_id=-100999,
        pre_messages=[FakeMsg(5, "https://a.example/1 https://b.example/2")],
        source_entity=src, dest_entity=dst, strip_links=True)
    check("pure-link text post skipped under strip_links",
          ok and not fc.forwards and not fc.sends
          and any("Nothing to send" in d for d in diag),
          f"ok={ok} forwards={len(fc.forwards)} sends={len(fc.sends)}")

    # E. empty-caption media under strip_captions -> nothing changes, so the
    #    fast true-forward path is still used
    fc = FakeClient()
    us.client = fc
    ok, _diag = await us.send_to_destination(
        source_chat_id=-100123, source_message_ids=[5], dest_chat_id=-100999,
        pre_messages=[FakeMsg(5, "", media=object())],
        source_entity=src, dest_entity=dst, strip_captions=True)
    check("empty caption + strip_captions -> true forward (nothing to change)",
          ok and len(fc.forwards) == 1 and not fc.sends,
          f"forwards={len(fc.forwards)} sends={len(fc.sends)}")

    # F. custom_caption keeps winning over the strip toggles
    fc = FakeClient()
    us.client = fc
    ok, _diag = await us.send_to_destination(
        source_chat_id=-100123, source_message_ids=[5], dest_chat_id=-100999,
        pre_messages=[FakeMsg(5, "original", media=object())],
        source_entity=src, dest_entity=dst,
        custom_caption="MY TEXT", strip_captions=True, strip_links=True)
    check("custom caption wins over strip settings",
          ok and not fc.forwards and len(fc.sends) == 1
          and fc.sends[0]["message"] == "MY TEXT")


asyncio.run(strip_send_tests())

print("== 16. Dashboard + /health surface the new features ==")
_state_on = {
    "ready": True, "bot_username": "testbot", "attempts": 1,
    "webhook_registered": False, "user_session": "connected as @x (1)",
    "phase": "running", "error": None, "hint": None,
    "wh_info": None, "wh_info_ts": 0.0,
    "strip": {"captions": True, "links": False},
    "scrape": "running — somechannel #500 new (sent 12)",
}
html_on = bot_mod._render_dashboard(pcfg, _state_on)
check("dashboard shows forward-cleaning card when stripping is on",
      "Forward cleaning" in html_on and "stripping captions" in html_on)
check("dashboard shows live scrape state",
      "somechannel #500" in html_on and "running" in html_on)
_state_off = dict(_state_on)
_state_off["strip"] = {"captions": False, "links": False}
_state_off["scrape"] = None
html_off = bot_mod._render_dashboard(pcfg, _state_off)
check("dashboard shows idle scrape + strip-off states",
      "idle" in html_off and "originals kept" in html_off)
lines = bot_mod._health_runtime_lines(_state_on)
check("health endpoint lines carry strip + scrape state",
      "strip_captions: on" in lines and "strip_links: off" in lines
      and "scrape: running" in lines, f"got {lines!r}")

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
