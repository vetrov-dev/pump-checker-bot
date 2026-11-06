"""Minimal async Solana JSON-RPC client: the configured provider first, the public endpoint as a fallback."""
import asyncio
import base64
import re
import struct

import aiohttp
from solders.pubkey import Pubkey

from config import PUBLIC_RPC

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
TOKEN_SUPPLY = 1_000_000_000
INITIAL_REAL_TOKENS = 793_100_000   # tokens a standard curve sells before it completes
TIMEOUT = aiohttp.ClientTimeout(total=20)


def mask(text: str) -> str:
    """Hide API keys that providers put in the URL, before anything reaches a log or a user."""
    return re.sub(r"(api[-_]?key=)[^&\s'\"]+", r"\1***", str(text), flags=re.I)


class RpcError(Exception):
    pass


def curve_pda(mint: str) -> Pubkey:
    return Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))], PUMP_PROGRAM)[0]


def is_pda(addr: str) -> bool:
    """Off-curve addresses belong to programs (bonding curve, AMM pool, lockers), not to people."""
    try:
        return not Pubkey.from_string(addr).is_on_curve()
    except ValueError:
        return False


class Rpc:
    def __init__(self, http: aiohttp.ClientSession, url: str):
        self.http = http
        self.urls = [url] + ([PUBLIC_RPC] if url != PUBLIC_RPC else [])
        self.sem = asyncio.Semaphore(4)

    async def call(self, method: str, params: list):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last: Exception = RpcError("no RPC configured")
        for url in self.urls:
            for attempt in range(3):
                wait = 1.0 + attempt * 2
                async with self.sem:
                    try:
                        async with self.http.post(url, json=body, timeout=TIMEOUT) as r:
                            text = await r.text()
                            if r.status == 429:
                                last = RpcError(f"{method}: rate limited")
                                if "max usage" in text:
                                    break  # plan quota is gone for the month: go straight to the fallback
                                wait = float(r.headers.get("Retry-After") or wait)
                            elif r.status >= 400:
                                last = RpcError(f"{method}: HTTP {r.status}")
                                break
                            else:
                                d = await r.json(content_type=None)
                                err = d.get("error")
                                if not err:
                                    return d.get("result")
                                if err.get("code") in (429, -32429, -32005):
                                    last = RpcError(f"{method}: rate limited")
                                else:
                                    raise RpcError(f"{method}: {mask(err.get('message'))}")
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                        last = RpcError(f"{method}: {mask(type(e).__name__)}")
                await asyncio.sleep(min(wait, 10))
        raise last

    # ---- helpers
    async def account(self, addr: str, encoding: str = "base64") -> dict | None:
        res = await self.call("getAccountInfo", [addr, {"encoding": encoding, "commitment": "confirmed"}])
        return res and res.get("value")

    async def curve(self, mint: str) -> dict | None:
        """Bonding-curve state of a Pump.fun token, or None if the mint has no curve."""
        pda = curve_pda(mint)
        acc = await self.account(str(pda))
        if not acc:
            return None
        b = base64.b64decode(acc["data"][0])
        if len(b) < 81:
            return None
        v_tok, v_sol, r_tok, r_sol, _supply = struct.unpack_from("<QQQQQ", b, 8)
        v_tok, v_sol, r_tok, r_sol = v_tok / 1e6, v_sol / 1e9, r_tok / 1e6, r_sol / 1e9
        return {"pda": str(pda), "v_sol": v_sol, "v_tok": v_tok, "real_sol": r_sol,
                "complete": bool(b[48]), "creator": str(Pubkey.from_bytes(b[49:81])),
                "mayhem": len(b) > 81 and b[81] == 1,
                "mcap_sol": v_sol / v_tok * TOKEN_SUPPLY if v_tok else 0.0,
                "progress": 1.0 if b[48] else max(0.0, min(1.0, 1 - r_tok / INITIAL_REAL_TOKENS))}

    async def mint_info(self, mint: str) -> dict | None:
        """Supply and the creator's remaining powers: minting more tokens, freezing holders' accounts."""
        acc = await self.account(mint, "jsonParsed")
        parsed = ((acc or {}).get("data") or {}).get("parsed") if isinstance((acc or {}).get("data"), dict) else None
        if not parsed or parsed.get("type") != "mint":
            return None
        i = parsed["info"]
        return {"supply": int(i["supply"]) / 10 ** i["decimals"], "decimals": i["decimals"],
                "mint_authority": i.get("mintAuthority"), "freeze_authority": i.get("freezeAuthority")}

    async def holders(self, mint: str, supply: float = TOKEN_SUPPLY) -> list[dict]:
        """Top 20 token accounts with their owners; pda=True for program-owned ones (curve, pool)."""
        res = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        vals = res["value"]
        accs = await self.call("getMultipleAccounts", [[v["address"] for v in vals], {"encoding": "jsonParsed"}])
        out = []
        for v, a in zip(vals, accs["value"]):
            try:
                owner = a["data"]["parsed"]["info"]["owner"]
            except (TypeError, KeyError):
                owner = v["address"]
            amount = float(v.get("uiAmount") or 0)
            out.append({"owner": owner, "amount": amount, "pct": amount / (supply or TOKEN_SUPPLY) * 100,
                        "pda": is_pda(owner)})
        return out

    async def signatures(self, addr: str, before: str | None = None, limit: int = 1000) -> list[dict]:
        opts = {"limit": limit, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        return await self.call("getSignaturesForAddress", [addr, opts]) or []

    async def tx(self, sig: str) -> dict | None:
        return await self.call("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                        "maxSupportedTransactionVersion": 0}])

    async def balance(self, addr: str) -> float:
        res = await self.call("getBalance", [addr, {"commitment": "confirmed"}])
        return (res or {}).get("value", 0) / 1e9
