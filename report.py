"""On-demand reports: /check for a token, /dev for a creator wallet. Output is Telegram HTML."""
import asyncio
import html
import ipaddress
import json
import logging
import re
import time
from urllib.parse import urlparse

import aiohttp
from solders.pubkey import Pubkey

from feed import decode_tx
from rpc import TOKEN_SUPPLY, Rpc
from store import Store
from tracker import dev_summary, is_good_dev, links, short

log = logging.getLogger("report")

DEX = "https://api.dexscreener.com"
TIMEOUT = aiohttp.ClientTimeout(total=8)
MAX_SIG_PAGES = 3        # the bundle scan needs the curve's first transaction: give up beyond 3000 signatures
MAX_EARLY_TXS = 40
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
SYSTEM_PROGRAM = "11111111111111111111111111111111"
B58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
DISCLAIMER = "<i>Не финансовый совет. Бот показывает данные из блокчейна; решение и риск — ваши.</i>"


def find_address(text: str) -> str | None:
    for m in B58.findall(text or ""):
        try:
            Pubkey.from_string(m)
            return m
        except ValueError:
            continue
    return None


def ago(t: float | None, now: float | None = None) -> str:
    if not t:
        return "?"
    s = max(0, (now or time.time()) - t)
    if s < 120:
        return f"{s:.0f} с"
    if s < 7200:
        return f"{s / 60:.0f} мин"
    if s < 172800:
        return f"{s / 3600:.0f} ч"
    return f"{s / 86400:.0f} дн"


def usd(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    return f"${x / 1e6:.2f}M" if x >= 1e6 else f"${x / 1e3:.1f}K" if x >= 1e3 else f"${x:.0f}"


def social_link(kind: str, url: str) -> str:
    """A clickable social link that shows its real domain: the creator chooses these URLs, they can be phishing."""
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    host = (urlparse(url).hostname or "").removeprefix("www.")
    if not host or not re.fullmatch(r"[a-z0-9.-]+", host, re.I):
        return f"{html.escape(kind)}: {html.escape(url[:60])}"
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(kind)}</a> ({html.escape(host)})'


def is_quick_dump(r) -> bool:
    """Dev sold at least 90% of the launch buy within 10 minutes and the coin never graduated."""
    return bool(r["dev_buy_tokens"] and r["dev_first_sell_at"] and not r["migrated_at"]
                and r["dev_first_sell_at"] - r["created_at"] < 600
                and r["dev_sold_tokens"] >= 0.9 * r["dev_buy_tokens"])


DEX_NAMES = {"raydium": "Raydium", "meteora": "Meteora", "orca": "Orca"}
LAUNCHPADS = {"moonshot": "Moonshot", "boop": "boop.fun", "launchlab": "LetsBonk (Raydium LaunchLab)",
              "pumpfun": "Pump.fun", "pumpswap": "Pump.fun → PumpSwap"}


def platform_name(mint: str, curve: dict | None, dex: dict | None, pool: str | None = None) -> str:
    """Where the coin was launched and trades, as far as the chain and DexScreener tell."""
    if curve:
        return "Pump.fun → PumpSwap" if curve["complete"] else "Pump.fun"
    dex_id = (dex or {}).get("dexId", "")
    if (pool == "bonk" or mint.lower().endswith("bonk")) and dex_id != "launchlab":
        return f"LetsBonk (bonk.fun), торгуется на {DEX_NAMES.get(dex_id, dex_id or '?')}"
    if dex_id == "meteora" and "DBC" in ((dex or {}).get("labels") or []):
        return "Meteora DBC (лаунчпады Believe, Bags и др.)"
    if dex_id in LAUNCHPADS:
        return LAUNCHPADS[dex_id]
    if dex_id in DEX_NAMES:
        return f"лаунчпад не определён, торгуется на {DEX_NAMES[dex_id]}"
    return f"лаунчпад не определён, торгуется на {dex_id}" if dex_id else "не определена"


class Trust:
    """0-100 trust score: starts at 100, each risk takes points away, a good dev history adds some back."""

    LEVELS = ((80, "🟢", "высокий"), (60, "🟡", "средний"), (40, "🟠", "низкий"), (0, "🔴", "очень низкий"))

    def __init__(self):
        self.items: list[tuple[int, str]] = []

    def risk(self, points: int, text: str) -> None:
        self.items.append((-points, text))

    def bonus(self, points: int, text: str) -> None:
        self.items.append((points, text))

    def score(self, cap: int = 100) -> int:
        return max(0, min(cap, 100 + sum(p for p, _ in self.items)))

    def header(self, cap: int = 100) -> list[str]:
        s = self.score(cap)
        icon, level = next((i, lv) for floor, i, lv in self.LEVELS if s >= floor)
        filled = round(s / 10)
        return [f"{icon} <b>ТРАСТ {s}/100 · {level}</b>", "▰" * filled + "▱" * (10 - filled)]

    def reasons(self) -> list[str]:
        out = []
        for p, text in sorted(self.items):
            mark = "🔴" if p <= -20 else "⚠️" if p < 0 else "✅"
            out.append(f"{mark} <b>{p:+d}</b> {text}")
        return out or ["✅ явных рисков не нашёл (это не гарантия)"]


class Reports:
    def __init__(self, rpc: Rpc, http: aiohttp.ClientSession, store: Store, holders_ok: bool, full_trades: bool):
        self.rpc = rpc
        self.http = http
        self.store = store
        self.holders_ok = holders_ok        # False when only the public RPC is configured
        self.full_trades = full_trades      # False on the public websocket, which misses many trades

    @staticmethod
    async def _safe(coro):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001 - a missing piece must not break the whole report
            log.debug("lookup failed: %s", e)
            return None

    async def _get_json(self, url: str, limit: int = 2_000_000):
        async with self.http.get(url, timeout=TIMEOUT, allow_redirects=False) as r:
            if r.status != 200:
                return None
            return json.loads(await r.content.read(limit))

    # ---- what is this address?
    async def classify(self, addr: str) -> tuple[str, str]:
        """('token', mint) or ('wallet', address)."""
        acc = await self._safe(self.rpc.account(addr, "jsonParsed"))
        if not acc or acc.get("owner") == SYSTEM_PROGRAM:
            return "wallet", addr
        if acc.get("owner") in TOKEN_PROGRAMS:
            parsed = (acc.get("data") or {}).get("parsed") or {}
            if parsed.get("type") == "account":
                return "token", parsed["info"]["mint"]
            return "token", addr
        pair = await self._safe(self._get_json(f"{DEX}/latest/dex/pairs/solana/{addr}"))
        for p in (pair or {}).get("pairs") or []:
            base = (p.get("baseToken") or {}).get("address")
            if base:
                return "token", base
        return "wallet", addr

    # ---- outside data
    async def _dex(self, mint: str) -> dict | None:
        pairs = await self._safe(self._get_json(f"{DEX}/tokens/v1/solana/{mint}"))
        if not pairs:
            return None
        return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)

    async def _orders(self, mint: str) -> list[str]:
        data = await self._safe(self._get_json(f"{DEX}/orders/v1/solana/{mint}"))
        if isinstance(data, list):
            data = {"orders": data}
        out = [o.get("type", "") for o in (data or {}).get("orders") or [] if o.get("status") == "approved"]
        if (data or {}).get("boosts"):
            out.append("boosts")
        return out

    async def _metadata(self, uri: str) -> dict | None:
        """The creator's metadata JSON (socials). Only plain https hosts: the URI is chosen by whoever launched."""
        u = urlparse(uri or "")
        if u.scheme != "https" or not u.hostname or u.hostname == "localhost" or u.hostname.endswith(".local"):
            return None
        try:
            ipaddress.ip_address(u.hostname)
            return None
        except ValueError:
            pass
        data = await self._safe(self._get_json(uri, limit=65536))
        return data if isinstance(data, dict) else None

    # ---- launch block analysis
    async def bundle(self, mint: str, pda: str, creator: str | None) -> dict | None:
        """Who bought in the launch block (bundle) and the next two blocks (snipers), excluding the dev."""
        r = self.store.one("SELECT * FROM bundles WHERE mint=?", (mint,))
        if r:
            return {**dict(r), "wallets": set(filter(None, r["wallets"].split(",")))}
        sigs, before = [], None
        for _ in range(MAX_SIG_PAGES):
            page = await self.rpc.signatures(pda, before)
            sigs += page
            if len(page) < 1000:
                break
            before = page[-1]["signature"]
        else:
            return None  # too many trades to reach the first one cheaply
        ok = [s for s in sigs if not s.get("err")]
        if not ok:
            return None
        first = min(s["slot"] for s in ok)
        early = sorted((s for s in ok if s["slot"] <= first + 2), key=lambda s: s["slot"])[:MAX_EARLY_TXS]
        txs = await asyncio.gather(*(self._safe(self.rpc.tx(s["signature"])) for s in early))
        b_tok = b_sol = s_tok = 0.0
        wallets: set[str] = set()
        for s, tx in zip(early, txs):
            if not tx or not tx.get("meta"):
                continue
            for ev in decode_tx(tx["meta"].get("logMessages") or [], s["signature"]):
                if ev["txType"] == "create" and not creator:
                    creator = ev["traderPublicKey"]
                if ev["txType"] != "buy" or ev["mint"] != mint or ev["traderPublicKey"] == creator:
                    continue
                wallets.add(ev["traderPublicKey"])
                if s["slot"] == first:
                    b_tok += ev["tokenAmount"]
                    b_sol += ev["solAmount"]
                else:
                    s_tok += ev["tokenAmount"]
        res = {"mint": mint, "checked_at": time.time(), "create_slot": first, "wallets": wallets,
               "bundle_tokens": b_tok, "bundle_sol": b_sol, "sniper_tokens": s_tok}
        self.store.db.execute("INSERT OR REPLACE INTO bundles VALUES(?,?,?,?,?,?,?)",
                              (mint, res["checked_at"], first, ",".join(wallets), b_tok, b_sol, s_tok))
        self.store.commit()
        return res

    # ---- /check
    async def check(self, mint: str) -> str:
        now = time.time()
        row = self.store.token(mint)
        curve, info, dex, orders = await asyncio.gather(
            self._safe(self.rpc.curve(mint)), self._safe(self.rpc.mint_info(mint)),
            self._dex(mint), self._orders(mint))
        if not row and not curve and not dex and not info:
            return "Не нашёл такую монету ни в блокчейне, ни в DexScreener. Проверьте адрес."
        supply = (info or {}).get("supply") or TOKEN_SUPPLY
        holders = await self._safe(self.rpc.holders(mint, supply)) if self.holders_ok else None
        creator = (row["creator"] if row else None) or (curve["creator"] if curve else None)
        meta = await self._metadata(row["uri"]) if row and row["uri"] else None
        bund = await self._safe(self.bundle(mint, curve["pda"], creator)) if curve else None

        base = (dex or {}).get("baseToken") or {}
        title = " ".join(x for x in (
            (row["name"] if row else "") or base.get("name", ""),
            f'({(row["symbol"] if row else "") or base.get("symbol", "")})') if x.strip("()"))
        created = row["created_at"] if row else ((dex.get("pairCreatedAt") or 0) / 1000 if dex else None)
        t = Trust()
        notes: list[str] = []
        if created and now - created < 300:
            t.risk(5, "монете меньше 5 минут: картина ещё меняется")

        # market
        market = []
        if curve:
            market.append("Вышла на PumpSwap" if curve["complete"] else
                          f'На кривой Pump.fun: {curve["progress"]:.0%} до выхода на PumpSwap'
                          + ("" if dex else f', капитализация {curve["mcap_sol"]:.0f} SOL'))
        if dex:
            liq = (dex.get("liquidity") or {}).get("usd")
            on_curve = curve and not curve["complete"]   # on the curve, the curve itself is the liquidity
            market.append(f'Капитализация <b>{usd(dex.get("marketCap") or dex.get("fdv"))}</b>'
                          + ("" if on_curve else f" · ликвидность {usd(liq)}")
                          + f' · объём 24 ч {usd((dex.get("volume") or {}).get("h24"))}')
            if not on_curve and liq is not None:
                if liq < 5000:
                    t.risk(15, f"мало ликвидности ({usd(liq)}): крупную продажу пул не выдержит")
                elif liq < 20000:
                    t.risk(5, f"небольшая ликвидность ({usd(liq)})")
        if (curve and curve["mayhem"]) or (row and row["standard"] == 0):
            t.risk(5, "режим Mayhem: кривая нестандартная, цифры по ней могут быть неточны")

        # what the creator can still do with the token itself
        contract = []
        if info:
            contract.append("Выпуск новых монет: " + ("🔴 включён" if info["mint_authority"] else "✅ отключён"))
            contract.append("Заморозка кошельков: " + ("🔴 включена" if info["freeze_authority"] else "✅ отключена"))
            if info["mint_authority"]:
                t.risk(40, "создатель может выпускать новые монеты")
            if info["freeze_authority"]:
                t.risk(40, "создатель может заморозить монеты в вашем кошельке")
        else:
            contract.append("Не удалось прочитать контракт монеты")

        # holders
        dev_pct = None
        if holders:
            people = [h for h in holders if not h["pda"]]
            top10 = sum(h["pct"] for h in people[:10])
            biggest = people[0]["pct"] if people else 0.0
            hold_lines = [f"Топ-10 держат <b>{top10:.1f}%</b> · крупнейший {biggest:.1f}%",
                          "<i>без кривой Pump.fun и пулов</i>"]
            if top10 >= 50:
                t.risk(30, f"топ-10 холдеров держат {top10:.0f}%")
            elif top10 >= 30:
                t.risk(15, f"топ-10 холдеров держат {top10:.0f}%")
            elif top10 >= 20:
                t.risk(5, f"топ-10 холдеров держат {top10:.0f}%")
            if biggest >= 10:
                t.risk(10, f"один кошелёк держит {biggest:.0f}%")
            dev_pct = next((h["pct"] for h in people if h["owner"] == creator), None)
        else:
            hold_lines = ["Нет данных: публичный RPC их не отдаёт, нужен RPC_URL с ключом" if not self.holders_ok
                          else "RPC не ответил, попробуйте позже"]
            t.risk(15, "нет данных о холдерах, оценка неполная")

        # dev
        dev_lines = []
        if creator:
            hold = (f"держит {dev_pct:.1f}%" if dev_pct is not None else
                    "не в топ-20 холдеров" if holders else "доля неизвестна")
            if row and row["dev_buy_tokens"] and row["dev_sold_tokens"]:
                sold = min(100.0, row["dev_sold_tokens"] / row["dev_buy_tokens"] * 100)
                hold += f", продал {sold:.0f}% стартовой покупки"
                if sold >= 90 and not (curve and curve["complete"]):
                    t.risk(10, "дев продал почти всю стартовую покупку")
            dev_lines += [f"<code>{creator}</code>", hold.capitalize()]
            if dev_pct is not None and dev_pct >= 10:
                t.risk(25, f"дев держит {dev_pct:.0f}%")
            elif dev_pct is not None and dev_pct >= 5:
                t.risk(10, f"дев держит {dev_pct:.0f}%")
            prior = [r for r in self.store.dev_tokens(creator) if r["mint"] != mint]
            s = dev_summary(prior, now)
            if s["n"]:
                dumps = sum(1 for r in prior if is_quick_dump(r))
                dev_lines.append(f'Других монет: {s["n"]} · на PumpSwap {s["migrated"]} ({s["rate"]:.0%}) · '
                                 f'за 24 ч {s["n24"]} · сливов {dumps}')
                if s["n24"] > 5:
                    t.risk(10, f'дев запустил {s["n24"]} монет за сутки')
                if dumps >= 3 and dumps >= s["n"] / 2:
                    t.risk(25, f'дев сливал свою покупку в первые 10 минут в {dumps} из {s["n"]} монет')
                if is_good_dev(s):
                    t.bonus(10, f'у дева {s["migrated"]} монет вышли на PumpSwap')
            else:
                dev_lines.append("Других его монет в базе нет")
        else:
            dev_lines.append("На этой платформе бот пока не определяет создателя")

        # launch block
        start_lines = []
        if bund:
            b_pct, s_pct = bund["bundle_tokens"] / 1e7, bund["sniper_tokens"] / 1e7
            start_lines.append(f"Бандл в блоке запуска: <b>{b_pct:.1f}%</b> · снайперы в 2 следующих блоках: "
                               f"{s_pct:.1f}% · {len(bund['wallets'])} кошельков без дева")
            if b_pct >= 40:
                t.risk(40, f"бандл: {b_pct:.1f}% куплено в блоке запуска")
            elif b_pct >= 15:
                t.risk(25, f"бандл: {b_pct:.1f}% куплено в блоке запуска")
            elif b_pct >= 5:
                t.risk(10, f"бандл: {b_pct:.1f}% куплено в блоке запуска")
            if holders and bund["wallets"]:
                still = [h for h in holders if h["owner"] in bund["wallets"]]
                if still:
                    pct = sum(h["pct"] for h in still)
                    start_lines.append(f"Ранние кошельки до сих пор держат {pct:.1f}% ({len(still)} в топ-20)")
                    if pct >= 5:
                        t.risk(10, f"ранние кошельки ещё держат {pct:.0f}%")
        elif curve:
            start_lines.append("Слишком много сделок, первые блоки не разбирал")

        # socials and promotion
        found: dict[str, str] = {}   # kind -> url; DexScreener first, it is what the team keeps up to date
        if dex:
            for x in (dex.get("info") or {}).get("socials") or []:
                if x.get("type") and x.get("url"):
                    found.setdefault(x["type"], x["url"])
            for x in (dex.get("info") or {}).get("websites") or []:
                if x.get("url"):
                    found.setdefault("website", x["url"])
        if meta:
            for k in ("twitter", "telegram", "website"):
                if isinstance(meta.get(k), str) and meta[k].strip():
                    found.setdefault(k, meta[k].strip())
        socials = [social_link(k, u) for k, u in sorted(found.items())]
        if not socials:
            t.risk(10, "нет ни одной соцсети")
        if orders:
            notes.append("Кто-то заплатил DexScreener за продвижение (" + ", ".join(sorted(set(orders))) + ")")

        # assemble: verdict first, details below
        cap = 100 if holders else 79     # without holders the biggest risk may be hidden: never "high"
        pair = curve["pda"] if curve else (dex or {}).get("pairAddress")
        L = t.header(cap)
        L += [f"<b>{html.escape(title or short(mint))}</b> · {platform_name(mint, curve, dex, row['pool'] if row else None)} · {ago(created, now)}",
              f"<code>{mint}</code>", "", "<b>Почему такая оценка</b>"] + t.reasons()
        if not holders:
            L.append("<i>Без данных о холдерах уровень не выше «средний»</i>")

        def section(head: str, lines: list[str]) -> None:
            if lines:
                L.extend(["", f"<b>{head}</b>", *lines])

        section("📊 Рынок", market)
        section("🔐 Контракт", contract)
        section("👥 Холдеры", hold_lines)
        section("🧑‍💻 Дев", dev_lines)
        section("🎯 Старт", start_lines)
        section("🌐 Соцсети", (socials or ["нет"]) + notes)
        L += ["", links(mint, pump=bool(curve) or mint.endswith("pump"), pair=pair), DISCLAIMER]
        return "\n".join(L)

    # ---- /dev
    async def dev(self, wallet: str) -> str:
        now = time.time()
        rows = self.store.dev_tokens(wallet)
        bal = await self._safe(self.rpc.balance(wallet))
        since = self.store.coverage_start()
        head = [f"🧑‍💻 Кошелёк <code>{wallet}</code>"]
        if bal is not None:
            head.append(f"Баланс: {bal:.2f} SOL")
        cover = f"База ведётся с {time.strftime('%d.%m.%Y', time.localtime(since))}" if since else "База пока пуста"
        if not rows:
            return "\n".join(head + ["", f"Запусков этого кошелька в базе нет. {cover}: если он запускал монеты "
                                         "раньше, бот их не видел.", "", DISCLAIMER])
        s = dev_summary(rows, now)
        dumps = sum(1 for r in rows if is_quick_dump(r))
        peaks = sorted(r["peak_mcap"] for r in rows if r["peak_mcap"])
        L = head + ["", f'Монет запущено: {s["n"]} (за 24 ч: {s["n24"]})',
                    f'Вышли на PumpSwap: {s["migrated"]} ({s["rate"]:.0%}; в среднем по Pump.fun ~4%)',
                    f"Быстрых сливов (продал ≥90% покупки за 10 мин): {dumps}"]
        if peaks:
            L.append(f"Медианный пик капитализации: {peaks[len(peaks) // 2]:.0f} SOL")
        L += ["", "Последние запуски:"]
        for r in rows[:10]:
            mark = "🎓 PumpSwap" if r["migrated_at"] else f'пик {r["peak_mcap"]:.0f} SOL'
            dump = " · слил" if is_quick_dump(r) else ""
            L.append(f'• <a href="https://pump.fun/coin/{r["mint"]}">{html.escape(r["symbol"] or short(r["mint"]))}'
                     f'</a> — {ago(r["created_at"], now)} назад — {mark}{dump}')
        notes = [cover + "."]
        if not self.full_trades:
            notes.append("Сделки видны не все (публичный поток), поэтому сливы и пики могут быть занижены.")
        if s["n"] <= 2:
            notes.append("Истории мало: по 1–2 монетам выводы делать нельзя.")
        L += ["", "<i>" + " ".join(notes) + "</i>", DISCLAIMER]
        return "\n".join(L)
