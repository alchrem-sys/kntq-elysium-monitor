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
# USDC and WHYPE tokens exist on Elysium; do not substitute this.
USDC_ELYSIUM = "0xb47c4C8F73F009CC0C0d3f92B388d13E293F0B65"

# HyperCore spot pair KNTQ/USDC, as app.hyperliquid.xyz/trade/KNTQ/USDC.
HYPERCORE_KNTQ_USDC = "@334"

# Native HYPE is spelled differently by each API.
KYBER_NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
LIFI_NATIVE = "0x0000000000000000000000000000000000000000"

# Buttons attached to every alert, so the three legs are one tap each.
HYPE_SPOT_URL = "https://app.hyperliquid.xyz/trade/KNTQ/USDC"
BRIDGE_URL = (
    "https://elysium.kinetiq.xyz/bridge"
    "?tokenIn=1337:0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE:124"
)


def jumper_url(to_chain: int, amount: float) -> str:
    """Jumper, pre-filled for the chain that currently has an open exit."""
    to_token = USDC_BY_CHAIN.get(to_chain, "")
    return (
        f"https://jumper.xyz/?fromAmount={amount:.0f}"
        f"&fromChain={ELYSIUM_CHAIN_ID}&fromToken={USDC_ELYSIUM}"
        f"&toChain={to_chain}&toToken={to_token}"
    )


def alert_buttons(to_chain: int | None, amount: float) -> list[list[dict]]:
    rows = [[
        {"text": "Hype Spot", "url": HYPE_SPOT_URL},
        {"text": "Bridge", "url": BRIDGE_URL},
    ]]
    if to_chain is not None:
        rows.append([{
            "text": f"Jumper → {CHAIN_NAMES.get(to_chain, to_chain)}",
            "url": jumper_url(to_chain, amount),
        }])
    return rows

# Every EVM mainnet LI.FI lists a USDC for, 2026-11-10. Testnets excluded.
# Destination decimals are read from each quote, so BSC's 18-decimal USDC
# needs no special case here.
USDC_BY_CHAIN: dict[int, str] = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    25: "0x3D7F2C478aAfdB65542BCB44bCeeC05849999d2D",
    40: "0x8D97Cea50351Fb4329d591682b148D43a0C3611b",
    50: "0xfA2958CB79b0491CC627c1557F441eF849Ca8eb1",
    56: "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    100: "0xDDAfbb505ad214D7b80b1f830fcCc89B60fb7A83",
    122: "0x620fd5fa44BE6af63715Ef4E65DDFA0387aD13F5",
    130: "0x078D782b760474a361dDA0AF3839290b0EF57AD6",
    137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    143: "0x754704Bc059F8C67012fEd69BC8A327a5aafb603",
    146: "0x29219dd400f2Bf60E5a23d13Be72B486D4038894",
    196: "0xB6CEceAB302E2E4948951eE7843FC24E92933061",
    324: "0x1d17CBcF0D6D143135aE902365D2E5e2A16538D4",
    480: "0x79A02482A880bCE3F13e09Da970dC34db4CD24d1",
    999: "0xb88339CB7199b77E23DB6E890353E22632Ba630f",
    1329: "0xe15fC38F6D8c56aF07bbCBe3BAf5708A2Bf42392",
    1337: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    1672: "0xC879C018dB60520F4355C26eD1a6D572cdAC1815",
    1776: "0xa00C59fF5a080D2b954d0c75e46E22a0c371235a",
    2020: "0x0B7007c13325C48911F73A2daD5FA5dCBf808aDc",
    2818: "0xCfb1186F4e93D60E60a8bDd997427D1F33bc372B",
    5000: "0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9",
    5042: "0x3600000000000000000000000000000000000000",
    8217: "0x6270B58BE569a7c0b8f47594F191631Ae5b2C86C",
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    13371: "0x6de8aCC0D406837030CE4dd28e7c08C5a96a30d2",
    34443: "0xd988097fb8612cc24eeC14542bC03424c656005f",
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    42170: "0x750ba8b76187092B0D1E87E28daaf484d1b5273b",
    42220: "0xcebA9300f2b948710d2653dD7B07f33A8B32118C",
    42793: "0x796Ea11Fa2dD751eD01b53C372fFDB4AAa8f00F9",
    43114: "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
    57073: "0x2D270e6886d130D724215A266106e6832161EAEd",
    59144: "0x176211869cA2b568f2A7D4EE941E073a821EE1ff",
    98866: "0x222365EF19F7947e5484218551B56bb3965Aa7aF",
    534352: "0x06eFdBFf2a14a7c8E15944D1F4A48F9F95F663A4",
    3586256: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
}

CHAIN_NAMES: dict[int, str] = {
    1: "Ethereum", 10: "Optimism", 25: "Cronos", 40: "Telos", 50: "XDC",
    56: "BSC", 100: "Gnosis", 122: "Fuse", 130: "Unichain", 137: "Polygon",
    143: "Monad", 146: "Sonic", 196: "XLayer", 324: "zkSync", 480: "World",
    999: "HyperEVM", 1329: "Sei", 1337: "Hyperliquid", 1672: "Pharos",
    1776: "Injective", 2020: "Ronin", 2818: "Morph", 5000: "Mantle",
    5042: "Arc", 8217: "Kaia", 8453: "Base", 13371: "Immutable",
    34443: "Mode", 42161: "Arbitrum", 42170: "Arbitrum Nova", 42220: "Celo",
    42793: "Etherlink", 43114: "Avalanche", 57073: "Ink", 59144: "Linea",
    98866: "Plume", 534352: "Scroll", 3586256: "Lighter",
}

# Probed first on a cold start, before any chain has a measured history.
# These are the ones LI.FI actually quoted from Elysium on 2026-11-10.
SEED_PRIORITY = [10, 42161, 8453, 999, 137, 1, 56, 130, 143, 1337]


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
    if raw.lower() == "all":
        return sorted(USDC_BY_CHAIN)
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
    telegram_thread_id: int | None
    telegram_admin_ids: frozenset[str]
    pushover_app_token: str
    pushover_priority: int
    redis_url: str
    lifi_api_key: str
    lifi_base: str
    quote_address: str

    notional_usd: float = 3000.0
    taker_fee_pct: float = 0.035

    go_spread_pct: float = 3.0
    urgent_spread_pct: float = 5.0
    max_exit_loss_pct: float = 1.0
    clear_margin_pct: float = 0.4

    chains: list[int] = field(default_factory=list)
    probes_hot: int = 12     # when the spread is live, search harder
    probes_warm: int = 5     # spread approaching the trigger
    probes_cold: int = 3     # background refresh only
    stale_seconds: float = 1800.0   # a chain's reading goes stale after this

    poll_seconds: float = 120.0
    spacing_seconds: float = 2.0
    alert_cooldown_seconds: float = 600.0
    # Upstash free tier is metered per command, so flush rarely and only on
    # change: one SET per interval works out to roughly 289 commands a day.
    flush_seconds: float = 300.0
    heartbeat_seconds: float = 900.0
    http_timeout: float = 25.0
    command_poll_seconds: float = 25.0   # Telegram long-poll timeout
    spread_poll_seconds: float = 10.0    # fast spread-only loop


def load() -> Config:
    go = _num("GO_SPREAD_PCT", 3.0)
    urgent = _num("URGENT_SPREAD_PCT", 5.0)
    if urgent < go:
        raise ConfigError(
            f"URGENT_SPREAD_PCT ({urgent}) must be >= GO_SPREAD_PCT ({go}); "
            "the urgent alert is the wider, no-exit-required case."
        )
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
        chains=_chains("EXIT_CHAINS", sorted(USDC_BY_CHAIN)),
        probes_hot=int(_num("PROBES_HOT", 12)),
        probes_warm=int(_num("PROBES_WARM", 5)),
        probes_cold=int(_num("PROBES_COLD", 3)),
        stale_seconds=_num("STALE_SECONDS", 1800.0),
        poll_seconds=_num("POLL_SECONDS", 120.0),
        spacing_seconds=_num("REQUEST_SPACING_SECONDS", 2.0),
        alert_cooldown_seconds=_num("ALERT_COOLDOWN_SECONDS", 600.0),
        flush_seconds=_num("FLUSH_SECONDS", 300.0),
        spread_poll_seconds=_num("SPREAD_POLL_SECONDS", 10.0),
    )


def summary(cfg: Config) -> str:
    return (
        f"notional=${cfg.notional_usd:,.0f} go>={cfg.go_spread_pct}% "
        f"urgent>={cfg.urgent_spread_pct}% exit<={cfg.max_exit_loss_pct}% "
        f"chains={len(cfg.chains)} probes hot/warm/cold="
        f"{cfg.probes_hot}/{cfg.probes_warm}/{cfg.probes_cold} "
        f"poll={cfg.poll_seconds:.0f}s "
        f"lifi_key={'set' if cfg.lifi_api_key else 'ABSENT (low rate limit)'} "
        f"pushover_app={'set' if cfg.pushover_app_token else 'ABSENT'}"
    )
