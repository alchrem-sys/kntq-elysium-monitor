"""Environment-driven configuration, validated at startup. No literals."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

# --- fixed chain / token facts, verified on-chain 2026-11-10 ---

ELYSIUM_CHAIN_ID = 99901
HYPEREVM_CHAIN_ID = 999

# Kinetiq governance token. The Elysium address is the canonical bridged
# representation of the HyperEVM one: the Elysium gateway router's
# calculateL2TokenAddress(KNTQ_HYPEREVM) returns exactly this address.
KNTQ_HYPEREVM = "0x000000000000780555bD0BCA3791f89f9542c2d6"
KNTQ_ELYSIUM = "0xe45a355d9e9cAd2aD85F2d03E3eE3102A036acF0"

# Canonical USDC on Elysium (Kinetiq docs + on-chain symbol()). Look-alike
# USDC and WHYPE tokens exist on Elysium; do not substitute these.
USDC_ELYSIUM = "0xb47c4C8F73F009CC0C0d3f92B388d13E293F0B65"

# HyperCore spot pair KNTQ/USDC, as app.hyperliquid.xyz/trade/KNTQ/USDC.
# Addressed as "@334" on the info API.
HYPERCORE_KNTQ_USDC = "@334"

CHAIN_NAMES = {
    1: "Ethereum", 10: "Optimism", 56: "BSC", 130: "Unichain", 137: "Polygon",
    143: "Monad", 999: "HyperEVM", 8453: "Base", 42161: "Arbitrum",
    480: "World", 534352: "Scroll", 59144: "Linea", 81457: "Blast",
}

# Destination USDC addresses, for exit quotes.
USDC_BY_CHAIN = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    999: "0xb88339cb7199b77e23db6e890353e22632ba630f",
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
}


class ConfigError(RuntimeError):
    pass


def _req(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise ConfigError(
            f"Missing required environment variable {name!r}. "
            "See .env.example for what it is and where to get it."
        )
    return v


def _num(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _chains(name: str, default: list[int]) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            cid = int(part)
        except ValueError as exc:
            raise ConfigError(f"{name} entry {part!r} is not a chain id") from exc
        if cid not in USDC_BY_CHAIN:
            raise ConfigError(
                f"{name} contains chain {cid}, which has no USDC address configured. "
                f"Known: {sorted(USDC_BY_CHAIN)}"
            )
        out.append(cid)
    if not out:
        raise ConfigError(f"{name} resolved to an empty list")
    return out


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_chat_id: str
    redis_url: str
    lifi_api_key: str
    lifi_base: str
    quote_address: str

    notional_usd: float = 3000.0
    taker_fee_pct: float = 0.035          # HyperCore spot taker, percent

    go_spread_pct: float = 3.0            # alert when spread >= this AND exit is open
    urgent_spread_pct: float = 5.0        # alert at this spread even with no exit
    max_exit_loss_pct: float = 1.0        # an exit counts as "open" below this
    clear_margin_pct: float = 0.4         # hysteresis band for going quiet again

    core_chains: list[int] = field(default_factory=list)   # polled every tick
    rotating_chains: list[int] = field(default_factory=list)
    rotate_per_tick: int = 2

    poll_seconds: float = 120.0
    spacing_seconds: float = 2.0
    alert_cooldown_seconds: float = 600.0
    flush_seconds: float = 60.0
    heartbeat_seconds: float = 900.0
    http_timeout: float = 25.0


def load() -> Config:
    go = _num("GO_SPREAD_PCT", 3.0)
    urgent = _num("URGENT_SPREAD_PCT", 5.0)
    if urgent < go:
        raise ConfigError(
            f"URGENT_SPREAD_PCT ({urgent}) must be >= GO_SPREAD_PCT ({go}); "
            "the urgent alert is the wider, no-exit-required case."
        )
    core = _chains("CORE_CHAINS", [10, 42161, 8453, 999])
    rotating = _chains("ROTATING_CHAINS", [1, 137])
    return Config(
        telegram_token=_req("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_req("TELEGRAM_CHAT_ID"),
        redis_url=_req("REDIS_URL"),
        lifi_api_key=os.getenv("LIFI_API_KEY", "").strip(),
        lifi_base=os.getenv("LIFI_BASE_URL", "https://li.quest/v1").rstrip("/"),
        quote_address=os.getenv(
            "QUOTE_ADDRESS", "0x1111111111111111111111111111111111111111"
        ),
        notional_usd=_num("NOTIONAL_USD", 3000.0),
        taker_fee_pct=_num("TAKER_FEE_PCT", 0.035),
        go_spread_pct=go,
        urgent_spread_pct=urgent,
        max_exit_loss_pct=_num("MAX_EXIT_LOSS_PCT", 1.0),
        clear_margin_pct=_num("CLEAR_MARGIN_PCT", 0.4),
        core_chains=core,
        rotating_chains=rotating,
        rotate_per_tick=int(_num("ROTATE_PER_TICK", 2)),
        poll_seconds=_num("POLL_SECONDS", 120.0),
        spacing_seconds=_num("REQUEST_SPACING_SECONDS", 2.0),
        alert_cooldown_seconds=_num("ALERT_COOLDOWN_SECONDS", 600.0),
    )


def summary(cfg: Config) -> str:
    return (
        f"notional=${cfg.notional_usd:,.0f} go>={cfg.go_spread_pct}% "
        f"urgent>={cfg.urgent_spread_pct}% exit<={cfg.max_exit_loss_pct}% "
        f"core={[CHAIN_NAMES.get(c, c) for c in cfg.core_chains]} "
        f"rotating={[CHAIN_NAMES.get(c, c) for c in cfg.rotating_chains]} "
        f"poll={cfg.poll_seconds:.0f}s "
        f"lifi_key={'set' if cfg.lifi_api_key else 'ABSENT (low rate limit)'}"
    )
