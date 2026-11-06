# Pump.fun Token Checker Bot

A Telegram bot that checks a Pump.fun token before you buy it. It reads on-chain data from Solana and returns a
**trust score from 0 to 100** with a clear breakdown of every risk it found.

The bot never trades, never holds funds and never asks for a private key.

[Русская версия](README.ru.md)

## Features

| Command | What it does |
|---|---|
| Token address or link, `/check` | Platform and status (bonding curve or PumpSwap), market cap and liquidity, mint / freeze authority, top holders, dev share, bundle in the launch block and snipers in the next two blocks, whether early wallets still hold, dev history, socials, paid DexScreener promotion. Ends with a **trust score** and the reasons points were taken off |
| Wallet address, `/dev` | How many tokens the wallet launched, how many reached PumpSwap, fast dumps, median peak, last 10 launches |
| `/watch <wallet> [label]` | Direct-message alerts on the wallet's launches, buys and sells, and when its tokens migrate to PumpSwap |
| `/list`, `/unwatch` | Manage watched wallets |
| `/alerts` (Pro) | Alert when a dev with a good track record launches a new token |
| `/pro` | Subscription paid in Telegram Stars |
| `/paysupport`, `/terms` | Payment support and terms, required by Telegram for bots that accept Stars |
| `/stats`, `/grant`, `/refund` | Admin only |

Links are accepted from pump.fun, Axiom, GMGN, Photon, BullX, DexScreener and Solscan.

## Trust score

Starts at 100. Each risk subtracts points; a good dev history adds 10.

| Risk | Points |
|---|---|
| Creator can mint new tokens / freeze wallets | −40 / −40 |
| Bundle in the launch block ≥40% / ≥15% / ≥5% | −40 / −25 / −10 |
| Top 10 holders ≥50% / ≥30% / ≥20% | −30 / −15 / −5 |
| Dev holds ≥10% / ≥5% | −25 / −10 |
| Dev dumped within 10 minutes in half of their tokens | −25 |
| Liquidity under $5K / $20K (after the curve) | −15 / −5 |

Levels: 80–100 high, 60–79 medium, 40–59 low, 0–39 very low.

## Architecture

- **PumpPortal feed:** every launch and migration, used to build the dev database.
- **Solana WebSocket:** live trades for wallet watching, dev sells and peaks.
- **Solana RPC:** bonding curve state, holders, first blocks of a token.
- **DexScreener:** market cap, liquidity, socials, paid profiles and boosts.
- **SQLite** (`aiosqlite`) for users, subscriptions, watches and launch history.
- **Telegram Stars** payments with refunds and admin grants.

Fully async Python: `aiohttp`, `websockets`, `solders`.

## Run

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
cp .env.example .env                          # set BOT_TOKEN, ADMIN_IDS, RPC_URL, WS_URL
python bot.py
```

Check a token from the command line without Telegram:

```bash
python bot.py check <token address>
```

A systemd unit for a Linux VPS is in `deploy/pump-alerts.service`.

## Limitations

- Dev history only covers tokens the bot has seen since it started collecting data.
- A public WebSocket drops some trades; use your own RPC provider for full coverage.
- Good-dev alerts are information, not a buy signal.
