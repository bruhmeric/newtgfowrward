"""SQLite-backed persistence layer.

Stores:
  * pending forwards — content awaiting topic selection
  * cached forum topics — refreshed via /refresh or lazily
  * runtime config — destination_group_id override (set via /setgroup)
  * manual topic overrides — added via /addtopic <name> <id>

Free-tier notes:
  * On Render's ephemeral disk the DB starts empty on every boot. That is
    fine — pending forwards are transient, the topic cache is rebuilt
    (automatically at startup when Telethon is available), and the
    destination group falls back to the DESTINATION_GROUP_ID env var.
  * journal_mode=WAL + synchronous=NORMAL cut fsync overhead dramatically
    on the shared 0.1-CPU instance while staying safe against corruption.
  * All writes are serialized through an asyncio.Lock so concurrent update
    handlers (concurrent_updates=True) can't interleave transactions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def init(self) -> None:
        # Idempotent: the webhook server retries startup on failure, and each
        # retry re-runs post_init -> db.init(). Without this guard we would
        # open (and leak) a second SQLite connection per attempt.
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        # Perf pragmas — WAL keeps readers unblocked, NORMAL avoids an
        # fsync per commit. Both are safe for this workload.
        try:
            await self._conn.execute("PRAGMA journal_mode=WAL;")
            await self._conn.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            logger.debug("Could not apply SQLite pragmas (non-fatal)")
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_forwards (
                id            TEXT PRIMARY KEY,
                user_id       INTEGER NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                payload       TEXT NOT NULL,
                kind          TEXT NOT NULL,   -- 'direct' | 'link'
                created_at    INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS topics_cache (
                group_id      INTEGER NOT NULL,
                topic_id      INTEGER NOT NULL,
                title         TEXT NOT NULL,
                PRIMARY KEY (group_id, topic_id)
            );

            CREATE TABLE IF NOT EXISTS topic_overrides (
                group_id      INTEGER NOT NULL,
                topic_id      INTEGER NOT NULL,
                title         TEXT NOT NULL,
                PRIMARY KEY (group_id, topic_id)
            );

            CREATE TABLE IF NOT EXISTS runtime_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_forwards(user_id);
            CREATE INDEX IF NOT EXISTS idx_pending_created ON pending_forwards(created_at);
            """
        )
        await self._conn.commit()
        # Drop abandoned pending forwards up front (e.g. pickers the user
        # never tapped) so the table can't grow without bound on a
        # long-lived instance.
        purged = await self.purge_old_pending(max_age_seconds=3600)
        if purged:
            logger.info("Purged %d stale pending forward(s)", purged)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # -------- pending forwards --------

    async def create_pending(
        self,
        user_id: int,
        source_chat_id: int,
        source_message_id: int,
        payload: dict[str, Any],
        kind: str,
    ) -> str:
        pid = uuid.uuid4().hex[:12]
        async with self._write_lock:
            await self._conn.execute(
                "INSERT INTO pending_forwards VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, user_id, source_chat_id, source_message_id,
                 json.dumps(payload, default=str), kind, int(time.time())),
            )
            await self._conn.commit()
        return pid

    async def get_pending(self, pending_id: str) -> Optional[dict[str, Any]]:
        async with self._conn.execute(
            "SELECT * FROM pending_forwards WHERE id = ?", (pending_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d["payload"] = json.loads(d["payload"])
            return d

    async def delete_pending(self, pending_id: str) -> None:
        async with self._write_lock:
            await self._conn.execute("DELETE FROM pending_forwards WHERE id = ?", (pending_id,))
            await self._conn.commit()

    async def purge_old_pending(self, max_age_seconds: int = 3600) -> int:
        cutoff = int(time.time()) - max_age_seconds
        async with self._write_lock:
            async with self._conn.execute(
                "DELETE FROM pending_forwards WHERE created_at < ?", (cutoff,)
            ) as cur:
                await self._conn.commit()
                return cur.rowcount or 0

    # -------- topics cache --------

    async def set_cached_topics(self, group_id: int, topics: list[dict[str, Any]]) -> None:
        async with self._write_lock:
            await self._conn.execute("DELETE FROM topics_cache WHERE group_id = ?", (group_id,))
            await self._conn.executemany(
                "INSERT INTO topics_cache VALUES (?, ?, ?)",
                [(group_id, t["id"], t["title"]) for t in topics],
            )
            await self._conn.commit()

    async def get_cached_topics(self, group_id: int) -> list[dict[str, Any]]:
        async with self._conn.execute(
            "SELECT topic_id, title FROM topics_cache WHERE group_id = ? ORDER BY topic_id",
            (group_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"id": r["topic_id"], "title": r["title"]} for r in rows]

    async def add_topic_override(self, group_id: int, topic_id: int, title: str) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "INSERT OR REPLACE INTO topic_overrides VALUES (?, ?, ?)",
                (group_id, topic_id, title),
            )
            await self._conn.commit()

    async def delete_topic_override(self, group_id: int, topic_id: int) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "DELETE FROM topic_overrides WHERE group_id = ? AND topic_id = ?",
                (group_id, topic_id),
            )
            await self._conn.commit()

    async def get_topic_overrides(self, group_id: int) -> list[dict[str, Any]]:
        async with self._conn.execute(
            "SELECT topic_id, title FROM topic_overrides WHERE group_id = ? ORDER BY topic_id",
            (group_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"id": r["topic_id"], "title": r["title"]} for r in rows]

    async def get_all_topics(self, group_id: int) -> list[dict[str, Any]]:
        """Merge cache + overrides (overrides win on conflict)."""
        cached = await self.get_cached_topics(group_id)
        overrides = await self.get_topic_overrides(group_id)
        merged = {t["id"]: t["title"] for t in cached}
        for ov in overrides:
            merged[ov["id"]] = ov["title"]
        return [{"id": tid, "title": title} for tid, title in sorted(merged.items())]

    # -------- runtime config --------

    async def set_runtime(self, key: str, value: str) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "INSERT OR REPLACE INTO runtime_config VALUES (?, ?)", (key, value)
            )
            await self._conn.commit()

    async def get_runtime(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with self._conn.execute(
            "SELECT value FROM runtime_config WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row["value"] if row else default
