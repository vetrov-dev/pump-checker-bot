"""PumpPortal's free data stream: every Pump.fun launch and every migration to PumpSwap.

Token and account trade streams there are paid (0.01 SOL per 10k messages), so trades come from Solana RPC instead.
"""
import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

log = logging.getLogger("pumpportal")

URL = "wss://pumpportal.fun/api/data"


class PumpPortalFeed:
    def __init__(self, on_event: Callable[[dict], Awaitable[None]]):
        self.on_event = on_event
        self.last_event_at = 0.0

    async def run(self) -> None:
        backoff = 5
        while True:
            try:
                # One connection only: PumpPortal times out clients that open many.
                async with connect(URL, ping_interval=20, open_timeout=15, max_size=2**22) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    log.info("Connected to PumpPortal: launches + migrations")
                    backoff = 5
                    async for raw in ws:
                        await self._handle(raw)
            except (ConnectionClosed, InvalidHandshake, InvalidStatus, OSError, TimeoutError) as e:
                log.warning("PumpPortal feed dropped (%s), reconnecting in %d s", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _handle(self, raw) -> None:
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if not isinstance(d, dict) or not d.get("mint"):
            return  # subscription acks
        tx = d.get("txType")
        if tx == "create":
            d["pool"] = d.get("pool") or "pump"   # "bonk" = LetsBonk launch
            d["standard"] = (0 if d.get("is_mayhem_mode") else 1) if d["pool"] == "pump" else None
        elif tx in ("migrate", "migration") or (tx is None and d.get("pool")):
            d["txType"] = "migrate"
        else:
            return
        d["t"] = time.time()
        self.last_event_at = d["t"]
        try:
            await self.on_event(d)
        except Exception:
            log.exception("Event handler failed")
