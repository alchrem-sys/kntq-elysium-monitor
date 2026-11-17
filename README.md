# kntq-elysium-monitor

Watches the spread on the KNTQ round trip between HyperCore spot and Elysium
(and the same for HYPE), keeps it in a pinned Telegram message and alerts when
it passes a threshold.

## The cycle

1. Buy KNTQ on HyperCore spot — pair `KNTQ/USDC`, addressed as `@334`
2. Move it to Elysium 1:1 through the canonical bridge (seconds, gas only)
3. Sell it on Elysium for USDC at a premium
4. Take the USDC off Elysium (fomo) and go round again

The monitor prices steps 1 and 3. Exit routes off Elysium are not scanned
(that part was removed in v1.7.0).

## How the numbers are produced

- **Buy price** walks the real ask side of the `@334` order book for the full
  size and adds the taker fee.
- **Sell price** is a KyberSwap quote on Elysium for the exact quantity the walk
  produced, routed across every pool on the chain (through HYPE when that pays).

Every few seconds (`/poll`, default 10 s), for the pinned message's size.

## Commands

| Command | What it does |
| --- | --- |
| `/status` | Latest KNTQ spread with buy and sell price, and the HYPE block. |
| `/only 1` | Alert when the KNTQ spread is above 1 % (`/spread 1` is the same). A group message on every +1 %, a Pushover push at most once an hour. `/only off`. |
| `/pin 5000` | Pinned message with the KNTQ and HYPE spread for $5,000, edited as it moves. `/pin off`. |
| `/hype` | HYPE spread HL → Elysium. `/hype 1` alert threshold, `/hype off`. |
| `/whales` | Big KNTQ holders: push when one sells at market. |
| `/poll 5` | How often the spread is checked, 3–120 s. |
| `/allow` / `/deny` | Owner only: reply to someone's message to grant or revoke command access. `/myid` shows your id. |
| `/pushover USER_KEY` | Connect Pushover. The bot deletes the message carrying the key. `/pushover test`, `/pushover off`. |

## HYPE spread

The fast loop also measures HYPE from HyperCore spot (`@107`) to Elysium for
the same lap size as the pin: buy on the HyperCore asks (taker fee), sell for
USDC on Elysium. Kyber refuses above roughly ten requests a minute and KNTQ
uses six, so HYPE gets one quote every third round (about every 30 s).

It shows in the pinned message and in `/status`; `/hype` prints it. A Pushover
push and a group message fire above 1 % (`/hype 1.5` changes the threshold,
`/hype off` stops it), again when the spread climbs another 0.5 %.

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

## Request budget

Per round: one HyperCore order book and one Kyber quote for KNTQ; HYPE adds one
of each every third round. Kyber starts refusing at roughly ten requests a
minute; a 429 pauses all Kyber calls for a few seconds.

Redis: one read at startup, then one write per flush interval and only when
something changed.

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
