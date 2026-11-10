"""HyperCore spot buy side: walk the KNTQ/USDC book for a real fill price.

A mid price is not good enough here. The edge being measured is a couple of
percent, so the buy has to be priced the way it actually executes: eat the ask
side level by level until the notional is filled, then add the taker fee.

Note on the info API: spotMetaAndAssetCtxs returns universe and contexts as two
arrays that are NOT aligned by position -- universe[297] is pair "@334" while
contexts[297].coin is "@297". Match on the coin field, never on the index.
This module avoids the problem entirely by reading l2Book directly.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import httpx

log = logging.getLogger("hypercore")

INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass
class Fill:
    qty: float              # base units acquired
    avg_px: float           # average price paid, before fees
    cost_usd: float         # notional spent, before fees
    worst_px: float         # deepest level touched
    book_depth_usd: float   # total ask depth available
    filled: bool            # False if the book could not cover the notional


class HyperCore:
    def __init__(self, coin: str, http: httpx.AsyncClient):
        self.coin = coin
        self.http = http
        self.request_count = 0

    async def _post(self, body: dict) -> dict | list | None:
        delay = 1.0
        for attempt in range(4):
            try:
                self.request_count += 1
                r = await self.http.post(INFO_URL, json=body)
                if r.is_success:
                    return r.json()
                log.warning("Hyperliquid %s: %s", r.status_code, r.text[:160])
            except httpx.HTTPError as exc:
                log.warning("Hyperliquid transport error (%s), attempt %d", exc, attempt + 1)
            await asyncio.sleep(delay + random.uniform(0, 0.4 * delay))
            delay = min(delay * 2, 8.0)
        return None

    async def buy_fill(self, notional_usd: float, taker_fee_pct: float) -> Fill | None:
        """What buying `notional_usd` of the base token actually costs right now."""
        book = await self._post({"type": "l2Book", "coin": self.coin})
        if not isinstance(book, dict):
            return None
        levels = book.get("levels") or []
        if len(levels) < 2:
            log.warning("Book for %s had no levels", self.coin)
            return None
        asks = levels[1] or []
        if not asks:
            log.warning("Book for %s has an empty ask side", self.coin)
            return None

        total_depth = sum(float(l["px"]) * float(l["sz"]) for l in asks)
        remaining = notional_usd
        qty = 0.0
        spent = 0.0
        worst = 0.0

        for level in asks:
            px = float(level["px"])
            sz = float(level["sz"])
            level_value = px * sz
            if level_value >= remaining:
                take = remaining / px
                qty += take
                spent += remaining
                worst = px
                remaining = 0.0
                break
            qty += sz
            spent += level_value
            worst = px
            remaining -= level_value

        if remaining > 0 or qty <= 0:
            log.warning(
                "Ask side of %s cannot fill $%.0f (depth $%.0f)",
                self.coin, notional_usd, total_depth,
            )
            return Fill(qty, spent / qty if qty else 0.0, spent, worst, total_depth, False)

        # Taker fee is charged on the notional; express it as a worse average price.
        fee = spent * (taker_fee_pct / 100.0)
        effective_cost = spent + fee
        return Fill(
            qty=qty,
            avg_px=effective_cost / qty,
            cost_usd=effective_cost,
            worst_px=worst,
            book_depth_usd=total_depth,
            filled=True,
        )
