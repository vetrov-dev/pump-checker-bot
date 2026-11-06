"""Bot settings, loaded from the .env file next to this script."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


def _ids(name: str) -> frozenset[int]:
    return frozenset(int(x) for x in _str(name).replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    rpc_url: str            # holders need a provider that serves getTokenLargestAccounts (the public RPC refuses it)
    ws_url: str             # trade stream; empty = public Solana websocket (sees only part of the trades)
    db_path: Path

    free_lookups_per_day: int
    free_watches: int
    pro_lookups_per_day: int
    pro_watches: int
    pro_price_stars: int
    pro_days: int
    support_contact: str


def load() -> Config:
    return Config(
        bot_token=_str("BOT_TOKEN"),
        admin_ids=_ids("ADMIN_IDS"),
        rpc_url=_str("RPC_URL") or PUBLIC_RPC,
        ws_url=_str("WS_URL"),
        db_path=BASE_DIR / (_str("DB_PATH") or "data/bot.db"),
        free_lookups_per_day=_int("FREE_LOOKUPS_PER_DAY", 5),
        free_watches=_int("FREE_WATCHES", 1),
        pro_lookups_per_day=_int("PRO_LOOKUPS_PER_DAY", 300),
        pro_watches=_int("PRO_WATCHES", 20),
        pro_price_stars=_int("PRO_PRICE_STARS", 250),
        pro_days=_int("PRO_DAYS", 30),
        support_contact=_str("SUPPORT_CONTACT"),
    )
