"""Offline harness driving the real spread loop and commands with recorded live data.

The build sandbox has no egress to api.hyperliquid.xyz or kyberswap, so the
HTTP boundary is stubbed with numbers those services returned on 2026-11-10:
the real @334 ask book and real KNTQ sell quotes. Everything below the HTTP
layer is production code.
"""
from __future__ import annotations

import asyncio
import os

os.environ.update(
    TELEGRAM_BOT_TOKEN="test-token",
    TELEGRAM_CHAT_ID="@test",
    TELEGRAM_ADMIN_ID="868931721",
    REDIS_URL="rediss://unused",
    PUSHOVER_APP_TOKEN="app-test-token",
    NOTIONAL_USD="3000",
)

import config  # noqa: E402
from commands import CommandServer, Runtime  # noqa: E402
from hyperliquid import HyperCore  # noqa: E402
import main as M  # noqa: E402
from main import snapshot  # noqa: E402
from notify import Pushover  # noqa: E402
from markets import Markets  # noqa: E402

ASKS = [
    {"px": "0.33833", "sz": "883.77"},
    {"px": "0.3386", "sz": "36.56"},
    {"px": "0.33893", "sz": "17456.49"},
]
MID = 71.7145
PX_USDC = 3105.506166 / 9000       # KNTQ -> USDC, measured
PX_HYPE = 43.39677515 / 9000       # KNTQ -> HYPE, measured

W = {"mult": 1.0, "exit": {("USDC", 10): 0.27, ("HYPE", 10): 0.76}, "default": 3.02}


class R:
    def __init__(self, p, code=200):
        self._p, self.status_code = p, code
        self.is_success = 200 <= code < 300
        self.text = ""

    def json(self):
        return self._p


class FakeHttp:
    def __init__(self):
        self.sent = []

    async def post(self, url, json=None, **kw):
        if "hyperliquid" in url:
            if json.get("type") == "allMids":
                return R({"HYPE": str(MID)})
            return R({"coin": "@334", "levels": [[], ASKS]})
        if "relay.link" in url:
            return R({"details": {"currencyOut": {"amountFormatted": "1"}, "timeEstimate": 2}})
        if "telegram" in url:
            if url.endswith("/deleteMessage"):
                print(f"    [deleteMessage chat={json['chat_id']} msg={json['message_id']}]")
            self.sent.append(json)
            return R({"ok": True})
        if "pushover.net" in url:
            d = kw.get("data") or {}
            if "validate" in url:
                return R({"status": 1, "devices": ["iphone"]})
            print(f"    >>> PUSHOVER to …{d.get('user','')[-4:]} [{d.get('priority')}] {d.get('title')} | {d.get('message')}"
                  f" | url={d.get('url_title')}"
                  + (f" | retry={d['retry']}s expire={d['expire']}s" if 'retry' in d else ""))
            return R({"status": 1})
        return R({}, 404)

    async def get(self, url, params=None, headers=None, **kw):
        if "kyberswap" in url:
            tin, tout = params["tokenIn"].lower(), params["tokenOut"].lower()
            amt = int(params["amountIn"])
            if tin.startswith("0xeeee"):                 # HYPE -> USDC
                usd = amt / 1e18 * MID * 0.99962         # same as the direct quote
                return R({"data": {"routeSummary": {
                    "amountOut": str(int(usd * 1e6)), "route": [1, 2]}}})
            if tout.startswith("0xeeee"):                # KNTQ -> HYPE
                return R({"data": {"routeSummary": {
                    "amountOut": str(int(amt / 1e18 * PX_HYPE * W["mult"] * 1e18)),
                    "route": [1, 2, 3, 4]}}})
            return R({"data": {"routeSummary": {         # KNTQ -> USDC
                "amountOut": str(int(amt / 1e18 * PX_USDC * W["mult"] * 1e6)),
                "route": [1] * 7}}})
        if "li.quest" in url or "relay.link" in url:
            raise AssertionError("exit routes must not be queried any more")
        if "getUpdates" in url:
            return R({"result": []})
        return R({}, 404)


class FakeStore:
    def __init__(self):
        self.data = {}

    def get(self, f):
        return self.data.setdefault(f, {})

    def update(self, f, **v):
        self.get(f).update(v)

    def touch(self):
        pass

    async def save_settings(self):
        print("    [settings saved to Redis]")

    async def reload_settings(self):
        pass

    async def flush(self, force=False):
        pass

    def in_cooldown(self, f, s):
        return False

    def mark_alerted(self, f):
        pass


class TG:
    def __init__(self, h):
        self.http = h

    async def send(self, text, buttons=None):
        print("\n" + "=" * 64)
        for t in ("<b>", "</b>", "<code>", "</code>"):
            text = text.replace(t, "")
        print(text)
        if buttons:
            print("-" * 64)
            for row in buttons:
                print("  [" + "]  [".join(f"{b['text']}" for b in row) + "]")
            for row in buttons:
                for b in row:
                    print(f"    {b['text']}: {b['url'][:110]}")
        print("=" * 64)
        return True


async def main() -> None:
    cfg = config.load()
    http = FakeHttp()
    core, markets, store = HyperCore("@334", http), Markets(cfg, http), FakeStore()
    tg = TG(http)
    rt = Runtime(cfg, store)
    M._push = Pushover(cfg.pushover_app_token, http, store)
    cmds = CommandServer(cfg, http, markets, store, rt, tg, snapshot, M._push)

    me = {'from': {'id': 868931721, 'first_name': 'Lyosha'}, 'chat': {'id': -100}, 'message_id': 41}
    friend = {'from': {'id': 555000111, 'first_name': 'Friend'}, 'chat': {'id': -100}, 'message_id': 42}
    print('### 0a. friend tries a command before access -> told why, with his ID')
    await cmds._dispatch('/only 2', friend)
    print('    only armed?', store.get('only').get('pct'))
    print('### 0a2. same person again within 10 min -> no repeated nagging')
    await cmds._dispatch('/only 2', friend)
    print('### 0a3. anonymous admin -> told to switch anonymity off')
    await cmds._dispatch('/only 2', {'from': {'id': 1087968824, 'first_name': 'Group'},
                                     'sender_chat': {'id': -100}, 'chat': {'id': -100}, 'message_id': 9})
    print('### 0b. owner replies /allow to friend')
    await cmds._dispatch('/allow', {**me, 'reply_to_message': friend})
    print('### 0c. both connect their own Pushover keys')
    await cmds._dispatch('/pushover uLyoshaKey000000000000000000AAAA', me)
    await cmds._dispatch('/pushover uFriendKey000000000000000000BBBB', {**friend, 'message_id': 43})
    print('### 0d. friend can now change things; /pushover test reaches both')
    await cmds._dispatch('/pushover test', friend)
    await cmds._dispatch('/myid', friend)
    print('### 0e. /pushover priority 2 -> emergency, test push carries retry/expire')
    await cmds._dispatch('/pushover priority 2', friend)
    await cmds._dispatch('/pushover test', me)
    await cmds._dispatch('/pushover priority 1', me)
    print('### 0f. /poll 5')
    await cmds._dispatch('/poll 5', friend)
    print('    poll now', rt.spread_poll_seconds)

    async def loop(seconds=0.3):
        store.get('runtime')['spread_poll_seconds'] = 0.05
        task = asyncio.create_task(M.spread_loop(cfg, rt, core, markets, store, tg))
        await asyncio.sleep(seconds)
        M._stop.set(); await task; M._stop.clear()

    print("### 1. Quiet spread -> silence, /status shows it")
    W["mult"] = 1.0
    sent = len(http.sent)
    await loop()
    assert "spread" in snapshot() and abs(snapshot()["spread"] - 1.79) < 0.05, snapshot()
    await cmds._dispatch('/status', me)

    print("\n### 2. /spread 2 is /only 2")
    await cmds._dispatch('/spread 2', me)
    assert store.get('only').get('pct') == 2.0

    print("\n### 3. spread above 2% -> push + message, then quiet while it stays")
    W["mult"] = 1.03
    await loop()
    fired = store.get('only').get('fired_at')
    assert fired and fired > 2, store.get('only')

    print("\n### 4. falls below the band, then back up -> message again, no push inside the hour")
    W["mult"] = 0.99
    await loop()
    assert not store.get('only').get('fired')
    W["mult"] = 1.03
    await loop()
    assert store.get('only').get('fired')

    print("\n### 5. /only off")
    await cmds._cmd_only("off")
    assert store.get('only').get('pct') is None

    for cmd in ('/pulls', '/tell', '/test'):
        await cmds._dispatch(cmd, me)           # gone: silently ignored
    print(f"\nTelegram payloads: {len(http.sent)}  Kyber requests: {markets.kyber_requests}")


if __name__ == "__main__":
    asyncio.run(main())
