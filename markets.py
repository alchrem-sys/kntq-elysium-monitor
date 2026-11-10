"""Elysium sell side (KyberSwap) and exit routes off Elysium (LI.FI, Relay).

KNTQ can leave Elysium through two carriers, and which one is better moves:

  * selling into USDC quotes ~1% worse on the DEX, because Elysium USDC
    trades under a dollar -- people leaving dump it cheap
  * selling into HYPE quotes ~1% better, but solvers know HYPE is dearer on
    Elysium and price the bridge accordingly, so some or all of that is
    handed straight back

Rather than mark HYPE at Hyperliquid spot and argue about the difference,
both carriers are measured end to end from the same KNTQ quantity to USDC
on the destination chain. The winner is whichever delivers more dollars.
That comparison needs no price feed and cannot be fooled by either leg.
"""
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
    KYBER_NATIVE,
    LIFI_NATIVE,
    USDC_BY_CHAIN,
    USDC_ELYSIUM,
    Config,
)

log = logging.getLogger("markets")

KYBER_BASE = "https://aggregator-api.kyberswap.com/elysium/api/v1"
KYBER_HEADERS = {"x-client-id": "kntq-elysium-monitor"}
RELAY_QUOTE = "https://api.relay.link/quote"

# carrier -> (kyber sell-to token, lifi/relay origin token, decimals)
CARRIERS: dict[str, tuple[str, str, int]] = {
    "USDC": (USDC_ELYSIUM, USDC_ELYSIUM, 6),
    "HYPE": (KYBER_NATIVE, LIFI_NATIVE, 18),
}


class RateLimited(Exception):
    """LI.FI asked us to back off. Skip remaining probes this tick."""


@dataclass
class Sale:
    carrier: str
    kntq_in: float
    amount_out: float     # human units of the carrier token
    hops: int


@dataclass
class Exit:
    chain_id: int
    carrier: str
    provider: str
    amount_in: float      # carrier units sent
    usdc_out: float       # USDC delivered on the destination chain
    usdc_out_min: float
    loss_pct: float       # vs the carrier's own face/spot value, for display
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

    async def quote_elysium(self, token_in: str, token_out: str,
                            raw_amount: str) -> tuple[int, int] | None:
        """Raw Kyber quote on Elysium. Returns (amount_out_raw, hops)."""
        params = {"tokenIn": token_in, "tokenOut": token_out,
                  "amountIn": raw_amount, "gasInclude": "true"}
        for attempt in range(3):
            try:
                self.other_requests += 1
                r = await self.http.get(f"{KYBER_BASE}/routes", params=params, headers=KYBER_HEADERS)
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

    async def sell_kntq(self, qty: float, carrier: str) -> Sale | None:
        """What `qty` KNTQ fetches in `carrier` on Elysium, across every pool."""
        to_token, _, decimals = CARRIERS[carrier]
        params = {
            "tokenIn": KNTQ_ELYSIUM,
            "tokenOut": to_token,
            "amountIn": str(int(qty * 1e18)),
            "gasInclude": "true",
        }
        for attempt in range(3):
            try:
                self.other_requests += 1
                r = await self.http.get(f"{KYBER_BASE}/routes", params=params, headers=KYBER_HEADERS)
                if r.is_success:
                    summary = (r.json().get("data") or {}).get("routeSummary") or {}
                    if not summary.get("amountOut"):
                        log.warning("Kyber: no %s route for %.2f KNTQ", carrier, qty)
                        return None
                    return Sale(
                        carrier=carrier,
                        kntq_in=qty,
                        amount_out=int(summary["amountOut"]) / (10 ** decimals),
                        hops=len(summary.get("route") or []),
                    )
                log.warning("Kyber %s: %s", r.status_code, r.text[:160])
            except httpx.HTTPError as exc:
                log.warning("Kyber transport error (%s), attempt %d", exc, attempt + 1)
            await _backoff(attempt)
        return None

    # ---------- exits ----------

    async def exit_quote(self, chain_id: int, carrier: str, amount: float,
                         reference_usd: float) -> Exit | None:
        """Move `amount` of `carrier` from Elysium to USDC on `chain_id`.

        `reference_usd` is what that amount is nominally worth, used only to
        render a loss percentage. Decisions are made on usdc_out.
        """
        lifi = await self._lifi_exit(chain_id, carrier, amount, reference_usd)
        if lifi and lifi.loss_pct <= self.cfg.max_exit_loss_pct:
            return lifi
        relay = await self._relay_exit(chain_id, carrier, amount, reference_usd)
        candidates = [q for q in (lifi, relay) if q is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda q: q.usdc_out)

    def _raw(self, carrier: str, amount: float) -> str:
        _, _, decimals = CARRIERS[carrier]
        return str(int(amount * (10 ** decimals)))

    async def _lifi_exit(self, chain_id: int, carrier: str, amount: float,
                         reference_usd: float) -> Exit | None:
        to_token = USDC_BY_CHAIN.get(chain_id)
        if not to_token:
            return None
        _, from_token, _ = CARRIERS[carrier]
        params = {
            "fromChain": str(ELYSIUM_CHAIN_ID),
            "toChain": str(chain_id),
            "fromToken": from_token,
            "toToken": to_token,
            "fromAmount": self._raw(carrier, amount),
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
                return None
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
                chain_id=chain_id, carrier=carrier,
                provider=str(data.get("tool", "lifi")),
                amount_in=amount, usdc_out=out, usdc_out_min=out_min,
                loss_pct=100.0 * (1 - out / reference_usd) if reference_usd else 0.0,
                seconds=int(est.get("executionDuration") or 0),
            )
        return None

    async def _relay_exit(self, chain_id: int, carrier: str, amount: float,
                          reference_usd: float) -> Exit | None:
        to_token = USDC_BY_CHAIN.get(chain_id)
        if not to_token:
            return None
        _, from_token, _ = CARRIERS[carrier]
        body = {
            "user": self.cfg.quote_address,
            "recipient": self.cfg.quote_address,
            "originChainId": ELYSIUM_CHAIN_ID,
            "destinationChainId": chain_id,
            "originCurrency": from_token,
            "destinationCurrency": to_token,
            "amount": self._raw(carrier, amount),
            "tradeType": "EXACT_INPUT",
        }
        for attempt in range(2):
            try:
                self.other_requests += 1
                r = await self.http.post(RELAY_QUOTE, json=body)
                if not r.is_success:
                    return None
                details = r.json().get("details") or {}
                out = float((details.get("currencyOut") or {}).get("amountFormatted") or 0)
                if out <= 0:
                    return None
                return Exit(
                    chain_id=chain_id, carrier=carrier, provider="relay",
                    amount_in=amount, usdc_out=out, usdc_out_min=out,
                    loss_pct=100.0 * (1 - out / reference_usd) if reference_usd else 0.0,
                    seconds=int(details.get("timeEstimate") or 0),
                )
            except httpx.HTTPError as exc:
                log.warning("Relay transport error (%s), attempt %d", exc, attempt + 1)
                await _backoff(attempt)
        return None
