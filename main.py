"""KNTQ Elysium round-trip monitor.

The cycle being watched:

    1. buy KNTQ on HyperCore spot (KNTQ/USDC, @334)
    2. move it to Elysium 1:1 through the canonical bridge  (seconds, gas only)
    3. sell it on Elysium for USDC at a premium
    4. bring that USDC back to some EVM chain so the capital can go round again

Step 4 is the binding constraint. Moving value off Elysium is not a swap against
a pool -- an intent solver fronts you its own funds on the destination and then
spends ~6 days recovering them through the canonical bridge. So the cost is set
by whether that solver still holds inventory on a given chain, which makes it a
cliff rather than a depth curve: around 0.3% while inventory lasts, then ~3% at
every size once it is gone. Splitting the order does not help.

Two alerts:
  GO      spread >= GO_SPREAD_PCT and some chain can take the money back out
          at <= MAX_EXIT_LOSS_PCT. Reports the net, not the gross.
  URGENT  spread >= URGENT_SPREAD_PCT, sent even when every exit is drained,
          because at that spread the ~6-day canonical withdrawal may be worth
          eating. The USDC sitting in the queue carries no price risk.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

import httpx

import config
from config import CHAIN_NAMES, Config, HYPERCORE_KNTQ_USDC
from hyperliquid import HyperCore
from markets import Exit, Markets, RateLimited
from notify import Telegram
from state import Store

VERSION = "1.0.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")

_stop = asyncio.Event()
BACKGROUND_EXIT_EVERY = 10  # ticks between exit probes while the spread is quiet


def _request_stop(*_args) -> None:
    log.info("Signal received; finishing tick and flushing state")
    _stop.set()


def _pct(v: float) -> str:
    return f"{v:+.2f}%"


async def probe_exits(
    cfg: Config, markets: Markets, amount_usd: float, chains: list[int]
) -> list[Exit]:
    found: list[Exit] = []
    for chain_id in chains:
        if _stop.is_set():
            break
        await asyncio.sleep(cfg.spacing_seconds)
        try:
            quote = await markets.exit_quote(chain_id, amount_usd)
        except RateLimited as exc:
            log.warning("LI.FI rate limited (%s); stopping exit probes this tick", str(exc)[:100])
            break
        except Exception as exc:  # noqa: BLE001 - one bad chain must not kill the tick
            log.warning("Exit probe for %s failed: %s", CHAIN_NAMES.get(chain_id, chain_id), exc)
            continue
        if quote:
            found.append(quote)
    return found


def go_message(cfg: Config, buy_px: float, qty: float, proceeds: float,
               spread: float, best: Exit, net_pct: float, depth: float) -> str:
    net_usd = cfg.notional_usd * net_pct / 100.0
    return (
        f"🟢 <b>КОЛО ВІДКРИТЕ</b>  нетто {_pct(net_pct)}  (~${net_usd:,.0f})\n\n"
        f"<b>1. Купівля</b> HyperCore KNTQ/USDC\n"
        f"    ${cfg.notional_usd:,.0f} → {qty:,.0f} KNTQ @ {buy_px:.5f}\n"
        f"    глибина асків ${depth:,.0f}\n"
        f"<b>2. Міст</b> на Elysium 1:1, канонічний, секунди\n"
        f"<b>3. Продаж</b> на Elysium → {proceeds:,.2f} USDC\n"
        f"    спред {_pct(spread)}\n"
        f"<b>4. Вихід</b> → {best.chain}\n"
        f"    {best.usdc_out:,.2f} USDC, мінус {best.loss_pct:.2f}%\n"
        f"    <code>{best.provider}</code> · ~{best.seconds}с\n"
        f"    гірший випадок {best.worst_loss_pct:.2f}%"
    )


def urgent_message(cfg: Config, spread: float, proceeds: float,
                   best: Exit | None) -> str:
    if best is None:
        exit_line = "Жодного виходу не проквотовано цього разу."
    else:
        exit_line = (
            f"Найдешевший зараз: {best.chain} −{best.loss_pct:.2f}% "
            f"(<code>{best.provider}</code>), це гірше за поріг "
            f"{cfg.max_exit_loss_pct:.2f}%."
        )
    return (
        f"🟠 <b>ШИРОКИЙ СПРЕД</b> {_pct(spread)} — виходу немає\n\n"
        f"${cfg.notional_usd:,.0f} → {proceeds:,.2f} USDC на Elysium.\n"
        f"{exit_line}\n\n"
        f"Швидко забрати нічим. Канонічний вивід 0% але ~6 днів — "
        f"у черзі лежить USDC, цінового ризику немає, тільки заморожений капітал."
    )


async def tick(cfg: Config, core: HyperCore, markets: Markets,
               store: Store, tg: Telegram, tick_no: int) -> None:
    fill = await core.buy_fill(cfg.notional_usd, cfg.taker_fee_pct)
    if fill is None:
        log.warning("No buy fill available this tick")
        return
    if not fill.filled:
        log.warning(
            "HyperCore book too thin for $%.0f (depth $%.0f) — skipping",
            cfg.notional_usd, fill.book_depth_usd,
        )
        return

    sale = await markets.sell_kntq(fill.qty)
    if sale is None:
        log.warning("No Elysium sell route this tick")
        return

    spread = 100.0 * (sale.usdc_out / fill.cost_usd - 1.0)
    store.update("spread", last_pct=round(spread, 3), buy_px=round(fill.avg_px, 6))

    interesting = spread >= (cfg.go_spread_pct - cfg.clear_margin_pct)
    background = tick_no % BACKGROUND_EXIT_EVERY == 0
    exits: list[Exit] = []

    if interesting or background:
        chains = list(cfg.core_chains)
        if cfg.rotating_chains and cfg.rotate_per_tick:
            start = (tick_no * cfg.rotate_per_tick) % len(cfg.rotating_chains)
            for i in range(min(cfg.rotate_per_tick, len(cfg.rotating_chains))):
                chains.append(cfg.rotating_chains[(start + i) % len(cfg.rotating_chains)])
        exits = await probe_exits(cfg, markets, sale.usdc_out, chains)

    best = min(exits, key=lambda e: e.loss_pct) if exits else None
    exit_ok = best is not None and best.loss_pct <= cfg.max_exit_loss_pct
    net_pct = (
        100.0 * ((sale.usdc_out * (1 - best.loss_pct / 100.0)) / cfg.notional_usd - 1.0)
        if best else float("nan")
    )

    log.info(
        "spread %+.2f%% | buy %.5f | sell %.2f USDC | best exit %s | net %s",
        spread, fill.avg_px, sale.usdc_out,
        f"{best.chain} -{best.loss_pct:.2f}% ({best.provider})" if best else "not probed",
        f"{net_pct:+.2f}%" if best else "n/a",
    )

    # --- GO: spread wide enough and the money can get back out ---
    armed_go = bool(store.get("go").get("armed"))
    if spread >= cfg.go_spread_pct and exit_ok:
        if not armed_go and not store.in_cooldown("go", cfg.alert_cooldown_seconds):
            store.update("go", armed=True)
            store.mark_alerted("go")
            await tg.send(go_message(
                cfg, fill.avg_px, fill.qty, sale.usdc_out, spread,
                best, net_pct, fill.book_depth_usd,
            ))
            log.info("GO alert sent: net %+.2f%% via %s", net_pct, best.chain)
        else:
            store.update("go", armed=True)
    elif armed_go and spread < (cfg.go_spread_pct - cfg.clear_margin_pct):
        store.update("go", armed=False)
        log.info("GO condition cleared at %+.2f%%", spread)

    # --- URGENT: spread very wide, sent whether or not an exit exists ---
    armed_urgent = bool(store.get("urgent").get("armed"))
    if spread >= cfg.urgent_spread_pct and not exit_ok:
        if not armed_urgent and not store.in_cooldown("urgent", cfg.alert_cooldown_seconds):
            store.update("urgent", armed=True)
            store.mark_alerted("urgent")
            await tg.send(urgent_message(cfg, spread, sale.usdc_out, best))
            log.info("URGENT alert sent at %+.2f%%", spread)
        else:
            store.update("urgent", armed=True)
    elif armed_urgent and spread < (cfg.urgent_spread_pct - cfg.clear_margin_pct):
        store.update("urgent", armed=False)


async def run() -> int:
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    log.info("kntq-elysium-monitor v%s starting", VERSION)
    log.info("Config: %s", config.summary(cfg))

    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    async with httpx.AsyncClient(timeout=cfg.http_timeout, limits=limits) as http:
        store = Store(cfg.redis_url)
        try:
            await store.load()
        except Exception as exc:  # noqa: BLE001 - a cold cache is survivable
            log.error("Redis load failed (%s); starting cold", exc)

        core = HyperCore(HYPERCORE_KNTQ_USDC, http)
        markets = Markets(cfg, http)
        tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id, http)

        await tg.send(
            f"▶️ <b>Монітор KNTQ Elysium запущено</b>\n"
            f"Коло: ${cfg.notional_usd:,.0f}\n"
            f"Алерт від {cfg.go_spread_pct:.1f}% зі здатним виходом (≤{cfg.max_exit_loss_pct:.1f}%)\n"
            f"Алерт від {cfg.urgent_spread_pct:.1f}% у будь-якому разі\n"
            f"Мережі: {', '.join(CHAIN_NAMES.get(c, str(c)) for c in cfg.core_chains)}"
        )

        tick_no = 0
        last_flush = last_heartbeat = time.monotonic()
        while not _stop.is_set():
            started = time.monotonic()
            try:
                await tick(cfg, core, markets, store, tg, tick_no)
            except Exception as exc:  # noqa: BLE001 - never let one tick kill the loop
                log.exception("Tick failed, continuing: %s", exc)
            tick_no += 1

            now = time.monotonic()
            if now - last_flush >= cfg.flush_seconds:
                try:
                    await store.flush()
                except Exception as exc:  # noqa: BLE001
                    log.error("Redis flush failed (%s); state kept in memory", exc)
                last_flush = now

            if now - last_heartbeat >= cfg.heartbeat_seconds:
                log.info(
                    "heartbeat: ticks=%d spread=%s lifi=%d other=%d hl=%d redis=%d",
                    tick_no, store.get("spread").get("last_pct"),
                    markets.lifi_requests, markets.other_requests,
                    core.request_count, store.command_count,
                )
                last_heartbeat = now

            elapsed = time.monotonic() - started
            try:
                await asyncio.wait_for(_stop.wait(), timeout=max(1.0, cfg.poll_seconds - elapsed))
            except asyncio.TimeoutError:
                pass

        log.info("Flushing state before exit")
        try:
            await store.flush(force=True)
        finally:
            await store.close()
        log.info("Stopped after %d ticks", tick_no)
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
