"""Redis-backed state, written to a free-tier request budget.

The whole state is one JSON blob under one key, so a flush is a single SET
with a TTL rather than an HSET plus an EXPIRE, and a cold start is a single
GET. Memory is the working copy; Redis only sees it on the flush interval,
and only when something actually changed. An idle service makes no Redis
calls at all.

Steady state at the default 300s flush: ~289 commands a day.
"""
from __future__ import annotations

import json
import logging
import time

import redis.asyncio as redis

log = logging.getLogger("state")

KEY = "kntq-elysium:monitor"
TTL_SECONDS = 60 * 60 * 24 * 30


class Store:
    def __init__(self, url: str):
        self._redis = redis.from_url(url, decode_responses=True)
        self.data: dict[str, dict] = {}
        self._dirty = False
        self.command_count = 0
        self.write_failures = 0

    async def load(self) -> None:
        self.command_count += 1
        raw = await self._redis.get(KEY)
        if not raw:
            log.info("No saved state; starting cold")
            return
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Saved state is unreadable; starting cold")
            return
        if isinstance(loaded, dict):
            self.data = loaded
            log.info("Loaded %d state field(s) from Redis", len(self.data))

    def get(self, field: str) -> dict:
        return self.data.setdefault(field, {})

    def update(self, field: str, **values) -> None:
        current = self.get(field)
        changed = {k: v for k, v in values.items() if current.get(k) != v}
        if changed:
            current.update(changed)
            self._dirty = True

    def touch(self) -> None:
        """Mark state dirty after mutating a nested structure in place."""
        self._dirty = True

    def in_cooldown(self, field: str, seconds: float) -> bool:
        last = self.get(field).get("last_alert_ts") or 0.0
        return (time.time() - last) < seconds

    def mark_alerted(self, field: str) -> None:
        self.update(field, last_alert_ts=time.time())

    async def flush(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        if not self.data:
            self._dirty = False
            return
        # One command: SET with TTL. Upstash denies MULTI/EXEC, and a free
        # tier is metered per command, so this stays deliberately minimal.
        await self._redis.set(KEY, json.dumps(self.data), ex=TTL_SECONDS)
        self.command_count += 1
        self._dirty = False

    async def close(self) -> None:
        await self._redis.aclose()
