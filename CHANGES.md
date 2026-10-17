# Changelog — Render Free-Tier Optimization Release

This release fixes several real bugs, hardens the webhook server, and tunes
the bot specifically for Render's free tier (512 MB RAM / 0.1 CPU / 15-min
sleep / ephemeral disk). All changes are covered by `smoke_test.py`
(29 checks) and `webhook_e2e_test.py` (11 checks) — run both locally with
`pip install -r requirements.txt` first.

---

## Critical bug fixes

### 1. Bot crashed at startup on fresh installs (Telethon import)
`user_session.py` imported `GetForumTopicsRequest` from
`telethon.tl.functions.messages`, but in Telethon ≥ 1.37 that request moved
to `telethon.tl.functions.channels`. Because `requirements.txt` only pinned
`Telethon>=1.36.0`, every fresh install pulled the latest Telethon and the
bot **crashed on boot** with `ImportError` before ever logging in. The
import now tries the new location first and falls back to the old one, and
all dependencies are pinned exactly. The request parameter also changed
(`peer` → `channel`), so the call now passes the peer positionally.

### 2. Albums silently degraded to a single item
When you linked a post that belonged to an album, `fetch_message` looked
for siblings among the **20 most recent messages of the channel**
(`get_messages(entity, limit=20)`). For any post older than ~20 messages
from the top of the channel, no siblings were ever found — you got 1 photo
out of a 9-photo album. The fix fetches a window of ids **around the target
message** (±10 ids, covering Telegram's 10-item album limit), so albums are
found regardless of the channel's size or the post's age.

### 3. Destination group silently reverted after restart
`/setgroup` wrote the destination to SQLite, but on every boot the env var
`DESTINATION_GROUP_ID` took precedence. If you changed the destination via
`/setgroup` after deploying, a Render restart silently reverted it to the
stale env value. Now the DB value (if present) wins at startup; the env var
only seeds the very first boot.

### 4. Malformed `ADMIN_IDS` crashed the whole bot
`Config.load()` called `int()` on every comma-separated entry — a typo like
`ADMIN_IDS=12345, oops` raised `ValueError` before the bot could even log.
Bad entries are now skipped with the rest of the list still honored.

### 5. Inline keyboard could exceed Telegram's 100-button limit
A forum destination with more than ~100 topics made `send_message` fail
with `Bad Request: too many buttons` — the picker never appeared. The
keyboard is now capped at 96 topic buttons plus a note about hidden topics.

### 6. Dead code and a fragile `NameError` dance
`handlers/link.py` carried ~130 lines of unused download helpers (a leftover
from the pre-hybrid flow), and `_forward_album` used
`try: _ = custom_caption except NameError:` to check a variable's
existence. Both cleaned up.

---

## Render free-tier optimizations

### Keep-alive self-ping (no more sleeping, no UptimeRobot needed)
Render spins free services down after **15 minutes without inbound
traffic**; a sleeping bot misses webhooks and cold-starts for 30-60 s. The
bot now pings its own **public** URL every 9 minutes (configurable via
`KEEP_ALIVE_INTERVAL`, clamped 60–840 s). Opt out with `KEEP_ALIVE=false`
(e.g. if you prefer UptimeRobot, or want the instance to sleep and save
instance hours).

### First deploy succeeds — `WEBHOOK_URL` is now optional
The old deploy flow required two deploys: the first failed because
`WEBHOOK_URL` wasn't known yet. The bot now falls back to Render's injected
`RENDER_EXTERNAL_URL` (`https://<service>.onrender.com`), so the webhook is
registered on the very first boot.

### Port binds before the Telegram handshake
The HTTP health server now starts **first**. Render's deploy health check
passes in under a second instead of racing the 1–3 s Telegram/Telethon
handshake. Webhook POSTs that arrive before the app is ready get a `503`,
so Telegram retries them instead of losing updates.

### SIGTERM handled at boot, not just at runtime
Render sends `SIGTERM` on every deploy/restart. Handlers are now registered
right after the port bind — even an instance stuck in a slow Telegram
handshake shuts down cleanly (port released, DB closed, Telethon
disconnected). A hung handshake can no longer make the instance unkillable;
each startup phase additionally has a 90 s timeout that crashes with a clear
error so the platform can restart it.

### Webhook anti-spoofing via `secret_token`
The webhook is registered with Telegram's `secret_token` (derived
deterministically from the bot token, never logged). Every update POST is
validated against the `X-Telegram-Bot-Api-Secret-Token` header — spoofed
updates are rejected with `401` even if the webhook URL leaks.

### Memory & CPU headroom
- Dropped `python-telegram-bot[webhooks]` extra — the bundled tornado server
  was never used; ~10 MB RAM and extra install time saved.
- `concurrent_updates` capped at 64 (was 256) — plenty for a single-user
  bot, bounds worst-case task memory on the 512 MB instance.
- `allowed_updates` restricted to `message` + `callback_query` — smaller
  webhook payloads, less JSON parsing on the 0.1 CPU.
- SQLite: WAL journal + `synchronous=NORMAL`, plus an asyncio write lock so
  concurrent handlers can't interleave transactions.
- `cryptg` bumped to 0.6.0 — Telethon's AES runs ~5x faster in C instead of
  Python (big win on 0.1 CPU).
- Measured baseline RSS at boot: **~75 MB** (health endpoint reports it;
  `/status` shows it live).

### Topic cache rebuilds itself after restarts
Render's ephemeral disk means the SQLite topic cache starts empty on every
boot. If a Telethon session and a destination group are configured, the
cache now refreshes in the background 2 s after startup — no more
remembering `/refresh` after each deploy.

---

## Scraper improvements

### Resume after interruption (`min=N` / `max=N`)
Scrapes don't survive restarts (ephemeral disk), and a Render redeploy used
to mean starting over. `/scrape_status` always shows `Last msg ID`; now you
can resume with:

```
/scrape <url> min=1042     # newest-first runs: skip ids <= 1042
/scrape <url> old max=1042 # oldest-first runs: stop after id > 1042
```

### Albums stay albums (and use 1 API call instead of N)
The old scraper sent each album item as an individual message. Consecutive
messages sharing a `grouped_id` are now batched and forwarded together —
the destination shows a real grouped album, and a 10-item album costs one
`forward_messages` call instead of ten.

### 3x fewer API calls per scraped item
`send_to_destination` previously resolved the source entity, the destination
entity, and re-fetched each message **per item** — three redundant RPCs the
scraper didn't need, since it had just iterated those exact messages. The
scraper now passes its pre-fetched `Message` objects and entities through,
cutting per-item calls by ~3x. Less CPU, less flood pressure, faster
scrapes on the 0.1-CPU instance.

---

## Quality-of-life

- `/status` now shows mode, uptime, live RSS memory (vs the 512 MB limit),
  Telethon state, keep-alive state, and scrape state.
- Sending a bare channel link (e.g. `https://t.me/somechannel`) to the bot
  now suggests `/scrape` instead of a generic parse error.
- Health endpoint (`GET /`) reports uptime, readiness, and RSS memory —
  handy for external monitors.
- Hourly housekeeping task purges abandoned pending forwards (pickers never
  tapped) so the table can't grow without bound.
- `/caption strip` text-only skip, hyphen normalization, and all prior
  behaviors unchanged.

---

## Dependency changes (all pinned exactly)

| Package | Before | After | Why |
|---|---|---|---|
| python-telegram-bot | 21.6 + `[webhooks]` | 21.11.1 (no extra) | bug fixes; drop unused tornado |
| Telethon | `>=1.36.0` (unpinned!) | 1.40.0 | unpinned installs broke the bot (see fix #1) |
| aiohttp | 3.10.10 | 3.12.15 | security patches for the public webhook endpoint |
| cryptg | 0.4.0 | 0.6.0 | faster crypto, prebuilt wheels |
| aiosqlite / python-dotenv | 0.20.0 / 1.0.1 | unchanged | fine as-is |

---

## New environment variables

| Variable | Default | Meaning |
|---|---|---|
| `KEEP_ALIVE` | `true` in webhook mode | Self-ping the public URL so Render never sleeps the service |
| `KEEP_ALIVE_INTERVAL` | `540` (9 min) | Ping interval in seconds, clamped 60–840 |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every incoming update |
| `WEBHOOK_URL` | *(optional)* | Falls back to Render's `RENDER_EXTERNAL_URL` automatically |

## New files

- `smoke_test.py` — 29 offline checks (config, parsing, DB, secrets, keyboard cap)
- `webhook_e2e_test.py` — 11 live checks of the webhook server with a mocked
  Telegram API (secret validation, 503-before-ready, keep-alive, SIGTERM)

Run them: `python smoke_test.py && python webhook_e2e_test.py`
