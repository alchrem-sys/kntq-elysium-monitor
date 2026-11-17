"""Telegram command surface: /only, /status, /pin, /hype, /whales, access and Pushover.

Long-polls getUpdates in its own task. Telegram's API is free and unmetered.
Commands that change behaviour are admin-only.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from config import Config
from markets import Markets
from hype import HYPE_RE
from pin import PIN_RE
from whales import WHALE_RE

log = logging.getLogger("commands")

STATUS_RE = re.compile(r"^/status(?:@\S+)?\s*$", re.I)
# Telegram sends messages from anonymous group admins as this user.
ANON_ADMIN_ID = "1087968824"
PUSHOVER_RE = re.compile(r"^/pushover(?:@\S+)?(?:\s+(\S+))?(?:\s+(\S+))?\s*$", re.I)
ALLOW_RE = re.compile(r"^/(allow|deny)(?:@\S+)?(?:\s+(\d+))?\s*$", re.I)
MYID_RE = re.compile(r"^/myid(?:@\S+)?\s*$", re.I)
POLL_RE = re.compile(r"^/poll(?:@\S+)?(?:\s+([0-9]*\.?[0-9]+))?\s*$", re.I)
ONLY_RE = re.compile(r"^/(?:only|spread)(?:@\S+)?(?:\s+(off|[0-9]*\.?[0-9]+)\s*%?)?\s*$", re.I)

class Runtime:
    """Values the operator can change at runtime, persisted in Redis."""

    def __init__(self, cfg: Config, store):
        self.cfg = cfg
        self.store = store

    @property
    def spread_poll_seconds(self) -> float:
        saved = self.store.get("runtime").get("spread_poll_seconds")
        return float(saved) if saved else self.cfg.spread_poll_seconds

    def set_poll(self, seconds: float) -> None:
        self.store.update("runtime", spread_poll_seconds=seconds)



class CommandServer:
    def __init__(self, cfg: Config, http: httpx.AsyncClient, markets: Markets,
                 store, runtime: Runtime, tg, snapshot, pushover=None, whales=None, pin=None,
                 hype=None):
        self.hype = hype
        self.cfg = cfg
        self.http = http
        self.markets = markets
        self.store = store
        self.runtime = runtime
        self.tg = tg
        self.snapshot = snapshot          # callable -> dict of latest tick data
        self.pushover = pushover
        self.whales = whales
        self.pin = pin
        self._denied: dict[str, float] = {}
        self._offset: int | None = None
        self._base = f"https://api.telegram.org/bot{cfg.telegram_token}"

    def _allowed(self) -> set[str]:
        return set(self.cfg.telegram_admin_ids) | set(self.store.get("access").get("ids") or [])

    def _is_owner(self, msg: dict) -> bool:
        uid = str((msg.get("from") or {}).get("id"))
        return not self.cfg.telegram_admin_ids or uid in self.cfg.telegram_admin_ids

    def _is_admin(self, msg: dict) -> bool:
        if not self.cfg.telegram_admin_ids:
            return True  # no admin configured: the bot is already private
        return str((msg.get("from") or {}).get("id")) in self._allowed()

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

    async def _deny(self, msg: dict, uid: str, name: str) -> None:
        """Say why a command was refused, at most once per person per 10 min.

        Silence made a missing grant indistinguishable from a broken bot.
        """
        now = time.monotonic()
        last = self._denied.get(uid)
        if last is not None and now - last < 600:
            return
        self._denied[uid] = now
        if uid == ANON_ADMIN_ID or msg.get("sender_chat"):
            await self.tg.send(
                "⛔ Ти пишеш анонімно, від імені групи — бот не бачить, хто ти.\n"
                "Налаштування групи → Адміністратори → ти → вимкни "
                "«Remain anonymous / Анонімність»."
            )
            return
        await self.tg.send(
            f"⛔ {name}, у тебе немає доступу до команд. Твій ID: <code>{uid}</code>\n"
            f"Власник має відповісти <code>/allow</code> на твоє повідомлення."
        )

    async def _dispatch(self, text: str, msg: dict) -> None:
        sender = msg.get("from") or {}
        uid = str(sender.get("id"))
        name = sender.get("first_name") or uid
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if not cmd:
            return

        async def gate() -> bool:
            ok = self._is_admin(msg)
            log.info("cmd %s from %s (%s) -> %s", cmd, uid, name, "ok" if ok else "DENIED")
            if not ok:
                await self._deny(msg, uid, name)
            return ok

        changed = False
        if MYID_RE.match(text):
            log.info("cmd /myid from %s (%s)", uid, name)
            await self.tg.send(f"{name}: <code>{uid}</code>")
        elif m := ALLOW_RE.match(text):
            ok = self._is_owner(msg)
            log.info("cmd /%s from %s (%s) -> %s", m.group(1), uid, name,
                     "ok" if ok else "DENIED (owner only)")
            if ok:
                await self._cmd_allow(m.group(1).lower(), m.group(2), msg)
                changed = True
            else:
                await self.tg.send("Доступ видає тільки власник бота.")
        elif m := PUSHOVER_RE.match(text):
            if await gate():
                await self._cmd_pushover(m.group(1), msg, m.group(2))
                changed = True
        elif m := POLL_RE.match(text):
            if await gate():
                await self._cmd_poll(m.group(1))
                changed = True
        elif m := ONLY_RE.match(text):
            if await gate():
                await self._cmd_only(m.group(1))
                changed = True
        elif STATUS_RE.match(text):
            await self._cmd_status()
        elif (m := PIN_RE.match(text)) and self.pin is not None:
            if await gate():
                changed = await self.pin.command(m.group(1))
        elif (m := HYPE_RE.match(text)) and self.hype is not None:
            if m.group(1) is None:
                await self.hype.command(None)        # reading the spread is open, like /status
            elif await gate():
                changed = await self.hype.command(m.group(1))
        elif (m := WHALE_RE.match(text)) and self.whales is not None:
            if await gate():
                changed = await self.whales.command(m.group(2))

        if changed:
            try:
                await self.store.save_settings()
            except Exception as exc:  # noqa: BLE001
                log.error("Could not save settings: %s", exc)

    async def _cmd_poll(self, value: str | None) -> None:
        if value is None:
            await self.tg.send(
                f"Спред перевіряю кожні <b>{self.runtime.spread_poll_seconds:g} с</b>.\n"
                f"Змінити: <code>/poll 5</code> (від 3 до 120)")
            return
        sec = float(value)
        if not 3 <= sec <= 120:
            await self.tg.send("Інтервал має бути від 3 до 120 секунд.")
            return
        self.runtime.set_poll(sec)
        await self.tg.send(f"⏱ Тепер перевіряю спред кожні <b>{sec:g} с</b>.")

    async def _delete(self, msg: dict) -> bool:
        """Remove the message that carried the user key, so it doesn't sit in the chat."""
        try:
            r = await self.http.post(f"{self._base}/deleteMessage", json={
                "chat_id": (msg.get("chat") or {}).get("id"),
                "message_id": msg.get("message_id")})
            return bool(r.is_success and r.json().get("ok"))
        except Exception:  # noqa: BLE001
            return False

    async def _cmd_pushover(self, value: str | None, msg: dict,
                            arg: str | None = None) -> None:
        po = self.pushover
        if po is None:
            await self.tg.send("Pushover не підключено в цій збірці.")
            return
        uid = str((msg.get("from") or {}).get("id"))
        name = (msg.get("from") or {}).get("first_name") or uid
        if not value:
            users = po.users()
            who = ", ".join(f"…{k[-4:]}" for k in users.values()) or "<b>нікого</b>"
            await self.tg.send(
                f"Pushover: app token {'є' if po.app_token else '<b>немає</b> (PUSHOVER_APP_TOKEN)'}\n"
                f"Підключено: {who} · пріоритет {po.priority}\n"
                f"Кожен підключає свій ключ сам.\n"
                f"<code>/pushover USER_KEY</code> — задати · "
                f"<code>/pushover test</code> — перевірити · "
                f"<code>/pushover off</code> — вимкнути · "
                f"<code>/pushover priority 2</code> — екстрений")
            return
        low = value.lower()
        if low == "off":
            po.set_user(uid, None)
            await self.tg.send(f"🔕 Pushover для {name} вимкнено.")
            return
        if low == "priority":
            names = {0: "звичайний", 1: "високий", 2: "екстрений (повторює до підтвердження)"}
            if arg is None:
                await self.tg.send(f"Пріоритет пушів: <b>{po.priority}</b> — {names.get(po.priority, '?')}\n"
                                   f"<code>/pushover priority 1</code> високий · "
                                   f"<code>/pushover priority 2</code> екстрений")
                return
            if arg not in ("0", "1", "2"):
                await self.tg.send("Пріоритет: 0, 1 або 2.")
                return
            po.set_priority(int(arg))
            await self.tg.send(f"✅ Пріоритет пушів: <b>{arg}</b> — {names[int(arg)]}")
            return
        if low == "test":
            if not po.ready:
                await self.tg.send("Спочатку задай ключ: <code>/pushover USER_KEY</code>")
                return
            ok = await po.send("KNTQ monitor", f"Тестовий пуш від {name} — усе працює ✅ "
                                                f"(пріоритет {po.priority})")
            await self.tg.send("✅ Пуш відправлено" if ok else "❌ Pushover відхилив запит, глянь логи")
            return
        deleted = await self._delete(msg)
        ok, info = await po.validate(value)
        if not ok:
            await self.tg.send(f"❌ Ключ не прийнято: {info}")
            return
        po.set_user(uid, value)
        await po.send("KNTQ monitor", "Pushover підключено ✅",
                      priority=min(po.priority, 1), only=uid)
        await self.tg.send(
            f"✅ {name}: Pushover підключено, ключ …{value[-4:]}, пристрої: {info}\n"
            + ("Повідомлення з ключем видалив." if deleted
               else "⚠️ Не зміг видалити повідомлення з ключем — видали його сам, група публічна."))

    async def _cmd_allow(self, action: str, value: str | None, msg: dict) -> None:
        """Owner grants or revokes command access. Reply to a message, or pass an id."""
        target = (msg.get("reply_to_message") or {}).get("from") or {}
        tid = value or (str(target["id"]) if target.get("id") else None)
        if not tid:
            await self.tg.send("Відповідай командою на повідомлення друга, "
                               "або <code>/allow ID</code> (ID дає <code>/myid</code>).")
            return
        ids = set(self.store.get("access").get("ids") or [])
        label = target.get("first_name") or tid
        if action == "allow":
            ids.add(tid)
            text = f"✅ {label} (<code>{tid}</code>) тепер може керувати ботом."
        else:
            ids.discard(tid)
            text = f"🚫 {label} (<code>{tid}</code>) більше не керує ботом."
        self.store.update("access", ids=sorted(ids))
        await self.tg.send(text)

    async def _cmd_only(self, value: str | None) -> None:
        """Alert on the KNTQ spread (/spread is the same command)."""
        current = self.store.get("only").get("pct")
        if value is None:
            state = f"<b>{current:.2f}%</b>" if current else "вимкнено"
            await self.tg.send(
                f"Стеження за спредом: {state}\n"
                f"Увімкнути: <code>/only 2</code> · вимкнути: <code>/only off</code>"
            )
            return
        if value.lower() == "off":
            self.store.update("only", pct=None, fired=False)
            await self.tg.send("🔕 Стеження за спредом вимкнено.")
            return
        pct = float(value)
        if not 0.1 <= pct <= 50:
            await self.tg.send("Поріг має бути між 0.1% і 50%.")
            return
        self.store.update("only", pct=pct, fired=False, pushed_ts=None)
        await self.tg.send(
            f"👁 Стежу за спредом вище <b>{pct:.2f}%</b>.\n"
            f"Коли перевалить — пуш у Pushover і повідомлення з цифрами тут.\n"
            f"Pushover після одного пуша мовчить годину; повідомлення тут ідуть далі, "
            f"кожен +1% спреду."
        )

    async def _cmd_status(self) -> None:
        snap = self.snapshot() or {}
        if "spread" not in snap:
            await self.tg.send("Ще рахую перше котирування.")
            return
        only = self.store.get("only").get("pct")
        age = time.time() - snap["spread_ts"]
        await self.tg.send(
            f"<b>KNTQ спред · ${snap['size']:,.0f}</b>: <b>{snap['spread']:+.2f}%</b> "
            f"({age:.0f}с тому)\n"
            f"Купівля HL: {snap['buy_px']:.5f} · книга ${snap.get('depth', 0):,.0f}\n"
            f"Продаж Elysium: {snap['sell_used'] / snap['qty']:.5f} → {snap['sell_used']:,.2f} USDC\n"
            f"Алерт: {f'від {only:.2f}%' if only else 'вимкнено'}"
            + ("\n\n" + "\n".join(self.hype.lines()) if self.hype is not None else "")
        )

