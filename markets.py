"""Elysium sell side (KyberSwap) and exit routes off Elysium (LI.FI, Relay)."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import httpx

from config import (
    CHAIN_NAMES,
    ELYSIUM_CHAIN_ID,
    KNTQ_ELYSIUM,
    USDC_BY_CHAIN,
    USDC_ELYSIUM,
    Config,
)

log = logging.getLogger("markets")

KYBER_BASE = "https://aggregator-api.kyberswap.com/elysium/api/v1"
RELAY_QUOTE = "https://api.relay.link/quote"


class RateLimited(Exception):
    """LI.FI asked us to back off. Skip the remaining exit probes this tick."""


@dataclass
class Sale:
    kntq_in: float
    usdc_out: float
    hops: int


@dataclass
class Exit:
    chain_id: int
    provider: str      # lifi tool name, or "relay"
    usdc_in: float
    usdc_out: float
    loss_pct: float
    worst_loss_pct: float
    seconds: int

    @property
    def chain(self) -> str:
        return CHAIN_NAMES.get(self.chain_id, str(self.chain_id))


async def _backoff(attempt: int) -> None:
    delay = min(2.0 ** attempt, 10.0)
    await asyncio.sleep(delay + random.uniform(0, 0.4 * delay))


class Markets:
    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self.cfg = cfg
        self.http = http
        self.lifi_requests = 0
        self.other_requests = 0

    # ---------- Elysium sell side ----------

    async def sell_kntq(self, qty: float) -> Sale | None:
        """What `qty` KNTQ fetches in USDC on Elysium, routed across every pool."""
        raw = int(qty * 1e18)
        params = {
            "tokenIn": KNTQ_ELYSIUM,
            "tokenOut": USDC_ELYSIUM,
            "amountIn": str(raw),
            "gasInclude": "true",
        }
        for attempt in range(3):
            try:
                self.other_requests += 1
                r = await self.http.get(f"{KYBER_BASE}/routes", params=params)
                if r.is_success:
                    summary = (r.json().get("data") or {}).get("routeSummary") or {}
                    if not summary.get("amountOut"):
                        log.warning("Kyber returned no route for %.2f KNTQ", qty)
                        return None
                    return Sale(
                        kntq_in=qty,
                        usdc_out=int(summary["amountOut"]) / 1e6,
                        hops=len(summary.get("route") or []),
                    )
                log.warning("Kyber %s: %s", r.status_code, r.text[:160])
            except httpx.HTTPError as exc:
                log.warning("Kyber transport error (%s), attempt %d", exc, attempt + 1)
            await _backoff(attempt)
        return None

    # ---------- exits ----------

    async def exit_quote(self, chain_id: int, usdc_amount: float) -> Exit | None:
        """Cheapest way to move `usdc_amount` of Elysium USDC to `chain_id`.

        LI.FI first: its intent solvers are the only sub-1% path off Elysium.
        Relay is the fallback, and in practice prices around 3% on every chain.
        """
        lifi = await self._lifi_exit(chain_id, usdc_amount)
        if lifi and lifi.loss_pct <= self.cfg.max_exit_loss_pct:
            return lifi
        relay = await self._relay_exit(chain_id, usdc_amount)
        candidates = [q for q in (lifi, relay) if q is not None]
        if not candidates:
            return None
        return min(candidates, key=lambda q: q.loss_pct)

    async def _lifi_exit(self, chain_id: int, usdc_amount: float) -> Exit | None:
        to_token = USDC_BY_CHAIN.get(chain_id)
        if not to_token:
            return None
        params = {
            "fromChain": str(ELYSIUM_CHAIN_ID),
            "toChain": str(chain_id),
            "fromToken": USDC_ELYSIUM,
            "toToken": to_token,
            "fromAmount": str(int(usdc_amount * 1e6)),
            "fromAddress": self.cfg.quote_address,
            "toAddress": self.cfg.quote_address,
        }
        headers = {"x-lifi-api-key": self.cfg.lifi_api_key} if self.cfg.lifi_api_key else {}
        for attempt in range(3):
            try:
                self.lifi_requests += 1
                r = await self.http.get(
                    f"{self.cfg.lifi_base}/quote", params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                log.warning("LI.FI transport error (%s), attempt %d", exc, attempt + 1)
                await _backoff(attempt)
                continue

            if r.status_code == 429 or "rate limit" in r.text.lower():
                raise RateLimited(r.text[:160])
            if r.status_code == 404:
                return None  # no route for this pair; not retryable
            if not r.is_success:
                log.warning("LI.FI %s: %s", r.status_code, r.text[:160])
                await _backoff(attempt)
                continue

            data = r.json()
            est = data.get("estimate") or {}
            decimals = int(((data.get("action") or {}).get("toToken") or {}).get("decimals", 6))
            try:
                out = int(est["toAmount"]) / (10 ** decimals)
                out_min = int(est.get("toAmountMin", est["toAmount"])) / (10 ** decimals)
            except (KeyError, TypeError, ValueError):
                return None
            return Exit(
                chain_id=chain_id,
                provider=str(data.get("tool", "lifi")),
                usdc_in=usdc_amount,
                usdc_out=out,
                loss_pct=100.0 * (1 - out / usdc_amount),
                worst_loss_pct=100.0 * (1 - out_min / usdc_amount),
                seconds=int(est.get("executionDuration") or 0),
            )
        return None

    async def _relay_exit(self, chain_id: int, usdc_amount: float) -> Exit | None:
        to_token = USDC_BY_CHAIN.get(chain_id)
        if not to_token:
            return None
        body = {
            "user": self.cfg.quote_address,
            "recipient": self.cfg.quote_address,
            "originChainId": ELYSIUM_CHAIN_ID,
            "destinationChainId": chain_id,
            "originCurrency": USDC_ELYSIUM,
            "destinationCurrency": to_token,
            "amount": str(int(usdc_amount * 1e6)),
            "tradeType": "EXACT_INPUT",
        }
        for attempt in range(2):
            try:
                self.other_requests += 1
                r = await self.http.post(RELAY_QUOTE, json=body)
                if not r.is_success:
                    return None
                details = (r.json().get("details") or {})
                cur_out = details.get("currencyOut") or {}
                out = float(cur_out.get("amountFormatted") or 0)
                if out <= 0:
                    return None
                return Exit(
                    chain_id=chain_id,
                    provider="relay",
                    usdc_in=usdc_amount,
                    usdc_out=out,
                    loss_pct=100.0 * (1 - out / usdc_amount),
                    worst_loss_pct=100.0 * (1 - out / usdc_amount),
                    seconds=int(details.get("timeEstimate") or 0),
                )
            except httpx.HTTPError as exc:
                log.warning("Relay transport error (%s), attempt %d", exc, attempt + 1)
                await _backoff(attempt)
        return None
