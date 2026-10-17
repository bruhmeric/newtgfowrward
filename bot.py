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
  * GET / serves an HTML status dashboard (status badge, startup phase,
    last error + how to fix it, memory usage, env-var checklist, Telegram
    webhook diagnostics). A broken deploy is now diagnosable straight from
    the browser instead of digging through Render's log console.
  * Startup failures (bad BOT_TOKEN, network blip, ...) never exit(1):
    the process stays up, shows the error on the dashboard + /health, and
    retries with backoff (5 s growing to 5 min). Previously any startup
    exception caused a Render crash-restart loop that always showed
    ready: False with the real error buried in the Logs tab.
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
import html as _html
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
# Shared runtime status + dashboard rendering
# ---------------------------------------------------------------------------

# Written by post_init and the webhook server loop, read by the /health and
# dashboard handlers. Kept at module level so post_init (which PTB calls)
# can populate it without threading a reference through Application.
_STATE: dict[str, Any] = {
    "ready": False,
    "phase": "1/5 bind-port",
    "error": None,      # last startup error, e.g. "InvalidToken: ..."
    "hint": None,       # human-readable fix for that error
    "attempts": 0,      # startup attempts this boot
    "bot_username": None,
    "webhook_registered": False,
    "user_session": "not configured",
    "wh_info": None,    # cached Telegram get_webhook_info payload
    "wh_info_ts": 0.0,
}


def _scrub(cfg: Config, text: Any) -> str:
    """Stringify + strip secrets (bot token, session string) from any text
    that will be shown on the dashboard or /health endpoint."""
    out = str(text)
    secrets = [cfg.bot_token]
    if ":" in cfg.bot_token:
        # the secret half of the token is also the webhook URL path
        secrets.append(cfg.bot_token.split(":", 1)[1])
    if cfg.session_string:
        secrets.append(cfg.session_string)
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


def _startup_hint(exc: BaseException) -> str:
    """Map a startup exception to a plain-English fix suggestion."""
    name = type(exc).__name__
    msg = str(exc)
    if name in ("InvalidToken", "Unauthorized", "NotFound") or "Unauthorized" in msg:
        return ("Telegram rejected BOT_TOKEN. Open @BotFather in Telegram, check the "
                "token for this bot (send /mybots), then update BOT_TOKEN in Render "
                "(your service -> Environment) and redeploy. If you revoked the old "
                "token, it is permanently dead — use the new one it gave you.")
    if "Conflict" in msg or "terminated by other getUpdates" in msg:
        return ("Another instance is running with this bot token (a local copy or a "
                "second deployment). Stop the other instance, or give this "
                "deployment its own bot token from @BotFather.")
    if name in ("NetworkError", "TimedOut", "TimeoutError") or "timed out" in msg.lower():
        return ("Could not reach api.telegram.org — usually transient. The bot "
                "retries automatically; no action needed.")
    if "Bad Request" in msg and "webhook" in msg.lower():
        return ("Telegram rejected the webhook URL. If you set WEBHOOK_URL, it must "
                "be a full HTTPS URL like https://your-app.onrender.com. On Render "
                "you can leave it unset — the platform URL is detected automatically.")
    return ("Check the service logs for the full traceback: Render -> your service "
            "-> Logs.")


_DASHBOARD_CSS = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         background:#0e1117; color:#e6edf3; padding:24px; }
  header { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:18px; }
  h1 { font-size:20px; margin:0; }
  h2 { font-size:15px; margin:0 0 10px; }
  .badge { padding:4px 12px; border-radius:999px; font-size:12px; font-weight:700; letter-spacing:.5px; }
  .badge.ok { background:#0f2e1d; color:#3fb950; }
  .badge.warn { background:#2d2200; color:#d29922; }
  .badge.err { background:#3a0d0d; color:#f85149; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:12px; }
  .card { background:#161b26; border:1px solid #21262d; border-radius:10px; padding:12px 14px; min-width:0; }
  .card .label { font-size:11px; text-transform:uppercase; letter-spacing:.8px; color:#8b949e; margin-bottom:6px; }
  .card .value { font-size:16px; font-weight:600; word-break:break-word; }
  .value.ok { color:#3fb950; }
  .value.warn { color:#d29922; }
  .dim { color:#8b949e; font-weight:400; font-size:12px; }
  .errorbox { background:#20070a; border:1px solid #5a1218; border-radius:10px; padding:14px 16px; margin-bottom:18px; }
  .errorbox pre { margin:0 0 10px; white-space:pre-wrap; word-break:break-all; color:#ff8a8a; font-size:13px; }
  section.env { margin-top:22px; }
  table { border-collapse:collapse; width:100%; background:#161b26; border:1px solid #21262d;
          border-radius:10px; overflow:hidden; font-size:13px; }
  th, td { text-align:left; padding:8px 12px; border-bottom:1px solid #21262d; }
  tr:last-child td { border-bottom:none; }
  th { color:#8b949e; font-size:11px; text-transform:uppercase; letter-spacing:.8px; }
  td.ok { color:#3fb950; font-weight:600; }
  td.miss { color:#f85149; font-weight:700; }
  td.opt { color:#8b949e; }
  footer { margin-top:22px; color:#8b949e; font-size:12px; line-height:1.7; }
  code { background:#21262d; padding:1px 6px; border-radius:5px; font-size:12px; }
"""


def _render_dashboard(cfg: Config, state: dict) -> str:
    """Self-contained HTML status page served at GET / — no external assets,
    one request per auto-refresh, secrets scrubbed."""
    esc = _html.escape
    ready = bool(state.get("ready"))
    error = state.get("error")

    if ready:
        badge, badge_cls = "READY", "ok"
    elif error:
        badge, badge_cls = "STARTUP ERROR", "err"
    else:
        badge, badge_cls = "STARTING", "warn"

    uptime = int(time.time() - _STARTED_AT)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    uptime_str = f"{h}h {m:02d}m {s:02d}s"

    rss = _rss_mb()
    rss_pct = max(0.0, min(100.0, rss / 512.0 * 100.0)) if rss > 0 else 0.0

    def card(label: str, value_html: str) -> str:
        return (f'<div class="card"><div class="label">{esc(label)}</div>'
                f'<div class="value">{value_html}</div></div>')

    cards: list[str] = [
        card("Uptime", esc(uptime_str)),
        card("Memory", f"{rss:.0f} MB <span class='dim'>of 512 MB ({rss_pct:.0f}%)</span>"),
        card("Mode", esc(cfg.mode)),
    ]
    if ready:
        bot_name = f"@{state['bot_username']}" if state.get("bot_username") else "—"
        cards.append(card("Bot", esc(bot_name)))
        cards.append(card(
            "Webhook",
            '<span class="ok">registered</span>' if state.get("webhook_registered")
            else '<span class="warn">not registered</span>'))
    else:
        cards.append(card("Startup phase", esc(str(state.get("phase", "?")))))
        cards.append(card("Startup attempt", esc(str(state.get("attempts", "?")))))

    us_status = str(state.get("user_session", "not configured"))
    us_cls = "ok" if us_status.startswith("connected") else "warn"
    cards.append(card("Telethon session", f'<span class="{us_cls}">{esc(us_status)}</span>'))
    cards.append(card(
        "Destination group",
        esc(str(cfg.destination_group_id)) if cfg.destination_group_id
        else "<span class='dim'>not set — use /setgroup</span>"))

    wh = state.get("wh_info") or {}
    if ready and wh:
        pending = wh.get("pending")
        if pending is not None:
            cards.append(card(
                "Pending updates",
                esc(str(pending)) if not pending
                else f"<span class='warn'>{esc(str(pending))}</span>"))
        last_err = str(wh.get("last_error") or "")
        if last_err:
            cards.append(card("Last delivery error",
                              f"<span class='warn'>{esc(last_err[:180])}</span>"))

    error_html = ""
    if error:
        error_html = (
            '<section class="errorbox">'
            f'<h2>Startup error — attempt {esc(str(state.get("attempts", "?")))}</h2>'
            f'<pre>{esc(_scrub(cfg, error))}</pre>'
            f'<p><b>How to fix:</b> {esc(state.get("hint") or "See the service logs.")}</p>'
            '<p class="dim">The bot keeps retrying automatically. Fix the '
            'environment variable and the next attempt will come up — or '
            'restart the service from the Render dashboard.</p>'
            '</section>'
        )

    def env_row(name: str, present: bool, note: str, optional: bool = False) -> str:
        # Required vars show green "set" / red "MISSING"; optional vars show a
        # neutral "optional" when absent so the checklist doesn't cry wolf.
        if optional and not present:
            cls, status = "opt", "optional"
        else:
            cls = "ok" if present else "miss"
            status = "set" if present else "MISSING"
        return (f'<tr><td>{esc(name)}</td><td class="{cls}">{status}</td>'
                f'<td><span class="dim">{esc(note)}</span></td></tr>')

    env_rows = "".join([
        env_row("BOT_TOKEN", True, "from @BotFather — required"),
        env_row("API_ID", cfg.api_id is not None, "required for locked-channel forwarding"),
        env_row("API_HASH", bool(cfg.api_hash), "required for locked-channel forwarding"),
        env_row("SESSION_STRING", bool(cfg.session_string), "generate with: python login.py"),
        env_row("DESTINATION_GROUP_ID", cfg.destination_group_id is not None,
                f"current value: {cfg.destination_group_id}" if cfg.destination_group_id
                else "or set later via /setgroup", optional=True),
        env_row("ADMIN_IDS", bool(cfg.admin_ids),
                f"{len(cfg.admin_ids)} admin(s) — empty means anyone can use the bot",
                optional=True),
        env_row("WEBHOOK_URL / RENDER_EXTERNAL_URL", bool(cfg.webhook_url),
                "auto-detected on Render" if cfg.webhook_url else "not detected"),
        env_row("KEEP_ALIVE", True,
                (f"on — self-ping every {cfg.keep_alive_interval}s prevents Render sleep")
                if cfg.keep_alive else "off — service may sleep after 15 min idle"),
    ])

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="refresh" content="5">'
        '<title>Forwarder Bot — Status</title>'
        f'<style>{_DASHBOARD_CSS}</style></head><body>'
        '<header><h1>Telegram Forwarder Bot</h1>'
        f'<span class="badge {badge_cls}">{badge}</span>'
        '<span class="dim">refreshes every 5 s</span></header>'
        f'{error_html}'
        f'<div class="grid">{"".join(cards)}</div>'
        '<section class="env"><h2>Environment variables (Render → your service → Environment)</h2>'
        f'<table><tr><th>Variable</th><th>Status</th><th>Notes</th></tr>{env_rows}</table></section>'
        '<footer>After changing an environment variable, redeploy or restart the service '
        'from the Render dashboard so the new value is picked up.<br>'
        'Machine-readable status: <code>/health</code>. If the service slept (free tier), '
        'opening this page woke it up.</footer>'
        '</body></html>'
    )


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
            _STATE["user_session"] = f"connected as {user_session.connected_as or '?'}"
        else:
            app.bot_data["user_session"] = None
            _STATE["user_session"] = (
                f"not connected — {user_session.last_error or 'unknown reason'}")
            logger.warning(
                "Telethon session unavailable (%s). Locked-channel forwarding and "
                "topic auto-discovery will not work until you log in.",
                user_session.last_error,
            )
    else:
        app.bot_data["user_session"] = None
        _STATE["user_session"] = "not configured (set API_ID + API_HASH + SESSION_STRING)"
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
    _STATE["bot_username"] = me.username
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
      * GET  /                      -> HTML status dashboard (browser-facing)
      * GET  /health                -> plain-text health (Render / monitoring)
      * GET  /<bot-token-secret>    -> 200 OK (also passes health checks)
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

    If any of steps 2-5 fails, the process does NOT exit (that caused a
    Render crash-restart loop where the dashboard always showed
    ready: False): the error + a fix hint are recorded and shown on the
    dashboard and /health, partial state is torn down, and startup retries
    with backoff (5 s -> 5 min) until it succeeds or SIGTERM arrives.
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

    state = _STATE  # shared with post_init and the dashboard renderer
    state.update({
        "ready": False, "phase": "1/5 bind-port", "error": None, "hint": None,
        "attempts": 0, "bot_username": None, "webhook_registered": False,
        "user_session": "not configured", "wh_info": None, "wh_info_ts": 0.0,
    })

    async def refresh_webhook_info(force: bool = False) -> None:
        """Fetch Telegram's own view of our webhook (pending updates, last
        delivery error) for the dashboard. Cached 15 s so the dashboard's
        auto-refresh never hammers the Telegram API."""
        now = time.time()
        if not force and state["wh_info"] is not None and now - state["wh_info_ts"] < 15:
            return
        try:
            info = await asyncio.wait_for(app.bot.get_webhook_info(), timeout=6)
            state["wh_info"] = {
                "pending": info.pending_update_count,
                "last_error": _scrub(cfg, info.last_error_message)
                              if info.last_error_message else "",
                "last_error_date": info.last_error_date,
            }
        except Exception as e:
            state["wh_info"] = {
                "pending": None,
                "last_error": f"unavailable ({type(e).__name__})",
                "last_error_date": None,
            }
        state["wh_info_ts"] = now

    # ----- aiohttp handlers -----

    async def health_handler(request: web.Request) -> web.Response:
        """Plain-text health for Render / fps.ms / UptimeRobot checks and for
        curl. Includes the failing phase and error when the bot isn't up, so
        a broken deploy is diagnosable straight from the URL alone."""
        body = (
            f"OK\n"
            f"uptime_sec: {int(time.time() - _STARTED_AT)}\n"
            f"ready: {state['ready']}\n"
        )
        if not state["ready"]:
            body += f"phase: {state['phase']}\n"
            if state["error"]:
                body += f"error: {_scrub(cfg, state['error'])}\n"
                body += f"hint: {state['hint']}\n"
        body += f"rss_mb: {_rss_mb():.0f}\n"
        return web.Response(text=body, status=200, content_type="text/plain")

    async def dashboard_handler(request: web.Request) -> web.Response:
        """HTML status dashboard at the service root — visiting the deployed
        URL in a browser now shows what the bot is doing instead of a bare
        health string."""
        if state["ready"]:
            try:
                await refresh_webhook_info()
            except Exception:
                pass  # dashboard works fine without webhook diagnostics
        return web.Response(text=_render_dashboard(cfg, state),
                            status=200, content_type="text/html")

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
    web_app.router.add_get("/", dashboard_handler)
    web_app.router.add_get("/health", health_handler)
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
    # a network partition), the attempt fails fast with a clear error and is
    # retried below — the HTTP server stays up the whole time.
    STARTUP_TIMEOUT = 90  # seconds
    # Backoff between startup attempts: quick retries for transient network
    # blips, then settle at one attempt every 5 minutes. We deliberately do
    # NOT exit(1) on failure: that made Render restart the container in a
    # crash loop where every visit showed ready: False and the actual error
    # was buried in the Logs tab. Now the process stays up, the dashboard
    # at / shows the error + fix, and startup keeps retrying in background.
    RETRY_DELAYS = [5, 15, 30, 60, 120, 300]

    async def _teardown_attempt() -> None:
        """Best-effort cleanup of a failed attempt so the next one starts
        clean. PTB's initialize() is a no-op when already initialized and
        shutdown() resets that flag; the Telethon client must be explicitly
        disconnected; the DB stays open (its init() is idempotent)."""
        us = app.bot_data.get("user_session")
        if us is not None:
            try:
                await us.stop()
            except Exception:
                pass
            app.bot_data["user_session"] = None
        try:
            if app.running:
                await app.stop()
        except Exception:
            pass
        try:
            await app.shutdown()
        except Exception:
            pass
        state["webhook_registered"] = False

    # ----- step 2-5: initialize PTB, post_init, set_webhook, start -----
    # Retries forever (with backoff) while the HTTP server keeps serving
    # the dashboard + health endpoints, so a bad env var never crash-loops.

    attempt = 0
    while not stop_event.is_set():
        attempt += 1
        state["attempts"] = attempt
        state["error"] = None
        state["hint"] = None
        try:
            state["phase"] = "2/5 connect-telegram"
            logger.info("[2/5] Initializing PTB application (attempt %d)...", attempt)
            await asyncio.wait_for(app.initialize(), timeout=STARTUP_TIMEOUT)

            state["phase"] = "3/5 setup-storage"
            logger.info("[3/5] Running post_init (DB init, Telethon, TopicManager)...")
            await asyncio.wait_for(post_init(app), timeout=STARTUP_TIMEOUT)

            if stop_event.is_set():
                logger.info("Shutdown requested during startup — skipping registration")
                break

            state["phase"] = "4/5 register-webhook"
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
            state["webhook_registered"] = True
            logger.info("✓ Webhook registered with Telegram (secret token enabled)")

            state["phase"] = "5/5 start-processing"
            logger.info("[5/5] Starting Application (update processing)...")
            await asyncio.wait_for(app.start(), timeout=STARTUP_TIMEOUT)

            state["ready"] = True
            state["phase"] = "running"
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
            state["hint"] = _startup_hint(exc)
            logger.error("Startup attempt %d failed at phase '%s': %s",
                         attempt, state["phase"], _scrub(cfg, state["error"]))
            logger.error("How to fix: %s", state["hint"])
            await _teardown_attempt()
            if stop_event.is_set():
                break
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
            logger.warning("Retrying startup in %d s — the dashboard at / shows "
                           "the error in the meantime", delay)
            try:
                # Interruptible backoff: SIGTERM during the wait exits the loop
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                continue

    if state["ready"]:
        logger.info("Custom webhook server ready on 0.0.0.0:%d", cfg.port)
        logger.info("Dashboard:         GET  /         -> status page")
        logger.info("Health check:      GET  /health   -> 200 OK")
        logger.info("Webhook endpoint:  POST /%s  -> forwards to PTB", url_path)
        try:
            await refresh_webhook_info(force=True)
        except Exception:
            pass
    else:
        logger.error("Shutdown requested before startup succeeded — exiting")

    # ----- background tasks: keep-alive self-ping + hourly DB housekeeping -----
    # Only started once the bot is actually up (a broken config should not
    # keep a dead service awake and burning free-tier instance hours).

    background_tasks: list[asyncio.Task] = []

    if state["ready"] and cfg.keep_alive:
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
        instances. Also refreshes the webhook diagnostics shown on the
        dashboard (pending updates / last delivery error)."""
        db: Database = app.bot_data["db"]
        while True:
            await asyncio.sleep(3600)
            try:
                purged = await db.purge_old_pending(max_age_seconds=3600)
                if purged:
                    logger.info("Housekeeping: purged %d stale pending forward(s)", purged)
                await refresh_webhook_info(force=True)
            except Exception:
                logger.debug("housekeeping failed", exc_info=True)

    if state["ready"]:
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
