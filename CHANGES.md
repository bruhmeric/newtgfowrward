# Changelog — Render Free-Tier Optimization Release

This release fixes several real bugs, hardens the webhook server, and tunes
the bot specifically for Render's free tier (512 MB RAM / 0.1 CPU / 15-min
sleep / ephemeral disk). All changes are covered by `smoke_test.py`
(148 checks) and `webhook_e2e_test.py` (28 checks) — run both locally with
`pip install -r requirements.txt` first.

---

## Routing fix: /scrape + post link = whole channel (per user report)

**The reported problem.** "When I send a private post link with the scrape
command it should scrape that whole channel/group according to the flags.
When I send just the link without scrape it should only send that post."
The second half already worked (a bare post link forwards just that post),
but the first half did NOT: `/scrape <post link> old` was hijacked into a
from-point run capped at 50 messages — instead of the whole channel,
oldest first, that the user asked for. Three concrete defects:

1. `old` / `new` on a post link silently selected **from-point** mode, so
   `/scrape https://t.me/c/1234567890/42 old` scraped only 50 messages
   backward from post #42 — not the whole channel. `/scrape <post> new
   videos` became a 50-message from-point run instead of "whole channel,
   newest first, videos only".
2. A bare count was inconsistent: `/scrape <post> 100` (no direction) was
   silently ignored (whole channel, uncapped) while `count=100` errored
   with "A count alone isn't enough". Neither did what a user expects:
   cap the run at 100.
3. Whole-channel runs ignored `/strip` entirely — `strip_captions` /
   `strip_links` were never passed to `scrape_channel`, despite `/strip`
   help claiming it applies to `/scrape`.

**The new routing rule** (one engine, `handlers/admin._parse_scrape_flags`,
unit-tested in `smoke_test.py` section 20):

- `/scrape <ANY link> [flags]` → **whole channel/group**. A post link
  identifies the same channel a channel link does; its message id is
  ignored. `old`/`oldest` → oldest first, `new`/`newest` → newest first,
  `N`/`count=N` → cap at N delivered messages (counted after the media
  filter, so `photo 100` = 100 photos).
- From-point scraping is now **explicit only**: `/scrape <post_link> from
  old|new [N]` — or the bare-message hashtag form (`link #old/#new`),
  which is unchanged. Plain `old`/`new` never trigger from-point anymore.
- A bare post link without `/scrape` (and without `#old`/`#new`) still
  forwards **only that post** — exactly as the user specified.

**Engine changes** (`user_session.scrape_channel`):

- New `limit` parameter: stops iterating once N messages have been
  selected for sending (after the media filter). Never splits an album
  that's already buffered. `result["hit_limit"]` + `Stopped at limit: N`
  in the final status; capped runs show the real progress bar + % + ETA
  (the target is known up front), and `/scrape_status` gets
  `expected_total` seeded.
- New `strip_captions` / `strip_links` parameters, passed through to
  `send_to_destination` — whole-channel `/scrape` now honors `/strip`.
- Initial status line announces the limit; periodic status passes
  `total=limit` so the bar renders.

**Docs**: `/help`, the `/scrape` usage text, and the README (Scraping
chapter, command table, examples) all document the new rule.

**Tests**: `smoke_test.py` 121 → **148 checks** (new sections 20 + 21:
routing matrix incl. regression checks for the old hijack, from-point
`from` errors, link demotion, fake-client limit/stop-early/reverse-order/
strip-passthrough/status-announcement tests). `webhook_e2e_test.py` still
28/28. Backward-incompatible: anyone who relied on `/scrape <post> old`
being from-point must add `from`.

## Feature release 2: Saved Messages destination + live progress

### 1. Forward to Saved Messages when no destination is set

Previously every forward path hard-failed with "No destination group set.
Use /setgroup" when `DESTINATION_GROUP_ID` / `/setgroup` was missing. Now
the destination is resolved by a single resolver (new
`handlers/destination.py`) used by every path:

1. an explicit per-run flag (`/scrape <url> saved` or the new
   `/scrape <url> group`)
2. the `/dest saved on` toggle (see below)
3. the configured group (env or `/setgroup`; DB value wins)
4. **fallback: the user's Saved Messages** — the bot works out of the box

- Sending a link, or any media, now **skips the picker entirely** and goes
  straight to Saved Messages (new `send_batch_to_saved` in
  `handlers/direct.py`) when the effective destination is Saved — no button
  tap needed.
- Saved Messages is only reachable through the Telethon user session (the
  Bot API cannot send to a user's self-chat), so that route needs a live
  `SESSION_STRING`; the error message when the session is down explains
  both ways out (fresh session vs `/setgroup` group path).
- Pending-forward taps (topic buttons) reroute through Saved Messages the
  same way when the group has been unset while a picker was in flight.

### 2. `/dest` — make Saved Messages the default (persistent toggle)

```
/dest                  # show settings + effective destination
/dest saved on         # Saved Messages is the default — even with a group set
/dest saved off        # back to the group (alias: /dest group)
```

- Persisted in the DB (`saved_messages_default`), survives restarts and
  Render redeploys; `SAVED_MESSAGES_DEFAULT=true` env seeds the same thing
  on first boot.
- `/scrape` gains the `group` flag to override the toggle per-run (the
  `saved` flag already existed).
- `/status` shows "Saved Messages default" + "Effective destination"; the
  web dashboard gains an **Effective destination** card and `/health` gains
  `saved_default:` / `destination:` lines.

### 3. Live progress, rebuilt

New `progress.py` (pure formatters + a throttled `ProgressReporter`):

- **from-point scrapes know their count up front** → real progress bar
  (`████████░░░░░░░░ 50%`), percentage, `sent/total`, msg/s rate and ETA.
- Whole-channel runs don't know their total — they now show counts +
  msg/s instead of a fabricated percentage.
- Byte-level (download/upload) progress gains a bar + **MB/s** (computed
  from deltas between samples) instead of a bare "Progress: 61.2%".
- Edits are throttled per reporter (first update immediate, then ≥0.8s for
  byte progress / ≥5s for scrape status), deduped, and never fail the
  forward they're reporting on.
- `/scrape_status` now computes progress %, rate and ETA from the live
  stats (from-point runs report `expected_total` through the stats
  callback); the final from-point summary shows "Sent X/Y" + "Took 34s ·
  1.5 msg/s".

### Files changed

- new: `handlers/destination.py` (resolver + toggle), `progress.py`
- `handlers/admin.py`: `/dest` command, resolver wired into `/scrape` +
  from-point, richer `/scrape_status` + `/status` + `/help`, `/saved`
  progress via the reporter, `expected_total` in scrape stats
- `handlers/direct.py`: `send_batch_to_saved`, saved fast path for the
  batched direct-send flow and the pending-forward tap, picker error text
- `handlers/link.py`: saved fast path (skip picker)
- `user_session.py`: rich status texts in both scrape engines,
  `expected_total` for from-point, elapsed + msg/s in the final summary
- `bot.py`: Effective-destination card, `saved_default:`/`destination:`
  health lines, `/dest` toggle restored from DB in post_init
- `.env.example`: `SAVED_MESSAGES_DEFAULT` documented
- tests: `smoke_test.py` 95 → **121 checks** (sections 17–19: resolver
  matrix, toggle persistence, env seed, bar/ETA/MB-s rendering, reporter
  throttle + dedup, dashboard/health); `webhook_e2e_test.py` still 28/28

---

## Feature release: from-point scraping + /strip (captions & links)

### 1. Scrape a channel FROM a specific post (old / new)

Send a post link with a direction and the bot scrapes from exactly that
point, instead of the whole channel:

```
/scrape https://t.me/c/1234567890/42 new        ← post #42 + the 49 newer posts
/scrape https://t.me/c/1234567890/42 old 100    ← post #42 + the 99 older posts
https://t.me/c/1234567890/42 #new               ← same thing, no command needed
https://t.me/somechannel/500 #old 200           ← bare message with count
```

- **Direction `old`** walks back in history from the linked post,
  **`new`** walks toward the newest posts. The linked post itself is
  included in both directions.
- Messages are delivered in **chronological order** so the destination
  reads like the source (the `old` fetch is reversed; album items are
  re-sorted by id so albums don't come out flipped).
- Count defaults to 50, clamped to 500 per run; give it as a bare number
  (`old 100`), `count=100`, or baked into the hashtag (`#old100`).
- Unlike whole-channel `/scrape` (media-only), from-point runs forward
  **everything — text posts included** — unless you add a media filter
  (`photo video doc …`).
- The `#old` / `#new` hashtag form works in a plain message (the admin
  forwards a post from the source channel and appends the hashtag); the
  hashtag is what makes the intent explicit, so a bare link in
  conversation still gets the normal one-post topic picker.
- Reuses the same engine as whole-channel scraping: album batching,
  3-tier protected-content fallback, FloodWait retry, parallel sends,
  `/stop_scrape` + `/scrape_status`, and the /strip settings.

### 2. `/strip` — strip captions and/or links on forward

```
/strip                  ← show current settings
/strip on / off         ← both toggles at once
/strip captions on|off  ← media posts forwarded WITHOUT their caption
/strip links on|off     ← URLs & hyperlinks removed from forwarded text
```

- Applies to **every sourced forward**: post links (topic picker), `/saved`,
  whole-channel `/scrape`, and from-point scraping. Content you upload
  directly to the bot is untouched.
- Link stripping handles all three forms: raw URLs (with or without
  entities), URL entities, and "click [here]" hyperlinks (visible text
  kept, link dropped). Bold/italic/hashtag entities survive with their
  offsets shifted (UTF-16 math via Telethon's surrogate helpers).
- Text cleanup after removal: doubled spaces collapse, a line that only
  held a link disappears, runs of blank lines collapse to one.
- Text-only posts keep their text (only links are removed); a post that
  is *just* links is skipped entirely.
- Smart fast path: when nothing would change (no links present, caption
  already empty), the true-forward path is still used — no re-upload cost.
- Persisted in the DB (`strip_captions` / `strip_links` runtime keys);
  `STRIP_CAPTIONS` / `STRIP_LINKS` env vars provide the boot defaults.
  A `/caption <text>` custom caption still wins over the captions toggle.
- Visible on the web dashboard ("Forward cleaning" card), in `/status`,
  and in `/health` (`strip_captions:` / `strip_links:` lines), alongside a
  new live **Scrape** card (`scrape:` in /health).

### Tests

- `smoke_test.py` **95/95** — new sections 11–16: the link-strip engine
  (raw URLs, entity shifting, TextUrl, pure-link, line cleanup),
  `#old/#new` request parsing, delivery ordering, env defaults, fake-client
  integration of every `send_to_destination` strip path (fast-path choice,
  re-send with empty/stripped caption, skip pure-link posts, custom-caption
  precedence), dashboard/health rendering.
- `webhook_e2e_test.py` **28/28** (unchanged).

---

## Hotfix 4: THE root cause — session-string dialect mismatch

**Symptom:** Telethon connect timed out on EVERY host (Render: 30 s per
attempt, forever) even though the SESSION_STRING was valid and the Bot API
worked fine. First run of the new diagnostics revealed it instantly:
`SESSION_STRING parsed: DC1 -> e:3134:392e:3135:342e:3137:352e:3538:443`.

### What actually happened

Telethon 1.40 packs the session address as **raw binary IP bytes without a
length prefix** (`dc_id B + packed-ip 4/16 B + port H + key 256 B`). Some
session generators (older Telethon and several third-party tools) emit a
**different dialect**: `dc_id B + ip_len H + ASCII ip + port H + key 256 B`.
Telethon 1.40 loads that dialect WITHOUT an error — but reads the length
prefix + ASCII bytes as the IP itself: `\x00\x0e"149.154.175.58"` is 16
bytes, which `ipaddress.ip_address()` happily interprets as an IPv6
address → the client dials garbage like `e:3134:392e:...:3538` forever.
A perfectly good session, permanently unconnectable.

### The fix (user_session.py)

1. **`_compat_parse_session_string()`** — raw-decodes the session string
   and detects BOTH dialects. The length-prefixed dialect has an exact
   total-length self-check, so a false positive on a native string is
   effectively impossible. On mismatch, the session is repaired via
   `set_dc(dc_id, correct_ip, port)` — the auth key is preserved (and
   re-attached defensively in case a future Telethon clears it).
2. **Canonical-DC fallback.** A session whose embedded address is a
   VPN/proxy artifact (private IP, or any non-Telegram IP) is now
   redirected to the canonical address of its dc_id — the auth key is
   bound to the DATA CENTER, not the front-door address, so this repairs
   the session instead of failing. The old "private address → refuse"
   fast-fail now applies only when the dc_id is unknown (nothing to fall
   back to).
3. **Address classification by routed range.** `classify_session_address`
   now matches Telegram's officially routed IPv4 ranges (149.154.160.0/20,
   91.108.0.0/16, …) instead of five exact IPs — genuine front doors like
   149.154.175.58 are no longer miswarned as "not a known Telegram DC".

Verified live: the previously-doomed session connected within seconds
(`dialect repaired ... -> DC1 149.154.175.58:443` → probe OK →
`connected as @…`, 166 dialogs cached).

Also in this release: **polling mode now serves the dashboard** —
`_run_polling_with_dashboard()` mirrors the webhook runner (port-first
bind, retry loop, SIGTERM-safe teardown) but pulls updates via
getUpdates, so the bot is fully usable (and observable) on hosts without
a public URL. Phase 4/5 detects and deletes a pre-existing webhook
(one token = one update pipe) and logs it loudly.

### Tests

- `smoke_test.py` **61/61** — new checks: dialect-B misparse reproduced
  on raw Telethon (documents the bug), dialect-B repair + key
  preservation, private+known-DC canonical override, private+unknown-DC
  fail-fast, in-range DC classification, polling dashboard card. Both
  test files are now hermetic (a real `.env` in the repo no longer
  changes test outcomes).
- `webhook_e2e_test.py` **28/28** (unchanged scenarios, env neutralized).

---

## Hotfix 3: Telethon connect timeout — diagnose, harden, surface

**Symptom:** the bot itself came up fine (webhook registered, commands
answered) but the Telethon user session logged
`Telethon connect timed out after 30s` on every attempt and the dashboard
could only say "not connected" — with no way to tell a bad `SESSION_STRING`
from a network problem. Notably the Bot API (HTTPS) worked from the same
host, so "Telegram is unreachable" was only half the story: a StringSession
embeds its *own* DC address, and Telethon dials exactly that.

### What was added

1. **Session introspection.** After parsing `SESSION_STRING`, the bot now
   logs and shows **which datacenter the session dials** (`DC4 ·
   149.154.167.91:443`) and whether the auth key is the expected 256 bytes.
   None of this is secret; the key itself never leaves the process.
2. **Raw TCP probe before every connect attempt.** An 8 s `asyncio`
   connect to the session's DC — independent of Telethon — answers the
   question a timeout cannot. On failure it sweeps all five official DCs
   (once per process) and writes a **verdict**:
   - *some DCs reachable, the session's is not* → the string points
     somewhere this host can't reach → regenerate it;
   - *no DC reachable at all* → MTProto egress is blocked on this network
     (the Bot API over HTTPS still works — which is why the bot is fine);
   - *TCP fine but Telethon still times out* → the handshake is stalling
     on a corrupt auth key → regenerate the string.
3. **Private-address fast-fail.** A string generated behind a VPN/proxy
   can embed a private IP (10.x/127.x/192.168.x). That can never connect
   from a public host; the bot now refuses it at parse time with
   `SESSION_STRING embeds a private address (…)` instead of burning a
   60 s timeout per retry forever.
4. **Dashboard + `/health` surfacing.** The Telethon card gains a second
   line with the DC and probe result; `/health` gains `telethon_dc:`,
   `telethon_probe:` and `telethon_verdict:` lines — the whole diagnosis
   is now readable from `curl https://…/health` alone.
5. **Connect hardening.** Timeout 30 s → 60 s (cold 0.1-CPU instances
   need it; override with `TELETHON_CONNECT_TIMEOUT`), explicit
   `use_ipv6=False` (blackholed IPv6 routes hang connects), internal
   Telethon retries trimmed to 3 so failures surface to our own backoff
   loop instead of hanging inside attempt #1, and a 10 s settle delay
   before the first attempt so the connect doesn't race webhook
   registration and the PTB update backlog.
6. **`login.py` now prints the DC** the generated string embeds and warns
   immediately if it's a private address — the "will this string ever work
   on Render" check happens before the string ever leaves your machine.

### Tests

- `smoke_test.py` **51/51** — 18 new checks: address classification
  (telegram/private/public/hostname/unknown), session introspection,
  private-address fast-fail (< 2 s, no network), malformed-string
  regression, live TCP probe against a local listener, probe failure
  against an unroutable TEST-NET IP, and dashboard rendering of the new
  DC/probe line.

---

## Post-deploy hotfix: crash-loop + missing dashboard

**Symptom:** Render said "deployed", but visiting the URL showed only
`ready: False` with a low `uptime_sec`, and the bot never answered.

### 7. Startup failures crash-looped the service (root cause)
Any exception during the Telegram handshake — a rejected `BOT_TOKEN`
(`InvalidToken`/`Unauthorized`), a failed `set_webhook`, a network blip —
escaped `_run_webhook_custom()` and hit `sys.exit(1)`. Render then restarted
the container, which failed again, forever. Every visit landed on a
16-second-old instance showing `ready: False`, and the actual error was
buried in Render's Logs tab. Startup now **retries with backoff
(5 s → 5 min) inside the live process**: partial state is torn down per
attempt (Telethon disconnected, PTB re-initialized, DB stays open via an
idempotent `init()`), the error + a plain-English fix hint are recorded,
and SIGTERM still exits cleanly at any point — even mid-backoff.

### 8. Web dashboard at the service root
`GET /` was a bare plain-text health string — users expected a dashboard.
The root now serves a self-contained HTML status page (auto-refresh 5 s,
no external assets, secrets scrubbed): status badge, failing startup phase,
the error + *How to fix* hint, uptime, memory vs the 512 MB limit, bot
username, Telethon session state (including *why* it isn't connected),
destination group, Telegram webhook diagnostics (pending updates, last
delivery error from `get_webhook_info`, cached 15 s), and an env-var
checklist (set / missing / optional — values never shown). Plain-text health
moved to `GET /health` (and `GET /<webhook-path>`), now with `phase:`,
`error:` and `hint:` lines when the bot isn't up.

### 9. Telethon failure reasons surfaced
`UserSession.start()` swallowed the reason it failed. It now records
`last_error` (missing session / unauthorized SESSION_STRING / exception)
and `connected_as`, both shown on the dashboard — a bad `SESSION_STRING`
no longer requires log access to diagnose.

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
- `webhook_e2e_test.py` — 19 live checks of the webhook server with a mocked
  Telegram API (secret validation, 503-before-ready, keep-alive, SIGTERM,
  plus a crash-loop regression scenario: failed startup must stay up,
  surface the error, and still shut down cleanly)

Run them: `python smoke_test.py && python webhook_e2e_test.py`

---

# Hotfix 3 — "stuck at 3/5 setup-storage, bot doesn't respond"

**Symptom (production):** dashboard showed `Startup phase: 3/5 setup-storage`,
`Startup attempt: 3`, `Telethon session: not configured` forever; the bot
never answered a single message despite `BOT_TOKEN` being valid.

**Root cause:** the Telethon user session was started SYNCHRONOUSLY inside
`post_init` (phase 3/5). Telethon's `connect()` / `is_user_authorized()`
have **no built-in timeout** — with a bad/unreachable `SESSION_STRING` the
call hung for the full 90 s per-phase timeout, the attempt failed, and the
retry loop repeated the same hang forever. An *optional* component
(locked-channel access + topic discovery) was therefore able to block the
*required* component (the bot itself) from ever registering its webhook.

## Fixes

1. **Telethon is fully decoupled from startup.** It now connects in a
   background loop with its own retry/backoff (30 s → 60 s → 120 s → 5 min).
   The bot reaches `ready` and answers messages even when the session string
   is complete garbage; the dashboard Telethon card shows the exact reason
   and the retry countdown, and `/health` gains a `telethon:` line.
2. **Timeouts inside `UserSession.start()`.** `connect()` capped at 30 s
   (`TELETHON_CONNECT_TIMEOUT`), authorization check at 20 s — a hang is now
   a clean, explainable failure instead of a frozen startup. Failure paths
   force-disconnect so half-open sockets are not leaked.
3. **Malformed `SESSION_STRING` handled.** `StringSession(...)` construction
   used to raise *outside* any try/except (crashing startup with an opaque
   KeyError-style error). It now produces a dashboard-ready message
   ("SESSION_STRING is malformed — copy the ENTIRE string printed by
   `python login.py --string`").
4. **Fresh-session entity warmup (the next bug you'd have hit).**
   `StringSession` — the only session type that survives Render's ephemeral
   disk — does NOT persist Telethon's entity cache, so on every fresh boot,
   resolving the destination group by numeric ID failed with
   "Could not find the input entity": forwarding and topic discovery would
   have silently failed even with a VALID session string. The bot now warms
   the entity cache with the account's dialogs right after connecting (and
   lazily on first resolve failure).
5. **`Config.has_user_session` is honest.** API_ID+API_HASH alone (or with
   the repo's empty placeholder `user_session.session`) no longer count as
   "a session is configured" — previously the bot kept trying to connect a
   session that could never exist.
6. **Non-numeric env values no longer crash `Config.load()`** (a typo'd
   `API_ID`/`DESTINATION_GROUP_ID`/`PORT` used to cause a Render crash
   loop). Bad values are ignored, logged, and shown in a "Configuration
   warnings" box on the dashboard.
7. **`UserSession.stop()` force-disconnects** (the old `if self._started`
   guard leaked connections when start() was cancelled mid-handshake), and
   startup-attempt teardown no longer disconnects a healthy session when an
   unrelated phase fails.
8. **`login.py --verify`** — validate an existing SESSION_STRING locally
   (parse → connect → authorized) *before* pasting it into Render.

## Tests

- `webhook_e2e_test.py` **28/28** — new scenario 3 regression: a UserSession
  whose `start()` hangs forever must NOT prevent `ready: True`, webhook
  registration, or update dispatch; SIGTERM cancels the hung loop cleanly.
  (The scenario also caught a real `KeyError: 'user_session'` bug in the
  rework before it shipped.)
- `smoke_test.py` **33/33** — new checks: api credentials alone do not count
  as a session; SESSION_STRING enables it; a non-numeric API_ID disables the
  session with a visible load issue instead of raising.
