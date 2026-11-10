"""Offline harness driving the real tick logic with recorded live data.

The build sandbox has no egress to api.hyperliquid.xyz, li.quest or
kyberswap, so the HTTP boundary is stubbed with responses those services
actually returned on 2026-11-10. The order book below is the real @334
KNTQ/USDC book. Everything below the HTTP layer -- book walking, spread
maths, thresholds, hysteresis, cooldown, message text -- is production code.
"""
from __future__ import annotations

import asyncio
import os

os.environ.update(
    TELEGRAM_BOT_TOKEN="test-token",
    TELEGRAM_CHAT_ID="868931721",
    REDIS_URL="rediss://unused",
    NOTIONAL_USD="3000",
    GO_SPREAD_PCT="3.0",
    URGENT_SPREAD_PCT="5.0",
    MAX_EXIT_LOSS_PCT="1.0",
    CORE_CHAINS="10,42161",
    ROTATING_CHAINS="8453",
    REQUEST_SPACING_SECONDS="0",
    ALERT_COOLDOWN_SECONDS="0",
)

import config  # noqa: E402
from hyperliquid import HyperCore  # noqa: E402
from main import tick  # noqa: E402
from markets import Markets  # noqa: E402

# Real ask side of @334, 2026-11-10 19:00 UTC
ASKS = [
    {"px": "0.33833", "sz": "883.77"},
    {"px": "0.3386", "sz": "36.56"},
    {"px": "0.33893", "sz": "17456.49"},
    {"px": "0.33903", "sz": "436.75"},
    {"px": "0.33906", "sz": "169.24"},
]
BIDS = [{"px": "0.33774", "sz": "586.12"}]

WORLD = {"elysium_px": 0.340817, "exit_loss": {10: 0.27, 42161: 3.02, 8453: 3.00}}


class R:
    def __init__(self, payload, code=200):
        self._p, self.status_code = payload, code
        self.is_success = 200 <= code < 300
        self.text = ""

    def json(self):
        return self._p

    def raise_for_status(self):
        return None


class FakeHttp:
    def __init__(self):
        self.sent = []

    async def post(self, url, json=None, **kw):
        if "hyperliquid" in url:
            return R({"coin": "@334", "levels": [BIDS, ASKS]})
        if "relay.link" in url:
            amt = int(json["amount"]) / 1e6
            loss = 3.05
            return R({"details": {
                "currencyOut": {"amountFormatted": str(amt * (1 - loss / 100)),
                                "currency": {"symbol": "USDC"}},
                "timeEstimate": 2}})
        if "telegram" in url:
            self.sent.append(json["text"])
            return R({"ok": True})
        return R({}, 404)

    async def get(self, url, params=None, headers=None, **kw):
        if "kyberswap" in url:
            qty = int(params["amountIn"]) / 1e18
            out = qty * WORLD["elysium_px"]
            return R({"data": {"routeSummary": {
                "amountOut": str(int(out * 1e6)), "route": [1, 2, 3]}}})
        if "li.quest" in url:
            chain = int(params["toChain"])
            amt = int(params["fromAmount"]) / 1e6
            loss = WORLD["exit_loss"].get(chain, 3.0)
            out = amt * (1 - loss / 100)
            return R({"tool": "lifiIntents" if loss < 1 else "relaydepository",
                      "action": {"toToken": {"decimals": 6}},
                      "estimate": {"toAmount": str(int(out * 1e6)),
                                   "toAmountMin": str(int(out * 0.995 * 1e6)),
                                   "executionDuration": 30}})
        return R({}, 404)


class FakeStore:
    def __init__(self):
        self.data = {}

    def get(self, f):
        return self.data.setdefault(f, {})

    def update(self, f, **v):
        self.get(f).update(v)

    def in_cooldown(self, f, s):
        return False

    def mark_alerted(self, f):
        pass


class TG:
    def __init__(self, h):
        self.http = h

    async def send(self, text):
        print("\n" + "=" * 58)
        for tag in ("<b>", "</b>", "<code>", "</code>"):
            text = text.replace(tag, "")
        print(text)
        print("=" * 58)
        return True


async def main() -> None:
    cfg = config.load()
    http = FakeHttp()
    core, markets, store, tg = HyperCore("@334", http), Markets(cfg, http), FakeStore(), TG(http)

    fill = await core.buy_fill(cfg.notional_usd, cfg.taker_fee_pct)
    print(f"Book walk: ${cfg.notional_usd:,.0f} -> {fill.qty:,.2f} KNTQ "
          f"@ {fill.avg_px:.5f} (worst level {fill.worst_px}), "
          f"ask depth ${fill.book_depth_usd:,.0f}, filled={fill.filled}")

    print("\n### 1. Live market right now: Elysium 0.340817, OP exit open")
    await tick(cfg, core, markets, store, tg, 0)

    print("\n### 2. Spread widens to ~3.5%, OP still open -> expect GO")
    WORLD["elysium_px"] = 0.3505
    await tick(cfg, core, markets, store, tg, 1)

    print("\n### 3. Same spread again -> must stay quiet (already armed)")
    await tick(cfg, core, markets, store, tg, 2)

    print("\n### 4. Spread 6%, every exit drained -> expect URGENT only")
    WORLD["elysium_px"] = 0.3600
    WORLD["exit_loss"] = {10: 3.01, 42161: 3.02, 8453: 3.00}
    store.data.clear()
    await tick(cfg, core, markets, store, tg, 3)

    print("\n### 5. Spread back to 1% -> silence")
    WORLD["elysium_px"] = 0.3425
    await tick(cfg, core, markets, store, tg, 4)

    print(f"\nTelegram messages sent: {len(http.sent)}")
    print(f"LI.FI requests: {markets.lifi_requests}  other: {markets.other_requests}  "
          f"hyperliquid: {core.request_count}")


if __name__ == "__main__":
    asyncio.run(main())
