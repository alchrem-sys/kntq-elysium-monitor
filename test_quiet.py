"""KNTQ spread alert: one an hour."""
import asyncio
import os

os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="@t", REDIS_URL="rediss://unused")

import main as M  # noqa: E402
from hyperliquid import Fill  # noqa: E402


class Store:
    def __init__(self):
        self.data = {"only": {"pct": 1.0}}

    def get(self, f):
        return self.data.setdefault(f, {})

    def update(self, f, **kw):
        self.get(f).update(kw)


class Tg:
    sent = 0

    async def send(self, text, buttons=None):
        Tg.sent += 1


class Cfg:
    clear_margin_pct = 0.4


async def go():
    pushes = []

    async def push(*a, **k):
        pushes.append(a[0])
    M.push = push
    st, tg, fill = Store(), Tg(), Fill(20000.0, 0.25, 5000.0, 0.25, 90000.0, True)
    fire = lambda spread: M.check_only(Cfg(), st, tg, spread, fill, 5000 * (1 + spread / 100), 5000.0)  # noqa: E731
    await fire(1.2)
    assert len(pushes) == 1 and Tg.sent == 1
    await fire(2.5)                      # +1% escalation, but inside the hour
    await fire(0.3)                      # re-arms
    await fire(1.4)                      # crosses again, still inside the hour
    assert len(pushes) == 1 and Tg.sent == 1
    st.get("only")["pushed_ts"] -= 3601
    await fire(1.4)                      # hour over and still above: now
    assert len(pushes) == 2 and Tg.sent == 2
    await fire(3.0)
    assert len(pushes) == 2
    print("quiet ok")

asyncio.run(go())
