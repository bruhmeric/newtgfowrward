"""Progress rendering for forwards and scrapes.

Replaces the old plain-text updates ("📊 Scraping in progress… Sent: 12")
with a compact visual layout:

    📤 from-point scrape
    ██████████░░░░░░░░░░ 50%
    25/50 sent · 0 failed · 2 skipped
    ⚡ 1.8 msg/s · ETA ~14s · last #4213

and for byte-level (download/upload) progress:

    📡 Downloading video_3.mp4  (item 3 of 7)
    ████████████░░░░░░ 61%
    25.3 / 41.2 MB · 3.1 MB/s

Two layers:
  * pure formatters (render_bar / format_scrape_progress /
    format_bytes_progress) — trivially unit-testable, no I/O;
  * ProgressReporter — wraps an async edit function with the throttle /
    dedup rules that keep us under Telegram's edit-message rate limits
    (first update always goes out, then at most one per `min_interval`).

Every layer swallows edit errors: a failed status edit must never fail the
actual forward/scrape it is reporting on.
"""
from __future__ import annotations

import time

BAR_WIDTH = 20
_FILLED = "█"
_EMPTY = "░"


def render_bar(fraction: float, width: int = BAR_WIDTH) -> str:
    """`████████░░░░░░` for a 0..1 fraction. Values outside are clamped so a
    mid-flight overshoot (parallel sends racing) never renders 21 blocks."""
    if width <= 0:
        return ""
    f = 0.0 if fraction is None else float(fraction)
    if f < 0.0:
        f = 0.0
    elif f > 1.0:
        f = 1.0
    filled = round(f * width)
    return _FILLED * filled + _EMPTY * (width - filled)


def _fmt_eta(seconds: float) -> str:
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def format_scrape_progress(
    *,
    title: str,
    sent: int,
    total: int | None,          # None -> unknown (whole-channel run)
    failed: int = 0,
    skipped: int = 0,
    flood_waits: int = 0,
    rate: float | None = None,  # messages/sec
    last_msg_id: int | None = None,
    note: str | None = None,
) -> str:
    """Status text for a scrape / bulk forward run. When `total` is known
    (from-point runs know their message count up front) the bar, percentage
    and ETA appear; otherwise the text stays honest — rate only, no fake %."""
    lines = [f"📤 {title}"]
    if note:
        lines.append(f"ℹ️ {note}")
    if total:
        pct = (sent / total) * 100 if total > 0 else 0.0
        lines.append(f"{render_bar(sent / total if total else 0)} "
                     f"{pct:.0f}%")
        lines.append(f"{sent}/{total} sent"
                     + (f" · {failed} failed" if failed else "")
                     + (f" · {skipped} skipped" if skipped else ""))
        if rate is not None and rate > 0:
            remaining = max(total - sent, 0)
            eta = remaining / rate if rate > 0 else None
            tail = f"⚡ {rate:.1f} msg/s · ETA ~{_fmt_eta(eta)}"
            if last_msg_id:
                tail += f" · last #{last_msg_id}"
            lines.append(tail)
    else:
        # Unknown total: no percentage, no bar — show what we actually know.
        parts = [f"{sent} sent"]
        if total is None:
            pass
        parts.append(f"{failed} failed" if failed else "0 failed")
        if skipped:
            parts.append(f"{skipped} skipped")
        if flood_waits:
            parts.append(f"{flood_waits} flood wait(s)")
        lines.append(" · ".join(parts))
        tail = ""
        if rate is not None and rate > 0:
            tail = f"⚡ {rate:.1f} msg/s"
        if last_msg_id:
            tail = (tail + " · " if tail else "") + f"last #{last_msg_id}"
        if tail:
            lines.append(tail)
    return "\n".join(lines)


def format_bytes_progress(
    *,
    label: str,
    sent_bytes: int,
    total_bytes: int,
    rate: float | None = None,   # MB/s
    item: str | None = None,
    of_items: int | None = None,
    item_index: int | None = None,
) -> str:
    """Status text for a download/upload with byte granularity."""
    lines = []
    head = f"📡 {label}"
    if item_index and of_items:
        head += f"  (item {item_index} of {of_items})"
    elif item:
        head += f"  ({item})"
    lines.append(head)
    if total_bytes > 0:
        pct = (sent_bytes / total_bytes) * 100
        lines.append(f"{render_bar(sent_bytes / total_bytes)} {pct:.0f}%")
        mb_sent = sent_bytes / (1024 * 1024)
        mb_total = total_bytes / (1024 * 1024)
        tail = f"{mb_sent:.1f} / {mb_total:.1f} MB"
        if rate is not None and rate > 0:
            tail += f" · {rate:.1f} MB/s"
        lines.append(tail)
    else:
        lines.append("(size unknown)")
    return "\n".join(lines)


class ProgressReporter:
    """Throttled async editor for one Telegram status message.

    Rules:
      * the FIRST update is always sent (immediate feedback after a tap);
      * afterwards at most one edit per `min_interval` seconds;
      * identical text is never re-sent (Telegram answers 400 "message is
        not modified" anyway);
      * edit exceptions are swallowed — progress display is best-effort.
    """

    def __init__(self, edit_fn, *, min_interval: float = 2.5):
        self._edit_fn = edit_fn
        self._min_interval = max(0.05, float(min_interval))
        self._last_ts = 0.0
        self._last_text: str | None = None
        self._started_at = time.time()
        # byte-rate tracking (last sample + its timestamp)
        self._last_bytes = 0
        self._last_bytes_ts = 0.0
        self._last_mbs: float | None = None

    @property
    def elapsed(self) -> float:
        return max(time.time() - self._started_at, 0.001)

    async def update(self, text: str) -> bool:
        """Edit the message to `text` under the throttle rules.
        Returns True when an edit was actually attempted."""
        if text == self._last_text:
            return False
        now = time.time()
        if self._last_text is not None and (now - self._last_ts) < self._min_interval:
            return False
        self._last_text = text
        self._last_ts = now
        try:
            await self._edit_fn(text)
            return True
        except Exception:
            # "message is not modified", rate limit, message deleted, ...
            return False

    # ---- convenience wrappers that render + update in one call ----

    async def scrape_progress(self, *, title: str, sent: int,
                              total: int | None, failed: int = 0,
                              skipped: int = 0, flood_waits: int = 0,
                              last_msg_id: int | None = None,
                              note: str | None = None) -> bool:
        rate = sent / self.elapsed if sent > 0 else None
        return await self.update(format_scrape_progress(
            title=title, sent=sent, total=total, failed=failed,
            skipped=skipped, flood_waits=flood_waits, rate=rate,
            last_msg_id=last_msg_id, note=note))

    async def bytes_progress(self, *, label: str, sent_bytes: int,
                             total_bytes: int, item: str | None = None,
                             of_items: int | None = None,
                             item_index: int | None = None) -> bool:
        # MB/s from the delta against the previous sample. Guarded against
        # the usual artifacts: 0-delta (re-render), negative (server gave us
        # a different item's callback), and sub-50ms gaps (unrealistic spike).
        now = time.time()
        rate = self._last_mbs
        if sent_bytes > self._last_bytes and now > self._last_bytes_ts:
            dt = now - self._last_bytes_ts
            if dt >= 0.05:
                mbs = (sent_bytes - self._last_bytes) / (1024 * 1024) / dt
                if mbs > 0:
                    rate = self._last_mbs = round(mbs, 1)
        self._last_bytes = sent_bytes
        self._last_bytes_ts = now
        return await self.update(format_bytes_progress(
            label=label, sent_bytes=sent_bytes, total_bytes=total_bytes,
            rate=rate, item=item, of_items=of_items, item_index=item_index))


__all__ = [
    "render_bar", "format_scrape_progress", "format_bytes_progress",
    "ProgressReporter", "BAR_WIDTH",
]
