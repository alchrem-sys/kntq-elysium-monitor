"""Environment-driven configuration, validated at startup. No literals."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# --- fixed chain / token facts, verified on-chain 2026-11-10 ---

# Kinetiq governance token. The Elysium address is the canonical bridged
# representation of the HyperEVM one: the Elysium gateway router's
# calculateL2TokenAddress(KNTQ_HYPEREVM) returns exactly this address.
KNTQ_HYPEREVM = "0x000000000000780555bD0BCA3791f89f9542c2d6"
KNTQ_ELYSIUM = "0xe45a355d9e9cAd2aD85F2d03E3eE3102A036acF0"

# Canonical USDC on Elysium (Kinetiq docs + on-chain symbol()). Look-alike
# USDC and WHYPE tokens exist on Elysium; do not substitute this.
USDC_ELYSIUM = "0xb47c4C8F73F009CC0C0d3f92B388d13E293F0B65"

# HyperCore spot pair KNTQ/USDC, as app.hyperliquid.xyz/trade/KNTQ/USDC.
HYPERCORE_KNTQ_USDC = "@334"

# HyperCore spot pair HYPE/USDC (spotMeta: universe "@107" = tokens [150, 0]), checked 2026-11-15.
HYPERCORE_HYPE_USDC = "@107"
HYPE_USDC_SPOT_URL = "https://app.hyperliquid.xyz/trade/HYPE/USDC"
KYBER_HYPE_URL = "https://kyberswap.com/swap/elysium/hype-to-usdc"

# After a Pushover push about a spread, that asset's pushes stay muted this long.
# The group messages keep coming, so the spread can still be followed.
PUSH_QUIET_S = 3600.0

# Native HYPE is spelled differently by each API.
KYBER_NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

# Buttons attached to every alert, so the three legs are one tap each.
HYPE_SPOT_URL = "https://app.hyperliquid.xyz/trade/KNTQ/USDC"
BRIDGE_URL = (
    "https://elysium.kinetiq.xyz/bridge"
    "?tokenIn=1337:0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE:124"
)


def alert_buttons(to_chain: int | None = None, amount: float = 0.0) -> list[list[dict]]:
    return [[
        {"text": "Hype Spot", "url": HYPE_SPOT_URL},
        {"text": "Bridge", "url": BRIDGE_URL},
    ]]

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


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_chat_id: str
    telegram_thread_id: int | None
    telegram_admin_ids: frozenset[str]
    pushover_app_token: str
    pushover_priority: int
    redis_url: str

    notional_usd: float = 3000.0
    taker_fee_pct: float = 0.035

    clear_margin_pct: float = 0.4

    # Upstash free tier is metered per command, so flush rarely and only on
    # change: one SET per interval works out to roughly 289 commands a day.
    flush_seconds: float = 300.0
    heartbeat_seconds: float = 900.0
    http_timeout: float = 25.0
    command_poll_seconds: float = 25.0   # Telegram long-poll timeout
    spread_poll_seconds: float = 10.0    # fast spread-only loop


def load() -> Config:
    return Config(
        telegram_token=_req("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_req("TELEGRAM_CHAT_ID"),
        telegram_thread_id=int(os.environ["TELEGRAM_THREAD_ID"])
        if os.environ.get("TELEGRAM_THREAD_ID", "").strip() else None,
        telegram_admin_ids=frozenset(
            x.strip() for x in os.getenv("TELEGRAM_ADMIN_ID", "").split(",") if x.strip()
        ),
        pushover_app_token=os.getenv("PUSHOVER_APP_TOKEN", "").strip(),
        pushover_priority=int(_num("PUSHOVER_PRIORITY", 1)),
        redis_url=_req("REDIS_URL"),
        notional_usd=_num("NOTIONAL_USD", 3000.0),
        taker_fee_pct=_num("TAKER_FEE_PCT", 0.035),
        clear_margin_pct=_num("CLEAR_MARGIN_PCT", 0.4),
        flush_seconds=_num("FLUSH_SECONDS", 300.0),
        spread_poll_seconds=_num("SPREAD_POLL_SECONDS", 10.0),
    )


def summary(cfg: Config) -> str:
    return (
        f"notional=${cfg.notional_usd:,.0f} fee={cfg.taker_fee_pct}% "
        f"spread_poll={cfg.spread_poll_seconds:.0f}s "
        f"pushover_app={'set' if cfg.pushover_app_token else 'ABSENT'}"
    )
