"""Telegram alerts. Outbound only -- the service exposes no command surface."""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

log = logging.getLogger("notify")


class Telegram:
    def __init__(self, token: str, chat_id: str, http: httpx.AsyncClient):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self.http = http

    async def send(self, text: str) -> bool:
        for attempt in range(3):
            try:
                r = await self.http.post(
                    self._url,
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if r.is_success:
                    return True
                log.warning("Telegram %s: %s", r.status_code, r.text[:200])
                if r.status_code in (400, 403):
                    return False  # bad chat id or bot removed; retrying won't help
            except httpx.HTTPError as exc:
                log.warning("Telegram transport error (%s), attempt %d", exc, attempt + 1)
            delay = min(2.0 ** attempt, 8.0)
            await asyncio.sleep(delay + random.uniform(0, 0.4 * delay))
        log.error("Giving up on Telegram message after retries")
        return False
