"""Pinned spread: one message at the top of the group, edited in place.

Shows what a fixed lap (default $5k) makes right now -- buy on the
HyperCore book with the taker fee, sell on Elysium through KyberSwap -- so
nobody has to send /status. It makes no requests of its own: the fast
spread loop quotes at the pin's size and kicks the pin with each result, so
a pinned message adds nothing to the Kyber rate limit. When a quote is
missing (Kyber cooling down), the last good numbers stay up with their age.
The message id lives in the settings key, so a redeploy keeps editing the
same pin instead of posting a new one.

Telegram allows roughly one edit per second per chat; this edits at most
every MIN_EDIT_S and only when the text actually changes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from config import BRIDGE_URL, HYPE_SPOT_URL

log = logging.getLogger("pin")

PIN_RE = re.compile(r"^/pin(?:@\S+)?(?:\s+(off|on|[0-9]*\.?[0-9]+\s*[kK]?))?\s*$", re.I)
DEFAULT_SIZE = 5000.0
MIN_EDIT_S = 10.0
FORCE_EDIT_S = 60.0      # refresh the clock even if the numbers did not move
FOMO_PCT = 0.05          # exit cost through fomo, measured ~0.04%
STALE_S = 45.0           # older than this, the numbers get an age label
try:
    from zoneinfo import ZoneInfo
    WARSAW = ZoneInfo("Europe/Warsaw")
except Exception:  # noqa: BLE001 - no tz database in the image
    WARSAW = timezone(timedelta(hours=1))


def _now_local() -> str:
    return datetime.now(WARSAW).strftime("%H:%M:%S")


class SpreadPin:
    def __init__(self, cfg, store, tg, runtime, snapshot):
        self.cfg = cfg
        self.store = store
        self.tg = tg
        self.runtime = runtime
        self.snapshot = snapshot      # callable -> fast-loop data
        self._last_text = ""
        self._last_edit = 0.0
        self._kick = asyncio.Event()

    # ---------- settings ----------

    @property
    def rec(self) -> dict:
        return self.store.get("pin")

    @property
    def on(self) -> bool:
        return bool(self.rec.get("on", True))

    @property
    def size(self) -> float:
        return float(self.rec.get("size") or DEFAULT_SIZE)

    def kick(self) -> None:
        self._kick.set()

    # ---------- loop ----------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            interval = max(MIN_EDIT_S, float(self.runtime.spread_poll_seconds))
            if self.on:
                try:
                    await self._round()
                except Exception as exc:  # noqa: BLE001 - never kill the loop
                    log.warning("pin round failed: %s", exc)
            self._kick.clear()
            try:
                await asyncio.wait_for(self._wait_any(stop), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _wait_any(self, stop: asyncio.Event) -> None:
        done, pending = await asyncio.wait(
            [asyncio.create_task(stop.wait()), asyncio.create_task(self._kick.wait())],
            return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

    async def _round(self) -> None:
        now = time.monotonic()
        if now - self._last_edit < MIN_EDIT_S - 0.5:
            return  # Telegram caps edits per chat; the next kick catches up
        text = await self._render()
        body = text.split("\n<i>", 1)[0]   # ignore the clock when comparing
        if body == self._last_text.split("\n<i>", 1)[0] and now - self._last_edit < FORCE_EDIT_S:
            return
        try:
            await self._publish(text)
        except RuntimeError:
            return
        self._last_text, self._last_edit = text, now

    async def _render(self) -> str:
        size = self.size
        snap = self.snapshot() or {}
        ts = snap.get("spread_ts")
        if ts is None or abs((snap.get("size") or 0) - size) > 0.5:
            return (f"📌 <b>KNTQ спред · ${size:,.0f}</b>\n"
                    f"⏳ рахую перше котирування…\n<i>{_now_local()}</i>")
        spread = snap["spread"]
        sell = snap["sell_used"]
        net_usd = size * (1 + spread / 100) * (1 - FOMO_PCT / 100) - size
        net_pct = 100.0 * net_usd / size
        go = self.runtime.go_spread_pct
        mark = "🟢" if spread >= go else ("🟡" if spread > 0 else "🔴")
        lines = [
            f"📌 <b>KNTQ спред · ${size:,.0f}</b>",
            f"{mark} <b>{spread:+.2f}%</b>  →  чистими <b>{net_usd:+,.0f}$</b> ({net_pct:+.2f}%)",
            f"Купівля HL: {snap['buy_px']:.5f} (з комісією)",
            f"Продаж Elysium: {sell / snap['qty']:.5f} → {sell:,.2f} USDC",
        ]
        if spread < 0:
            lines.append(f"↩️ Elysium дешевший на {-spread:.2f}% — зворотний бік")
        age = time.time() - ts
        stamp = datetime.fromtimestamp(ts, WARSAW).strftime("%H:%M:%S")
        if age > STALE_S:
            lines.append(f"⏳ котирування {age:.0f}с тому — Kyber пригальмовує, тримаю останнє")
        lines.append(f"<i>котирування {stamp} · поріг {go:.2f}%</i>")
        return "\n".join(lines)

    def _buttons(self):
        return {"inline_keyboard": [[{"text": "Hype Spot", "url": HYPE_SPOT_URL},
                                     {"text": "Bridge", "url": BRIDGE_URL}]]}

    async def _publish(self, text: str) -> None:
        mid = self.rec.get("message_id")
        if mid:
            r = await self.tg.call("editMessageText", message_id=mid, text=text,
                                   parse_mode="HTML", disable_web_page_preview=True,
                                   reply_markup=self._buttons())
            desc = r.get("description") or ""
            if r.get("ok") or "not modified" in desc:
                return
            if "not found" not in desc and "can't be edited" not in desc:
                # Telegram hiccup (502, timeout): keep the pin, try next round.
                log.warning("pin edit failed (%s); will retry", desc)
                self._last_text = ""
                raise RuntimeError(desc)
            log.warning("pin message gone (%s); posting a new one", desc)
        r = await self.tg.call("sendMessage", text=text, parse_mode="HTML",
                               disable_web_page_preview=True, disable_notification=True,
                               reply_markup=self._buttons())
        if not r.get("ok"):
            log.warning("pin send failed: %s", r.get("description"))
            raise RuntimeError(r.get("description") or "send failed")
        mid = r["result"]["message_id"]
        p = await self.tg.call("pinChatMessage", message_id=mid, disable_notification=True)
        if not p.get("ok"):
            log.warning("pin failed (bot needs 'Pin messages' right): %s", p.get("description"))
        self.store.update("pin", message_id=mid)
        await self.store.save_settings()
        log.info("pinned spread message %s", mid)

    # ---------- /pin ----------

    async def command(self, arg: str | None) -> bool:
        arg = (arg or "").strip().lower()
        if arg == "off":
            mid = self.rec.get("message_id")
            if mid:
                await self.tg.call("unpinChatMessage", message_id=mid)
            self.store.update("pin", on=False, message_id=None)
            await self.tg.send("📌 Закріплений спред вимкнено.")
            return True
        if arg and arg != "on":
            raw = arg.replace(" ", "")
            size = float(raw[:-1]) * 1000 if raw.endswith("k") else float(raw)
            if not 100 <= size <= 200_000:
                await self.tg.send("Сума від $100 до $200k, напр. <code>/pin 5000</code>")
                return False
            self.store.update("pin", size=size)
        if arg == "on" or not self.on:
            self.store.update("pin", on=True)
        if not arg:
            # /pin with no argument: post a fresh pin (old one may be buried)
            old = self.rec.get("message_id")
            if old:
                await self.tg.call("unpinChatMessage", message_id=old)
            self.store.update("pin", message_id=None)
        self._last_text = ""
        self._kick.set()
        return True
