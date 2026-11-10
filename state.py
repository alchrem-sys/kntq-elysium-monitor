"""Redis-backed state, written to a request budget.

Memory is the working copy. One HGETALL at startup, one pipelined write per
flush interval and only when something changed. An idle service makes no
Redis calls at all.
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

    async def load(self) -> None:
        self.command_count += 1
        raw = await self._redis.hgetall(KEY)
        for field, blob in raw.items():
            try:
                self.data[field] = json.loads(blob)
            except json.JSONDecodeError:
                log.warning("Dropping unreadable state field %s", field)
        log.info("Loaded %d state field(s) from Redis", len(self.data))

    def get(self, field: str) -> dict:
        return self.data.setdefault(field, {})

    def update(self, field: str, **values) -> None:
        current = self.get(field)
        changed = {k: v for k, v in values.items() if current.get(k) != v}
        if changed:
            current.update(changed)
            self._dirty = True

    def in_cooldown(self, field: str, seconds: float) -> bool:
        last = self.get(field).get("last_alert_ts") or 0.0
        return (time.time() - last) < seconds

    def mark_alerted(self, field: str) -> None:
        self.update(field, last_alert_ts=time.time())

    async def flush(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        payload = {k: json.dumps(v) for k, v in self.data.items()}
        if not payload:
            self._dirty = False
            return
        pipe = self._redis.pipeline()
        pipe.hset(KEY, mapping=payload)
        pipe.expire(KEY, TTL_SECONDS)
        await pipe.execute()
        self.command_count += 2
        self._dirty = False

    async def close(self) -> None:
        await self._redis.aclose()
