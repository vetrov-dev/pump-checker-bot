"""Real-time Pump.fun events read straight from Solana: logsSubscribe on the Pump.fun program via your RPC.

Events are emitted in the same dict shape the rest of the bot uses (PumpPortal-style keys).
"""
import asyncio
import base64
import hashlib
import json
import logging
import struct
import time
from collections import deque
from typing import Awaitable, Callable, Iterable

from solders.pubkey import Pubkey
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

log = logging.getLogger("feed")

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN_SUPPLY = 1_000_000_000  # every Pump.fun token has 1B supply
LAMPORTS = 1e9
TOKEN_UNITS = 1e6             # Pump.fun tokens have 6 decimals


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


CREATE_EVENT = _disc("CreateEvent")
TRADE_EVENT = _disc("TradeEvent")


def _pk(b: bytes, o: int) -> str:
    return str(Pubkey.from_bytes(b[o:o + 32]))


def _mcap(v_sol: float, v_tok: float) -> float:
    return v_sol / v_tok * TOKEN_SUPPLY if v_tok else 0.0


def parse_create(b: bytes) -> dict:
    o, strings = 8, []
    for _ in range(3):  # name, symbol, uri
        n = struct.unpack_from("<I", b, o)[0]
        strings.append(b[o + 4:o + 4 + n].decode(errors="replace"))
        o += 4 + n
    _ts, v_tok, v_sol = struct.unpack_from("<qQQ", b, o + 128)
    v_sol, v_tok = v_sol / LAMPORTS, v_tok / TOKEN_UNITS
    # After the supply fields: token program (32), mayhem-mode flag (1), 1 byte, quote mint (32).
    # Mayhem tokens and curves quoted in another mint do not follow the standard SOL x*y=k curve.
    tail = b[o + 128:]
    mayhem = len(tail) > 72 and tail[72] == 1
    other_quote = len(tail) >= 106 and any(tail[74:106])
    return {
        "txType": "create", "name": strings[0], "symbol": strings[1], "uri": strings[2],
        "mint": _pk(b, o), "traderPublicKey": _pk(b, o + 96),  # creator
        "standardCurve": not (mayhem or other_quote),
        "tokenProgram": str(Pubkey.from_bytes(tail[40:72])) if len(tail) >= 72 else "",
        "solAmount": 0.0, "initialBuy": 0.0,
        "vSolInBondingCurve": v_sol, "vTokensInBondingCurve": v_tok, "marketCapSol": _mcap(v_sol, v_tok),
    }


def parse_trade(b: bytes) -> dict:
    o = 8
    sol, tok = struct.unpack_from("<QQ", b, o + 32)
    _ts, v_sol, v_tok = struct.unpack_from("<qQQ", b, o + 81)
    v_sol, v_tok = v_sol / LAMPORTS, v_tok / TOKEN_UNITS
    fee_bps = trade_fee_bps(b)
    return {
        "txType": "buy" if b[o + 48] else "sell", "mint": _pk(b, o), "traderPublicKey": _pk(b, o + 49),
        "solAmount": sol / LAMPORTS, "tokenAmount": tok / TOKEN_UNITS,
        "vSolInBondingCurve": v_sol, "vTokensInBondingCurve": v_tok, "marketCapSol": _mcap(v_sol, v_tok),
        "rawTokens": tok, "feeBps": fee_bps,
    }


def trade_fee_bps(b: bytes) -> int | None:
    """Protocol + creator fee of this trade in basis points (the buyback fee is a share of the protocol fee)."""
    try:
        fee_bps = struct.unpack_from("<Q", b, 8 + 153)[0]
        creator_bps = struct.unpack_from("<Q", b, 8 + 201)[0]
    except struct.error:
        return None
    return fee_bps + creator_bps


def pump_events(logs: list[str]) -> list[bytes]:
    """Event payloads emitted by the Pump.fun program itself (not by routers or other programs in the tx)."""
    stack, out = [], []
    for line in logs:
        parts = line.split(" ", 3)
        if len(parts) >= 3 and parts[0] == "Program" and parts[2] == "invoke":
            stack.append(parts[1])
        elif len(parts) >= 3 and parts[0] == "Program" and parts[2] in ("success", "failed"):
            if stack:
                stack.pop()
        elif line.startswith("Program data: ") and stack and stack[-1] == PUMP_PROGRAM:
            try:
                out.append(base64.b64decode(line[14:]))
            except ValueError:
                pass
    return out


def decode_tx(logs: list[str], signature: str) -> list[dict]:
    creates, trades = [], []
    for b in pump_events(logs):
        try:
            if b[:8] == CREATE_EVENT:
                creates.append(parse_create(b))
            elif b[:8] == TRADE_EVENT:
                trades.append(parse_trade(b))
        except (struct.error, ValueError):
            continue
    # Fold the creator's first buy into the create event, like PumpPortal does.
    for c in creates:
        for t in trades:
            if t["mint"] == c["mint"] and t["traderPublicKey"] == c["traderPublicKey"] and t["txType"] == "buy":
                c.update(solAmount=t["solAmount"], initialBuy=t["tokenAmount"],
                         vSolInBondingCurve=t["vSolInBondingCurve"],
                         vTokensInBondingCurve=t["vTokensInBondingCurve"], marketCapSol=t["marketCapSol"])
                trades.remove(t)
                break
    # Curves quoted in something other than SOL report zero SOL reserves; skip them.
    trades = [t for t in trades if t["vSolInBondingCurve"] > 0]
    events = creates + trades
    for e in events:
        e["signature"] = signature
    return events


PUBLIC_WS = "wss://api.mainnet-beta.solana.com"   # fallback: drops every ~30 s, but free and unmetered
RETRY_PRIMARY_SECONDS = 1800


class PumpFeed:
    def __init__(self, ws_url: str, on_event: Callable[[dict], Awaitable[None]]):
        self.ws_url = ws_url
        self.on_event = on_event
        self.last_trade_at = 0.0
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()

    @property
    def healthy(self) -> bool:
        return time.time() - self.last_trade_at < 15

    async def run(self) -> None:
        """Stream Pump.fun logs from WS_URL; if it keeps refusing (e.g. HTTP 429 on a free Helius plan),
        use the public Solana endpoint and try WS_URL again every 30 minutes."""
        sub = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
               "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "processed"}]}
        url, failures, fell_back_at = self.ws_url, 0, 0.0
        while True:
            if url != self.ws_url and time.time() - fell_back_at > RETRY_PRIMARY_SECONDS:
                url, failures = self.ws_url, 0
            name = "public Solana RPC" if url == PUBLIC_WS else "Solana RPC"
            try:
                async with connect(url, ping_interval=20, max_size=2**24, open_timeout=15) as ws:
                    await ws.send(json.dumps(sub))
                    log.info("Connected to %s, listening to Pump.fun program", name)
                    failures = 0
                    async for raw in ws:
                        await self._handle(raw)
            except ConnectionClosed as e:
                log.warning("Feed disconnected (%s), reconnecting...", e)
                await asyncio.sleep(1)
            except (InvalidHandshake, InvalidStatus, OSError, TimeoutError) as e:
                failures += 1
                if url == self.ws_url and url != PUBLIC_WS and failures >= 3:
                    log.warning("Feed: %s keeps refusing (%s), switching to the public endpoint for 30 min",
                                name, e)
                    url, failures, fell_back_at = PUBLIC_WS, 0, time.time()
                    continue
                wait = min(5 * 2 ** (failures - 1), 120)
                log.warning("Feed cannot connect to %s (%s), retrying in %d s", name, e, wait)
                await asyncio.sleep(wait)

    async def _handle(self, raw) -> None:
        try:
            value = json.loads(raw)["params"]["result"]["value"]
        except (ValueError, KeyError, TypeError):
            return  # subscription ack or non-notification
        sig = value.get("signature", "")
        if value.get("err") or sig in self._seen:
            return
        self._seen.add(sig)
        self._seen_order.append(sig)
        if len(self._seen_order) > 20000:
            self._seen.discard(self._seen_order.popleft())

        for ev in decode_tx(value.get("logs") or [], sig):
            if ev["txType"] != "create":
                self.last_trade_at = time.time()
            try:
                await self.on_event(ev)
            except Exception:
                log.exception("Event handler failed")

    # The chain feed already delivers every Pump.fun trade; these keep the bot's interface unchanged.
    async def subscribe(self, mints: Iterable[str]) -> None:
        pass

    async def unsubscribe(self, mints: Iterable[str]) -> None:
        pass
