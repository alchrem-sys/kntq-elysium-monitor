#!/usr/bin/env python3
"""KNTQ spread in the terminal: Hyperliquid spot buy -> Elysium sell.

Live table: for each lap size it walks the real Hyperliquid ask book (with
taker fee), asks KyberSwap what that KNTQ fetches in USDC on Elysium right
now, subtracts the fomo exit fee and Elysium gas, and tells you GO or WAIT.

No installs needed (standard library only). Python 3.8+.

    python3 kntq_spread.py                    # 1k / 3k / 5k laps, refresh every 4s
    python3 kntq_spread.py -s 5000 -s 10000   # your own sizes
    python3 kntq_spread.py --go 1.5 --beep    # GO only from 1.5% net, beep on GO
    python3 kntq_spread.py --once             # print once and exit

Ctrl+C to quit.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HL_INFO = "https://api.hyperliquid.xyz/info"
HL_PAIR = "@334"  # KNTQ/USDC spot
KYBER = "https://aggregator-api.kyberswap.com/elysium/api/v1/routes"
KNTQ_ELYSIUM = "0xe45a355d9e9cAd2aD85F2d03E3eE3102A036acF0"
USDC_ELYSIUM = "0xb47c4C8F73F009CC0C0d3f92B388d13E293F0B65"
HEADERS = {"User-Agent": "kntq-spread/1.0", "x-client-id": "kntq-spread-cli"}

G, Y, R, B, D, X = "\033[92m", "\033[93m", "\033[91m", "\033[1m", "\033[2m", "\033[0m"


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


CTX = _ssl_context()


def http_json(url: str, body: dict | None = None, timeout: float = 8.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(HEADERS)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read())


def hl_asks() -> list[tuple[float, float]]:
    book = http_json(HL_INFO, {"type": "l2Book", "coin": HL_PAIR})
    return [(float(l["px"]), float(l["sz"])) for l in book["levels"][1]]


def buy_fill(asks, usd: float, fee_pct: float):
    """Walk the ask book for `usd` notional. Fee is taken in KNTQ, like HL does."""
    left, qty = usd, 0.0
    for px, sz in asks:
        take = min(sz, left / px)
        qty += take
        left -= take * px
        if left <= 1e-9:
            break
    if left > 1e-6:
        return None  # book too thin
    net_qty = qty * (1 - fee_pct / 100)
    return net_qty, usd / qty  # KNTQ received, avg price before fee


def kyber_sell(kntq: float):
    q = urllib.parse.urlencode({
        "tokenIn": KNTQ_ELYSIUM, "tokenOut": USDC_ELYSIUM,
        "amountIn": str(int(kntq * 1e18)), "gasInclude": "true",
    })
    s = (http_json(f"{KYBER}?{q}").get("data") or {}).get("routeSummary") or {}
    if not s.get("amountOut"):
        return None
    return int(s["amountOut"]) / 1e6, float(s.get("gasUsd") or 0)


def snapshot(sizes, fee_pct, fomo_pct):
    asks = hl_asks()
    fills = {sz: buy_fill(asks, sz, fee_pct) for sz in sizes}
    jobs = {sz: f for sz, f in fills.items() if f}
    with ThreadPoolExecutor(max_workers=len(jobs) or 1) as pool:
        quotes = dict(zip(jobs, pool.map(lambda f: kyber_sell(f[0]), jobs.values())))
    rows = []
    for sz in sizes:
        f, q = fills[sz], quotes.get(sz)
        if not f or not q:
            rows.append((sz, None))
            continue
        kntq, avg = f
        usdc, gas = q
        sell_px = usdc / kntq
        back = usdc * (1 - fomo_pct / 100) - gas
        profit = back - sz
        rows.append((sz, dict(kntq=kntq, buy=avg, sell=sell_px,
                              gross=(sell_px / avg - 1) * 100,
                              net=profit / sz * 100, profit=profit, gas=gas)))
    return asks[0][0] if asks else None, rows


def render(best_ask, rows, a, errors):
    out = []
    t = time.strftime("%H:%M:%S")
    out.append(f"{B}KNTQ  Hyperliquid spot → Elysium{X}   {D}{t}  ·  fee {a.fee}%  ·  fomo {a.fomo}%  ·  GO ≥ {a.go}%{X}")
    out.append(f"HL best ask: {B}{best_ask:.5f}{X}" if best_ask else "HL best ask: —")
    out.append("")
    out.append(f"{'lap':>8}  {'buy avg':>9}  {'sell':>9}  {'spread':>8}  {'net':>8}  {'profit':>9}  ")
    out.append(D + "-" * 64 + X)
    verdict = None
    for sz, r in rows:
        if r is None:
            out.append(f"{sz:>8,.0f}  {R}no quote / book too thin{X}")
            continue
        c = G if r["net"] >= a.go else (Y if r["net"] > 0 else R)
        out.append(f"{sz:>8,.0f}  {r['buy']:>9.5f}  {r['sell']:>9.5f}  {r['gross']:>7.2f}%  "
                   f"{c}{r['net']:>7.2f}%  {r['profit']:>+9.2f}{X}")
        if r["net"] >= a.go:
            verdict = sz
    out.append("")
    if verdict:
        out.append(f"{G}{B}  ▶ GO — до ${verdict:,.0f} на коло має сенс{X}")
    elif any(r and r["net"] > 0 for _, r in rows):
        out.append(f"{Y}{B}  ‖ WAIT — плюс є, але нижче порогу {a.go}%{X}")
    else:
        out.append(f"{R}{B}  ■ НЕ ГНАТИ — спреду немає{X}")
    if errors:
        out.append(f"{D}  last error: {errors}{X}")
    out.append(f"{D}  net = після комісії HL, газу Elysium і fomo. Ctrl+C — вихід{X}")
    return "\n".join(out), bool(verdict)


def main():
    p = argparse.ArgumentParser(description="Live KNTQ spread HL spot → Elysium")
    p.add_argument("-s", "--size", type=float, action="append", help="lap size in USDC (repeatable)")
    p.add_argument("-e", "--every", type=float, default=4.0, help="refresh seconds (default 4)")
    p.add_argument("--go", type=float, default=1.0, help="net %% needed for GO (default 1.0)")
    p.add_argument("--fee", type=float, default=0.07, help="HL taker fee %% (default 0.07)")
    p.add_argument("--fomo", type=float, default=0.05, help="fomo exit cost %% (default 0.05)")
    p.add_argument("--beep", action="store_true", help="beep when GO appears")
    p.add_argument("--once", action="store_true", help="print once and exit")
    a = p.parse_args()
    sizes = sorted(a.size or [1000, 3000, 5000])
    if os.name == "nt":
        os.system("")  # enable ANSI colours on Windows
    was_go, err, fails = False, "", 0
    try:
        while True:
            try:
                best, rows = snapshot(sizes, a.fee, a.fomo)
                text, go = render(best, rows, a, err)
                err, fails = "", 0
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
                fails += 1
                err = f"{type(e).__name__}: {e}"[:120]
                if "CERTIFICATE_VERIFY_FAILED" in err:
                    err += "  → pip3 install certifi"
                text, go = f"{R}fetch failed ({fails}){X}  {err}", False
            if a.once:
                print(text)
                return
            sys.stdout.write("\033[H\033[2J" + text + "\n")
            sys.stdout.flush()
            if go and not was_go and a.beep:
                sys.stdout.write("\a")
                sys.stdout.flush()
            was_go = go
            time.sleep(a.every if not fails else min(30, a.every * 2 ** fails))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
