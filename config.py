"""Configuration loader — reads .env / environment and exposes typed settings.

Supports two run modes:
  * MODE=polling  (default, for local dev) — bot uses long-polling
  * MODE=webhook  (recommended for Render) — bot serves a webhook on $PORT

For Render:
  * Render injects PORT automatically — the bot listens on 0.0.0.0:$PORT
  * The Telethon session must be a StringSession stored in SESSION_STRING,
    because Render's free tier filesystem is ephemeral (the .session file
    would be lost on every restart)
  * Render also injects RENDER_EXTERNAL_URL (https://<service>.onrender.com)
    for web services — we use it as the webhook URL when WEBHOOK_URL is not
    set, so the FIRST deploy succeeds without the old "deploy once, set the
    URL, redeploy" dance.
  * KEEP_ALIVE (default: on in webhook mode) makes the bot ping its own
    public URL every KEEP_ALIVE_INTERVAL seconds so Render's free tier
    (which sleeps a service after 15 min without inbound traffic) keeps the
    service awake. Set KEEP_ALIVE=false to opt out (e.g. if you prefer an
    external pinger such as UptimeRobot, or want the service to sleep).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv is optional; environment may already be set
    pass


def _int_list(value: str) -> list[int]:
    """Parse a comma-separated list of ints, skipping invalid entries
    instead of crashing the whole bot on a typo."""
    result: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            # Skip malformed entries rather than raising — a bad ADMIN_IDS
            # used to crash Config.load() before the bot could even log.
            continue
    return result


def _file_session_has_auth(path: str) -> bool:
    """True if a Telethon .session file exists AND contains an auth key.

    The repo ships an EMPTY placeholder user_session.session — on a fresh
    clone (or a Render deploy, where the disk is ephemeral) that file has no
    auth key, so it must NOT count as "a session is configured". Otherwise
    the bot would try to connect with it on every boot and burn CPU on a
    guaranteed-to-fail login check.
    """
    if not os.path.exists(path):
        return False
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT auth_key FROM sessions").fetchone()
        finally:
            con.close()
        return bool(row and row[0])
    except Exception:
        return False


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class Config:
    bot_token: str
    api_id: Optional[int]
    api_hash: Optional[str]
    phone: Optional[str]
    destination_group_id: Optional[int]
    admin_ids: list[int] = field(default_factory=list)
    session_name: str = "user_session"
    db_path: str = "forwarder.db"
    # --- deployment mode ---
    mode: str = "polling"  # "polling" | "webhook"
    webhook_url: Optional[str] = None
    port: int = 8080
    session_string: Optional[str] = None
    # --- free-tier keep-alive ---
    keep_alive: bool = True            # self-ping to avoid Render sleeping
    keep_alive_interval: int = 540     # seconds (9 min < Render's 15-min limit)
    # --- forward cleaning (/strip) ---
    # Env defaults only — the /strip command flips them at runtime and the
    # DB value wins over these on every boot.
    strip_captions: bool = False       # STRIP_CAPTIONS=true -> media forwards lose captions
    strip_links: bool = False          # STRIP_LINKS=true -> URLs removed from forwarded text
    # --- misc tuning ---
    log_level: str = "INFO"
    # Non-fatal problems noticed while loading env vars (bad numbers etc.) —
    # surfaced on the web dashboard so a typo doesn't turn into a mystery.
    load_issues: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Config":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN missing — copy .env.example to .env and fill it in.")

        issues: list[str] = []

        api_id: Optional[int] = None
        api_id_raw = os.environ.get("API_ID", "").strip()
        if api_id_raw:
            try:
                api_id = int(api_id_raw)
            except ValueError:
                # A non-numeric API_ID used to crash Config.load() before the
                # bot could even boot — turning a typo into a Render crash
                # loop. Now it disables the user session with a clear reason.
                issues.append(
                    "API_ID is set but not a number — get the numeric value from "
                    "my.telegram.org -> API development tools.")
        api_hash = os.environ.get("API_HASH", "").strip() or None
        phone = os.environ.get("PHONE", "").strip() or None

        dest: Optional[int] = None
        dest_raw = os.environ.get("DESTINATION_GROUP_ID", "").strip()
        if dest_raw:
            try:
                dest = int(dest_raw)
            except ValueError:
                issues.append(
                    f"DESTINATION_GROUP_ID is set but not a number (got {dest_raw!r}) "
                    "— ignoring it. Group IDs look like -1001234567890.")

        admin_raw = os.environ.get("ADMIN_IDS", "").strip()
        admins = _int_list(admin_raw) if admin_raw else []

        mode_raw = os.environ.get("MODE", "polling").strip().lower()
        mode = mode_raw if mode_raw in ("polling", "webhook") else "polling"

        # WEBHOOK_URL takes precedence; fall back to the URL Render injects
        # into every web service (RENDER_EXTERNAL_URL) so the first deploy
        # works out of the box with zero manual steps.
        webhook_url = (
            os.environ.get("WEBHOOK_URL", "").strip()
            or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
            or None
        )
        if webhook_url and webhook_url.endswith("/"):
            webhook_url = webhook_url.rstrip("/") or None

        port_raw = os.environ.get("PORT", "").strip()
        port = 8080
        if port_raw:
            try:
                port = int(port_raw)
            except ValueError:
                issues.append(f"PORT is set but not a number (got {port_raw!r}) — "
                              "falling back to 8080.")

        session_string = os.environ.get("SESSION_STRING", "").strip() or None

        keep_alive_interval = 540
        kai_raw = os.environ.get("KEEP_ALIVE_INTERVAL", "").strip()
        if kai_raw:
            try:
                # Clamp to a sane range: never above 14 min (Render sleeps at
                # 15), never below 60 s (would be pure waste).
                keep_alive_interval = max(60, min(int(kai_raw), 840))
            except ValueError:
                pass

        log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            log_level = "INFO"

        return cls(
            bot_token=token,
            api_id=api_id,  # already parsed (None + a load_issue if invalid)
            api_hash=api_hash or None,
            phone=phone,
            destination_group_id=dest,
            admin_ids=admins,
            session_name=os.environ.get("SESSION_NAME", "user_session") or "user_session",
            db_path=os.environ.get("DB_PATH", "forwarder.db") or "forwarder.db",
            mode=mode,
            webhook_url=webhook_url,
            port=port,
            session_string=session_string,
            keep_alive=_env_bool("KEEP_ALIVE", default=(mode == "webhook")),
            keep_alive_interval=keep_alive_interval,
            strip_captions=_env_bool("STRIP_CAPTIONS", default=False),
            strip_links=_env_bool("STRIP_LINKS", default=False),
            log_level=log_level,
            load_issues=issues,
        )

    @property
    def has_user_session(self) -> bool:
        # A usable session needs api credentials AND an actual session: a
        # SESSION_STRING, or a file session that really contains an auth key
        # (the repo's placeholder user_session.session does not). Previously
        # this returned True with only API_ID/API_HASH set, so the bot kept
        # trying to "connect" a session that could never exist.
        if not (self.api_id and self.api_hash):
            return False
        return bool(
            self.session_string
            or _file_session_has_auth(f"{self.session_name}.session")
        )

    def is_admin(self, user_id: int) -> bool:
        if not self.admin_ids:
            # No whitelist configured -> allow anyone (single-user self-hosted bot)
            return True
        return user_id in self.admin_ids

    @property
    def webhook_url_path(self) -> str:
        """Path component of the webhook URL — uses the secret part of the
        bot token so the webhook endpoint is not easily guessable."""
        return self.bot_token.split(":")[-1]
