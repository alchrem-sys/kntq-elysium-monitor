"""HYPE two-way spread with numbers recorded live on 2026-11-15 (HL book @107, Kyber Elysium quotes)."""
import asyncio
import os

os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="@t", REDIS_URL="rediss://unused")

from config import KYBER_NATIVE, USDC_ELYSIUM  # noqa: E402
from hype import HYPE_RE, HypeScan  # noqa: E402
from hyperliquid import HyperCore  # noqa: E402
from pin import SpreadPin  # noqa: E402

ASKS = [{"px": "66.002", "sz": "735.17"}, {"px": "66.01", "sz": "900"}]
BIDS = [{"px": "66.001", "sz": "0.64"}, {"px": "66.0", "sz": "500"}, {"px": "65.99", "sz": "900"}]


class Cfg:
    taker_fee_pct = 0.07
    clear_margin_pct = 0.4


class Core:
    buy_from = staticmethod(HyperCore.buy_from)
    sell_into = staticmethod(HyperCore.sell_into)

    def __init__(self):
        self.ok = True

    async def sides(self):
        return (BIDS, ASKS) if self.ok else None


class Markets:
    def __init__(self):
        self.sell_usdc, self.buy_hype = 4942.923919, 76.319341
        self.calls = []

    async def quote_elysium(self, tin, tout, raw):
        self.calls.append((tin, tout, raw))
        if tin == KYBER_NATIVE and tout == USDC_ELYSIUM:
            return (int(self.sell_usdc * 1e6), 4) if self.sell_usdc else None
        if tin == USDC_ELYSIUM and tout == KYBER_NATIVE:
            return (int(self.buy_hype * 1e18), 4) if self.buy_hype else None
        raise AssertionError("unexpected pair")


class Store:
    def __init__(self):
        self.data = {}

    def get(self, f):
        return self.data.setdefault(f, {})

    def update(self, f, **kw):
        self.get(f).update(kw)


class Tg:
    def __init__(self):
        self.sent = []

    async def send(self, text, buttons=None):
        self.sent.append(text)
        return True


def make():
    pushes, tg, store, mk, core = [], Tg(), Store(), Markets(), Core()

    async def push(title, message, priority=None, url=None, url_title=None):
        pushes.append(title)
    return HypeScan(Cfg(), core, mk, store, tg, push), core, mk, store, tg, pushes


async def main():
    h, core, mk, store, tg, pushes = make()
    s = await h.round(5000.0)
    # forward: 5000$ buys 75.7553 HYPE (+0.07% fee = 5003.50), sold for 4942.92 on Elysium
    assert abs(s["fwd"]["pct"] - (-1.2107)) < 0.01 and "rev" not in s, s
    s = await h.round(5000.0)
    # reverse: 5000 USDC -> 76.3193 HYPE -> 0.64@66.001 + rest@66.0 = 5037.08, minus fee = 5033.55
    assert abs(s["rev"]["pct"] - 0.6710) < 0.01 and "fwd" in s, s
    assert len(mk.calls) == 2                               # one Kyber quote per round
    assert mk.calls[0][:2] == (KYBER_NATIVE, USDC_ELYSIUM) and mk.calls[1] == (USDC_ELYSIUM, KYBER_NATIVE, "5000000000")
    assert not tg.sent and not pushes                      # no threshold set: silent
    print("\n".join(h.lines()))

    async def both():
        await h.round(5000.0)
        return await h.round(5000.0)

    # /hype 0.5 -> reverse side is above: one alert, then quiet until it climbs 0.5 more
    assert await h.command("0.5") and store.get("hype")["pct"] == 0.5
    tg.sent.clear()
    await both()
    assert len(pushes) == 1 and "Elysium → HL" in pushes[0] and "Купити на Elysium" in tg.sent[-1], (pushes, tg.sent)
    await both()
    assert len(pushes) == 1
    mk.buy_hype = 76.80                                     # +1.30%: escalation
    await both()
    assert len(pushes) == 2
    mk.buy_hype = 75.70                                     # below threshold - margin: re-arm
    await both()
    assert store.get("hype")["at_rev"] is None
    # forward crossing fires its own alert
    mk.sell_usdc = 5060.0
    await both()
    assert len(pushes) == 3 and "HL → Elysium" in pushes[-1]

    # Kyber cooling down on one side: its last reading stays, gets an age mark, and never alerts
    mk.sell_usdc = None
    before = dict(h.snap["fwd"])
    assert await h.round(5000.0) is None and h.snap["fwd"] == before and len(pushes) == 3
    h.snap["fwd"]["ts"] -= 100
    assert "⏳" in "\n".join(h.lines())
    core.ok = False
    assert await h.round(5000.0) is None                    # no book: nothing changes
    core.ok = True
    await h.round(3000.0)                                   # the lap size changed: old readings are dropped
    assert h.snap["size"] == 3000.0 and len([d for d in ("fwd", "rev") if d in h.snap]) == 1
    mk.sell_usdc = 5060.0
    await both(); await both()

    await h.command("off")
    assert store.get("hype")["pct"] is None
    assert HYPE_RE.match("/hype").group(1) is None and HYPE_RE.match("/hype@bot 1.5%").group(1) == "1.5"
    assert HYPE_RE.match("/hype off").group(1) == "off" and not HYPE_RE.match("/hyperliquid")

    # the pinned message carries the HYPE block under the KNTQ one
    class RT:
        go_spread_pct, spread_poll_seconds = 3.0, 10.0
    import time
    snap = {"spread": 0.42, "spread_ts": time.time(), "buy_px": 0.2583, "size": 5000.0,
            "sell_used": 5021.0, "qty": 19357.0}
    store.update("pin", size=5000.0)
    pin = SpreadPin(None, store, tg, RT(), lambda: snap, h)
    text = await pin._render()
    assert "KNTQ спред" in text and "HYPE спред · $5,000" in text and "Elysium → HL" in text, text
    assert [b["text"] for b in pin._buttons()["inline_keyboard"][0]] == ["KNTQ Spot", "HYPE Spot", "Bridge"]
    print("---\n" + text + "\n\nhype ok")


asyncio.run(main())
