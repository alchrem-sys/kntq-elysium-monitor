"""Whale watch: push when a tracked wallet dumps KNTQ.

Listens on the Hyperliquid websocket rather than polling, so it costs no
request weight and sees a sell within a second of it filling. Two feeds per
wallet:

  * userFills -- sells on the KNTQ/USDC spot book (@334). By default only
    taker fills count (`crossed`): a market or marketable sell that hits
    bids, not a resting limit order someone else bought into. One market order
    lands as many fills and a TWAP as many orders, so sells are summed per
    wallet until the wallet goes quiet for SETTLE_S seconds (or MAX_WINDOW_S
    passes), and the total is compared with the threshold.
  * userNonFundingLedgerUpdates -- KNTQ leaving the wallet: to HyperEVM
    (0x2000…), to Elysium via EVM, or to another address. That is how the
    biggest holder moves size before selling it on a DEX, so it alerts too.

Hyperliquid allows 10 distinct users across user subscriptions per IP, which
caps the list at 10 wallets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

log = logging.getLogger("whales")

WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"
KNTQ_PAIR = "@334"
SYSTEM_PREFIX = "0x2000000000000000000000000000000000"  # HyperCore -> HyperEVM
SETTLE_S = 12.0
MAX_WINDOW_S = 90.0
MAX_WALLETS = 10
EXPLORER = "https://hypurrscan.io/address/"

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
WHALE_RE = re.compile(r"^/(whales?)(?:@\S+)?(?:\s+(.*))?$", re.I)

DEFAULT_WALLETS = {
    "0xa9b95f2a2e7ef219021efc5c04c32761b8553bbd": "11M HYPE-кит",
    "0xaf0fdd39e5d92499b0ed9f68693da99c0ec1e92e": "19M активний",
    "0x9b28837deb55b014f7a37f2812d6ec042ce87ca1": "2.8M трейдер",
    "0x11ec60c4ea23c182fda02616c375ffa0a7e424d0": "2.2M свіжий",
    "0xe867fbdad3291530e41530301ecb77693850c78e": "2M HYPE-трейдер",
    "0x549e6dd8453ed87fccdcf4f8b37162b10edc0533": "2.1M генезис",
}
DEFAULT_MIN_USD = 5_000.0


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


class WhaleWatch:
    def __init__(self, store, tg, pushover, http: httpx.AsyncClient):
        self.store = store
        self.tg = tg
        self.pushover = pushover
        self.http = http
        self._buf: dict[str, dict] = {}      # wallet -> pending sell aggregate
        self._seen: set[str] = set()         # fill/ledger ids already counted
        self._resubscribe = asyncio.Event()
        self.connected = False
        self.last_msg_ts = 0.0

    # ---------- settings ----------

    @property
    def cfg(self) -> dict:
        rec = self.store.get("whales")
        if "wallets" not in rec:
            self.store.update("whales", wallets=dict(DEFAULT_WALLETS),
                              min_usd=DEFAULT_MIN_USD, on=True, taker_only=True)
        elif "taker_only" not in rec:
            # v1.4.0 counted every sell from $10k; now market sells from $5k.
            self.store.update("whales", min_usd=DEFAULT_MIN_USD, taker_only=True)
        return rec

    @property
    def wallets(self) -> dict[str, str]:
        return dict(self.cfg.get("wallets") or {})

    @property
    def min_usd(self) -> float:
        return float(self.cfg.get("min_usd") or DEFAULT_MIN_USD)

    @property
    def taker_only(self) -> bool:
        """Count only sells that hit the book (market / marketable), not a
        resting limit order being filled by someone else's buy."""
        return bool(self.cfg.get("taker_only", True))

    @property
    def on(self) -> bool:
        return bool(self.cfg.get("on", True))

    def name(self, addr: str) -> str:
        return self.wallets.get(addr) or _short(addr)

    # ---------- websocket ----------

    async def run(self, stop: asyncio.Event) -> None:
        import websockets  # imported here so the rest of the bot runs without it

        backoff = 1.0
        flusher = asyncio.create_task(self._flush_loop(stop))
        try:
            while not stop.is_set():
                wallets = list(self.wallets)[:MAX_WALLETS]
                if not wallets:
                    await self._sleep(stop, 30)
                    continue
                try:
                    async with websockets.connect(WS_URL, ping_interval=None,
                                                  max_size=8 * 2**20) as ws:
                        for w in wallets:
                            for kind in ("userFills", "userNonFundingLedgerUpdates"):
                                await ws.send(json.dumps({"method": "subscribe",
                                                          "subscription": {"type": kind, "user": w}}))
                        self.connected = True
                        self._resubscribe.clear()
                        log.info("whale ws: watching %d wallet(s)", len(wallets))
                        backoff = 1.0
                        await self._pump(ws, stop)
                except Exception as exc:  # noqa: BLE001 - reconnect on anything
                    log.warning("whale ws dropped: %s", exc)
                finally:
                    self.connected = False
                if not stop.is_set():
                    await self._sleep(stop, backoff)
                    backoff = min(backoff * 2, 60.0)
        finally:
            flusher.cancel()

    async def _pump(self, ws, stop: asyncio.Event) -> None:
        last_ping = time.monotonic()
        while not stop.is_set() and not self._resubscribe.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
            except asyncio.TimeoutError:
                raw = None
            if time.monotonic() - last_ping > 30:
                await ws.send(json.dumps({"method": "ping"}))
                last_ping = time.monotonic()
            if raw is None:
                continue
            self.last_msg_ts = time.time()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ch, data = msg.get("channel"), msg.get("data") or {}
            if not isinstance(data, dict) or data.get("isSnapshot"):
                # Snapshots replay history on (re)connect; remember, don't alert.
                self._remember_snapshot(ch, data)
                continue
            if ch == "userFills":
                self._on_fills((data.get("user") or "").lower(), data.get("fills") or [])
            elif ch == "userNonFundingLedgerUpdates":
                user = (data.get("user") or "").lower()
                updates = data.get("nonFundingLedgerUpdates") or data.get("updates") or []
                for u in updates:
                    await self._on_ledger(user, u)

    def _remember_snapshot(self, ch, data) -> None:
        if not isinstance(data, dict):
            return
        for f in data.get("fills") or []:
            self._seen.add(f"f:{f.get('tid')}")
        for u in data.get("nonFundingLedgerUpdates") or data.get("updates") or []:
            self._seen.add(f"l:{u.get('hash')}:{u.get('time')}")

    # ---------- events ----------

    def _on_fills(self, user: str, fills: list) -> None:
        if user not in self.wallets:
            return
        for f in fills:
            key = f"f:{f.get('tid')}"
            if key in self._seen or f.get("coin") != KNTQ_PAIR or f.get("side") != "A":
                self._seen.add(key)
                continue
            self._seen.add(key)
            sz, px = float(f["sz"]), float(f["px"])
            taker = bool(f.get("crossed"))
            if self.taker_only and not taker:
                log.info("whale %s limit sell filled %.0f KNTQ @ %.5f (ignored)",
                         self.name(user), sz, px)
                continue
            b = self._buf.setdefault(user, {"usd": 0.0, "kntq": 0.0, "first": time.time(),
                                            "lo": px, "hi": px, "n": 0, "maker_usd": 0.0,
                                            "twap": False})
            if not taker:
                b["maker_usd"] += sz * px
            if f.get("twapId") is not None:
                b["twap"] = True
            b["usd"] += sz * px
            b["kntq"] += sz
            b["lo"], b["hi"] = min(b["lo"], px), max(b["hi"], px)
            b["n"] += 1
            b["last"] = time.time()
            log.info("whale %s sold %.0f KNTQ @ %.5f (window $%.0f)",
                     self.name(user), sz, px, b["usd"])

    async def _on_ledger(self, user: str, u: dict) -> None:
        key = f"l:{u.get('hash')}:{u.get('time')}"
        if key in self._seen or user not in self.wallets:
            return
        self._seen.add(key)
        d = u.get("delta") or {}
        if d.get("token") != "KNTQ" or (d.get("user") or "").lower() != user:
            return  # only KNTQ leaving the wallet
        amount = float(d.get("amount") or 0)
        usd = float(d.get("usdcValue") or 0) or amount * await self._mark()
        if not self.on or usd < self.min_usd:
            return
        dest = (d.get("destination") or "").lower()
        where = "на HyperEVM (міст)" if dest.startswith(SYSTEM_PREFIX) else f"на {_short(dest)}"
        title = f"🐋 {self.name(user)}: вивів KNTQ ${usd:,.0f}"
        body = (f"{amount:,.0f} KNTQ {where}. Часто так готують продаж на DEX / Elysium.")
        await self._alert(user, title, body)

    async def _flush_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(2)
            now = time.time()
            for user, b in list(self._buf.items()):
                if now - b["last"] < SETTLE_S and now - b["first"] < MAX_WINDOW_S:
                    continue
                self._buf.pop(user, None)
                if self.on and b["usd"] >= self.min_usd:
                    px = b["usd"] / b["kntq"]
                    how = "TWAP" if b["twap"] else "маркетом"
                    if b["maker_usd"] >= b["usd"] - 1:
                        how = "лімітками"
                    elif b["maker_usd"]:
                        how += f" (+${b['maker_usd']:,.0f} лімітками)"
                    title = f"🐋 {self.name(user)} ПРОДАЄ {how} ${b['usd']:,.0f}"
                    body = (f"{b['kntq']:,.0f} KNTQ на Hyperliquid spot, середня {px:.5f} "
                            f"({b['lo']:.5f}–{b['hi']:.5f}), {b['n']} філів.")
                    await self._alert(user, title, body)
            if len(self._seen) > 50_000:
                self._seen = set(list(self._seen)[-20_000:])

    async def _alert(self, user: str, title: str, body: str) -> None:
        log.info("WHALE ALERT %s | %s", title, body)
        url = EXPLORER + user
        try:
            if self.pushover is not None:
                await self.pushover.send(title, body, url=url, url_title="Hypurrscan")
        except Exception as exc:  # noqa: BLE001
            log.warning("whale push failed: %s", exc)
        await self.tg.send(f"<b>{title}</b>\n{body}\n<a href=\"{url}\">{_short(user)}</a>",
                           [[{"text": "Hypurrscan", "url": url}]])

    # ---------- REST helpers ----------

    async def _info(self, body: dict):
        r = await self.http.post(INFO_URL, json=body)
        r.raise_for_status()
        return r.json()

    async def _mark(self) -> float:
        try:
            mids = await self._info({"type": "allMids"})
            return float(mids.get(KNTQ_PAIR) or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    async def _sleep(self, stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ---------- /whales command ----------

    async def command(self, arg: str | None) -> bool:
        """Handle /whales [...]. Returns True when settings changed."""
        parts = (arg or "").split()
        sub = parts[0].lower() if parts else ""
        if sub in ("", "list", "status"):
            await self._cmd_list()
            return False
        if sub == "add" and len(parts) >= 2 and ADDR_RE.match(parts[1]):
            wallets = self.wallets
            if parts[1].lower() not in wallets and len(wallets) >= MAX_WALLETS:
                await self.tg.send(f"Максимум {MAX_WALLETS} гаманців (ліміт Hyperliquid). "
                                   f"Спершу прибери один: <code>/whales del 0x…</code>")
                return False
            wallets[parts[1].lower()] = " ".join(parts[2:]) or _short(parts[1])
            self.store.update("whales", wallets=wallets)
            self._resubscribe.set()
            await self.tg.send(f"➕ Стежу за {wallets[parts[1].lower()]} "
                               f"(<code>{parts[1].lower()}</code>)")
            return True
        if sub in ("del", "rm", "remove") and len(parts) >= 2:
            target = " ".join(parts[1:]).lower()
            wallets = self.wallets
            hit = next((a for a, n in wallets.items()
                        if a == target or n.lower() == target or a.startswith(target)), None)
            if not hit:
                await self.tg.send("Не знайшов такого гаманця в списку.")
                return False
            name = wallets.pop(hit)
            self.store.update("whales", wallets=wallets)
            self._resubscribe.set()
            await self.tg.send(f"➖ Більше не стежу за {name}")
            return True
        if sub == "min" and len(parts) >= 2:
            try:
                raw = parts[1].lower().replace("$", "").replace(",", "")
                value = float(raw[:-1]) * 1000 if raw.endswith("k") else float(raw)
            except ValueError:
                value = 0
            if value < 100:
                await self.tg.send("Приклад: <code>/whales min 10000</code> або <code>/whales min 25k</code>")
                return False
            self.store.update("whales", min_usd=value)
            await self.tg.send(f"Поріг продажу: <b>${value:,.0f}</b>")
            return True
        if sub in ("limits", "limit") and len(parts) >= 2 and parts[1].lower() in ("on", "off"):
            self.store.update("whales", taker_only=(parts[1].lower() == "off"))
            await self.tg.send("Лімітні продажі " + ("теж рахую" if parts[1].lower() == "on"
                                                      else "ігнорую — тільки маркет"))
            return True
        if sub in ("on", "off"):
            self.store.update("whales", on=(sub == "on"))
            await self.tg.send("🐋 Трекер китів " + ("увімкнено" if sub == "on" else "вимкнено"))
            return True
        if sub == "test":
            await self._alert(next(iter(self.wallets), "0x" + "0" * 40),
                              "🐋 ТЕСТ: кит ПРОДАЄ маркетом $12,345",
                              "Так виглядатиме алерт про продаж.")
            return False
        await self.tg.send(
            "<b>/whales</b> — список і баланси\n"
            "<code>/whales add 0x… назва</code> — додати\n"
            "<code>/whales del 0x…</code> — прибрати\n"
            "<code>/whales min 5000</code> — поріг, $\n"
            "<code>/whales limits on|off</code> — рахувати лімітні продажі\n"
            "<code>/whales on|off</code> · <code>/whales test</code>")
        return False

    async def _cmd_list(self) -> None:
        wallets = self.wallets
        mark = await self._mark()
        lines = []
        for addr, name in wallets.items():
            try:
                st = await self._info({"type": "spotClearinghouseState", "user": addr})
                bal = next((float(b["total"]) for b in st.get("balances", [])
                            if b.get("coin") == "KNTQ"), 0.0)
                lines.append(f"• <b>{name}</b> — {bal:,.0f} KNTQ (${bal * mark:,.0f})\n"
                             f"  <code>{addr}</code>")
            except Exception:  # noqa: BLE001
                lines.append(f"• <b>{name}</b> — баланс недоступний\n  <code>{addr}</code>")
        state = "🟢 онлайн" if self.connected else "🔴 перепідключення"
        await self.tg.send(
            f"🐋 <b>Кити KNTQ</b> · {state} · {'увімк' if self.on else 'вимк'}\n"
            f"Алерт: {'маркет-продаж' if self.taker_only else 'будь-який продаж'} або вивід "
            f"від <b>${self.min_usd:,.0f}</b> · KNTQ {mark:.5f}\n\n"
            + ("\n".join(lines) or "Список порожній."))
