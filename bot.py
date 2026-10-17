"""Main entry point for the Telegram Forwarder Bot.

Wires together:
  * config.py     — env-based settings
  * db.py         — SQLite persistence
  * user_session.py — Telethon user session (for locked channels & topic
                     enumeration)
  * topics.py     — forum topic discovery + inline keyboard
  * handlers/    — direct / link / admin handlers

Run modes:
  * MODE=polling  — local dev (uses PTB's run_polling)
  * MODE=webhook  — production / Render / fps.ms (uses our custom aiohttp
                    server that ALSO serves GET / with 200 OK so platform
                    health checks pass — PTB's built-in tornado server
                    returns 404 for /, which fails Render's deploy)

Render free-tier optimizations in this file:
  * The HTTP port is bound BEFORE the Telegram/Telethon handshake, so
    Render's deploy health check passes immediately instead of racing the
    1-3 s Telegram round-trips (webhook updates arriving before the app is
    ready get a 503 so Telegram retries them — nothing is dropped).
  * KEEP_ALIVE (on by default in webhook mode) self-pings the public URL
    every ~9 minutes so Render never spins the service down (free services
    sleep after 15 min without inbound traffic).
  * A per-deploy webhook secret token is registered with Telegram and
    validated on every POST — spoofed updates are rejected even if the
    URL path leaks.
  * SIGTERM (Render sends it on every redeploy/restart) triggers a clean
    shutdown: in-flight tasks finish, the DB closes, Telethon disconnects.
  * allowed_updates is restricted to message + callback_query — the only
    update types the bot uses — which shrinks every webhook payload.
  * A startup task re-populates the topic cache automatically (the DB on
    Render's ephemeral disk starts empty after every restart).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import signal
import sys
import time
from typing import Any

from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)

from config import Config
from db import Database
from user_session import UserSession
from topics import TopicManager
from handlers.admin import register_admin_handlers
from handlers.direct import handle_direct_message, topic_callback
from handlers.link import handle_link, LINK_FILTER

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
# Telegram lib is verbose; quiet it down
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logger = logging.getLogger("forwarder")

# Only the update types our handlers actually consume. Restricting this
# shrinks every webhook payload Telegram sends us (they would otherwise
# ship my_chat_member, chat_member, edited_message, ... on every event).
ALLOWED_UPDATES = ["message", "callback_query"]

_STARTED_AT = time.time()


def _rss_mb() -> float:
    """Best-effort RSS memory in MB (for the /health endpoint and logs)."""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return -1.0


# ---------------------------------------------------------------------------
# Application lifecycle hooks (called by PTB in both polling and webhook mode)
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    """Called after the Application is fully initialised, before polling starts."""
    db: Database = app.bot_data["db"]
    await db.init()

    cfg: Config = app.bot_data["config"]

    # The destination group set via /setgroup lives in the DB. On Render the
    # disk is ephemeral, so on a fresh boot the DB is empty and the env var
    # (DESTINATION_GROUP_ID) seeds the value. But once the user has called
    # /setgroup, the DB value is authoritative and must WIN over a stale env
    # value (previously a restart silently reverted the destination back to
    # whatever the env var said — surprising and hard to debug).
    db_group_raw = await db.get_runtime("destination_group_id")
    if db_group_raw:
        try:
            cfg.destination_group_id = int(db_group_raw)
            logger.info("Destination group restored from DB: %d", cfg.destination_group_id)
        except ValueError:
            pass

    user_session: UserSession | None = None
    if cfg.has_user_session:
        user_session = UserSession(
            cfg.session_name, cfg.api_id, cfg.api_hash,
            session_string=cfg.session_string,
        )
        ok = await user_session.start()
        if ok:
            app.bot_data["user_session"] = user_session
        else:
            app.bot_data["user_session"] = None
            logger.warning(
                "Telethon session unavailable. Locked-channel forwarding and "
                "topic auto-discovery will not work until you log in."
            )
    else:
        app.bot_data["user_session"] = None
        logger.info("Telethon not configured (API_ID/API_HASH missing). "
                    "Only direct forwarding will work.")

    app.bot_data["topics"] = TopicManager(db, app.bot_data["user_session"])
    app.bot_data["started_at"] = _STARTED_AT  # for /status uptime display

    # After a restart on ephemeral storage the topic cache is empty. If we
    # have a Telethon session and a destination group, rebuild the cache in
    # the background so the topic picker works immediately — the user no
    # longer has to remember to run /refresh after every deploy.
    async def _background_topic_refresh() -> None:
        try:
            await asyncio.sleep(2)  # let startup finish first
            gid = cfg.destination_group_id
            if gid is None:
                return
            cached = await db.get_all_topics(gid)
            if cached:
                return  # cache survived (non-ephemeral disk) — nothing to do
            if not (user_session and user_session.available):
                return
            topics = await user_session.list_forum_topics(gid)
            if topics:
                await db.set_cached_topics(gid, topics)
                logger.info("Auto-refreshed %d forum topic(s) for %d",
                            len(topics), gid)
        except Exception:
            logger.debug("Background topic refresh failed", exc_info=True)

    asyncio.create_task(_background_topic_refresh())

    me = await app.bot.get_me()
    logger.info("Bot started: @%s (%d) mode=%s", me.username, me.id, cfg.mode)
    if cfg.destination_group_id:
        logger.info("Destination group: %d", cfg.destination_group_id)
    else:
        logger.info("Destination group not set yet — use /setgroup <group_id>")
    if cfg.mode == "webhook":
        if not cfg.webhook_url:
            logger.error("MODE=webhook but WEBHOOK_URL is not set! The bot will "
                         "start but Telegram cannot reach it.")
        else:
            logger.info("Webhook URL: %s/%s", cfg.webhook_url.rstrip('/'),
                        cfg.webhook_url_path)


async def post_stop(app: Application) -> None:
    db: Database = app.bot_data.get("db")
    if db:
        await db.close()
    us: UserSession | None = app.bot_data.get("user_session")
    if us:
        await us.stop()


# ---------------------------------------------------------------------------
# Application builder
# ---------------------------------------------------------------------------

def build_application(cfg: Config, db: Database) -> Application:
    app = (
        Application.builder()
        .token(cfg.bot_token)
        .post_init(post_init)
        .post_stop(post_stop)
        # Enable concurrent update processing. Default is 1 (sequential) which
        # means while one link is being fetched via Telethon (10-30 sec), the
        # second link waits in the queue and only processes AFTER the first
        # completes — the user sees the second link "ignored".
        #
        # 64 is plenty for a single-user bot and caps worst-case memory on
        # the 512 MB free instance (the previous value of 256 had no upside).
        .concurrent_updates(64)
        .build()
    )
    app.bot_data["config"] = cfg
    app.bot_data["db"] = db

    # Global error handler — any exception thrown by a handler that isn't
    # caught will land here. Without this, exceptions in handlers are
    # silently swallowed by PTB and the user sees no reply (the
    # "/setgroup does nothing" symptom).
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error if hasattr(context, "error") else None
        if error:
            logger.error("🚨 Unhandled exception in handler for update %s: %s",
                         update, type(error).__name__, exc_info=error)
        else:
            logger.error("🚨 Unhandled exception in handler for update %s (no error info)", update)

        # Try to notify the user that something went wrong
        try:
            if isinstance(update, Update) and update.effective_message:
                err_str = str(error)[:500] if error else "unknown error"
                await update.effective_message.reply_text(
                    f"Internal error: {err_str}\n\n"
                    f"Check the bot logs for details."
                )
        except Exception:
            pass  # don't recurse

    app.add_error_handler(on_error)

    register_admin_handlers(app)

    # IMPORTANT: both MessageHandlers MUST be in the same group with mutually
    # exclusive filters. In PTB v21, handlers in DIFFERENT groups all run,
    # which caused this bug:
    #   - User sends a t.me/c/... link
    #   - handle_link (group 1) runs -> fetches media via Telethon, shows picker
    #   - handle_direct_message (group 2) ALSO runs -> treats the link as
    #     plain text, shows a SECOND picker that just copy_message's the link
    #   - User taps the wrong picker -> bot forwards the link as text, not media
    #
    # By putting both handlers in the same group (0, the default), PTB stops
    # after the first match — only one of them runs per update.
    app.add_handler(
        MessageHandler(LINK_FILTER & ~filters.COMMAND, handle_link),
        group=0,
    )
    app.add_handler(
        MessageHandler(~LINK_FILTER & ~filters.COMMAND, handle_direct_message),
        group=0,
    )
    # Topic picker callback
    app.add_handler(CallbackQueryHandler(topic_callback, pattern=r"^(fwd|cancel):"))
    return app


# ---------------------------------------------------------------------------
# Custom aiohttp webhook server (Render / fps.ms / any cloud)
# ---------------------------------------------------------------------------

def _webhook_secret(cfg: Config) -> str:
    """Deterministic per-bot secret for Telegram's webhook secret_token.

    Derived from the bot token, so it is identical across restarts (Teletron
    rejects a changing secret) and never appears in the URL or logs.
    """
    return hashlib.sha256(("tg-whook:" + cfg.bot_token).encode()).hexdigest()[:48]


async def _run_webhook_custom(app: Application, cfg: Config) -> None:
    """Run the bot in webhook mode using a custom aiohttp server.

    Routes:
      * GET  /                      -> 200 JSON health (Render health check)
      * GET  /<bot-token-secret>    -> 200 OK (also passes health check)
      * POST /<bot-token-secret>    -> forwards to PTB's process_update
      * *                           -> 404

    Startup order (optimized for Render):
      1. Bind the HTTP port FIRST — Render's deploy health check passes
         immediately while the Telegram/Telethon handshakes are still running.
      2. Initialize PTB (network to Telegram).
      3. Run post_init manually (DB, Telethon, TopicManager).
      4. Register the webhook URL + secret token with Telegram.
      5. Start the app (update processing).
      6. Start the keep-alive self-ping + housekeeping tasks.
      7. Wait for SIGTERM/SIGINT (Render sends SIGTERM on deploys), then
         shut down cleanly.
    """
    from aiohttp import web

    if not cfg.webhook_url:
        raise RuntimeError(
            "MODE=webhook requires WEBHOOK_URL to be set (or deploy on "
            "Render, which injects RENDER_EXTERNAL_URL automatically). "
            "Set it to your service URL, e.g. "
            "https://your-service-name.onrender.com"
        )

    url_path = cfg.webhook_url_path
    full_webhook_url = f"{cfg.webhook_url.rstrip('/')}/{url_path}"
    secret_token = _webhook_secret(cfg)

    # CRITICAL: We must call post_init MANUALLY because we're bypassing PTB's
    # run_webhook() / run_polling(). In PTB v21, post_init is invoked by
    # run_polling/run_webhook but NOT by app.initialize() / app.start()
    # directly. We mirror PTB's official run_webhook order:
    #   initialize -> post_init -> set_webhook -> start -> run_forever

    state = {"ready": False}

    # ----- aiohttp handlers -----

    async def health_handler(request: web.Request) -> web.Response:
        """200 OK for Render / fps.ms / UptimeRobot health checks, with a
        tiny bit of operational info (no secrets)."""
        body = (
            f"OK\n"
            f"uptime_sec: {int(time.time() - _STARTED_AT)}\n"
            f"ready: {state['ready']}\n"
            f"rss_mb: {_rss_mb():.0f}\n"
        )
        return web.Response(text=body, status=200,
                            headers={"Content-Type": "text/plain"})

    async def webhook_handler(request: web.Request) -> web.Response:
        """Receive an update from Telegram, dispatch to PTB's process_update."""
        if request.path != f"/{url_path}":
            return web.Response(status=404)

        # Validate Telegram's secret token header. Telegram includes it on
        # every real update; a mismatch means a spoofed request.
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret_token:
            logger.warning("webhook_handler: invalid/missing secret token from %s",
                           request.remote)
            return web.Response(status=401, text="Unauthorized")

        # If PTB isn't finished starting yet (first seconds after a deploy),
        # tell Telegram to retry shortly instead of dropping the update.
        if not state["ready"]:
            return web.Response(status=503, text="Starting")

        try:
            data = await request.json()
        except Exception:
            logger.warning("webhook_handler: invalid JSON body")
            return web.Response(status=400, text="Bad request")
        try:
            update = Update.de_json(data, app.bot)
            if update is None:
                logger.warning("webhook_handler: Update.de_json returned None")
                return web.Response(status=400, text="Could not parse update")
            logger.debug("📨 update_id=%s", data.get("update_id", "?"))
            # Schedule the update in the background — return 200 immediately
            # so Telegram doesn't think the bot is slow.
            task = asyncio.create_task(app.process_update(update))
            # Surface exceptions from the fire-and-forget task instead of
            # silently losing them ("Task exception was never retrieved").
            def _log_task_exception(t: asyncio.Task) -> None:
                if not t.cancelled() and t.exception():
                    logger.error("process_update task failed", exc_info=t.exception())
            task.add_done_callback(_log_task_exception)
        except Exception:
            logger.exception("webhook_handler: failed to dispatch update")
            # Telegram expects 200 even on error, otherwise it retries forever
            return web.Response(status=200, text="OK (handler error)")
        return web.Response(status=200, text="OK")

    # ----- step 1: bind the port BEFORE any Telegram handshake -----

    web_app = web.Application(client_max_size=10 * 1024 * 1024)  # 10 MB
    web_app.router.add_get("/", health_handler)
    # aiohttp automatically registers HEAD alongside GET, so we don't need
    # an explicit add_head call.
    web_app.router.add_get(f"/{url_path}", health_handler)  # GET passes health check too
    web_app.router.add_post(f"/{url_path}", webhook_handler)

    runner = web.AppRunner(web_app, access_log=None)  # access_log=None to keep logs clean
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.port)
    await site.start()
    logger.info("Health server listening on 0.0.0.0:%d (binding port first so "
                "Render's health check passes during Telegram handshake)", cfg.port)

    # ----- register SIGTERM/SIGINT EARLY -----
    # Render sends SIGTERM on every deploy/restart — including while this
    # instance is still booting. Registering the handlers right after the
    # port bind (instead of after the Telegram handshake) keeps even a HUNG
    # startup interruptible: the stop event is set immediately and the
    # cleanup path runs as soon as the current startup step yields.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover — non-Unix
            pass

    # Generous per-phase timeout: if Telegram/Telethon handshake hangs (e.g.
    # a network partition), crash fast with a clear error instead of hanging
    # forever — Render's supervisor then restarts the service.
    STARTUP_TIMEOUT = 90  # seconds

    # ----- step 2-5: initialize PTB, post_init, set_webhook, start -----

    try:
        logger.info("[2/5] Initializing PTB application...")
        await asyncio.wait_for(app.initialize(), timeout=STARTUP_TIMEOUT)

        logger.info("[3/5] Running post_init (DB init, Telethon, TopicManager)...")
        await asyncio.wait_for(post_init(app), timeout=STARTUP_TIMEOUT)

        if stop_event.is_set():
            logger.info("Shutdown requested during startup — skipping registration")
        else:
            logger.info("[4/5] Registering webhook URL with Telegram: %s", full_webhook_url)
            await asyncio.wait_for(
                app.bot.set_webhook(
                    url=full_webhook_url,
                    allowed_updates=ALLOWED_UPDATES,
                    drop_pending_updates=False,  # keep pending updates — they'll arrive
                    secret_token=secret_token,   # Telegram echoes this back on every POST
                ),
                timeout=STARTUP_TIMEOUT,
            )
            logger.info("✓ Webhook registered with Telegram (secret token enabled)")

            logger.info("[5/5] Starting Application (update processing)...")
            await asyncio.wait_for(app.start(), timeout=STARTUP_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error("Startup timed out after %ds during the Telegram "
                     "handshake — exiting so the platform can restart me",
                     STARTUP_TIMEOUT)
        await site.stop()
        await runner.cleanup()
        try:
            await post_stop(app)
        except Exception:
            pass
        raise SystemExit(1)

    state["ready"] = True
    logger.info("Custom webhook server ready on 0.0.0.0:%d", cfg.port)
    logger.info("Health check endpoint: GET /  -> 200 OK")
    logger.info("Webhook endpoint:      POST /%s  -> forwards to PTB", url_path)

    # ----- background tasks: keep-alive self-ping + hourly DB housekeeping -----

    background_tasks: list[asyncio.Task] = []

    if cfg.keep_alive:
        async def keep_alive_loop() -> None:
            """Self-ping the PUBLIC url so Render counts inbound traffic and
            never spins the free service down (sleeps after 15 min idle).

            The request must go through Render's edge router (not localhost)
            to count, hence we ping the public URL. Keep-alive can also be
            provided by an external pinger (UptimeRobot, cron-job.org) — set
            KEEP_ALIVE=false if you prefer that.
            """
            import aiohttp as _aiohttp
            url = cfg.webhook_url.rstrip("/")
            interval = cfg.keep_alive_interval
            logger.info("Keep-alive enabled: GET %s every %ds", url, interval)
            async with _aiohttp.ClientSession(
                timeout=_aiohttp.ClientTimeout(total=30),
            ) as session:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        async with session.get(url) as resp:
                            logger.debug("keep-alive ping: %s %s", url, resp.status)
                    except Exception as e:
                        # Non-fatal: log and try again next interval.
                        logger.warning("keep-alive ping failed: %s", type(e).__name__)

        background_tasks.append(asyncio.create_task(keep_alive_loop()))

    async def housekeeping_loop() -> None:
        """Hourly purge of abandoned pending forwards (pickers never tapped).
        Keeps the SQLite table from growing without bound on long-lived
        instances."""
        db: Database = app.bot_data["db"]
        while True:
            await asyncio.sleep(3600)
            try:
                purged = await db.purge_old_pending(max_age_seconds=3600)
                if purged:
                    logger.info("Housekeeping: purged %d stale pending forward(s)", purged)
            except Exception:
                logger.debug("housekeeping purge failed", exc_info=True)

    background_tasks.append(asyncio.create_task(housekeeping_loop()))

    # ----- wait for SIGTERM / SIGINT (handlers registered right after the
    # port bind, so a hung startup stays interruptible too) -----

    try:
        await stop_event.wait()
        logger.info("Shutdown signal received — draining...")
    finally:
        logger.info("Shutting down webhook server...")
        for t in background_tasks:
            t.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await site.stop()
        await runner.cleanup()
        # NOTE: we deliberately do NOT delete the webhook here — on Render a
        # NEW instance is already registering the webhook during a deploy, and
        # a delete from the OLD instance racing it would leave the bot
        # unreachable. The next boot re-registers the webhook anyway.
        try:
            await app.stop()
        except Exception:
            logger.debug("app.stop() failed", exc_info=True)
        try:
            await post_stop(app)
        except Exception:
            logger.exception("post_stop failed")
        try:
            await app.shutdown()
        except Exception:
            logger.debug("app.shutdown() failed", exc_info=True)
        logger.info("Shutdown complete (uptime %.0f sec)", time.time() - _STARTED_AT)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = Config.load()

    # Apply the configured log level after basicConfig
    logging.getLogger().setLevel(cfg.log_level)
    logging.getLogger("forwarder").setLevel(cfg.log_level)

    db = Database(cfg.db_path)

    app = build_application(cfg, db)
    logger.info("Starting bot in %s mode...", cfg.mode)

    if cfg.mode == "webhook":
        # Use our custom aiohttp server (Render / fps.ms / etc.)
        # We use asyncio.run because we need finer control over the lifecycle
        # than PTB's run_webhook provides.
        try:
            asyncio.run(_run_webhook_custom(app, cfg))
        except KeyboardInterrupt:
            logger.info("Interrupted")
        except Exception:
            logger.exception("Webhook mode crashed")
            sys.exit(1)
    else:
        # Polling mode (local dev) — use PTB's built-in run_polling
        app.run_polling(allowed_updates=ALLOWED_UPDATES)


if __name__ == "__main__":
    main()
