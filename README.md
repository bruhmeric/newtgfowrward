# Telegram Forwarder Bot

A self-hosted Telegram bot that forwards whatever you send it to a chosen topic (forum thread) or regular chat. Supports pulling content from locked / private channels via your personal Telegram account session (Telethon), **auto-scraping entire channels**, and sending directly to Saved Messages for the fastest path.

## Features

| Feature | How |
|---|---|
| **Direct forward** of text / photo / video / file / album | Send to bot → tap a topic button (or single Forward button for non-forum) |
| **Time-based batch** for multiple messages | Send many photos rapidly → bot waits 5s, shows ONE picker for everything |
| **Pull from locked private channels** | Send a `t.me/c/<id>/<msg_id>` link — bot uses Telethon to fetch and re-upload |
| **Pull from public channels** | Send a `t.me/<channelname>/<msg_id>` link |
| **Auto-scrape entire channels** | `/scrape <url> [flags]` — bot iterates ALL messages and sends media in parallel |
| **Media type filters** | `/scrape <url> photo video` — only photos and videos; `/scrape <url> docs audio` — only docs and audio |
| **Direct to Saved Messages** | `/saved <url>` — skip the picker, fastest path |
| **Live progress display** | Bot edits the status message during download/upload with % complete and MB transferred |
| **Streamable video uploads** | Videos re-uploaded with `supports_streaming=True`, original thumbnail, duration, dimensions |
| **Three-tier protected-content fallback** | true forward → send_message(file=) → download + send_file (works on noforwards channels) |
| **Auto-discover forum topics** | `/refresh` enumerates via Telethon `GetForumTopics` |
| **Manual topic override** | `/addtopic <title> <id>` |
| **Non-forum destinations** | Bot auto-detects via `get_chat().is_forum` and shows single Forward button instead of topic picker |
| **Persistent state** | SQLite (`forwarder.db`) |
| **Admin whitelist** | Optional `ADMIN_IDS` in `.env` |
| **Concurrent update processing** | Multiple links / messages handled in parallel (no queue-blocked updates) |
| **Webhook + polling modes** | Webhook for Render/fps.ms, polling for local dev |
| **StringSession** | No `.session` file needed on ephemeral filesystems (Render) |
| **Deploy to Render free tier** | `render.yaml` Blueprint included |
| **Self keep-alive (Render free tier)** | Bot pings its own public URL every ~9 min so Render never spins it down — no UptimeRobot needed (`KEEP_ALIVE=false` to opt out) |
| **One-shot Render deploy** | Uses Render's injected `RENDER_EXTERNAL_URL` — first deploy just works, no WEBHOOK_URL redeploy dance |
| **Scrape resume flags** | `/scrape <url> min=N` / `max=N` — resume an interrupted scrape after a restart |
| **Album-aware scraping** | Albums are forwarded as real grouped albums (1 API call per album, not N) |
| **Auto topic-cache rebuild** | Topic cache rebuilds itself in the background after every restart (no /refresh needed) |
| **Webhook anti-spoofing** | Telegram `secret_token` validated on every update POST |
| **Graceful SIGTERM** | Clean shutdown on Render deploys/restarts — port released, DB closed, Telethon disconnected |

The bot re-uploads media from locked channels (it does NOT use Telegram's native forward feature) — this works even when the source channel has "forwarding / saving restricted" enabled, because your **personal account** can still view the content and the bot re-uploads the bytes through your user session's download.

---

## Quick start (three paths)

| If you want to... | Follow... |
|---|---|
| Run it on your own machine or VPS | [Local setup](#local-setup) |
| Deploy to Render.com free tier | [Render setup](#render-setup) |
| Just see all the commands | [Commands](#commands) |

---

## Local setup

### 1. Prerequisites

- Python 3.10 or newer
- A Telegram bot token — create a bot via [@BotFather](https://t.me/BotFather), copy the token
- Telegram API credentials — get `API_ID` and `API_HASH` from <https://my.telegram.org/apps> (sign in -> "API development tools")
- A destination chat (group or channel, forum or non-forum). The bot must be a member with "Send Messages" permission. Your personal Telegram account must also be a member (for Telethon-based features).

### 2. Install

```bash
cd telegram-forwarder-bot

python -m venv .venv
source .venv/bin/activate    # on Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | From @BotFather |
| `API_ID` | From my.telegram.org |
| `API_HASH` | From my.telegram.org |
| `PHONE` | Your phone (used only by `login.py`) |
| `DESTINATION_GROUP_ID` | e.g. `-1001234567890` (optional for `/saved` and `/scrape saved`) |
| `ADMIN_IDS` | Comma-separated Telegram user IDs (optional but recommended) |

### 3. One-time Telethon login

```bash
python login.py            # file-based session (local dev)
# OR
python login.py --string   # StringSession (recommended for Render)
```

You'll be prompted for:
- A confirmation code sent to your Telegram
- (Optional) 2FA password if your account has it

### 4. Add the bot to the destination group

1. Add the bot to your destination group as a **member** (not admin — member is enough)
2. Make sure "Send Messages" is allowed for the bot
3. If the group is a forum, the bot will be able to post into specific topics

### 5. Run

```bash
python bot.py
```

### 6. First-time configuration

In a private chat with the bot:

```
/setgroup -1001234567890      # set destination (optional for /saved, /scrape saved)
/info                         # see chat info — is it a forum? title?
/refresh                      # discover topics (forum only)
/topics                       # confirm topics were discovered
```

If `/refresh` fails (e.g. because your account isn't a member of the group), you can add topics manually:

```
/addtopic Videos 12
/addtopic Images 34
```

### 7. Using the bot

**Direct forward** — send anything to the bot. The bot waits 5 seconds (in case you send more items), then shows a single picker with everything. Tap a topic (or the "Forward to <chat>" button for non-forum destinations).

**Locked-channel forward** — paste a link to a single message:
```
https://t.me/c/1234567890/42
https://t.me/privatechannelname/42
```
The bot fetches via your Telethon session, then shows the picker.

**Direct to Saved Messages** (fast path):
```
/saved https://t.me/c/1234567890/42
```
Skips the picker entirely — content goes straight to Saved Messages.

**Auto-scrape entire channel**:
```
/scrape https://t.me/somechannel
/scrape https://t.me/c/1234567890 saved old
/scrape https://t.me/c/1234567890 photo video parallel=5
```
Iterates ALL messages in the channel and sends media in parallel. See [Scraping](#scraping) below.

---

## Commands

| Command | Action |
|---|---|
| `/start` | Show the intro message |
| `/help` | Show the help message |
| `/setgroup <id>` | Set the destination group/channel (forum or non-forum) |
| `/info` | Show destination chat info (is it a forum? title? member count?) |
| `/refresh` | Re-fetch forum topics via Telethon (forum only) |
| `/topics` | List known topics (forum only) |
| `/addtopic <title> <id>` | Add a topic manually (forum only) |
| `/deltopic <id>` | Remove a manually-added topic |
| `/status` | Show bot status (Telethon, group, topics, admins) |
| `/whoami` | Show your Telegram user ID + admin status |
| `/test_link <url>` | Diagnostic: test fetching a t.me link (shows step-by-step report) |
| `/saved <url>` | 🚀 FAST: send t.me link content directly to Saved Messages |
| `/scrape <url> [flags]` | 🤖 AUTO: scrape ALL media from a channel |
| `/stop_scrape` | 🛑 stop the active scrape |
| `/scrape_status` | 📊 check scrape progress (shows `Last msg ID` — use it with `min=N` to resume) |
| `/caption <text>` | 📝 set a custom caption (replaces original on all forwards) |
| `/caption strip` | 📝 strip ALL captions from forwarded media |
| `/caption clear` | 📝 restore original caption behavior |
| `/cancel` | Cancel any pending forward for this chat |

---

## Captions

By default, the bot preserves original captions when forwarding. You can change this:

### `/caption <text>` — set a custom caption

```
/caption Check out this content!
```

All forwarded media (via `/scrape`, `/saved`, or sending a link) will use this caption instead of the original. Truncated to Telegram's 1024-char limit.

### `/caption strip` — strip all captions

```
/caption strip
```

Forwards media WITHOUT any caption. Useful when you don't want the source's captions polluting your destination.

### `/caption clear` — restore original captions

```
/caption clear
```

Returns to the default behavior: forwards use the original caption from the source.

### `/caption` (no args) — show current setting

```
/caption
```

Shows the current caption mode and a preview of the text if set.

### How it works

When a custom caption is set:
- **`/scrape`** — every forwarded media item gets your custom caption (no original captions)
- **`/saved <url>`** — the saved message gets your custom caption
- **Direct link forward** — the picked topic/chat receives media with your custom caption
- **Direct message forward** (send photos to bot → tap topic) — same: custom caption applied

The caption is applied to the **first item** of an album only (Telegram albums only show one caption). For single media, the custom caption is used directly.

When set to "strip" (empty string), no captions are sent at all. Text-only messages from the source are skipped entirely.

When set to "clear" (None), original captions are preserved (legacy behavior).

---

## Scraping

`/scrape <url> [flags]` is the most powerful command — it iterates all messages in a channel and forwards each media message to your destination.

### Syntax

```
/scrape <channel_url> [flags]
```

### Flags (any combination)

| Flag | Effect |
|---|---|
| `old` | Oldest first (chronological order). Default: newest first. |
| `saved` | Send to Saved Messages. Default: destination group. |
| `photo` / `photos` | Only photos |
| `video` / `videos` | Only videos |
| `doc` / `docs` | Only documents |
| `audio` | Only audio |
| `voice` | Only voice messages |
| `animation` | Only animations (GIFs) |
| `parallel=N` | Set parallel send count (default 3, max 10) |
| `min=N` | **Resume**: skip messages with id <= N (use after an interrupted newest-first run) |
| `max=N` | **Resume**: stop after messages with id > N (use after an interrupted oldest-first run) |

If no media type filter is specified, ALL media is forwarded.

### Examples

```
/scrape https://t.me/publicchannel
/scrape https://t.me/c/1234567890
/scrape https://t.me/c/1234567890 saved old
/scrape https://t.me/c/1234567890 photo video         # only photos + videos
/scrape https://t.me/c/1234567890 saved old videos parallel=5  # all options
/scrape https://t.me/c/1234567890 min=1042           # resume after interruption
```

### How it works

1. **Parses** the channel URL (`t.me/c/<id>` or `t.me/<username>`)
2. **Resolves the channel** via Telethon (confirms your user account is a member)
3. **Iterates all messages** with `client.iter_messages` (memory-efficient — doesn't load everything at once)
4. **For each message with media**:
   - Classifies the media type (photo, video, animation, document, audio, voice)
   - Applies your filter (if any)
   - Sends via the **same three-tier fallback** as `/saved`:
     1. Try `forward_messages` (fastest, works for non-protected)
     2. Fall back to `send_message(file=msg.media)` (works for some protected)
     3. Final fallback: download + re-upload with `send_file` (works for fully-protected, preserves thumbnail, streaming, duration, dimensions)
   - **Skips text-only messages** and non-downloadable types (web pages, contacts, geos, polls)
5. **Rate limiting**: 0.3 sec delay per parallel slot + automatic FloodWait handling. Albums count as ONE send (forwarded as a grouped album)
6. **Status updates**: edits the status message every 5 sec with progress (sent/failed/skipped counts)
7. **Cancellation**: check between each message; `/stop_scrape` stops cleanly (waits for in-flight sends)
8. **Efficient sending**: the scraper reuses the message objects it already fetched — no redundant `get_entity`/`get_messages` round-trips per message (3x fewer API calls, friendlier to the 0.1-CPU free instance and to Telegram's flood limits)

### Example session

```
You: /scrape https://t.me/somechannel saved old videos parallel=5

Bot: 🔍 Starting scrape...
     Source: somechannel
     Destination: Saved Messages
     Order: oldest first
     Filter: only: video
     Parallel: 5 sends

     Use /stop_scrape to cancel, /scrape_status to check progress.

[5 sec later]
Bot: [edits same message]
     📊 Scraping in progress...
     Total seen: 47
     Sent: 23
     Failed: 0
     Skipped (filtered/no media): 24
     Last msg ID: 1042
     Parallel sends: 5

You: /stop_scrape
Bot: 🛑 Stop signal sent...

Bot: [final status]
     🛑 Scraping cancelled by user.
        Sent: 156, Failed: 2, Skipped: 89
```

### Speed tuning

- **Default: `parallel=3`** — safe, ~9 msgs/sec total (3 slots × 3 msgs/sec each)
- **`parallel=5`** — ~15 msgs/sec, may hit FloodWait occasionally
- **`parallel=10`** (max) — ~30 msgs/sec, will likely hit FloodWait often; bot handles it automatically by sleeping and retrying

If you hit FloodWait frequently, lower the parallel count.

### Notes

- Your user account must be a member of the source channel
- Your user account must be a member of the destination chat (or use `saved` for Saved Messages)
- The scrape runs in the background — you can use other bot commands meanwhile
- Only one scrape at a time — the second `/scrape` will be rejected
- If the service restarts mid-scrape (Render redeploy, crash), the run is lost — but `/scrape_status` showed you the `Last msg ID`, so you can resume with `/scrape <url> min=<last_id>` (newest-first) or `max=<last_id>` (oldest-first)
- With `KEEP_ALIVE=true` (default on Render) the service no longer sleeps mid-scrape
- For huge channels (10000+ posts), expect it to take hours at the safe rate

---

## /saved — fast path to Saved Messages

`/saved <url>` skips the topic picker entirely and sends content directly to your Saved Messages via Telethon. This is the fastest path because:
- No topic picker (instant, no waiting for user input)
- No topic thread (simpler API call)
- Your account is always a member of Saved Messages

```
/saved https://t.me/c/1234567890/42
```

Live progress is shown during download/upload (with % complete and MB transferred). Then `✅ Sent to Saved Messages!`

The same three-tier fallback applies: true forward → send_message(file=) → download + send_file.

---

## How protected content (noforwards) works

When a channel has "Save / Forward" disabled by the admin:

1. **Try `forward_messages`** — Telegram blocks this with `ChatForwardsRestrictedError`
2. **Try `send_message(file=msg.media)`** — also blocked (Telethon detects the reference to a protected message)
3. **Download to disk + `send_file(file=path)`** — this works:
   - Downloads the bytes (your account has view access as a member)
   - Re-uploads as a brand new file with no link to the protected source
   - Preserves: original attributes (DocumentAttributeVideo with duration, w, h), `supports_streaming=True`, mime_type, original filename
   - Downloads the **thumbnail** separately and attaches it as the video poster
   - Uses **512KB chunk size** for download and upload (4x faster than Telethon's auto-picker)

---

## Render setup

See [Render setup (in original README)](#render-setup-details) below for the full step-by-step.

### Quick start

1. Push the project to GitHub (unzip `telegram-forwarder-bot.zip`, `git init`, push)
2. Generate SESSION_STRING locally (and verify it BEFORE pasting into Render):
   ```bash
   pip install -r requirements.txt
   cp .env.example .env  # fill in API_ID, API_HASH, PHONE
   python login.py --string
   # Copy the printed SESSION_STRING into Render's SESSION_STRING env var,
   # then double-check it with:
   python login.py --verify   # parses + connects + checks authorization
   ```
3. Render Dashboard → New → Blueprint → select your repo → Apply
4. Fill in the secret env vars when prompted: `BOT_TOKEN`, `API_ID`, `API_HASH`, `SESSION_STRING`, `DESTINATION_GROUP_ID`, `ADMIN_IDS`
5. Done — the deploy succeeds on the FIRST try. The bot picks up its own URL from Render's `RENDER_EXTERNAL_URL` (no `WEBHOOK_URL` round-trip needed), registers the webhook with a secret token, and starts a keep-alive self-ping so the free instance never sleeps.
6. Send `/start`, `/whoami`, `/status` to verify

> Prefer an external pinger like UptimeRobot, or want the service to sleep?
> Set `KEEP_ALIVE=false` in the Render environment.

> Cold starts still exist after Render *redeploys* or restarts the service —
> but the port now binds before the Telegram handshake, so the health check
> passes in under a second and webhook updates arriving early get a `503`
> (Telegram retries them automatically — nothing is lost).

### Web dashboard & health endpoint

Once deployed, open the service URL in a browser — you get a live status
page (auto-refreshes every 5 s):

* **Status badge** — READY / STARTING / STARTUP ERROR
* **Startup diagnostics** — failing phase, the actual error, and a
  plain-English *"How to fix"* hint (e.g. `BOT_TOKEN` rejected → check
  @BotFather and the Render Environment tab)
* **Live stats** — uptime, memory vs the 512 MB free-tier limit, mode,
  bot username, Telethon session state, destination group
* **Telethon session card** — `connected as @you`, or the exact reason it
  is not (`SESSION_STRING is malformed / timed out / revoked`), with the
  background retry countdown. A broken session disables only locked-channel
  forwarding and topic discovery — the bot itself keeps working
* **Configuration warnings** — non-fatal problems like a non-numeric
  `API_ID` are listed in a warning box instead of silently disabling a
  feature
* **Telegram webhook diagnostics** — pending update count and the last
  delivery error straight from `get_webhook_info`
* **Environment checklist** — which of `BOT_TOKEN`, `API_ID`, `API_HASH`,
  `SESSION_STRING`, ... are set or missing (values are never shown)

If the bot can't start (bad token, missing var, network blip), the process
**stays up and keeps retrying with backoff** (5 s → 5 min) instead of
exit-crash-looping — so the dashboard always shows you exactly what is
wrong, right in the browser, no log digging required. Fix the env var in
Render → Environment, redeploy, and the next attempt comes up.

For monitoring tools (UptimeRobot, cron-job.org, ...): `GET /health`
returns plain text:

```
OK
uptime_sec: 1234
ready: True
telethon: connected as @you (123456)
rss_mb: 142
```

and when the bot is not up yet, `phase:`, `error:` and `hint:` lines are
included so even a curl tells you what's wrong. The `telethon:` line always
shows the user-session state — `ready: True` with `telethon: not connected`
means the bot works but locked-channel forwarding / topic discovery are
unavailable until the session connects.

<a id="render-setup-details"></a>

### Render Blueprint (`render.yaml`)

The repo includes `render.yaml` for one-click deploy:

```yaml
services:
  - type: web
    name: telegram-forwarder-bot
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: python bot.py
    autoDeploy: true
    envVars:
      - key: MODE
        value: webhook
      # ... (BOT_TOKEN, API_ID, etc. set in dashboard)
```

---

## File layout

```
telegram-forwarder-bot/
├── bot.py                # main entry (webhook + polling modes, keep-alive, SIGTERM handling)
├── login.py              # one-time Telethon login (--string for Render)
├── config.py             # env loader (keep-alive, RENDER_EXTERNAL_URL fallback)
├── db.py                 # SQLite layer (WAL, write-lock, hourly purge)
├── user_session.py       # Telethon manager + link parser + scrape_channel (album-aware)
├── topics.py             # forum topic discovery + inline keyboard (100-button cap)
├── handlers/
│   ├── __init__.py
│   ├── admin.py          # /setgroup, /scrape (min=/max= resume), /saved, /info, /status, ...
│   ├── direct.py         # direct forward + time-based batch window
│   └── link.py           # locked-channel URL → fetch + forward
├── render.yaml           # Render Blueprint
├── smoke_test.py         # 29 offline checks (config/parsing/DB/secrets)
├── webhook_e2e_test.py   # 11 live checks of the webhook server (mocked Telegram)
├── .python-version       # pins Python 3.12.0
├── requirements.txt      # all deps pinned exactly
├── run.sh                # convenience launcher
├── .env.example
├── .gitignore
├── CHANGES.md            # what changed in this optimization release
└── README.md
```

---

## Troubleshooting

### `/start` doesn't respond

1. Open the **service URL** in a browser — the dashboard shows the startup
   phase, the error (if any), a fix hint, and which env vars are missing.
   This is the fastest way to diagnose a broken deploy.
2. `curl https://<your-service>.onrender.com/health` — same info in plain text.
3. Check `/status` — it shows uptime, memory, mode, Telethon state, and keep-alive state at a glance

- Check Render logs for the startup sequence (`Health server listening` → `Bot started: @... mode=webhook`) — if absent, the service is crashing; the log will show why
- Check `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` — `url` should match your Render URL, `last_error_message` should be empty
- If `pending_update_count > 0` and `last_error_message="Read timeout expired"` — the service was asleep or restarting. With `KEEP_ALIVE=true` (default) this should no longer happen; verify in the logs that `Keep-alive enabled` appears at startup
- If `ADMIN_IDS` is set but doesn't include your user ID, all commands silently fail. Send `/whoami` (always responds) to see your ID
- Check `/status` — it now shows uptime, memory, mode, Telethon state, and keep-alive state at a glance

### Locked-channel link fails

- Send `/test_link <url>` — the bot shows a step-by-step diagnostic of where it failed
- Most common: your user account is not a member of the source channel → `ChannelPrivateError`
- If the dashboard says `Telethon session: not connected — …`, the user session
  isn't up: run `python login.py --verify` locally against the same
  `SESSION_STRING`, fix it in Render → Environment, redeploy
- If you see `ChatForwardsRestrictedError` → the bot should fall through to the third-tier fallback (download + re-upload). If it doesn't, send the diagnostic to me.

### "Telethon session: not connected" but the bot works

This is the *expected* degraded mode when `SESSION_STRING` is bad — the bot
itself (webhook, commands, direct forwarding) keeps running while Telethon
retries in the background (30 s → 60 s → 120 s → every 5 min). The card on
the dashboard and the `telethon:` line in `/health` always show the exact
reason, plus **which datacenter the session dials** (`DC4 · 149.154.167.91:443`)
and the result of a raw TCP reachability probe — run *before* each connect
attempt — so the cause is visible without opening the logs:

| Reason on the dashboard | What it means | Fix |
|---|---|---|
| `SESSION_STRING is malformed` | copy/paste error, quotes, or truncated | re-copy the full string from `python login.py --string` |
| `SESSION_STRING embeds a private address` | the string was generated behind a VPN/proxy — it dials an address no public host can reach | disconnect the VPN and regenerate: `python login.py --string` |
| `connection to DCx (…) timed out after 60s … Probe verdict: …` | see the verdict on the same line — it names whether the host can't reach MTProto at all, or only the session's DC | run `python login.py --verify`; if it fails there too, regenerate |
| `… raw TCP probe reached … — the hang is in the MTProto handshake: likely a corrupt auth key` | TCP works, the key doesn't | regenerate the string (its auth key is damaged) |
| `SESSION_STRING is invalid, expired, or was revoked` | you logged in again elsewhere or terminated the session | generate a fresh string with `python login.py --string` |
| `not configured — …` | `API_ID`/`API_HASH`/`SESSION_STRING` missing | set the listed variables in Render → Environment |

Only locked-channel forwarding, protected-content fetching and topic
discovery need the user session — everything else works without it.

### Scraping is slow

- Default `parallel=3` is conservative. Try `parallel=5` or `parallel=10` to go faster.
- If you hit FloodWait often (you'll see `⏳ Flood wait` in the status), lower the parallel count.
- For huge channels, scraping will take hours at any safe rate. Run it overnight.

### Videos sent as documents (no streaming, no thumbnail)

- The bot preserves `DocumentAttributeVideo`, mime_type, and `supports_streaming=True`
- If the source video has a thumbnail, the bot downloads and attaches it
- If you see `(no thumbnail)` in the diagnostic, the source had no embedded thumb — Telegram will generate one from the video itself

---

## Security checklist

- [ ] Keep `SESSION_STRING` private — grants full access to your Telegram account
- [ ] Set `ADMIN_IDS` so only you can use the bot
- [ ] Don't commit `.env` or `*.session` to git (`.gitignore` covers them)
- [ ] Use a strong, unique password on the Telegram account whose session you're using

---

## Roadmap (not implemented)

- Fully persistent scrape state (auto-resume after restart without typing `min=N` — the resume flags cover it manually for now)
- Topic-aware scraping (send to a specific topic, not just chat-level)
- Retry queue for failed sends
