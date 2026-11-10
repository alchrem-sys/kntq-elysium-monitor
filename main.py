"""KNTQ Elysium round-trip monitor.

The cycle being watched:

    1. buy KNTQ on HyperCore spot (KNTQ/USDC, @334)
    2. move it to Elysium 1:1 through the canonical bridge  (seconds, gas only)
    3. sell it on Elysium, into USDC or into HYPE
    4. bring the proceeds back to some EVM chain as USDC, so the capital can
       go round again

Step 4 is the binding constraint. Moving value off Elysium is not a swap
against a pool -- an intent solver fronts you its own funds on the destination
and then spends ~6 days recovering them through the canonical bridge. The cost
is therefore set by whether that solver still holds inventory on a given chain,
which makes it a cliff rather than a depth curve: around 0.3% while inventory
lasts, then ~3% at every size once it is gone. Splitting the order does not
help; finding an unspent chain does.

Step 3 has two carriers and the better one moves. Selling into HYPE quotes
about 1% above selling into USDC, because Elysium USDC trades under a dollar.
Solvers know that too and price the HYPE bridge accordingly, handing some or
all of it back. So both carriers are measured end to end, from the same KNTQ
quantity to USDC actually delivered on the destination, and the winner is
whichever pays more. No HYPE price feed enters that decision.

Alerts:
  GO      spread >= the runtime threshold (/spread) and some chain can take
          the money out at <= MAX_EXIT_LOSS_PCT. Reports the net, not the gross.
  URGENT  spread >= URGENT_SPREAD_PCT, sent even with every exit drained,
          because at that spread the ~6-day canonical withdrawal may be worth
          eating. The USDC sitting in that queue carries no price risk.
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
from config import CHAIN_NAMES, Config, HYPERCORE_KNTQ_USDC
from hyperliquid import HyperCore
from markets import CARRIERS, Exit, Markets, RateLimited, Sale
from routing import exit_order, remember
from notify import Telegram
from state import Store

VERSION = "1.2.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")

_stop = asyncio.Event()
_snapshot: dict = {}


def snapshot() -> dict:
    return _snapshot


def _request_stop(*_args) -> None:
    log.info("Signal received; finishing tick and flushing state")
    _stop.set()


def _pct(v: float) -> str:
    return f"{v:+.2f}%"



async def sweep_exits(cfg: Config, markets: Markets, store: Store,
                      proceeds: dict[str, dict], budget: int) -> list[Exit]:
    """Probe until one usable exit is found, then price the other carrier there."""
    found: list[Exit] = []
    probed = 0
    hit: tuple[int, str] | None = None

    for chain_id, carrier in exit_order(cfg, store, time.time()):
        if _stop.is_set() or probed >= budget:
            break
        leg = proceeds.get(carrier)
        if not leg:
            continue
        await asyncio.sleep(cfg.spacing_seconds)
        probed += 1
        try:
            q = await markets.exit_quote(chain_id, carrier, leg["amount"], leg["usd"])
        except RateLimited as exc:
            log.warning("LI.FI rate limited (%s); ending sweep", str(exc)[:90])
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("Probe %s/%s failed: %s", CHAIN_NAMES.get(chain_id, chain_id), carrier, exc)
            continue
        if q is None:
            remember(store, chain_id, carrier, 99.0)
            continue
        remember(store, chain_id, carrier, q.loss_pct)
        found.append(q)
        if q.loss_pct <= cfg.max_exit_loss_pct:
            hit = (chain_id, carrier)
            break

    # Price the other carrier on the same chain, so the comparison is like for
    # like: same destination, same moment, only the carrier differs.
    if hit:
        chain_id, used = hit
        other = next(c for c in CARRIERS if c != used)
        leg = proceeds.get(other)
        if leg:
            await asyncio.sleep(cfg.spacing_seconds)
            try:
                q = await markets.exit_quote(chain_id, other, leg["amount"], leg["usd"])
                if q:
                    remember(store, chain_id, other, q.loss_pct)
                    found.append(q)
            except Exception:  # noqa: BLE001 - comparison is a bonus, not required
                pass

    log.info("sweep: %d probe(s), %d quote(s)", probed, len(found))
    return found


def go_message(cfg: Config, threshold: float, fill, sale: Sale, proceeds: dict,
               spread: float, best: Exit, net_pct: float, alt: Exit | None) -> str:
    net_usd = cfg.notional_usd * net_pct / 100.0
    carrier_line = ""
    if alt is not None:
        delta = 100.0 * (best.usdc_out / alt.usdc_out - 1.0)
        carrier_line = (
            f"\n    через {alt.carrier} було б {alt.usdc_out:,.2f} "
            f"({delta:+.2f}% на користь {best.carrier})"
        )
    leg = proceeds[best.carrier]
    unit = "USDC" if best.carrier == "USDC" else "HYPE"
    return (
        f"🟢 <b>КОЛО ВІДКРИТЕ</b>  нетто {_pct(net_pct)}  (~${net_usd:,.0f})\n\n"
        f"<b>1. Купівля</b> HyperCore KNTQ/USDC\n"
        f"    ${cfg.notional_usd:,.0f} → {fill.qty:,.0f} KNTQ @ {fill.avg_px:.5f}\n"
        f"    глибина асків ${fill.book_depth_usd:,.0f}\n"
        f"<b>2. Міст</b> на Elysium 1:1, канонічний, секунди\n"
        f"<b>3. Продаж</b> у <b>{best.carrier}</b> → {leg['amount']:,.4f} {unit}\n"
        f"    спред {_pct(spread)} (поріг {threshold:.2f}%)\n"
        f"<b>4. Вихід</b> → {best.chain}\n"
        f"    <b>{best.usdc_out:,.2f} USDC</b>, мінус {best.loss_pct:.2f}%\n"
        f"    <code>{best.provider}</code> · ~{best.seconds}с{carrier_line}"
    )


def urgent_message(cfg: Config, spread: float, best: Exit | None, probed: int) -> str:
    line = (
        f"Найдешевший із {probed} перевірених: {best.chain} / {best.carrier} "
        f"−{best.loss_pct:.2f}%, гірше за поріг {cfg.max_exit_loss_pct:.2f}%."
        if best else f"Жодного маршруту серед {probed} перевірених."
    )
    return (
        f"🟠 <b>ШИРОКИЙ СПРЕД</b> {_pct(spread)} — виходу немає\n\n{line}\n\n"
        f"Швидко забрати нічим. Канонічний вивід 0% але ~6 днів — "
        f"у черзі лежить USDC, цінового ризику немає, тільки заморожений капітал."
    )


async def tick(cfg: Config, rt: Runtime, core: HyperCore, markets: Markets,
               store: Store, tg: Telegram, tick_no: int) -> None:
    fill = await core.buy_fill(cfg.notional_usd, cfg.taker_fee_pct)
    if fill is None:
        log.warning("No buy fill this tick")
        return
    if not fill.filled:
        log.warning("Book too thin for $%.0f (depth $%.0f)", cfg.notional_usd, fill.book_depth_usd)
        return

    mid = await core.hype_mid()
    proceeds: dict[str, dict] = {}
    for carrier in CARRIERS:
        sale = await markets.sell_kntq(fill.qty, carrier)
        if sale is None:
            continue
        usd = sale.amount_out if carrier == "USDC" else (
            sale.amount_out * mid if mid else 0.0
        )
        if usd:
            proceeds[carrier] = {"amount": sale.amount_out, "usd": usd, "sale": sale}
    if not proceeds:
        log.warning("No Elysium sell route this tick")
        return

    top = max(proceeds.values(), key=lambda p: p["usd"])
    spread = 100.0 * (top["usd"] / fill.cost_usd - 1.0)
    store.update("spread", last_pct=round(spread, 3), buy_px=round(fill.avg_px, 6))

    threshold = rt.go_spread_pct
    if spread >= threshold:
        budget = cfg.probes_hot
    elif spread >= threshold - cfg.clear_margin_pct:
        budget = cfg.probes_warm
    elif tick_no % 10 == 0:
        budget = cfg.probes_cold
    else:
        budget = 0

    exits = await sweep_exits(cfg, markets, store, proceeds, budget) if budget else []
    opens = [e for e in exits if e.loss_pct <= cfg.max_exit_loss_pct]
    best = max(opens, key=lambda e: e.usdc_out) if opens else None
    cheapest = min(exits, key=lambda e: e.loss_pct) if exits else None
    alt = next((e for e in exits if best and e.chain_id == best.chain_id
                and e.carrier != best.carrier), None)
    net_pct = 100.0 * (best.usdc_out / cfg.notional_usd - 1.0) if best else float("nan")

    _snapshot.update({
        "spread": spread, "buy_px": fill.avg_px, "depth": fill.book_depth_usd,
        "carrier": best.carrier if best else max(proceeds, key=lambda c: proceeds[c]["usd"]),
        "final": best.usdc_out if best else 0.0,
        "exit": f"{best.chain} −{best.loss_pct:.2f}%" if best else "не знайдено",
        "proceeds": {c: {"amount": p["amount"], "usd": p["usd"]} for c, p in proceeds.items()},
    })

    log.info(
        "spread %+.2f%% | buy %.5f | %s | best %s | net %s",
        spread, fill.avg_px,
        " ".join(f"{c}:{p['usd']:,.0f}" for c, p in proceeds.items()),
        f"{best.chain}/{best.carrier} -{best.loss_pct:.2f}%" if best else "none",
        f"{net_pct:+.2f}%" if best else "n/a",
    )

    armed_go = bool(store.get("go").get("armed"))
    if spread >= threshold and best:
        if not armed_go and not store.in_cooldown("go", cfg.alert_cooldown_seconds):
            store.update("go", armed=True)
            store.mark_alerted("go")
            await tg.send(go_message(
                cfg, threshold, fill, proceeds[best.carrier]["sale"], proceeds,
                spread, best, net_pct, alt,
            ))
            log.info("GO sent: net %+.2f%% via %s/%s", net_pct, best.chain, best.carrier)
        else:
            store.update("go", armed=True)
    elif armed_go and spread < (threshold - cfg.clear_margin_pct):
        store.update("go", armed=False)
        log.info("GO cleared at %+.2f%%", spread)

    armed_urgent = bool(store.get("urgent").get("armed"))
    if spread >= cfg.urgent_spread_pct and not best:
        if not armed_urgent and not store.in_cooldown("urgent", cfg.alert_cooldown_seconds):
            store.update("urgent", armed=True)
            store.mark_alerted("urgent")
            await tg.send(urgent_message(cfg, spread, cheapest, len(exits)))
            log.info("URGENT sent at %+.2f%%", spread)
        else:
            store.update("urgent", armed=True)
    elif armed_urgent and spread < (cfg.urgent_spread_pct - cfg.clear_margin_pct):
        store.update("urgent", armed=False)


async def monitor_loop(cfg, rt, core, markets, store, tg) -> None:
    tick_no = 0
    last_flush = last_heartbeat = time.monotonic()
    while not _stop.is_set():
        started = time.monotonic()
        try:
            await tick(cfg, rt, core, markets, store, tg, tick_no)
        except Exception as exc:  # noqa: BLE001 - one tick must not kill the loop
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
            chains = store.get("chains")
            open_now = [k for k, v in chains.items()
                        if v.get("loss") is not None and v["loss"] <= cfg.max_exit_loss_pct]
            log.info(
                "heartbeat: ticks=%d spread=%s threshold=%.2f%% measured=%d open=%s "
                "lifi=%d other=%d hl=%d redis=%d",
                tick_no, store.get("spread").get("last_pct"), rt.go_spread_pct,
                len(chains), open_now or "none", markets.lifi_requests,
                markets.other_requests, core.request_count, store.command_count,
            )
            last_heartbeat = now

        elapsed = time.monotonic() - started
        try:
            await asyncio.wait_for(_stop.wait(), timeout=max(1.0, cfg.poll_seconds - elapsed))
        except asyncio.TimeoutError:
            pass


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
        tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id, http)
        rt = Runtime(cfg, store)
        commands = CommandServer(cfg, http, markets, store, rt, tg, snapshot)

        await tg.send(
            f"▶️ <b>Монітор KNTQ Elysium</b>  v{VERSION}\n"
            f"Коло: ${cfg.notional_usd:,.0f} · мереж: {len(cfg.chains)}\n"
            f"Поріг спреду: {rt.go_spread_pct:.2f}% · вихід ≤{cfg.max_exit_loss_pct:.2f}%\n"
            f"Продаж звіряється у USDC і в HYPE, береться кращий\n\n"
            f"<code>/spread 2.5%</code> — змінити поріг\n"
            f"<code>/pulls</code> — де зараз можна вийти, з глибиною\n"
            f"<code>/status</code> — поточний стан"
        )

        tasks = [
            asyncio.create_task(monitor_loop(cfg, rt, core, markets, store, tg)),
            asyncio.create_task(commands.run(_stop)),
        ]
        await _stop.wait()
        for t in tasks:
            t.cancel()
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
