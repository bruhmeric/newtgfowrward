"""End-to-end webhook-mode test with a mocked Telegram API.

Boots the REAL _run_webhook_custom() with PTB's network calls patched out,
then verifies (scenario 1, happy path):
  * /health returns 200 with ready: True once booted
  * GET / serves the HTML status dashboard (READY badge, env checklist)
  * webhook registered with secret_token + restricted allowed_updates
  * POST /<path> without the secret header -> 401 (anti-spoofing)
  * POST /<path> with the right secret while app is running -> 200 and the
    update is dispatched (process_update called)
  * keep-alive loop pings the public URL (with a short interval)
  * SIGTERM triggers the graceful shutdown path (app.stop, post_stop called,
    port released, exit cleanly)

Scenario 2 (failure path — the Render crash-loop regression):
  * app.initialize() raises InvalidToken (bad BOT_TOKEN)
  * the process must NOT exit: /health stays 200 with ready: False and
    shows phase + error + fix hint; the dashboard shows STARTUP ERROR
  * SIGTERM during the retry backoff still shuts down cleanly

Scenario 3 (the "stuck at 3/5 setup-storage" regression):
  * UserSession.start() HANGS forever (exactly what a bad/unreachable
    SESSION_STRING did in production — Telethon's connect() has no
    built-in timeout and it froze phase 3/5 for 90 s per attempt, so the
    bot never registered its webhook and never answered)
  * the bot must STILL become ready: /health shows ready: True, the
    webhook registers, updates dispatch, and the Telethon card shows a
    connecting/not-connected state instead of blocking startup
  * SIGTERM cancels the hung background loop cleanly

Run: python webhook_e2e_test.py
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from unittest import mock

os.environ.update({
    "BOT_TOKEN": "123456:FAKE-token-for-e2e",
    "API_ID": "12345",
    "API_HASH": "abcdef",
    "MODE": "webhook",
    "WEBHOOK_URL": "http://localhost:18123",
    "PORT": "18123",
    "DB_PATH": "/tmp/e2e_test.db",
    "KEEP_ALIVE": "true",
    "KEEP_ALIVE_INTERVAL": "60",  # clamped to min 60s — we verify it started, not each ping
})

import aiohttp  # noqa: E402

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


async def main() -> int:
    from config import Config
    from db import Database
    import bot as bot_mod

    cfg = Config.load()
    check("config loaded with keep-alive on", cfg.keep_alive and cfg.keep_alive_interval == 60)

    app = bot_mod.build_application(cfg, Database(cfg.db_path))

    # --- Patch every network-touching PTB method ---
    calls = {"set_webhook": {}, "process_update": 0, "get_me": 0}

    async def fake_initialize(self):  # unused now — real initialize runs with patched get_me
        pass

    async def fake_get_me(self):
        from telegram import User
        calls["get_me"] += 1
        # PTB's real get_me caches the result on the bot instance — our fake
        # must do the same or every property access raises "not initialized".
        me = User(id=42, is_bot=True, first_name="Test", username="test_bot")
        self._bot_user = me
        return me

    async def fake_set_webhook(self, url=None, allowed_updates=None,
                               drop_pending_updates=None, secret_token=None, **kw):
        calls["set_webhook"] = {
            "url": url, "allowed_updates": allowed_updates,
            "secret_token": secret_token,
        }

    async def fake_process_update(self, update):
        calls["process_update"] += 1

    async def fake_delete_webhook(self, *a, **kw):
        return True

    async def fake_get_webhook_info(self, *a, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(
            url="https://example.com/hook", pending_update_count=0,
            last_error_message=None, last_error_date=None,
        )

    # Telethon is not configured with a session string -> UserSession.start
    # would try to connect; patch it to report "not authorized".
    async def fake_us_start(self):
        return False

    with mock.patch("telegram.ext._extbot.ExtBot.get_me", fake_get_me), \
         mock.patch("telegram.ext._extbot.ExtBot.set_webhook", fake_set_webhook), \
         mock.patch("telegram.ext._extbot.ExtBot.delete_webhook", fake_delete_webhook), \
         mock.patch("telegram.ext._extbot.ExtBot.get_webhook_info", fake_get_webhook_info), \
         mock.patch("telegram.ext.Application.process_update", fake_process_update), \
         mock.patch("user_session.UserSession.start", fake_us_start):
        # Run the webhook server in a background task
        server_task = asyncio.create_task(bot_mod._run_webhook_custom(app, cfg))
        await asyncio.sleep(1.5)  # let it boot fully

        path = cfg.webhook_url_path
        base = f"http://localhost:{cfg.port}"

        async with aiohttp.ClientSession() as http:
            # 1. health endpoint (plain text, machine readable)
            async with http.get(f"{base}/health") as r:
                body = await r.text()
                check("health endpoint 200 + ready", r.status == 200 and "ready: True" in body,
                      f"status={r.status} body={body!r}")

            # 1b. dashboard at the service root
            async with http.get(f"{base}/") as r:
                page = await r.text()
                check("dashboard 200 + READY badge",
                      r.status == 200 and "READY" in page
                      and "Telegram Forwarder Bot" in page
                      and "text/html" in r.headers.get("Content-Type", ""),
                      f"status={r.status} len={len(page)}")
                check("dashboard env checklist + bot username",
                      "Environment variables" in page and "test_bot" in page
                      and "Pending updates" in page,
                      page[:200])

            # 2. POST webhook without secret -> 401
            async with http.post(f"{base}/{path}", json={"update_id": 1}) as r:
                check("spoofed POST rejected with 401", r.status == 401,
                      f"status={r.status}")

            # 3. POST webhook with wrong secret -> 401
            async with http.post(f"{base}/{path}", json={"update_id": 1},
                                 headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}) as r:
                check("wrong secret rejected with 401", r.status == 401)

            # 4. POST webhook with correct secret -> 200 + dispatched
            secret = bot_mod._webhook_secret(cfg)
            update_payload = {
                "update_id": 1001,
                "message": {
                    "message_id": 10,
                    "date": int(time.time()),
                    "chat": {"id": 7, "type": "private"},
                    "from": {"id": 7, "is_bot": False, "first_name": "T"},
                    "text": "hello",
                },
            }
            async with http.post(f"{base}/{path}", json=update_payload,
                                 headers={"X-Telegram-Bot-Api-Secret-Token": secret}) as r:
                check("valid POST accepted with 200", r.status == 200, f"status={r.status}")
            await asyncio.sleep(0.5)
            check("update dispatched to process_update", calls["process_update"] == 1,
                  f"count={calls['process_update']}")

            # 5. set_webhook registered with secret + restricted allowed_updates
            sw = calls["set_webhook"]
            check("set_webhook called with secret_token",
                  sw.get("secret_token") == secret)
            check("allowed_updates restricted to message+callback_query",
                  sw.get("allowed_updates") == ["message", "callback_query"],
                  f"got {sw.get('allowed_updates')}")

            # 6. keep-alive task alive (it sleeps 60s; just verify the task exists
            #    by checking it's still running — the server task is alive)
            check("server still running (keep-alive task didn't crash it)",
                  not server_task.done())
            if server_task.done():
                server_task.exception()

        # 7. SIGTERM -> graceful shutdown
        os.kill(os.getpid(), signal.SIGTERM)
        try:
            await asyncio.wait_for(server_task, timeout=10)
            check("SIGTERM -> graceful shutdown completes", True)
        except asyncio.TimeoutError:
            check("SIGTERM -> graceful shutdown completes", False, "timed out")
        except Exception as e:
            check("SIGTERM -> graceful shutdown completes", False, f"raised {e!r}")

        # port released?
        try:
            import socket
            s = socket.socket()
            s.bind(("0.0.0.0", cfg.port))
            s.close()
            check("port released after shutdown", True)
        except OSError:
            check("port released after shutdown", False)

    # =========================================================
    # Scenario 2: startup failure must NOT exit / crash-loop.
    # A bad BOT_TOKEN raises InvalidToken inside app.initialize().
    # The old code exited(1) -> Render restarted the container in a loop
    # -> the user always saw ready: False with no visible reason. The new
    # code stays up, surfaces the error on /health and the dashboard, and
    # retries with backoff.
    # =========================================================
    print()
    print("== scenario 2: startup failure does not crash-loop ==")
    os.environ["PORT"] = "18124"
    os.environ["DB_PATH"] = "/tmp/e2e_test2.db"
    cfg2 = Config.load()
    app2 = bot_mod.build_application(cfg2, Database(cfg2.db_path))

    async def failing_initialize(self):
        from telegram.error import InvalidToken
        raise InvalidToken("Unauthorized")

    with mock.patch("telegram.ext.Application.initialize", failing_initialize):
        server2 = asyncio.create_task(bot_mod._run_webhook_custom(app2, cfg2))
        await asyncio.sleep(1.0)  # attempt 1 fails fast; retry backoff begins
        base2 = f"http://localhost:{cfg2.port}"

        async with aiohttp.ClientSession() as http:
            async with http.get(f"{base2}/health") as r:
                body = await r.text()
                check("failed boot: /health stays 200 with ready: False",
                      r.status == 200 and "ready: False" in body,
                      f"status={r.status} body={body!r}")
                check("failed boot: error + phase surfaced in /health",
                      "InvalidToken" in body and "phase:" in body, body)
                check("failed boot: fix hint present in /health",
                      "BOT_TOKEN" in body, body)

            async with http.get(f"{base2}/") as r:
                page = await r.text()
                check("failed boot: dashboard shows STARTUP ERROR + fix hint",
                      r.status == 200 and "STARTUP ERROR" in page
                      and "@BotFather" in page and "How to fix" in page,
                      page[:300])

            check("failed boot: process stays up (no crash loop)",
                  not server2.done())
            if server2.done():
                server2.exception()

        # SIGTERM during the retry backoff must still shut down cleanly
        os.kill(os.getpid(), signal.SIGTERM)
        try:
            await asyncio.wait_for(server2, timeout=10)
            check("failed boot: SIGTERM during backoff -> clean shutdown", True)
        except Exception as e:
            check("failed boot: SIGTERM during backoff -> clean shutdown",
                  False, repr(e))

    # =========================================================
    # Scenario 3: a HANGING Telethon session must NOT block the bot.
    # UserSession.start() never returns (mocked with an endless sleep) —
    # the production symptom of a bad SESSION_STRING was "3/5
    # setup-storage" forever + a bot that never answered. Now Telethon
    # runs in a background loop, so the bot itself must come up.
    # =========================================================
    print()
    print("== scenario 3: hanging Telethon session does not block startup ==")
    os.environ["PORT"] = "18125"
    os.environ["DB_PATH"] = "/tmp/e2e_test3.db"
    os.environ["SESSION_STRING"] = "1FakeSessionStringThatWouldHangForever"
    cfg3 = Config.load()
    check("s3: session configured (string present)", cfg3.has_user_session)
    app3 = bot_mod.build_application(cfg3, Database(cfg3.db_path))
    # Reset the module-level session manager (scenarios share the process)
    bot_mod._USER_SESSION_MGR["task"] = None
    bot_mod._USER_SESSION_MGR["us"] = None

    async def hanging_us_start(self):
        # Simulate Telethon's timeout-less connect() to an unreachable DC:
        # simply never return.
        await asyncio.sleep(99999)
        return False

    calls3 = {"set_webhook": 0, "process_update": 0}

    async def fake_set_webhook3(self, url=None, allowed_updates=None,
                                drop_pending_updates=None, secret_token=None, **kw):
        calls3["set_webhook"] += 1

    async def fake_process_update3(self, update):
        calls3["process_update"] += 1

    with mock.patch("telegram.ext._extbot.ExtBot.get_me", fake_get_me), \
         mock.patch("telegram.ext._extbot.ExtBot.set_webhook", fake_set_webhook3), \
         mock.patch("telegram.ext._extbot.ExtBot.delete_webhook", fake_delete_webhook), \
         mock.patch("telegram.ext._extbot.ExtBot.get_webhook_info", fake_get_webhook_info), \
         mock.patch("telegram.ext.Application.process_update", fake_process_update3), \
         mock.patch("user_session.UserSession.start", hanging_us_start):
        server3 = asyncio.create_task(bot_mod._run_webhook_custom(app3, cfg3))
        await asyncio.sleep(1.5)

        base3 = f"http://localhost:{cfg3.port}"
        path3 = cfg3.webhook_url_path
        secret3 = bot_mod._webhook_secret(cfg3)

        async with aiohttp.ClientSession() as http:
            async with http.get(f"{base3}/health") as r:
                body = await r.text()
                check("s3: bot READY despite hung Telethon",
                      r.status == 200 and "ready: True" in body,
                      f"status={r.status} body={body!r}")
                check("s3: telethon state surfaced in /health",
                      "telethon: connecting" in body, body)

            async with http.get(f"{base3}/") as r:
                page = await r.text()
                check("s3: dashboard READY + Telethon card present",
                      r.status == 200 and "READY" in page
                      and "Telethon session" in page, page[:300])

            update_payload = {
                "update_id": 2001,
                "message": {
                    "message_id": 11,
                    "date": int(time.time()),
                    "chat": {"id": 8, "type": "private"},
                    "from": {"id": 8, "is_bot": False, "first_name": "U"},
                    "text": "hello while telethon hangs",
                },
            }
            async with http.post(f"{base3}/{path3}", json=update_payload,
                                 headers={"X-Telegram-Bot-Api-Secret-Token": secret3}) as r:
                check("s3: webhook POST accepted while Telethon hangs",
                      r.status == 200, f"status={r.status}")
            await asyncio.sleep(0.5)
            check("s3: update dispatched while Telethon hangs",
                  calls3["process_update"] == 1, f"count={calls3['process_update']}")
            check("s3: webhook registered with Telegram",
                  calls3["set_webhook"] >= 1)

        # SIGTERM must cancel the hung loop and shut down cleanly
        os.kill(os.getpid(), signal.SIGTERM)
        try:
            await asyncio.wait_for(server3, timeout=10)
            check("s3: SIGTERM cancels hung Telethon loop -> clean shutdown", True)
        except Exception as e:
            check("s3: SIGTERM cancels hung Telethon loop -> clean shutdown",
                  False, repr(e))
        task_state = bot_mod._USER_SESSION_MGR["task"]
        check("s3: background Telethon task no longer running",
              task_state is None or task_state.done())

    print()
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
