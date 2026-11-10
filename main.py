"""KNTQ Elysium round-trip monitor.

The cycle, as traded:

    1. buy KNTQ on HyperCore spot (KNTQ/USDC, @334)
    2. bridge it to Elysium 1:1, canonical, seconds, gas only
    3. sell it on Elysium and end up holding USDC there
    4. move that USDC to any EVM chain, so the capital can go round again

Step 4 is the binding constraint. Moving value off Elysium is not a swap
against a pool -- an intent solver fronts you its own funds on the destination
and then waits out the ~6 day canonical withdrawal to recover them. The cost
is set by whether that solver still holds inventory on a given chain, which
makes it a cliff rather than a depth curve: around 0.3% while inventory lasts,
then ~3% at every size once it is gone. Splitting the order does not help;
finding an unspent chain does.

On step 3: selling into HYPE quotes about 1% above selling into USDC, but
converting that HYPE back to USDC on Elysium lands at exactly the direct
number -- measured to 0.000% at 3k and 9k. KyberSwap already routes through
HYPE when that is the better path. So the two-step is the direct quote, and
both are reported so the claim can be checked by hand with /test. Bridging
the HYPE itself is a genuinely different option, and is priced alongside.

Alerts:
  GO      spread >= the runtime threshold (/spread) and some chain can take
          the money out at <= MAX_EXIT_LOSS_PCT. Reports net, not gross.
  URGENT  spread >= URGENT_SPREAD_PCT even with every exit drained.
  TELL    one-shot, armed by /tell: fires when an exit appears that can
          absorb a given size.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

import httpx

import config
from commands import DEPTH_LADDER, CommandServer, Runtime
from config import (
    BRIDGE_URL,
    CHAIN_NAMES,
    HYPE_SPOT_URL,
    Config,
    HYPERCORE_KNTQ_USDC,
    KNTQ_ELYSIUM,
    KYBER_NATIVE,
    USDC_ELYSIUM,
    alert_buttons,
)
from hyperliquid import HyperCore
from markets import Exit, Markets, RateLimited
from notify import Pushover, Telegram
from routing import exit_order, remember
from state import Store

VERSION = "1.3.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")

_stop = asyncio.Event()
_snapshot: dict = {}

# /only re-fires once the spread climbs this much above where it last fired.
ESCALATION_PCT = 1.0
_push: Pushover | None = None


async def push(title: str, message: str, priority: int = 1,
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


async def sweep_exits(cfg: Config, markets: Markets, store: Store,
                      usdc_amount: float, hype_amount: float, hype_usd: float,
                      budget: int) -> list[Exit]:
    """Probe until one usable exit is found, then price the other carrier there."""
    found: list[Exit] = []
    probed = 0
    hit: tuple[int, str] | None = None
    legs = {"USDC": (usdc_amount, usdc_amount), "HYPE": (hype_amount, hype_usd)}

    for chain_id, carrier in exit_order(cfg, store):
        if _stop.is_set() or probed >= budget:
            break
        amount, reference = legs[carrier]
        if not amount or not reference:
            continue
        await asyncio.sleep(cfg.spacing_seconds)
        probed += 1
        try:
            q = await markets.exit_quote(chain_id, carrier, amount, reference)
        except RateLimited as exc:
            log.warning("LI.FI rate limited (%s); ending sweep", str(exc)[:90])
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("Probe %s/%s failed: %s",
                        CHAIN_NAMES.get(chain_id, chain_id), carrier, exc)
            continue
        if q is None:
            remember(store, chain_id, carrier, 99.0)
            continue
        remember(store, chain_id, carrier, q.loss_pct)
        found.append(q)
        if q.loss_pct <= cfg.max_exit_loss_pct:
            hit = (chain_id, carrier)
            break

    if hit:
        chain_id, used = hit
        other = "HYPE" if used == "USDC" else "USDC"
        amount, reference = legs[other]
        if amount and reference:
            await asyncio.sleep(cfg.spacing_seconds)
            try:
                q = await markets.exit_quote(chain_id, other, amount, reference)
                if q:
                    remember(store, chain_id, other, q.loss_pct)
                    found.append(q)
            except Exception:  # noqa: BLE001 - the comparison is a bonus
                pass

    log.info("sweep: %d probe(s), %d quote(s)", probed, len(found))
    return found


async def measure_depth(cfg: Config, markets: Markets, chain_id: int, carrier: str,
                        amount: float, reference: float) -> tuple[float, bool]:
    """Largest notional this route still clears the threshold at."""
    best, capped = reference, False
    for i, rung in enumerate(DEPTH_LADDER[1:], start=1):
        await asyncio.sleep(cfg.spacing_seconds)
        try:
            q = await markets.exit_quote(chain_id, carrier, amount * rung, reference * rung)
        except Exception:  # noqa: BLE001
            break
        if q is None or q.loss_pct > cfg.max_exit_loss_pct:
            break
        best = reference * rung
        capped = i == len(DEPTH_LADDER) - 1
    return best, capped


def go_message(threshold: float, t: dict, alt: Exit | None) -> str:
    extra = ""
    if alt is not None and alt.usdc_out:
        delta = 100.0 * (t["final"] / alt.usdc_out - 1.0)
        extra = f"\n      через {alt.carrier} було б {alt.usdc_out:,.2f} ({delta:+.2f}%)"
    return (
        f"🟢 <b>КОЛО ВІДКРИТЕ</b>  нетто {_pct(t['net'])}  ({t['net_usd']:+,.0f}$)\n\n"
        f"<b>1.</b> HyperCore: ${t['notional']:,.0f} → {t['qty']:,.0f} KNTQ "
        f"@ {t['buy_px']:.5f}\n"
        f"      глибина асків ${t['book_depth']:,.0f}\n"
        f"<b>2.</b> Міст на Elysium 1:1, секунди\n"
        f"<b>3.</b> Продаж → {t['hype_out']:,.2f} HYPE → "
        f"<b>{t['sell_used']:,.2f} USDC</b>\n"
        f"      спред {_pct(t['spread'])} (поріг {threshold:.2f}%)\n"
        f"<b>4.</b> Вихід на {t['chain']} → <b>{t['final']:,.2f} USDC</b>\n"
        f"      −{t['exit_loss']:.2f}% · <code>{t['provider']}</code> "
        f"· ~{t['seconds']}с{extra}"
    )


def tell_message(chain: str, carrier: str, provider: str, loss: float,
                 delivered: float, depth: float, capped: bool, target: float) -> str:
    return (
        f"🔔 <b>З'ЯВИВСЯ ВИХІД</b>\n\n"
        f"<b>{chain}</b> · {carrier} · −{loss:.2f}%\n"
        f"Маршрут: <code>{provider}</code>\n"
        f"На ${target:,.0f} віддає {delivered:,.2f} USDC\n"
        f"Тримає {'щонайменше ' if capped else 'до '}<b>${depth:,.0f}</b>\n\n"
        f"Готуйся до кола — інвентар солвера може піти так само швидко."
    )


async def tick(cfg: Config, rt: Runtime, core: HyperCore, markets: Markets,
               store: Store, tg: Telegram, tick_no: int) -> None:
    fill = await core.buy_fill(cfg.notional_usd, cfg.taker_fee_pct)
    if fill is None or not fill.filled:
        log.warning("No usable buy fill this tick")
        return

    raw_qty = str(int(fill.qty * 1e18))
    direct = await markets.quote_elysium(KNTQ_ELYSIUM, USDC_ELYSIUM, raw_qty)
    to_hype = await markets.quote_elysium(KNTQ_ELYSIUM, KYBER_NATIVE, raw_qty)
    if direct is None:
        log.warning("No Elysium sell route this tick")
        return
    direct_usdc, direct_hops = direct[0] / 1e6, direct[1]

    hype_out = two_step = 0.0
    hype_hops = two_hops = 0
    if to_hype:
        hype_out, hype_hops = to_hype[0] / 1e18, to_hype[1]
        back = await markets.quote_elysium(KYBER_NATIVE, USDC_ELYSIUM, str(to_hype[0]))
        if back:
            two_step, two_hops = back[0] / 1e6, back[1]

    # The traded path ends in USDC on Elysium. Kyber already routes through
    # HYPE when that pays more, so the two-step usually equals the direct
    # quote; take whichever is larger and say which it was.
    sell_used = max(direct_usdc, two_step)
    sell_note = "via HYPE" if two_step > direct_usdc else "direct == via HYPE"
    mid = await core.hype_mid() or 0.0
    hype_usd = hype_out * mid

    spread = 100.0 * (sell_used / fill.cost_usd - 1.0)
    store.update("spread", last_pct=round(spread, 3))

    # --- /only: spread watch, fired before any exit probing so the push is
    # immediate. Needs no exit, so it costs nothing against the LI.FI budget.
    only = store.get("only")
    only_pct = only.get("pct")
    if only_pct:
        # Fire on crossing the threshold, and again whenever the spread climbs
        # another ESCALATION_PCT above wherever it last fired -- otherwise a
        # move from 2% to 5% would pass in silence.
        fired_at = only.get("fired_at")
        due = spread >= only_pct and (
            not only.get("fired") or
            (fired_at is not None and spread >= fired_at + ESCALATION_PCT)
        )
        if due:
            store.update("only", fired=True, fired_at=round(spread, 3))
            await push(f"KNTQ спред {_pct(spread)}",
                       f"Поріг {only_pct:.2f}%. ${cfg.notional_usd:,.0f} → "
                       f"{sell_used:,.2f} USDC на Elysium "
                       f"({cfg.notional_usd * spread / 100:+,.0f}$ валом).",
                       priority=1, url=HYPE_SPOT_URL, url_title="Hype Spot")
            await tg.send(
                f"📈 <b>Спред {_pct(spread)}</b>  (поріг {only_pct:.2f}%)\n\n"
                f"Купівля {fill.avg_px:.5f} · книга ${fill.book_depth_usd:,.0f}\n"
                f"${cfg.notional_usd:,.0f} → {fill.qty:,.0f} KNTQ → "
                f"<b>{sell_used:,.2f} USDC</b> на Elysium\n"
                f"Валом {cfg.notional_usd * spread / 100:+,.0f}$ до вартості виходу.",
                alert_buttons(None, sell_used),
            )
            log.info("ONLY alert sent at %+.2f%%", spread)
        elif only.get("fired") and spread < (only_pct - cfg.clear_margin_pct):
            store.update("only", fired=False, fired_at=None)
            log.info("ONLY re-armed at %+.2f%%", spread)

    threshold = rt.go_spread_pct
    tell = store.get("tell")
    tell_armed = bool(tell.get("armed"))
    if spread >= threshold:
        budget = cfg.probes_hot
    elif spread >= threshold - cfg.clear_margin_pct or tell_armed:
        budget = cfg.probes_warm
    elif tick_no % 10 == 0:
        budget = cfg.probes_cold
    else:
        budget = 0

    exits = (await sweep_exits(cfg, markets, store, sell_used, hype_out, hype_usd, budget)
             if budget else [])
    opens = [e for e in exits if e.loss_pct <= cfg.max_exit_loss_pct]
    best = max(opens, key=lambda e: e.usdc_out) if opens else None
    cheapest = min(exits, key=lambda e: e.loss_pct) if exits else None
    alt = next((e for e in exits if best and e.chain_id == best.chain_id
                and e.carrier != best.carrier), None)

    final = best.usdc_out if best else 0.0
    net = 100.0 * (final / cfg.notional_usd - 1.0) if best else float("nan")

    t = {
        "notional": cfg.notional_usd, "fee_pct": cfg.taker_fee_pct,
        "buy_px": fill.avg_px, "qty": fill.qty, "book_depth": fill.book_depth_usd,
        "hype_out": hype_out, "hype_hops": hype_hops,
        "two_step": two_step, "two_hops": two_hops,
        "direct": direct_usdc, "direct_hops": direct_hops,
        "sell_used": sell_used, "sell_note": sell_note, "spread": spread,
        "chain": best.chain if best else "-", "provider": best.provider if best else "-",
        "exit_loss": best.loss_pct if best else 0.0,
        "seconds": best.seconds if best else 0,
        "final": final, "net": net if best else 0.0,
        "net_usd": cfg.notional_usd * net / 100.0 if best else 0.0,
        "hype_direct": alt.usdc_out if alt and alt.carrier == "HYPE" else None,
    }
    _snapshot.update({
        "test": t, "spread": spread, "buy_px": fill.avg_px,
        "depth": fill.book_depth_usd,
        "carrier": best.carrier if best else "-", "final": final,
        "exit": f"{best.chain} −{best.loss_pct:.2f}%" if best else "не знайдено",
        "proceeds": {"USDC": {"amount": sell_used, "usd": sell_used},
                     "HYPE": {"amount": hype_out, "usd": hype_usd}},
    })

    log.info(
        "spread %+.2f%% | buy %.5f | sell %.2f USDC (hype $%.0f) | best %s | net %s",
        spread, fill.avg_px, sell_used, hype_usd,
        f"{best.chain}/{best.carrier} -{best.loss_pct:.2f}%" if best else "none",
        f"{net:+.2f}%" if best else "n/a",
    )

    buttons = alert_buttons(best.chain_id if best else None, sell_used)

    # --- /tell: one-shot, fires on the first exit that can absorb the size ---
    if tell_armed and best:
        target = float(tell.get("min_usd") or cfg.notional_usd)
        leg = (sell_used, sell_used) if best.carrier == "USDC" else (hype_out, hype_usd)
        depth, capped = await measure_depth(cfg, markets, best.chain_id,
                                            best.carrier, leg[0], leg[1])
        if depth >= target:
            store.update("tell", armed=False)
            await push(f"Вихід відкрився: {best.chain}",
                       f"{best.carrier} −{best.loss_pct:.2f}%, тримає ${depth:,.0f}",
                       priority=1)
            await tg.send(tell_message(best.chain, best.carrier, best.provider,
                                       best.loss_pct, best.usdc_out, depth,
                                       capped, target), buttons)
            log.info("TELL fired: %s depth $%.0f", best.chain, depth)

    armed_go = bool(store.get("go").get("armed"))
    if spread >= threshold and best:
        if not armed_go and not store.in_cooldown("go", cfg.alert_cooldown_seconds):
            store.update("go", armed=True)
            store.mark_alerted("go")
            await push(f"КОЛО ВІДКРИТЕ {_pct(t['net'])}",
                       f"Спред {_pct(spread)}, вихід {t['chain']} −{t['exit_loss']:.2f}%, "
                       f"нетто {t['net_usd']:+,.0f}$", priority=1,
                       url=HYPE_SPOT_URL, url_title="Hype Spot")
            await tg.send(go_message(threshold, t, alt), buttons)
            log.info("GO sent: net %+.2f%% via %s", net, best.chain)
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
            line = (f"Найдешевший із {len(exits)}: {cheapest.chain} / {cheapest.carrier} "
                    f"−{cheapest.loss_pct:.2f}%." if cheapest else "Жодного маршруту.")
            await push(f"Широкий спред {_pct(spread)}", "Виходу немає. " + line,
                       priority=1, url=BRIDGE_URL, url_title="Bridge")
            await tg.send(
                f"🟠 <b>ШИРОКИЙ СПРЕД</b> {_pct(spread)} — виходу немає\n\n"
                f"${cfg.notional_usd:,.0f} → {sell_used:,.2f} USDC на Elysium.\n{line}",
                buttons,
            )
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
        except Exception as exc:  # noqa: BLE001
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
            log.info("heartbeat: ticks=%d spread=%s threshold=%.2f%% open=%s "
                     "lifi=%d other=%d hl=%d redis=%d",
                     tick_no, store.get("spread").get("last_pct"), rt.go_spread_pct,
                     open_now or "none", markets.lifi_requests,
                     markets.other_requests, core.request_count, store.command_count)
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
        global _push
        _push = Pushover(cfg.pushover_app_token, http, store)
        commands = CommandServer(cfg, http, markets, store, rt, tg, snapshot, _push)

        await tg.send(
            f"▶️ <b>Монітор KNTQ Elysium</b>  v{VERSION}\n"
            f"Коло: ${cfg.notional_usd:,.0f} · мереж: {len(cfg.chains)}\n"
            f"Поріг: {rt.go_spread_pct:.2f}% · вихід ≤{cfg.max_exit_loss_pct:.2f}%\n\n"
            f"<code>/only 2</code> — стежити лише за спредом, пуш у Pushover\n"
            f"<code>/pushover USER_KEY</code> — підключити Pushover\n"
            f"<code>/spread 2.5%</code> — поріг для алерта з виходом\n"
            f"<code>/tell</code> — сповістити, коли з'явиться пул на "
            f"${cfg.notional_usd:,.0f}\n"
            f"<code>/pulls</code> — де зараз можна вийти, з глибиною\n"
            f"<code>/test</code> — повний розрахунок, англійською\n"
            f"<code>/status</code> — поточний стан",
            alert_buttons(None, cfg.notional_usd),
        )

        tasks = [
            asyncio.create_task(monitor_loop(cfg, rt, core, markets, store, tg)),
            asyncio.create_task(commands.run(_stop)),
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
