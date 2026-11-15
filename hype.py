"""HYPE spread from HyperCore spot to Elysium: buy HYPE on the HyperCore book
(taker fee), bridge, sell it for USDC on Elysium.

HYPE is Elysium's native coin, so KyberSwap takes the native placeholder.

Kyber's Elysium endpoint starts refusing at roughly ten requests a minute, and
the KNTQ quote already uses six. So HYPE takes one quote every HYPE_EVERY-th
round of the fast loop, halfway between two KNTQ quotes: about every 30 s.
"""
from __future__ import annotations

import logging
import re
import time

from config import (BRIDGE_URL, HYPE_USDC_SPOT_URL, KYBER_HYPE_URL, KYBER_NATIVE, PUSH_QUIET_S,
                    USDC_ELYSIUM)

log = logging.getLogger("hype")

HYPE_RE = re.compile(r"^/hype(?:@\S+)?(?:\s+(off|[0-9]*\.?[0-9]+)\s*%?)?\s*$", re.I)
HYPE_EVERY = 3              # fast-loop rounds per HYPE quote
ESCALATION_PCT = 0.5        # fire again when the spread climbs this much above the last alert
DEFAULT_PCT = 1.0           # push threshold until /hype sets another


def buttons() -> list[list[dict]]:
    return [[{"text": "HYPE Spot", "url": HYPE_USDC_SPOT_URL}, {"text": "Bridge", "url": BRIDGE_URL}],
            [{"text": "Kyber HYPE", "url": KYBER_HYPE_URL}]]


class HypeScan:
    def __init__(self, cfg, core, markets, store, tg, push):
        self.cfg, self.core, self.markets = cfg, core, markets
        self.store, self.tg, self.push = store, tg, push
        self.snap: dict = {}

    @property
    def rec(self) -> dict:
        return self.store.get("hype")

    @property
    def pct(self) -> float | None:
        """Push threshold; None when switched off."""
        rec = self.rec
        if rec.get("off"):
            return None
        return float(rec.get("pct") or DEFAULT_PCT)

    async def round(self, size: float) -> dict | None:
        """What a lap of `size` dollars makes right now. None on a miss: the last reading stays."""
        sides = await self.core.sides()
        if sides is None:
            return None
        bought = self.core.buy_from(sides[1], size, self.cfg.taker_fee_pct)
        if not bought:
            return None
        qty, cost = bought
        q = await self.markets.quote_elysium(KYBER_NATIVE, USDC_ELYSIUM, str(int(qty * 1e18)), soft=True)
        if not q:
            return None                     # Kyber cooling down
        usdc = q[0] / 1e6
        self.snap = {"size": size, "ts": time.time(), "pct": 100.0 * (usdc / cost - 1.0),
                     "buy_px": cost / qty, "sell_px": usdc / qty, "out": usdc, "qty": qty}
        await self._alert(self.snap)
        return self.snap

    async def _alert(self, s: dict) -> None:
        pct = self.pct
        if not pct:
            return
        spread, fired_at, size = s["pct"], self.rec.get("at"), s["size"]
        if spread >= pct and (fired_at is None or spread >= fired_at + ESCALATION_PCT):
            self.store.update("hype", at=round(spread, 3))
            gain = size * spread / 100
            loud = time.time() - (self.rec.get("pushed_ts") or 0) >= PUSH_QUIET_S
            if loud:        # one loud push an hour; the message below is sent every time
                self.store.update("hype", pushed_ts=time.time())
                await self.push(f"HYPE спред {spread:+.2f}%",
                                f"Поріг {pct:.2f}%. ${size:,.0f} → {s['out']:,.2f} USDC на Elysium "
                                f"({gain:+,.0f}$ валом).",
                                url=HYPE_USDC_SPOT_URL, url_title="HYPE Spot")
            await self.tg.send(f"📈 <b>HYPE спред {spread:+.2f}%</b>  (поріг {pct:.2f}%)\n\n"
                               f"Купівля HL {s['buy_px']:.3f} (з комісією)\n"
                               f"${size:,.0f} → {s['qty']:,.2f} HYPE → <b>{s['out']:,.2f} USDC</b> на Elysium\n"
                               f"Валом {gain:+,.0f}$"
                               + ("" if loud else "\n🔇 без пуша: Pushover мовчить годину після останнього"),
                               buttons())
            log.info("HYPE alert at %+.2f%%", spread)
        elif fired_at is not None and spread < pct - self.cfg.clear_margin_pct:
            self.store.update("hype", at=None)
            log.info("HYPE re-armed at %+.2f%%", spread)

    def lines(self, stale_s: float = 100.0) -> list[str]:
        """Block for the pinned message, /status and /hype."""
        s = self.snap
        if not s:
            return ["<b>HYPE спред</b>", "⏳ рахую перше котирування…"]
        pct, spread = self.pct, s["pct"]
        mark = "🟢" if (pct and spread >= pct) else ("🟡" if spread > 0 else "🔴")
        out = [f"<b>HYPE спред · ${s['size']:,.0f}</b>",
               f"{mark} <b>{spread:+.2f}%</b>  →  <b>{s['size'] * spread / 100:+,.0f}$</b>",
               f"Купівля HL: {s['buy_px']:.3f} (з комісією)",
               f"Продаж Elysium: {s['sell_px']:.3f} → {s['out']:,.2f} USDC"]
        age = time.time() - s["ts"]
        if age > stale_s:
            out.append(f"⏳ котирування HYPE {age:.0f}с тому")
        return out

    async def command(self, arg: str | None) -> bool:
        cur = self.pct
        if arg is None:
            state = f"пуш від <b>{cur:.2f}%</b>" if cur else "пуш вимкнено"
            await self.tg.send("\n".join(self.lines()) + f"\n\nАлерт: {state}\n"
                               "<code>/hype 1</code> — поріг пуша · <code>/hype off</code>", buttons())
            return False
        if arg.lower() == "off":
            self.store.update("hype", off=True, at=None)
            await self.tg.send("🔕 Пуш по HYPE вимкнено. Спред далі видно в закріпленому і в /hype.")
            return True
        pct = float(arg)
        if not 0.1 <= pct <= 50:
            await self.tg.send("Поріг має бути між 0.1% і 50%.")
            return False
        self.store.update("hype", pct=pct, off=False, at=None, pushed_ts=None)
        await self.tg.send(f"👁 HYPE: пуш у Pushover, коли спред HL → Elysium вище <b>{pct:.2f}%</b>. "
                           f"Pushover після пуша мовчить годину, повідомлення тут ідуть далі.")
        return True
