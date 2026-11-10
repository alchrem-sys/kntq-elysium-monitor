# kntq-elysium-monitor

Watches the KNTQ round trip between HyperCore spot and Elysium, and sends a
Telegram alert when a lap is actually worth doing.

## The cycle

1. Buy KNTQ on HyperCore spot — pair `KNTQ/USDC`, addressed as `@334`
2. Move it to Elysium 1:1 through the canonical bridge (seconds, gas only)
3. Sell it on Elysium for USDC at a premium
4. Bring that USDC back to an EVM chain so the capital can go round again

Step 4 is the binding constraint, and the reason this service exists.

## Why the exit is the hard part

Moving value off Elysium is not a swap against a pool. An intent solver fronts
you its own funds on the destination chain and then spends about six days
recovering them through the canonical bridge. The price you pay is therefore set
by whether that solver still holds inventory on a given chain — which makes it a
cliff, not a depth curve:

| Solver inventory on that chain | Cost |
| --- | --- |
| available | ~0.3% at any size |
| drained | ~3% at any size, including $300 |

Splitting the order does not help. The question is never "how big can I go", it
is "which chain is still open". Measured 2026-11-10: Optimism 0.27% while
Arbitrum, Base and HyperEVM all sat at ~3.0% after being drained.

The canonical bridge is always available at 0% and about six days. Since what
sits in that queue is USDC, the wait carries no price risk — only frozen capital.

## Two carriers, and why neither is hardcoded

KNTQ can leave Elysium as USDC or as HYPE, and the better one moves.

Selling into HYPE quotes about 1% above selling into USDC — measured
2026-11-10, consistently across sizes:

| Size | → USDC | → HYPE (at spot) | HYPE better by |
| --- | --- | --- | --- |
| 3,000 KNTQ | $1,024.50 | $1,033.99 | +0.93% |
| 9,000 KNTQ | $3,070.24 | $3,099.33 | +0.95% |
| 30,000 KNTQ | $10,213.57 | $10,313.39 | +0.98% |

That gap is not free money. Converting the HYPE straight back to USDC on
Elysium lands at $3,070.10 — exactly the direct route — because Elysium USDC
trades under a dollar: everyone leaving sells it cheap. Solvers know that and
price the HYPE bridge accordingly, handing some or all of the premium back.
Through Relay the premium vanished entirely; through LI.FI Intents roughly
half of it survived.

So both carriers are measured end to end, from the same KNTQ quantity to USDC
actually delivered on the destination chain, and the winner is whichever pays
more. No HYPE price feed enters that decision, which is what makes it
trustworthy — a spot mark would quietly decide the answer.

## Commands

| Command | What it does |
| --- | --- |
| `/spread 2.5%` | Set the alert threshold. Persisted, survives restarts. |
| `/pulls` | Routes that can take the money out under `MAX_EXIT_LOSS_PCT`, with how much each can absorb. |
| `/status` | Latest spread, fill price, chosen carrier, best exit. |
| `/only 2` | Spread watch only, no exit needed. Pushes via Pushover, re-fires every +1%. `/only off` stops it. |
| `/pushover USER_KEY` | Connect Pushover. The bot deletes the message carrying the key. `/pushover test`, `/pushover off`. |

`/spread` and `/pulls` are limited to `TELEGRAM_ADMIN_ID` when it is set.

## Alerts

**GO** — spread ≥ `GO_SPREAD_PCT` *and* some chain can take the money back out
at ≤ `MAX_EXIT_LOSS_PCT`. Reports the net after the exit, not the gross.

**URGENT** — spread ≥ `URGENT_SPREAD_PCT`, sent even when every exit is drained,
because at that spread the six-day canonical withdrawal may be worth eating.

Both are edge-triggered with a hysteresis band (`CLEAR_MARGIN_PCT`) and a
cooldown, so a spread hovering at the threshold does not produce a stream of
messages.

## How the numbers are produced

- **Buy price** walks the real ask side of the `@334` order book for the full
  notional and adds the taker fee. A mid price would hide most of the cost on a
  two-percent edge.
- **Sell price** is a KyberSwap quote on Elysium for the exact quantity the walk
  produced, routed across every pool on the chain.
- **Exit cost** is a LI.FI quote per destination, with Relay as the fallback.

One caveat on the Hyperliquid info API: `spotMetaAndAssetCtxs` returns `universe`
and the context array *not* aligned by position — `universe[297]` is pair `@334`
while `contexts[297].coin` is `@297`. Match on the `coin` field, never the index.
This service reads `l2Book` directly and sidesteps it.

## Running it

```bash
cp .env.example .env     # fill in the values
pip install -r requirements.txt
python main.py
```

Telegram chat id, after adding the bot to the group and posting a message there:

```bash
python tools/get_chat_id.py
```

## Environment

Every variable is documented in `.env.example`. Required: `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `REDIS_URL`.

`LIFI_API_KEY` is optional but close to required in practice — the anonymous
rate limit is low enough that a few minutes of probing will lock you out for an
hour. Free key at <https://portal.li.fi>.

## Which chains, and how they are probed

38 EVM chains are watched — every one LI.FI lists a USDC for. They are never
all quoted at once; LI.FI's rate limit does not allow it, and finding out the
hard way costs an hour's lockout.

Instead each chain's last measured exit cost is kept in Redis, and probes are
spent in priority order:

1. chains last seen open, cheapest first
2. chains never measured, or whose reading has gone stale
3. chains last seen drained

The sweep stops at the **first** open exit, because one working route is all
the trade needs. In the common case that costs a single request. The budget
only bites when nothing is open anywhere — which is exactly when searching
further down the list is worth it.

## Request budget

Per tick (default 120s): 1 order book + 1 Kyber quote. Exit quotes are spent
only when the spread is within `CLEAR_MARGIN_PCT` of the trigger, plus one
background sample every tenth tick. So a quiet market costs ~1,440 calls a day
and burns no LI.FI quota; a live spread with a working exit costs one LI.FI
call per tick; a live spread with every chain drained costs up to `PROBES_HOT`.

Redis: one `HGETALL` at startup, then one pipelined write per 60s flush and only
when something changed — about 2,900 commands a day worst case, and none at all
while nothing moves.

## Addresses

Verified on-chain 2026-11-10. Look-alike USDC and WHYPE tokens exist on Elysium;
do not substitute these.

| What | Address |
| --- | --- |
| KNTQ (HyperEVM) | `0x000000000000780555bD0BCA3791f89f9542c2d6` |
| KNTQ (Elysium) | `0xe45a355d9e9cAd2aD85F2d03E3eE3102A036acF0` |
| USDC (Elysium) | `0xb47c4C8F73F009CC0C0d3f92B388d13E293F0B65` |

The Elysium KNTQ is the canonical bridged representation of the HyperEVM one:
the Elysium gateway router's `calculateL2TokenAddress(KNTQ_HYPEREVM)` returns
exactly that address.

## Deployment

Railway worker service, `Procfile` included. State lives in Upstash Redis
because Railway's filesystem is ephemeral.
