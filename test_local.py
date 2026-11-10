"""Offline harness driving the real tick logic with recorded live data.

The build sandbox has no egress to api.hyperliquid.xyz, li.quest or kyberswap,
so the HTTP boundary is stubbed with numbers those services actually returned
on 2026-11-10: the real @334 ask book, the real KNTQ sell quotes into both
carriers, and exit costs in the range measured that evening. Everything below
the HTTP layer is production code.
"""
from __future__ import annotations

import asyncio
import os

os.environ.update(
    TELEGRAM_BOT_TOKEN="test-token",
    TELEGRAM_CHAT_ID="@test",
    TELEGRAM_ADMIN_ID="868931721",
    REDIS_URL="rediss://unused",
    NOTIONAL_USD="3000",
    GO_SPREAD_PCT="3.0",
    URGENT_SPREAD_PCT="5.0",
    MAX_EXIT_LOSS_PCT="1.0",
    REQUEST_SPACING_SECONDS="0",
    ALERT_COOLDOWN_SECONDS="0",
)

import config  # noqa: E402
from commands import CommandServer, Runtime  # noqa: E402
from hyperliquid import HyperCore  # noqa: E402
from main import snapshot, tick  # noqa: E402
from markets import Markets  # noqa: E402

ASKS = [
    {"px": "0.33833", "sz": "883.77"},
    {"px": "0.3386", "sz": "36.56"},
    {"px": "0.33893", "sz": "17456.49"},
]
MID = 71.7145
PX_USDC = 3070.24 / 9000          # KNTQ -> USDC, measured
PX_HYPE = 43.2537 / 9000          # KNTQ -> HYPE, measured

W = {"mult": 1.0, "exit": {("USDC", 10): 0.27, ("HYPE", 10): 0.76}, "default": 3.02}


class R:
    def __init__(self, p, code=200):
        self._p, self.status_code = p, code
        self.is_success = 200 <= code < 300
        self.text = ""

    def json(self):
        return self._p


class FakeHttp:
    def __init__(self):
        self.sent = []

    async def post(self, url, json=None, **kw):
        if "hyperliquid" in url:
            if json.get("type") == "allMids":
                return R({"HYPE": str(MID)})
            return R({"coin": "@334", "levels": [[], ASKS]})
        if "relay.link" in url:
            return R({"details": {"currencyOut": {"amountFormatted": "1"}, "timeEstimate": 2}})
        if "telegram" in url:
            self.sent.append(json["text"])
            return R({"ok": True})
        return R({}, 404)

    async def get(self, url, params=None, headers=None, **kw):
        if "kyberswap" in url:
            qty = int(params["amountIn"]) / 1e18
            if params["tokenOut"].lower().startswith("0xeeee"):
                return R({"data": {"routeSummary": {
                    "amountOut": str(int(qty * PX_HYPE * W["mult"] * 1e18)), "route": [1, 2]}}})
            return R({"data": {"routeSummary": {
                "amountOut": str(int(qty * PX_USDC * W["mult"] * 1e6)), "route": [1, 2, 3]}}})
        if "li.quest" in url:
            chain = int(params["toChain"])
            carrier = "HYPE" if params["fromToken"].startswith("0x0000") else "USDC"
            raw = int(params["fromAmount"])
            face = raw / 1e18 * MID if carrier == "HYPE" else raw / 1e6
            loss = W["exit"].get((carrier, chain), W["default"])
            out = face * (1 - loss / 100)
            return R({"tool": "lifiIntents" if loss < 1 else "relaydepository",
                      "action": {"toToken": {"decimals": 6}},
                      "estimate": {"toAmount": str(int(out * 1e6)),
                                   "toAmountMin": str(int(out * 0.995 * 1e6)),
                                   "executionDuration": 30}})
        if "getUpdates" in url:
            return R({"result": []})
        return R({}, 404)


class FakeStore:
    def __init__(self):
        self.data = {}

    def get(self, f):
        return self.data.setdefault(f, {})

    def update(self, f, **v):
        self.get(f).update(v)

    def touch(self):
        pass

    def in_cooldown(self, f, s):
        return False

    def mark_alerted(self, f):
        pass


class TG:
    def __init__(self, h):
        self.http = h

    async def send(self, text):
        print("\n" + "=" * 62)
        for t in ("<b>", "</b>", "<code>", "</code>"):
            text = text.replace(t, "")
        print(text)
        print("=" * 62)
        return True


async def main() -> None:
    cfg = config.load()
    http = FakeHttp()
    core, markets, store = HyperCore("@334", http), Markets(cfg, http), FakeStore()
    tg = TG(http)
    rt = Runtime(cfg, store)
    cmds = CommandServer(cfg, http, markets, store, rt, tg, snapshot)
    print(f"Chains: {len(cfg.chains)}  carriers: USDC, HYPE  threshold: {rt.go_spread_pct}%")

    print("\n### 1. Spread ~1% -> silence, no exit probes")
    await tick(cfg, rt, core, markets, store, tg, 1)

    print("\n### 2. Spread lifted above 3%: HYPE sells 1% better, USDC exits cheaper")
    print("        -> decision must be made on USDC actually delivered")
    W["mult"] = 1.025
    await tick(cfg, rt, core, markets, store, tg, 2)

    print("\n### 3. /spread 1.5% -> threshold lowered and persisted")
    await cmds._cmd_spread("1.5")

    print("\n### 4. Back to ~3.5% spread with the lower threshold still armed")
    store.data["go"]["armed"] = False
    await tick(cfg, rt, core, markets, store, tg, 3)

    print("\n### 5. /pulls -> only routes under 1%, with depth")
    await cmds._cmd_pulls()

    print("\n### 6. Spread 6%, every chain drained -> URGENT")
    W["mult"] = 1.06
    W["exit"] = {}
    store.data.clear()
    rt2 = Runtime(cfg, store)
    await tick(cfg, rt2, core, markets, store, tg, 4)

    print(f"\nLI.FI requests: {markets.lifi_requests}")


if __name__ == "__main__":
    asyncio.run(main())
