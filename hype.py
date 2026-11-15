"""HYPE spread between HyperCore spot and Elysium, both ways.

  HL -> Elysium   buy HYPE on the HyperCore book, sell it for USDC on Elysium
  Elysium -> HL   buy HYPE with USDC on Elysium, sell it on the HyperCore book

HYPE is Elysium's native coin, so KyberSwap takes the native placeholder. The
second direction assumes the USDC is already on Elysium (e.g. after a KNTQ
lap) and counts it at one dollar; Elysium USDC usually trades a bit under.

Kyber's Elysium endpoint starts refusing at roughly ten requests a minute, and
the KNTQ quote already uses six. So HYPE takes one quote every HYPE_EVERY-th
round of the fast loop, halfway between two KNTQ quotes, the two directions
taking turns: each direction is refreshed about once a minute.
"""
from __future__ import annotations

import logging
import re
import time

from config import BRIDGE_URL, HYPE_USDC_SPOT_URL, KYBER_HYPE_URL, KYBER_NATIVE, USDC_ELYSIUM

log = logging.getLogger("hype")

HYPE_RE = re.compile(r"^/hype(?:@\S+)?(?:\s+(off|[0-9]*\.?[0-9]+)\s*%?)?\s*$", re.I)
HYPE_EVERY = 3              # fast-loop rounds per HYPE quote
ESCALATION_PCT = 0.5        # fire again when the spread climbs this much above the last alert
DIRS = {"fwd": "HL → Elysium", "rev": "Elysium → HL"}


def buttons() -> list[list[dict]]:
    return [[{"text": "HYPE Spot", "url": HYPE_USDC_SPOT_URL}, {"text": "Bridge", "url": BRIDGE_URL}],
            [{"text": "Kyber HYPE", "url": KYBER_HYPE_URL}]]


class HypeScan:
    def __init__(self, cfg, core, markets, store, tg, push):
        self.cfg, self.core, self.markets = cfg, core, markets
        self.store, self.tg, self.push = store, tg, push
        self.snap: dict = {}
        self._turn = 0

    @property
    def rec(self) -> dict:
        return self.store.get("hype")

    async def round(self, size: float) -> dict | None:
        """Measure one direction per call, taking turns, so HYPE adds a single Kyber
        request to each round of the fast loop. The other side keeps its last reading."""
        fee = self.cfg.taker_fee_pct
        sides = await self.core.sides()
        if sides is None:
            return None
        bids, asks = sides
        which = "fwd" if self._turn % 2 == 0 else "rev"
        self._turn += 1
        side = None
        if which == "fwd":
            bought = self.core.buy_from(asks, size, fee)
            if bought:
                qty, cost = bought
                q = await self.markets.quote_elysium(KYBER_NATIVE, USDC_ELYSIUM, str(int(qty * 1e18)), soft=True)
                if q:
                    usdc = q[0] / 1e6
                    side = {"pct": 100.0 * (usdc / cost - 1.0), "buy_px": cost / qty,
                            "sell_px": usdc / qty, "out": usdc}
        else:
            q = await self.markets.quote_elysium(USDC_ELYSIUM, KYBER_NATIVE, str(int(size * 1e6)), soft=True)
            if q:
                qty = q[0] / 1e18
                got = self.core.sell_into(bids, qty, fee)
                if got:
                    side = {"pct": 100.0 * (got / size - 1.0), "buy_px": size / qty,
                            "sell_px": got / qty, "out": got}
        if side is None:
            return None                     # Kyber cooling down or a thin book: the last reading stays
        side["ts"] = time.time()
        keep = {d: v for d, v in self.snap.items() if d in DIRS and self.snap.get("size") == size}
        self.snap = {**keep, which: side, "size": size, "ts": side["ts"]}
        await self._alert(which, side, size)
        return self.snap

    async def _alert(self, d: str, side: dict, size: float) -> None:
        pct = self.rec.get("pct")
        if not pct:
            return
        label, spread, fired_at = DIRS[d], side["pct"], self.rec.get(f"at_{d}")
        if spread >= pct and (fired_at is None or spread >= fired_at + ESCALATION_PCT):
            self.store.update("hype", **{f"at_{d}": round(spread, 3)})
            gain = size * spread / 100
            how = (f"Купити на HL по {side['buy_px']:.3f}, продати на Elysium по {side['sell_px']:.3f}"
                   if d == "fwd" else
                   f"Купити на Elysium по {side['buy_px']:.3f}, продати на HL по {side['sell_px']:.3f}")
            await self.push(f"HYPE {label} {spread:+.2f}%",
                            f"Поріг {pct:.2f}%. ${size:,.0f} → {side['out']:,.2f} ({gain:+,.0f}$ валом).",
                            url=HYPE_USDC_SPOT_URL, url_title="HYPE Spot")
            await self.tg.send(f"📈 <b>HYPE {label}: {spread:+.2f}%</b>  (поріг {pct:.2f}%)\n\n"
                               f"{how}\n${size:,.0f} → <b>{side['out']:,.2f}</b> · валом {gain:+,.0f}$",
                               buttons())
            log.info("HYPE %s alert at %+.2f%%", d, spread)
        elif fired_at is not None and spread < pct - self.cfg.clear_margin_pct:
            self.store.update("hype", **{f"at_{d}": None})
            log.info("HYPE %s re-armed at %+.2f%%", d, spread)

    def lines(self, stale_s: float = 150.0) -> list[str]:
        """Block for the pinned message and /status."""
        s = self.snap
        if not s:
            return ["<b>HYPE</b>: ще рахую…"]
        pct = self.rec.get("pct")
        out = [f"<b>HYPE спред · ${s['size']:,.0f}</b>"]
        for d, label in DIRS.items():
            side = s.get(d)
            if not side:
                out.append(f"⏳ {label}: немає котирування")
                continue
            mark = "🟢" if (pct and side["pct"] >= pct) else ("🟡" if side["pct"] > 0 else "🔴")
            age = time.time() - side["ts"]
            out.append(f"{mark} {label}: <b>{side['pct']:+.2f}%</b> ({size_gain(s['size'], side['pct'])}) · "
                       f"{side['buy_px']:.3f} → {side['sell_px']:.3f}" + (f" ⏳{age:.0f}с" if age > stale_s else ""))
        return out

    async def command(self, arg: str | None) -> bool:
        cur = self.rec.get("pct")
        if arg is None:
            state = f"пуш від <b>{cur:.2f}%</b>" if cur else "пуш вимкнено"
            await self.tg.send("\n".join(self.lines()) + f"\n\nАлерт: {state}\n"
                               "<code>/hype 1</code> — пуш, коли будь-який бік вище 1% · <code>/hype off</code>",
                               buttons())
            return False
        if arg.lower() == "off":
            self.store.update("hype", pct=None, at_fwd=None, at_rev=None)
            await self.tg.send("🔕 Алерт по HYPE вимкнено. Спред далі видно в закріпленому і в /hype.")
            return True
        pct = float(arg)
        if not 0.1 <= pct <= 50:
            await self.tg.send("Поріг має бути між 0.1% і 50%.")
            return False
        self.store.update("hype", pct=pct, at_fwd=None, at_rev=None)
        await self.tg.send(f"👁 HYPE: пуш, коли HL → Elysium або Elysium → HL вище <b>{pct:.2f}%</b>.")
        return True


def size_gain(size: float, pct: float) -> str:
    return f"{size * pct / 100:+,.0f}$"
