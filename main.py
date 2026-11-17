"""KNTQ Elysium spread monitor.

The cycle, as traded:

    1. buy KNTQ on HyperCore spot (KNTQ/USDC, @334)
    2. bridge it to Elysium 1:1, canonical, seconds, gas only
    3. sell it on Elysium for USDC (KyberSwap, which routes through HYPE when that pays)
    4. take the USDC off Elysium (fomo) and go round again

The monitor only prices steps 1 and 3: the HyperCore book against a KyberSwap
quote, every few seconds, for the size of the pinned message. Exit routes off
Elysium are no longer scanned.

Alerts: /only (KNTQ) and /hype (HYPE) above a threshold, a group message on
every step up and a Pushover push at most once an hour.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

import httpx

import config
from commands import CommandServer, Runtime
from config import (
    HYPE_SPOT_URL,
    Config,
    HYPERCORE_HYPE_USDC,
    PUSH_QUIET_S,
    HYPERCORE_KNTQ_USDC,
    KNTQ_ELYSIUM,
    USDC_ELYSIUM,
    alert_buttons,
)
from hyperliquid import HyperCore
from markets import Markets
from notify import Pushover, Telegram
from state import Store
from hype import HYPE_EVERY, HypeScan
from pin import SpreadPin
from whales import WhaleWatch

VERSION = "1.7.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")
# httpx logs every request URL at INFO, and Telegram URLs carry the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

_stop = asyncio.Event()
_snapshot: dict = {}

# /only re-fires once the spread climbs this much above where it last fired.
ESCALATION_PCT = 1.0
_push: Pushover | None = None


async def push(title: str, message: str, priority: int | None = None,
               url: str | None = None, url_title: str | None = None) -> None:
    if _push is not None and _push.ready:
        if not await _push.send(title, message, priority, url, url_title):
            log.warning("Pushover send failed")


def snapshot() -> dict:
    return _snapshot


def _request_stop(*_args) -> None:
    log.info("Signal received; finishing tick and flushing state")
    _stop.set()


def _pct(v: float) -> str:
    return f"{v:+.2f}%"


async def check_only(cfg: Config, store: Store, tg: Telegram, spread: float,
                     fill, sell_used: float, size: float) -> None:
    """/only: spread watch. Fires on crossing the threshold, and again each
    time the spread climbs ESCALATION_PCT above where it last fired, so a
    move from 2% to 5% doesn't pass in silence. Needs no exit at all."""
    only = store.get("only")
    only_pct = only.get("pct")
    if not only_pct:
        return
    fired_at = only.get("fired_at")
    due = spread >= only_pct and (
        not only.get("fired") or
        (fired_at is not None and spread >= fired_at + ESCALATION_PCT)
    )
    if due:
        store.update("only", fired=True, fired_at=round(spread, 3))
        loud = time.time() - (only.get("pushed_ts") or 0) >= PUSH_QUIET_S
        if loud:        # one loud push an hour; the message below is sent every time
            store.update("only", pushed_ts=time.time())
            await push(f"KNTQ спред {_pct(spread)}",
                       f"Поріг {only_pct:.2f}%. ${size:,.0f} → "
                       f"{sell_used:,.2f} USDC на Elysium "
                       f"({size * spread / 100:+,.0f}$ валом).",
                       url=HYPE_SPOT_URL, url_title="Hype Spot")
        await tg.send(
            f"📈 <b>Спред {_pct(spread)}</b>  (поріг {only_pct:.2f}%)\n\n"
            f"Купівля {fill.avg_px:.5f} · книга ${fill.book_depth_usd:,.0f}\n"
            f"${size:,.0f} → {fill.qty:,.0f} KNTQ → "
            f"<b>{sell_used:,.2f} USDC</b> на Elysium\n"
            f"Валом {size * spread / 100:+,.0f}$ до вартості виходу."
            + ("" if loud else "\n🔇 без пуша: Pushover мовчить годину після останнього"),
            alert_buttons(None, sell_used),
        )
        log.info("ONLY alert sent at %+.2f%%", spread)
    elif only.get("fired") and spread < (only_pct - cfg.clear_margin_pct):
        store.update("only", fired=False, fired_at=None)
        log.info("ONLY re-armed at %+.2f%%", spread)


async def spread_loop(cfg: Config, rt, core: HyperCore, markets: Markets,
                      store: Store, tg: Telegram, pin=None, hype=None) -> None:
    """Fast loop: spread only, every few seconds.

    Two requests a round -- the HyperCore book and one Kyber quote. KyberSwap
    already routes through HYPE when that pays more, so the direct quote is
    the traded number.
    """
    fails = rounds = 0
    last_logged: float | None = None
    last_log_ts = 0.0
    while not _stop.is_set():
        interval = rt.spread_poll_seconds
        # One quote feeds /only and the pinned message: the lap size is
        # the pin's when the pin is on, so the pin costs no extra requests.
        size = pin.size if pin is not None and pin.on else cfg.notional_usd
        try:
            fill = await core.buy_fill(size, cfg.taker_fee_pct)
            q = None
            if fill is not None and fill.filled:
                q = await markets.quote_elysium(KNTQ_ELYSIUM, USDC_ELYSIUM,
                                                str(int(fill.qty * 1e18)))
            if q is None:
                fails += 1
            else:
                fails = 0
                sell_used = q[0] / 1e6
                spread = 100.0 * (sell_used / fill.cost_usd - 1.0)
                now = time.time()
                _snapshot.update({"spread": spread, "spread_ts": now,
                                  "buy_px": fill.avg_px, "depth": fill.book_depth_usd,
                                  "size": size, "sell_used": sell_used, "qty": fill.qty})
                if pin is not None:
                    pin.kick()
                store.update("spread", last_pct=round(spread, 3))
                await check_only(cfg, store, tg, spread, fill, sell_used, size)
                if (last_logged is None or abs(spread - last_logged) >= 0.1
                        or now - last_log_ts >= 60):
                    log.info("fast spread %+.2f%% | buy %.5f | sell %.2f USDC",
                             spread, fill.avg_px, sell_used)
                    last_logged, last_log_ts = spread, now
        except Exception as exc:  # noqa: BLE001 - a bad round must not kill the loop
            fails += 1
            log.warning("Fast spread round failed: %s", exc)
        # Back off when a source is refusing us, recover as soon as it answers.
        delay = interval if fails == 0 else min(60.0, interval * (2 ** min(fails, 4)))
        rounds += 1
        if hype is not None and fails == 0 and rounds % HYPE_EVERY == 0:
            # Halfway to the next KNTQ quote, so the two never hit Kyber back to back.
            if await _sleep(delay / 2):
                break
            delay /= 2
            try:        # its own guard: a HYPE miss must not slow the KNTQ loop down
                h = await hype.round(size)
                if h is not None:
                    if pin is not None:
                        pin.kick()
                    cur = round(h["pct"], 1)
                    if cur != _snapshot.get("hype_logged"):
                        _snapshot["hype_logged"] = cur
                        log.info("hype spread %+.2f%% | buy %.3f | sell %.2f USDC",
                                 h["pct"], h["buy_px"], h["out"])
            except Exception as exc:  # noqa: BLE001
                log.warning("HYPE round failed: %s", exc)
        if await _sleep(delay):
            break


async def _sleep(seconds: float) -> bool:
    """Wait, or return True at once when the service is stopping."""
    try:
        await asyncio.wait_for(_stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def housekeeping(cfg: Config, rt, core: HyperCore, markets: Markets, store: Store) -> None:
    """Redis flush (on change, every few minutes) and a heartbeat line."""
    last_flush = last_heartbeat = time.monotonic()
    while not _stop.is_set():
        now = time.monotonic()
        if now - last_flush >= cfg.flush_seconds:
            try:
                await store.flush()
                await store.reload_settings()
            except Exception as exc:  # noqa: BLE001
                log.error("Redis flush failed (%s); state kept in memory", exc)
            last_flush = now
        if now - last_heartbeat >= cfg.heartbeat_seconds:
            snap = _snapshot
            log.info("heartbeat: spread=%s hype=%s poll=%.0fs kyber=%d (429: %d) hl=%d redis=%d",
                     f"{snap['spread']:+.2f}%" if "spread" in snap else "-",
                     snap.get("hype_logged", "-"), rt.spread_poll_seconds,
                     markets.kyber_requests, markets.kyber_429, core.request_count, store.command_count)
            last_heartbeat = now
        if await _sleep(30):
            break


async def run() -> int:
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    log.info("kntq-elysium-monitor v%s starting", VERSION)
    log.info("Config: %s", config.summary(cfg))

    limits = httpx.Limits(max_connections=6, max_keepalive_connections=3)
    async with httpx.AsyncClient(timeout=cfg.http_timeout, limits=limits) as http:
        store = Store(cfg.redis_url)
        try:
            await store.load()
        except Exception as exc:  # noqa: BLE001
            log.error("Redis load failed (%s); starting cold", exc)

        core = HyperCore(HYPERCORE_KNTQ_USDC, http)
        markets = Markets(cfg, http)
        tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id, http, cfg.telegram_thread_id)
        rt = Runtime(cfg, store)
        # 2026-11-15, once: the owner asked for a Pushover alert above 1% on both KNTQ and HYPE.
        if not store.get("runtime").get("push_1pct"):
            store.update("only", pct=1.0, fired=False, fired_at=None)
            store.update("hype", pct=1.0, off=False, at=None)
            store.update("runtime", push_1pct=True)
            try:
                await store.save_settings()
            except Exception as exc:  # noqa: BLE001
                log.error("Could not save the 1%% alert thresholds: %s", exc)
        global _push
        _push = Pushover(cfg.pushover_app_token, http, store, cfg.pushover_priority)
        whales = WhaleWatch(store, tg, _push, http)
        hype = HypeScan(cfg, HyperCore(HYPERCORE_HYPE_USDC, http), markets, store, tg, push)
        pin = SpreadPin(cfg, store, tg, rt, snapshot, hype)
        commands = CommandServer(cfg, http, markets, store, rt, tg, snapshot, _push, whales, pin, hype)

        await tg.send(
            f"▶️ <b>Монітор KNTQ Elysium</b>  v{VERSION}\n"
            f"Спред KNTQ і HYPE з HL в Elysium, пули виходу більше не скануються\n\n"
            f"<code>/only 1</code> — алерт по спреду KNTQ (пуш раз на годину)\n"
            f"<code>/pushover USER_KEY</code> — підключити свій Pushover (кожен свій)\n"
            f"<code>/allow</code> у відповідь на повідомлення — дати доступ другу\n"
            f"<code>/myid</code> — показати свій ID\n"
            f"<code>/poll 5</code> — як часто перевіряти спред, секунд\n"
            f"<code>/status</code> — поточний стан\n"
            f"<code>/pin 5000</code> — закріплений спред, оновлюється сам "
            f"(<code>/pin off</code>)\n"
            f"<code>/hype</code> — спред HYPE з HL в Elysium "
            f"(<code>/hype 1</code> — поріг пуша)\n"
            f"<code>/whales</code> — кити KNTQ: пуш, коли продають маркетом від "
            f"${whales.min_usd:,.0f}",
            alert_buttons(None, cfg.notional_usd),
        )

        tasks = [
            asyncio.create_task(housekeeping(cfg, rt, core, markets, store)),
            asyncio.create_task(spread_loop(cfg, rt, core, markets, store, tg, pin, hype)),
            asyncio.create_task(commands.run(_stop)),
            asyncio.create_task(whales.run(_stop)),
            asyncio.create_task(pin.run(_stop)),
        ]
        await _stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        log.info("Flushing state before exit")
        try:
            await store.flush(force=True)
        except Exception as exc:  # noqa: BLE001
            log.error("Final flush failed: %s", exc)
        finally:
            await store.close()
    return 0


def main() -> int:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, _request_stop)
    try:
        return loop.run_until_complete(run())
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
