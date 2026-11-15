"""HYPE two-way spread with numbers recorded live on 2026-11-15 (HL book @107, Kyber Elysium quotes)."""
import asyncio
import os

os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="@t", REDIS_URL="rediss://unused")

from config import KYBER_NATIVE, USDC_ELYSIUM  # noqa: E402
from hype import HYPE_RE, HypeScan  # noqa: E402
from hyperliquid import HyperCore  # noqa: E402
from pin import SpreadPin  # noqa: E402

ASKS = [{"px": "66.002", "sz": "735.17"}, {"px": "66.01", "sz": "900"}]
BIDS = [{"px": "66.001", "sz": "0.64"}]


class Cfg:
    taker_fee_pct = 0.07
    clear_margin_pct = 0.4


class Core:
    buy_from = staticmethod(HyperCore.buy_from)

    def __init__(self):
        self.ok = True

    async def sides(self):
        return (BIDS, ASKS) if self.ok else None


class Markets:
    def __init__(self):
        self.sell_usdc, self.buy_hype = 4942.923919, 76.319341
        self.calls = []

    async def quote_elysium(self, tin, tout, raw, soft=False):
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
    # 5000$ buys 75.7553 HYPE (+0.07% fee = 5003.50), sold for 4942.92 on Elysium
    assert abs(s["pct"] - (-1.2107)) < 0.01, s
    assert mk.calls == [(KYBER_NATIVE, USDC_ELYSIUM, mk.calls[0][2])]          # one quote, HL -> Elysium only
    assert not tg.sent and not pushes and h.pct == 1.0                         # 1% by default, not reached
    text = "\n".join(h.lines())
    assert "HYPE спред · $5,000" in text and "-1.21%" in text and "Elysium →" not in text, text
    print(text)

    # crossing 1% pushes once, then again only after another +0.5%
    mk.sell_usdc = 5060.0                                   # +1.13%
    await h.round(5000.0)
    assert len(pushes) == 1 and "HYPE спред +1.13%" in pushes[0] and "USDC</b> на Elysium" in tg.sent[-1]
    await h.round(5000.0)
    assert len(pushes) == 1
    mk.sell_usdc = 5090.0                                   # +1.73%
    await h.round(5000.0)
    assert len(pushes) == 2
    mk.sell_usdc = 5020.0                                   # +0.33% < 1 - 0.4: re-arm
    await h.round(5000.0)
    assert store.get("hype")["at"] is None
    mk.sell_usdc = 5060.0
    await h.round(5000.0)
    assert len(pushes) == 3

    # a miss keeps the last reading and never alerts; an old reading gets an age mark
    mk.sell_usdc = None
    before = dict(h.snap)
    assert await h.round(5000.0) is None and h.snap == before
    core.ok = False
    assert await h.round(5000.0) is None
    h.snap["ts"] -= 200
    assert "⏳" in "\n".join(h.lines())
    core.ok, mk.sell_usdc = True, 5060.0

    # /hype 2 moves the threshold, /hype off silences, /hype shows
    assert await h.command("2") and h.pct == 2.0
    await h.round(5000.0)
    assert len(pushes) == 3
    assert await h.command("off") and h.pct is None
    mk.sell_usdc = 5200.0
    await h.round(5000.0)
    assert len(pushes) == 3
    assert await h.command("1") and h.pct == 1.0
    assert not await h.command(None) and "Алерт: пуш від <b>1.00%</b>" in tg.sent[-1]
    assert HYPE_RE.match("/hype").group(1) is None and HYPE_RE.match("/hype@bot 1.5%").group(1) == "1.5"
    assert HYPE_RE.match("/hype off").group(1) == "off" and not HYPE_RE.match("/hyperliquid")

    # the pinned message carries the HYPE block under the KNTQ one
    class RT:
        go_spread_pct, spread_poll_seconds = 3.0, 10.0
    import time
    snap = {"spread": 0.42, "spread_ts": time.time(), "buy_px": 0.2583, "size": 5000.0,
            "sell_used": 5021.0, "qty": 19357.0}
    store.update("pin", size=5000.0)
    mk.sell_usdc = 5060.0
    await h.round(5000.0)
    pin = SpreadPin(None, store, tg, RT(), lambda: snap, h)
    text = await pin._render()
    assert "KNTQ спред" in text and "HYPE спред · $5,000" in text and "🟢 <b>+1.13%</b>" in text, text
    assert "Elysium → HL" not in text
    assert [b["text"] for b in pin._buttons()["inline_keyboard"][0]] == ["KNTQ Spot", "HYPE Spot", "Bridge"]
    print("---\n" + text + "\n\nhype ok")


asyncio.run(main())
