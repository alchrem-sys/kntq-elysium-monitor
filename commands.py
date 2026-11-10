"""Telegram command surface: /spread, /pulls, /status.

Long-polls getUpdates in its own task. Telegram's API is free and unmetered,
so this costs nothing against the Upstash or LI.FI budgets except when a
command actually runs a sweep.

Commands that change behaviour are admin-only. /pulls spends LI.FI quota, so
it is admin-only too.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from config import Config
from markets import Markets, RateLimited
from routing import exit_order, remember

log = logging.getLogger("commands")

SPREAD_RE = re.compile(r"^/spread(?:@\S+)?(?:\s+([0-9]*\.?[0-9]+)\s*%?)?\s*$", re.I)
PULLS_RE = re.compile(r"^/pulls(?:@\S+)?\s*$", re.I)
STATUS_RE = re.compile(r"^/status(?:@\S+)?\s*$", re.I)
TELL_RE = re.compile(r"^/tell(?:@\S+)?(?:\s+([0-9]*\.?[0-9]+)\s*[kK]?)?\s*$", re.I)
TEST_RE = re.compile(r"^/test(?:@\S+)?\s*$", re.I)

# Ladder used to measure how much a route can absorb before it stops clearing
# the threshold. The cost curve is a cliff, not a slope, so a handful of rungs
# locates it well enough.
DEPTH_LADDER = (1.0, 2.0, 4.0, 8.0, 16.0)


class Runtime:
    """Values the operator can change at runtime, persisted in Redis."""

    def __init__(self, cfg: Config, store):
        self.cfg = cfg
        self.store = store
        saved = store.get("runtime").get("go_spread_pct")
        self.go_spread_pct: float = float(saved) if saved else cfg.go_spread_pct

    def set_go_spread(self, pct: float) -> None:
        self.go_spread_pct = pct
        self.store.update("runtime", go_spread_pct=pct)


class CommandServer:
    def __init__(self, cfg: Config, http: httpx.AsyncClient, markets: Markets,
                 store, runtime: Runtime, tg, snapshot):
        self.cfg = cfg
        self.http = http
        self.markets = markets
        self.store = store
        self.runtime = runtime
        self.tg = tg
        self.snapshot = snapshot          # callable -> dict of latest tick data
        self._offset: int | None = None
        self._base = f"https://api.telegram.org/bot{cfg.telegram_token}"

    def _is_admin(self, msg: dict) -> bool:
        if not self.cfg.telegram_admin_id:
            return True  # no admin configured: the bot is already private
        return str((msg.get("from") or {}).get("id")) == self.cfg.telegram_admin_id

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                r = await self.http.get(
                    f"{self._base}/getUpdates",
                    params={
                        "timeout": int(self.cfg.command_poll_seconds),
                        "offset": self._offset,
                        "allowed_updates": '["message"]',
                    },
                    timeout=self.cfg.command_poll_seconds + 10,
                )
                if not r.is_success:
                    log.warning("getUpdates %s: %s", r.status_code, r.text[:160])
                    await asyncio.sleep(5)
                    continue
                for update in r.json().get("result", []):
                    self._offset = update["update_id"] + 1
                    msg = update.get("message") or {}
                    text = (msg.get("text") or "").strip()
                    if text:
                        await self._dispatch(text, msg)
            except httpx.HTTPError as exc:
                log.warning("getUpdates transport error: %s", exc)
                await asyncio.sleep(5)
            except Exception as exc:  # noqa: BLE001 - command loop must survive
                log.exception("Command loop error: %s", exc)
                await asyncio.sleep(5)

    async def _dispatch(self, text: str, msg: dict) -> None:
        if m := SPREAD_RE.match(text):
            if not self._is_admin(msg):
                return
            await self._cmd_spread(m.group(1))
        elif PULLS_RE.match(text):
            if not self._is_admin(msg):
                return
            await self._cmd_pulls()
        elif m := TELL_RE.match(text):
            if not self._is_admin(msg):
                return
            await self._cmd_tell(m.group(1))
        elif TEST_RE.match(text):
            if not self._is_admin(msg):
                return
            await self._cmd_test()
        elif STATUS_RE.match(text):
            await self._cmd_status()

    async def _cmd_tell(self, value: str | None) -> None:
        """Arm a one-shot watch for an exit that can absorb a given size."""
        size = float(value) if value else self.cfg.notional_usd
        if size < 100:          # "/tell 3" means 3k
            size *= 1000
        self.store.update("tell", armed=True, min_usd=size)
        await self.tg.send(
            f"🔔 Стежу за виходом щонайменше на <b>${size:,.0f}</b>.\n"
            f"Прилетить одне повідомлення, щойно такий з'явиться: мережа, "
            f"маршрут, ціна і скільки він тримає.\n"
            f"Потім зніметься сам — викличеш <code>/tell</code> ще раз, якщо треба."
        )

    async def _cmd_test(self) -> None:
        """Full working, in English, so the arithmetic can be checked by hand."""
        snap = self.snapshot() or {}
        if not snap.get("test"):
            await self.tg.send("No completed tick yet — wait for the first one.")
            return
        t = snap["test"]
        lines = [
            "<b>CYCLE BREAKDOWN</b>  (all figures from the last tick)",
            "",
            f"<b>1. Buy on HyperCore spot</b>  KNTQ/USDC @334",
            f"    spend            {t['notional']:>12,.2f} USDC",
            f"    taker fee        {t['fee_pct']:>12.3f} %",
            f"    walked the book, avg fill {t['buy_px']:.6f}",
            f"    ask depth        {t['book_depth']:>12,.0f} USD",
            f"    received         {t['qty']:>12,.2f} KNTQ",
            "",
            f"<b>2. Bridge to Elysium</b>  canonical, 1:1, gas only",
            f"    KNTQ on Elysium  {t['qty']:>12,.2f}",
            "",
            f"<b>3. Sell on Elysium</b>  KyberSwap, every pool",
            f"    via HYPE         {t['hype_out']:>12,.4f} HYPE  ({t['hype_hops']} hops)",
            f"    HYPE -> USDC     {t['two_step']:>12,.2f} USDC  ({t['two_hops']} hops)",
            f"    direct -> USDC   {t['direct']:>12,.2f} USDC  ({t['direct_hops']} hops)",
            f"    using            {t['sell_used']:>12,.2f} USDC  ({t['sell_note']})",
            "",
            f"<b>4. Exit to {t['chain']}</b>",
            f"    provider         {t['provider']:>12}",
            f"    cost             {t['exit_loss']:>12.2f} %",
            f"    delivered        {t['final']:>12,.2f} USDC",
            "",
            "<b>RESULT</b>",
            f"    in               {t['notional']:>12,.2f} USDC",
            f"    out              {t['final']:>12,.2f} USDC",
            f"    gross spread     {t['spread']:>12.2f} %   (step 1 -> 3)",
            f"    exit cost        {-t['exit_loss']:>12.2f} %   (step 4)",
            f"    <b>net          {t['net']:>12.2f} %   {t['net_usd']:+,.2f} USD</b>",
        ]
        if t.get("hype_direct"):
            lines += ["", f"Bridging HYPE itself instead would deliver "
                          f"{t['hype_direct']:,.2f} USDC."]
        await self.tg.send("\n".join(lines))

    async def _cmd_spread(self, value: str | None) -> None:
        if value is None:
            await self.tg.send(
                f"Поточний поріг: <b>{self.runtime.go_spread_pct:.2f}%</b>\n"
                f"Змінити: <code>/spread 2.5%</code>"
            )
            return
        pct = float(value)
        if not 0.1 <= pct <= 50:
            await self.tg.send("Поріг має бути між 0.1% і 50%.")
            return
        old = self.runtime.go_spread_pct
        self.runtime.set_go_spread(pct)
        await self.tg.send(
            f"✅ Поріг спреду: <b>{old:.2f}% → {pct:.2f}%</b>\n"
            f"Алерт від {pct:.2f}% за наявності виходу ≤{self.cfg.max_exit_loss_pct:.2f}%."
        )

    async def _cmd_status(self) -> None:
        snap = self.snapshot() or {}
        if not snap:
            await self.tg.send("Ще не було жодного тіку.")
            return
        await self.tg.send(
            f"<b>Стан</b>\n"
            f"Спред: {snap.get('spread', float('nan')):+.2f}% "
            f"(поріг {self.runtime.go_spread_pct:.2f}%)\n"
            f"Купівля: {snap.get('buy_px', 0):.5f} · книга ${snap.get('depth', 0):,.0f}\n"
            f"Продаж: {snap.get('carrier', '?')} → {snap.get('final', 0):,.2f} USDC\n"
            f"Вихід: {snap.get('exit', 'не перевірявся')}"
        )

    async def _cmd_pulls(self) -> None:
        """Routes that can take the money out under the threshold, with depth."""
        snap = self.snapshot() or {}
        base = snap.get("proceeds") or {}
        if not base:
            await self.tg.send("Ще немає котирувань продажу — зачекай перший тік.")
            return

        budget = self.cfg.probes_hot * 2
        await self.tg.send(
            f"🔎 Шукаю виходи під {self.cfg.max_exit_loss_pct:.2f}% "
            f"(до {budget} перевірок, хвилина-дві)…"
        )
        rows: list[str] = []
        checked = 0
        truncated = False

        for chain_id, carrier in exit_order(self.cfg, self.store):
            if checked >= budget:
                break
            leg = base.get(carrier) or {}
            amount, reference = leg.get("amount"), leg.get("usd")
            if not amount or not reference:
                continue
            await asyncio.sleep(self.cfg.spacing_seconds)
            checked += 1
            try:
                q = await self.markets.exit_quote(chain_id, carrier, amount, reference)
            except RateLimited:
                truncated = True
                break
            except Exception:  # noqa: BLE001
                continue
            if q is None:
                remember(self.store, chain_id, carrier, 99.0)
                continue
            remember(self.store, chain_id, carrier, q.loss_pct)
            if q.loss_pct > self.cfg.max_exit_loss_pct:
                continue
            depth, capped = await self._measure_depth(chain_id, carrier, amount, reference)
            rows.append(
                f"<b>{q.chain}</b> · {carrier} · −{q.loss_pct:.2f}%\n"
                f"    {q.usdc_out:,.2f} USDC · <code>{q.provider}</code> · ~{q.seconds}с\n"
                f"    тримає {'щонайменше ' if capped else 'до '}<b>${depth:,.0f}</b>"
            )

        if not rows:
            tail = " (перервано лімітом LI.FI)" if truncated else ""
            await self.tg.send(
                f"Жодного виходу під {self.cfg.max_exit_loss_pct:.2f}% "
                f"серед {checked} перевірених маршрутів{tail}.\n"
                f"Інвентар солверів вичерпано — лишається канонічний міст, "
                f"0% і ~6 днів."
            )
            return
        head = f"🚰 <b>Доступні виходи</b>  ({checked} перевірено)\n\n"
        tail = "\n\n⚠️ Обірвано лімітом LI.FI, список неповний." if truncated else ""
        await self.tg.send(head + "\n\n".join(rows) + tail)

    async def _measure_depth(self, chain_id: int, carrier: str,
                             amount: float, reference: float) -> tuple[float, bool]:
        """Largest notional that still clears the threshold on this route.

        Returns (size, capped). `capped` means the ladder ran out while the
        route was still clearing, so the real depth is at least that much --
        the cost here is a cliff, so it holds flat until it suddenly doesn't.
        """
        best = reference
        capped = False
        for i, rung in enumerate(DEPTH_LADDER[1:], start=1):
            await asyncio.sleep(self.cfg.spacing_seconds)
            try:
                q = await self.markets.exit_quote(
                    chain_id, carrier, amount * rung, reference * rung
                )
            except RateLimited:
                break
            except Exception:  # noqa: BLE001
                break
            if q is None or q.loss_pct > self.cfg.max_exit_loss_pct:
                break
            best = reference * rung
            capped = i == len(DEPTH_LADDER) - 1
        return best, capped
