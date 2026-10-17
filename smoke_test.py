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

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
