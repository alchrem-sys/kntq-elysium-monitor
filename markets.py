"""Elysium sell side: KyberSwap quotes, behind one shared rate gate."""
from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config import Config

log = logging.getLogger("markets")

KYBER_BASE = "https://aggregator-api.kyberswap.com/elysium/api/v1"
KYBER_HEADERS = {"x-client-id": "kntq-elysium-monitor"}
# Every Kyber call in the service goes through one gate: at most one request
# per KYBER_MIN_GAP_S, and after a 429 nobody calls Kyber until the cooldown
# ends (retrying into a 429 only extends the ban).
KYBER_MIN_GAP_S = 1.2
KYBER_COOLDOWN_S = (5.0, 60.0)


async def _backoff(attempt: int) -> None:
    delay = min(2.0 ** attempt, 10.0)
    await asyncio.sleep(delay + random.uniform(0, 0.4 * delay))


class Markets:
    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self.cfg = cfg
        self.http = http
        self.kyber_requests = 0
        self._kyber_lock = asyncio.Lock()
        self._kyber_next = 0.0
        self._kyber_cool_until = 0.0
        self._kyber_cooldown = KYBER_COOLDOWN_S[0]
        self.kyber_429 = 0

    async def _kyber_get(self, params: dict, soft: bool = False) -> httpx.Response | None:
        """Rate-gated Kyber GET. None while cooling down after a 429.

        `soft` marks a secondary quote (HYPE): its 429 costs one short pause and
        does not lengthen the cooldown the KNTQ quote has to wait out."""
        async with self._kyber_lock:
            now = time.monotonic()
            if now < self._kyber_cool_until:
                return None
            if now < self._kyber_next:
                await asyncio.sleep(self._kyber_next - now)
            self._kyber_next = time.monotonic() + KYBER_MIN_GAP_S
            self.kyber_requests += 1
            r = await self.http.get(f"{KYBER_BASE}/routes", params=params, headers=KYBER_HEADERS)
            if r.status_code == 429 and soft:
                self.kyber_429 += 1
                self._kyber_cool_until = time.monotonic() + KYBER_COOLDOWN_S[0]
                return None
            if r.status_code == 429:
                self.kyber_429 += 1
                self._kyber_cool_until = time.monotonic() + self._kyber_cooldown
                log.warning("Kyber 429: cooling down %.0fs", self._kyber_cooldown)
                self._kyber_cooldown = min(self._kyber_cooldown * 2, KYBER_COOLDOWN_S[1])
                return None
            if r.is_success:
                self._kyber_cooldown = KYBER_COOLDOWN_S[0]
            return r

    # ---------- Elysium sell side ----------

    async def quote_elysium(self, token_in: str, token_out: str,
                            raw_amount: str, soft: bool = False) -> tuple[int, int] | None:
        """Raw Kyber quote on Elysium. Returns (amount_out_raw, hops)."""
        params = {"tokenIn": token_in, "tokenOut": token_out,
                  "amountIn": raw_amount, "gasInclude": "true"}
        for attempt in range(1 if soft else 3):
            try:
                r = await self._kyber_get(params, soft)
                if r is None:
                    return None  # cooling down after a 429
                if r.is_success:
                    s = (r.json().get("data") or {}).get("routeSummary") or {}
                    if not s.get("amountOut"):
                        return None
                    return int(s["amountOut"]), len(s.get("route") or [])
                log.warning("Kyber %s: %s", r.status_code, r.text[:140])
            except httpx.HTTPError as exc:
                log.warning("Kyber transport error (%s), attempt %d", exc, attempt + 1)
            await _backoff(attempt)
        return None
