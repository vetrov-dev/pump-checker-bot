"""Turns the live event streams into database rows and Telegram notifications."""
import asyncio
import html
import logging
import time
from typing import Awaitable, Callable

from rpc import curve_pda
from store import Store

log = logging.getLogger("tracker")

CREATOR_CACHE_SECONDS = 48 * 3600   # dev sells are tracked for tokens launched in the last 48 h
GRAD_VSOL_MIN, GRAD_VSOL_MAX = 114.0, 125.0  # a standard SOL curve is complete at ~115 virtual SOL
WATCH_THROTTLE_SECONDS = 5           # at most one message per watched wallet per user in this window

Notify = Callable[[int, str], Awaitable[None]]


def short(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}"


def links(mint: str, pump: bool = True, pair: str | None = None) -> str:
    if not pair and pump:
        try:
            pair = str(curve_pda(mint))   # Axiom opens a Pump.fun coin by its bonding curve
        except ValueError:
            pair = None
    out = [f'<a href="https://pump.fun/coin/{mint}">pump.fun</a>'] if pump else []
    if pair:
        out.append(f'<a href="https://axiom.trade/meme/{pair}?chain=sol">axiom</a>')
    out += [f'<a href="https://dexscreener.com/solana/{mint}">dexscreener</a>',
            f'<a href="https://gmgn.ai/sol/token/{mint}">gmgn</a>',
            f'<a href="https://solscan.io/token/{mint}">solscan</a>']
    return " · ".join(out)


def dev_summary(rows, now: float) -> dict:
    n = len(rows)
    migrated = sum(1 for r in rows if r["migrated_at"])
    return {"n": n, "migrated": migrated, "rate": migrated / n if n else 0.0,
            "n24": sum(1 for r in rows if now - r["created_at"] < 86400)}


def is_good_dev(s: dict) -> bool:
    """Launch alert rule: the dev has shipped coins that graduated and is not mass-launching."""
    return s["migrated"] >= 1 and s["rate"] >= 0.5 and s["n24"] <= 5


class Tracker:
    def __init__(self, store: Store, notify: Notify | None = None, watch_limits: Callable[[int], int] | None = None):
        self.store = store
        self.notify = notify            # None while seeding from old recordings
        self.watch_limits = watch_limits
        self.creators: dict[str, tuple[str, float, int, float]] = {}  # mint -> creator, dev tokens, standard, t
        self.pending: dict[str, dict] = {}
        self.watchers: dict[str, list[tuple[int, str]]] = {}
        self._throttle: dict[tuple[int, str], tuple[float, int]] = {}
        self._alert_users: list[int] = []
        self._alert_users_at = 0.0
        self.counts = {"create": 0, "trade": 0, "migrate": 0}
        since = time.time() - CREATOR_CACHE_SECONDS
        for r in store.q("SELECT mint, creator, dev_buy_tokens, standard, created_at FROM tokens"
                         " WHERE created_at>=?", (since,)):
            self.creators[r["mint"]] = (r["creator"], r["dev_buy_tokens"], int(r["standard"] != 0), r["created_at"])
        self.reload_watches()

    def reload_watches(self) -> None:
        idx: dict[str, list[tuple[int, str]]] = {}
        per_user: dict[int, int] = {}
        for r in self.store.all_watches():
            uid = r["user_id"]
            per_user[uid] = per_user.get(uid, 0) + 1
            if self.watch_limits and per_user[uid] > self.watch_limits(uid):
                continue  # Pro expired: only the free allowance keeps working
            idx.setdefault(r["wallet"], []).append((uid, r["label"] or short(r["wallet"])))
        self.watchers = idx

    async def on_event(self, ev: dict) -> None:
        ev.setdefault("t", time.time())
        tx = ev.get("txType")
        if tx == "create":
            await self._create(ev)
        elif tx in ("buy", "sell"):
            await self._trade(ev)
        elif tx == "migrate":
            await self._migrate(ev["mint"], ev["t"])

    # ---- launches
    async def _create(self, ev: dict) -> None:
        # Only PumpPortal says reliably whether a curve is mayhem; the chain decoder's flag misfires on new layouts.
        ev.setdefault("standard", None)
        mint, dev = ev["mint"], ev["traderPublicKey"]
        # Graduation by reserves is only meaningful on a normal SOL curve, which starts at ~30 virtual SOL.
        pump = (ev.get("pool") or "pump") == "pump"
        sol_curve = int(pump and ev.get("standard") != 0 and 0 < (ev.get("vSolInBondingCurve") or 0) < 35)
        if not self.store.add_token(ev):
            if ev["standard"] is not None:   # the chain stream was first: keep PumpPortal's curve type
                self.store.db.execute("UPDATE tokens SET standard=? WHERE mint=?", (ev["standard"], mint))
                c = self.creators.get(mint)
                if c and ev["standard"] == 0:
                    self.creators[mint] = (c[0], c[1], 0, c[3])
            return
        self.counts["create"] += 1
        self.creators[mint] = (dev, ev.get("initialBuy") or 0.0, sol_curve, ev["t"])
        if self.notify is None:
            return
        self.store.commit()
        where = "" if pump else " на LetsBonk" if ev.get("pool") == "bonk" else f' ({ev.get("pool")})'
        name = html.escape(f'{ev.get("name", "")} ({ev.get("symbol", "")})' if ev.get("name") else short(mint))
        dev_buy = ev.get("solAmount") or 0.0

        for uid, label in self.watchers.get(dev, []):
            await self.notify(uid, f"🚀 <b>{html.escape(label)}</b> запустил монету {name}{where}\n"
                                   f"Дев купил {dev_buy:.2f} SOL\n<code>{mint}</code>\n{links(mint, pump=pump)}")

        users = self._alerts()
        if not users:
            return
        s = dev_summary(self.store.dev_tokens(dev, before=ev["t"]), ev["t"])
        if not is_good_dev(s):
            return
        text = (f"🧑‍💻 Запуск от дева с историей: {name}\n"
                f"Прошлые монеты: {s['n']}, на PumpSwap вышли {s['migrated']} ({s['rate']:.0%}), "
                f"запусков за 24 ч: {s['n24']}\nДев купил {dev_buy:.2f} SOL\n"
                f"<code>{mint}</code>\n{links(mint, pump=pump)}\n\n"
                "<i>Это не сигнал на покупку: история короткая, покупка на старте в среднем убыточна. "
                "Проверьте монету: /check " + mint + "</i>")
        for uid in users:
            await self.notify(uid, text)

    def _alerts(self) -> list[int]:
        if time.time() - self._alert_users_at > 60:
            self._alert_users = self.store.alert_users()
            self._alert_users_at = time.time()
        return self._alert_users

    # ---- trades
    async def _trade(self, ev: dict) -> None:
        self.counts["trade"] += 1
        mint, trader, t = ev["mint"], ev["traderPublicKey"], ev["t"]
        p = self.pending.get(mint)
        if p is None:
            p = self.pending[mint] = {"buys": 0, "sells": 0, "vol": 0.0, "peak": 0.0, "last": 0.0, "t": t,
                                      "dev_tok": 0.0, "dev_sol": 0.0, "dev_first": None}
        is_buy = ev["txType"] == "buy"
        p["buys" if is_buy else "sells"] += 1
        p["vol"] += ev["solAmount"]
        p["last"] = ev["marketCapSol"]
        p["peak"] = max(p["peak"], ev["marketCapSol"])
        p["t"] = t

        known = self.creators.get(mint)
        if known and not is_buy and trader == known[0]:
            p["dev_tok"] += ev["tokenAmount"]
            p["dev_sol"] += ev["solAmount"]
            p["dev_first"] = p["dev_first"] or t
        if known and known[2] and GRAD_VSOL_MIN <= ev["vSolInBondingCurve"] <= GRAD_VSOL_MAX:
            await self._migrate(mint, t)

        if self.notify is not None and trader in self.watchers:
            await self._notify_trade(ev)

    async def _notify_trade(self, ev: dict) -> None:
        mint, trader = ev["mint"], ev["traderPublicKey"]
        row = self.store.token(mint)
        sym = html.escape(row["symbol"] if row and row["symbol"] else short(mint))
        verb = "🟢 купил" if ev["txType"] == "buy" else "🔴 продал"
        now = time.time()
        for uid, label in self.watchers[trader]:
            last, skipped = self._throttle.get((uid, trader), (0.0, 0))
            if now - last < WATCH_THROTTLE_SECONDS:
                self._throttle[(uid, trader)] = (last, skipped + 1)
                continue
            self._throttle[(uid, trader)] = (now, 0)
            extra = f"\n(+{skipped} сделок за последние секунды не показаны)" if skipped else ""
            await self.notify(uid, f"👀 <b>{html.escape(label)}</b> {verb} {ev['solAmount']:.3f} SOL · {sym}\n"
                                   f"Капитализация {ev['marketCapSol']:.0f} SOL\n<code>{mint}</code>\n"
                                   f"{links(mint)}{extra}")

    # ---- migrations
    async def _migrate(self, mint: str, t: float) -> None:
        cur = self.store.db.execute("UPDATE tokens SET migrated_at=? WHERE mint=? AND migrated_at IS NULL",
                                    (t, mint))
        if not cur.rowcount:
            return
        self.counts["migrate"] += 1
        if self.notify is None:
            return
        self.store.commit()
        row = self.store.token(mint)
        if not row:
            return
        sym = html.escape(f'{row["name"]} ({row["symbol"]})')
        for uid, label in self.watchers.get(row["creator"], []):
            await self.notify(uid, f"🎓 Монета {sym} от <b>{html.escape(label)}</b> вышла на PumpSwap\n"
                                   f"<code>{mint}</code>\n{links(mint, pump=row['pool'] in (None, 'pump'))}")

    # ---- persistence
    def flush(self) -> None:
        if not self.pending:
            return
        rows = [(p["buys"], p["sells"], p["vol"], p["peak"], p["last"], p["t"],
                 p["dev_tok"], p["dev_sol"], p["dev_first"], mint) for mint, p in self.pending.items()]
        self.pending = {}
        self.store.db.executemany(
            "UPDATE tokens SET buys=buys+?, sells=sells+?, vol_sol=vol_sol+?, peak_mcap=MAX(peak_mcap, ?),"
            " last_mcap=?, last_trade_at=?, dev_sold_tokens=dev_sold_tokens+?, dev_sold_sol=dev_sold_sol+?,"
            " dev_first_sell_at=COALESCE(dev_first_sell_at, ?) WHERE mint=?", rows)
        self.store.commit()

    def prune(self, now: float) -> None:
        cut = now - CREATOR_CACHE_SECONDS
        self.creators = {m: c for m, c in self.creators.items() if c[3] >= cut}
        self._throttle = {k: v for k, v in self._throttle.items() if now - v[0] < 600}

    async def flush_loop(self) -> None:
        last_prune = last_log = time.time()
        while True:
            await asyncio.sleep(5)
            try:
                self.flush()
            except Exception:
                log.exception("Flush failed")
            now = time.time()
            if now - last_prune > 3600:
                self.prune(now)
                last_prune = now
            if now - last_log > 600:
                log.info("Last 10 min: %d launches, %d trades, %d migrations",
                         self.counts["create"], self.counts["trade"], self.counts["migrate"])
                self.counts = dict.fromkeys(self.counts, 0)
                last_log = now
