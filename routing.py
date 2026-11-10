"""Which (chain, carrier) to try, and in what order.

Shared by the monitor loop and the /pulls command so both spend their probe
budget the same way: on routes that recently worked, before anything else.
"""
from __future__ import annotations

import time

from config import SEED_PRIORITY, Config
from markets import CARRIERS


def exit_order(cfg: Config, store, now: float | None = None) -> list[tuple[int, str]]:
    """(chain, carrier) pairs, most likely to be open first.

    Fresh and cheap first, then anything unmeasured or stale, then the routes
    last seen drained. Ties break on the seed list, which is the set LI.FI
    actually quoted from Elysium when this was built.
    """
    now = time.time() if now is None else now
    seen = store.get("chains")
    scored: list[tuple[tuple[int, float], tuple[int, str]]] = []
    for carrier in CARRIERS:
        for cid in cfg.chains:
            rec = seen.get(f"{carrier}:{cid}") or {}
            loss = rec.get("loss")
            age = now - (rec.get("ts") or 0.0)
            seed = SEED_PRIORITY.index(cid) if cid in SEED_PRIORITY else 99.0
            if loss is None or age > cfg.stale_seconds:
                key = (1, seed)
            elif loss <= cfg.max_exit_loss_pct:
                key = (0, loss)
            else:
                key = (2, loss)
            scored.append((key, (cid, carrier)))
    scored.sort()
    return [pair for _, pair in scored]


def remember(store, chain_id: int, carrier: str, loss: float) -> None:
    store.get("chains")[f"{carrier}:{chain_id}"] = {
        "loss": round(loss, 3), "ts": time.time()
    }
    store.touch()
