"""Telegram alerts. Outbound only -- the service exposes no command surface."""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

log = logging.getLogger("notify")


class Telegram:
    def __init__(self, token: str, chat_id: str, http: httpx.AsyncClient):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self.http = http

    async def send(self, text: str, buttons: list[list[dict]] | None = None) -> bool:
        """Send a message. `buttons` is an inline keyboard of {text, url} rows."""
        payload: dict = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        for attempt in range(3):
            try:
                r = await self.http.post(self._url, json=payload)
                if r.is_success:
                    return True
                log.warning("Telegram %s: %s", r.status_code, r.text[:200])
                if r.status_code in (400, 403):
                    return False  # bad chat id or bot removed; retrying won't help
            except httpx.HTTPError as exc:
                log.warning("Telegram transport error (%s), attempt %d", exc, attempt + 1)
            delay = min(2.0 ** attempt, 8.0)
            await asyncio.sleep(delay + random.uniform(0, 0.4 * delay))
        log.error("Giving up on Telegram message after retries")
        return False


class Pushover:
    """Push notifications through the Pushover API.

    The application token comes from the environment (PUSHOVER_APP_TOKEN).
    The user key is set at runtime with /pushover and kept in the store, so
    it never has to live in Railway variables or in the repo.
    """

    URL = "https://api.pushover.net/1/messages.json"
    VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"

    def __init__(self, app_token: str, http: httpx.AsyncClient, store,
                 default_priority: int = 1):
        self.app_token = app_token
        self.http = http
        self.store = store
        self.default_priority = default_priority

    def users(self) -> dict[str, str]:
        """Telegram user id -> Pushover user key, one per person in the group."""
        rec = self.store.get("pushover")
        users = dict(rec.get("users") or {})
        if rec.get("user"):            # single key saved by the previous build
            users.setdefault("legacy", rec["user"])
        return users

    @property
    def priority(self) -> int:
        """Configured Pushover priority: 1 high (default), 2 emergency, 0 normal."""
        saved = self.store.get("pushover").get("priority")
        return int(saved) if saved is not None else self.default_priority

    def set_priority(self, value: int) -> None:
        self.store.update("pushover", priority=int(value))

    def set_user(self, tg_id: str, key: str | None) -> None:
        users = {k: v for k, v in self.users().items() if k != "legacy"}
        if key:
            users[tg_id] = key
        else:
            users.pop(tg_id, None)
        self.store.update("pushover", users=users, user=None)

    @property
    def ready(self) -> bool:
        return bool(self.app_token and self.users())

    async def validate(self, user_key: str) -> tuple[bool, str]:
        if not self.app_token:
            return False, "PUSHOVER_APP_TOKEN не задано в змінних Railway"
        try:
            r = await self.http.post(self.VALIDATE_URL,
                                     data={"token": self.app_token, "user": user_key})
            j = r.json()
        except Exception as exc:  # noqa: BLE001
            return False, f"Pushover недоступний: {exc}"
        if j.get("status") == 1:
            return True, ", ".join(j.get("devices") or []) or "усі пристрої"
        return False, "; ".join(j.get("errors") or ["невідома помилка"])

    async def send(self, title: str, message: str, priority: int | None = None,
                   url: str | None = None, url_title: str | None = None,
                   only: str | None = None) -> bool:
        """Push to every connected person, or just `only` (a Telegram id).

        `priority` None means the configured one, so tests and alerts look
        exactly alike on the phone.
        """
        if not self.app_token:
            return False
        if priority is None:
            priority = self.priority
        users = self.users()
        if only is not None:
            users = {k: v for k, v in users.items() if k == only}
        results = [await self._send_one(key, title, message, priority, url, url_title)
                   for key in users.values()]
        return bool(results) and all(results)

    async def _send_one(self, user_key: str, title: str, message: str, priority: int,
                        url: str | None, url_title: str | None) -> bool:
        data = {"token": self.app_token, "user": user_key, "title": title,
                "message": message, "priority": priority, "html": 1}
        if priority >= 2:
            # Emergency: Pushover repeats it every `retry` seconds until it is
            # acknowledged on the phone or `expire` runs out.
            data.update(retry=30, expire=600)
        if url:
            data["url"] = url
            data["url_title"] = url_title or url
        for attempt in range(3):
            try:
                r = await self.http.post(self.URL, data=data)
                if r.is_success:
                    return True
                log.warning("Pushover %s: %s", r.status_code, r.text[:200])
                if 400 <= r.status_code < 500:
                    return False  # bad token or key; retrying won't help
            except httpx.HTTPError as exc:
                log.warning("Pushover transport error (%s), attempt %d", exc, attempt + 1)
            await asyncio.sleep(min(2.0 ** attempt, 8.0) + random.uniform(0, 0.5))
        return False
