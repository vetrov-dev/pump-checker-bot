"""Pump.fun checker bot for Telegram.

    python bot.py               run everything (collector + Telegram bot; collector only if BOT_TOKEN is empty)
    python bot.py check <mint>  print a token report to the console
    python bot.py dev <wallet>  print a dev report to the console
"""
import asyncio
import html
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler

import aiohttp
from solders.pubkey import Pubkey

from config import PUBLIC_RPC, Config, load
from feed import PUBLIC_WS, PumpFeed
from pumpportal import PumpPortalFeed
from report import DISCLAIMER, Reports, find_address
from rpc import Rpc
from store import Store
from tg import TG, Notifier, TGError, mask
from tracker import Tracker, short

log = logging.getLogger("bot")

COMMANDS = [
    ("check", "Проверить монету: /check <адрес>"),
    ("dev", "История дева: /dev <кошелёк>"),
    ("watch", "Следить за кошельком: /watch <кошелёк> [метка]"),
    ("unwatch", "Перестать следить: /unwatch <кошелёк>"),
    ("list", "Мои кошельки"),
    ("alerts", "Алерты о запусках от девов с историей (Pro)"),
    ("pro", "Подписка Pro"),
    ("help", "Как пользоваться"),
    ("paysupport", "Вопросы по оплате"),
    ("terms", "Условия"),
]


class BotApp:
    def __init__(self, cfg: Config, tg: TG, store: Store, tracker: Tracker, reports: Reports):
        self.cfg = cfg
        self.tg = tg
        self.store = store
        self.tracker = tracker
        self.reports = reports
        self.busy: set[int] = set()

    # ---- limits
    def is_admin(self, uid: int) -> bool:
        return uid in self.cfg.admin_ids

    def lookup_limit(self, uid: int) -> int:
        if self.is_admin(uid):
            return 10**9
        return self.cfg.pro_lookups_per_day if self.store.is_pro(uid) else self.cfg.free_lookups_per_day

    def watch_limit(self, uid: int) -> int:
        if self.is_admin(uid):
            return 1000
        return self.cfg.pro_watches if self.store.is_pro(uid) else self.cfg.free_watches

    # ---- loop
    async def run(self) -> None:
        me = await self.tg.call("getMe")
        log.info("Telegram bot @%s is up", me.get("username"))
        await self.tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        offset = 0
        while True:
            try:
                updates = await self.tg.call("getUpdates", offset=offset, timeout=50,
                                             allowed_updates=["message", "pre_checkout_query"])
            except TGError as e:
                log.warning("getUpdates: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                asyncio.create_task(self.handle(u))

    async def handle(self, u: dict) -> None:
        try:
            if "pre_checkout_query" in u:
                q = u["pre_checkout_query"]
                ok = q.get("invoice_payload") == "pro" and q.get("currency") == "XTR"
                await self.tg.call("answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=ok,
                                   **({} if ok else {"error_message": "Неизвестный счёт"}))
                return
            msg = u.get("message")
            if not msg or msg.get("chat", {}).get("type") != "private":
                return  # the bot works in private chats only
            uid = msg["from"]["id"]
            self.store.user(uid, msg["from"].get("username", ""))
            if "successful_payment" in msg:
                await self.paid(uid, msg["successful_payment"])
                return
            await self.command(uid, (msg.get("text") or "").strip())
        except TGError as e:
            log.warning("Reply failed: %s", e)
        except Exception:
            log.exception("Update handling failed")

    async def reply(self, uid: int, text: str, **extra) -> None:
        try:
            await self.tg.send(uid, text, **extra)
        except TGError as e:
            if e.code == 403:
                self.store.set_blocked(uid)
            else:
                raise

    async def command(self, uid: int, text: str) -> None:
        m = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", text, re.S)
        cmd, arg = (m.group(1).lower(), m.group(2).strip()) if m else ("", text)
        handler = getattr(self, f"cmd_{cmd}", None) if cmd else None
        if handler:
            await handler(uid, arg)
        elif find_address(text):
            await self.lookup(uid, find_address(text), None)
        else:
            await self.reply(uid, "Пришлите адрес монеты или кошелька, ссылку на pump.fun / DexScreener, "
                                  "или откройте /help")

    # ---- lookups
    async def lookup(self, uid: int, addr: str, want: str | None) -> None:
        if uid in self.busy:
            await self.reply(uid, "⏳ Подождите, предыдущая проверка ещё идёт")
            return
        if not self.store.use_lookup(uid, self.lookup_limit(uid)):
            await self.reply(uid, f"Лимит бесплатных проверок на сегодня исчерпан "
                                  f"({self.cfg.free_lookups_per_day} в день). С Pro — до "
                                  f"{self.cfg.pro_lookups_per_day} в день: /pro")
            return
        self.busy.add(uid)
        try:
            await self.tg.call("sendChatAction", chat_id=uid, action="typing")
            kind, addr = await self.reports.classify(addr)
            if want == "dev" or (want is None and kind == "wallet"):
                text = await self.reports.dev(addr)
            elif kind == "token":
                text = await self.reports.check(addr)
            else:
                text = "Это похоже на кошелёк, а не на монету. Для истории дева: /dev " + addr
            await self.reply(uid, text)
        finally:
            self.busy.discard(uid)

    async def cmd_check(self, uid: int, arg: str) -> None:
        addr = find_address(arg)
        if not addr:
            await self.reply(uid, "Формат: /check &lt;адрес монеты или ссылка&gt;")
            return
        await self.lookup(uid, addr, "check")

    async def cmd_dev(self, uid: int, arg: str) -> None:
        addr = find_address(arg)
        if not addr:
            await self.reply(uid, "Формат: /dev &lt;кошелёк&gt;")
            return
        await self.lookup(uid, addr, "dev")

    # ---- watches
    async def cmd_watch(self, uid: int, arg: str) -> None:
        addr = find_address(arg)
        if not addr:
            await self.reply(uid, "Формат: /watch &lt;кошелёк&gt; [метка]\nНапример: /watch 8Njm…rwFy дев Pax")
            return
        label = arg.replace(addr, "").strip()[:40]
        have = self.store.watches(uid)
        if all(w["wallet"] != addr for w in have) and len(have) >= self.watch_limit(uid):
            more = "" if self.store.is_pro(uid) else f" С Pro — до {self.cfg.pro_watches}: /pro"
            await self.reply(uid, f"Лимит кошельков для слежки: {self.watch_limit(uid)}.{more}")
            return
        self.store.add_watch(uid, addr, label)
        self.tracker.reload_watches()
        note = "" if self.cfg.ws_url else ("\n\n<i>Сейчас бот читает публичный поток Solana: он видит не все сделки, "
                                           "часть покупок и продаж может не прийти.</i>")
        await self.reply(uid, f"👀 Слежу за <code>{addr}</code>{' (' + html.escape(label) + ')' if label else ''}: "
                              f"напишу о его запусках монет, покупках и продажах на Pump.fun.{note}")

    async def cmd_unwatch(self, uid: int, arg: str) -> None:
        addr = find_address(arg)
        if addr and self.store.remove_watch(uid, addr):
            self.tracker.reload_watches()
            await self.reply(uid, "Больше не слежу за " + short(addr))
        else:
            await self.reply(uid, "Такого кошелька нет в списке. Список: /list")

    async def cmd_list(self, uid: int, arg: str) -> None:
        ws = self.store.watches(uid)
        if not ws:
            await self.reply(uid, "Вы ни за кем не следите. Добавить: /watch &lt;кошелёк&gt; [метка]")
            return
        lines = [f"• <code>{w['wallet']}</code> {html.escape(w['label'] or '')}" for w in ws]
        await self.reply(uid, f"Слежу ({len(ws)} из {self.watch_limit(uid)}):\n" + "\n".join(lines))

    async def cmd_alerts(self, uid: int, arg: str) -> None:
        if not (self.store.is_pro(uid) or self.is_admin(uid)):
            await self.reply(uid, "Алерты о запусках — функция Pro: /pro")
            return
        on = not self.store.user(uid)["alerts"]
        if arg.lower() in ("on", "вкл"):
            on = True
        elif arg.lower() in ("off", "выкл"):
            on = False
        self.store.set_alerts(uid, on)
        await self.reply(uid, "🔔 Алерты включены: напишу, когда монету запустит дев, у которого прошлые монеты "
                              "выходили на PumpSwap (≥50% и не больше 5 запусков в сутки).\n\n"
                              "<i>Это не сигнал на покупку. В наших тестах покупка таких монет на старте в среднем "
                              "была убыточной.</i>" if on else "🔕 Алерты выключены")

    # ---- payments
    async def cmd_pro(self, uid: int, arg: str) -> None:
        r = self.store.user(uid)
        if r["pro_until"] > time.time():
            status = "✅ Pro активен до " + time.strftime("%d.%m.%Y %H:%M", time.localtime(r["pro_until"]))
        else:
            status = "Сейчас у вас бесплатный тариф."
        await self.reply(uid, f"{status}\n\n<b>Pro на {self.cfg.pro_days} дней</b>\n"
                              f"• до {self.cfg.pro_lookups_per_day} проверок в день "
                              f"(бесплатно {self.cfg.free_lookups_per_day})\n"
                              f"• слежка за {self.cfg.pro_watches} кошельками (бесплатно {self.cfg.free_watches})\n"
                              "• алерты о запусках от девов с историей\n\nОплата в Telegram Stars, кнопка ниже. "
                              "Продление добавляет дни к текущему сроку.")
        await self.tg.call("sendInvoice", chat_id=uid, title=f"Pro на {self.cfg.pro_days} дней",
                           description="Больше проверок, слежка за кошельками и алерты о запусках",
                           payload="pro", currency="XTR",
                           prices=[{"label": "Pro", "amount": self.cfg.pro_price_stars}])

    async def paid(self, uid: int, p: dict) -> None:
        charge = p.get("telegram_payment_charge_id", "")
        if p.get("invoice_payload") != "pro" or not self.store.add_payment(charge, uid, p.get("total_amount", 0),
                                                                           self.cfg.pro_days):
            return
        until = self.store.extend_pro(uid, self.cfg.pro_days)
        self.tracker.reload_watches()
        log.info("Payment: user %s, %s stars", uid, p.get("total_amount"))
        await self.reply(uid, "Спасибо! ✅ Pro активен до " + time.strftime("%d.%m.%Y", time.localtime(until)) +
                              "\nВключить алерты: /alerts")

    async def cmd_paysupport(self, uid: int, arg: str) -> None:
        contact = self.cfg.support_contact or "администратору бота"
        await self.reply(uid, f"По вопросам оплаты напишите {html.escape(contact)} и укажите ваш ID: "
                              f"<code>{uid}</code>. Если Pro не включился после оплаты, деньги вернём.")

    async def cmd_terms(self, uid: int, arg: str) -> None:
        await self.reply(uid, "Условия\n\n• Бот только читает публичные данные блокчейна Solana, DexScreener и "
                              "PumpPortal. Он не торгует, не хранит деньги и никогда не просит приватный ключ или "
                              "seed-фразу.\n• Отчёты — не финансовый совет и не гарантия. Данные могут быть "
                              "неполными или с задержкой.\n• Pro даёт расширенные лимиты на оплаченный срок. "
                              "Возврат — через /paysupport.\n\n" + DISCLAIMER)

    async def cmd_start(self, uid: int, arg: str) -> None:
        await self.cmd_help(uid, arg)

    async def cmd_help(self, uid: int, arg: str) -> None:
        await self.reply(uid, "Привет! Я проверяю монеты Pump.fun, прежде чем вы их купите.\n\n"
                              "Просто пришлите адрес монеты или ссылку (pump.fun, Axiom, GMGN, Photon, BullX, "
                              "DexScreener), и я покажу траст от 0 до 100 и из чего он сложился:\n"
                              "• сколько держат топ-холдеры и сам дев\n"
                              "• был ли бандл: сколько монет скупили в первом блоке\n"
                              "• историю дева: сколько монет запускал, сколько вышло на PumpSwap, как часто сливал\n"
                              "• соцсети и платное продвижение на DexScreener\n\n"
                              "Пришлёте кошелёк — покажу историю дева.\n\n"
                              "/watch &lt;кошелёк&gt; — пришлю его запуски, покупки и продажи\n"
                              "/list, /unwatch — список и удаление\n/alerts — алерты о запусках (Pro)\n"
                              "/pro — подписка\n\n🔒 Я никогда не прошу приватный ключ и не торгую за вас. "
                              "Любой бот, который просит ключ, — повод насторожиться.")

    # ---- admin
    async def cmd_stats(self, uid: int, arg: str) -> None:
        if not self.is_admin(uid):
            return
        now = time.time()
        one = self.store.one
        users = one("SELECT COUNT(*) n, SUM(pro_until>?) pro, SUM(blocked) bl FROM users", (now,))
        toks = one("SELECT COUNT(*) n, SUM(created_at>?) d, SUM(migrated_at>?) m FROM tokens", (now - 86400,) * 2)
        pays = one("SELECT COUNT(*) n, COALESCE(SUM(stars),0) s FROM payments WHERE refunded=0")
        await self.reply(uid, f"Пользователи: {users['n']} (Pro {users['pro'] or 0}, заблокировали {users['bl'] or 0})\n"
                              f"Монет в базе: {toks['n']} (за 24 ч {toks['d'] or 0}, миграций {toks['m'] or 0})\n"
                              f"Оплаты: {pays['n']} на {pays['s']} Stars\n"
                              f"Слежек: {sum(len(v) for v in self.tracker.watchers.values())}")

    async def cmd_grant(self, uid: int, arg: str) -> None:
        if not self.is_admin(uid):
            return
        try:
            target, days = (int(x) for x in arg.split()[:2])
        except ValueError:
            await self.reply(uid, "Формат: /grant &lt;user_id&gt; &lt;дней&gt;")
            return
        until = self.store.extend_pro(target, days)
        self.tracker.reload_watches()
        await self.reply(uid, f"Pro для {target} до {time.strftime('%d.%m.%Y', time.localtime(until))}")

    async def cmd_refund(self, uid: int, arg: str) -> None:
        if not self.is_admin(uid):
            return
        parts = arg.split()
        if len(parts) != 2 or not parts[0].isdigit():
            await self.reply(uid, "Формат: /refund &lt;user_id&gt; &lt;charge_id&gt;")
            return
        target, charge = int(parts[0]), parts[1]
        pay = self.store.one("SELECT * FROM payments WHERE charge_id=? AND user_id=? AND refunded=0", (charge, target))
        if not pay:
            await self.reply(uid, "Такой оплаты нет или она уже возвращена")
            return
        await self.tg.call("refundStarPayment", user_id=target, telegram_payment_charge_id=charge)
        self.store.db.execute("UPDATE payments SET refunded=1 WHERE charge_id=?", (charge,))
        self.store.extend_pro(target, -pay["days"])
        self.tracker.reload_watches()
        await self.reply(uid, f"Возвращено {pay['stars']} Stars пользователю {target}")


async def housekeeping(app: BotApp | None, tracker: Tracker) -> None:
    while True:
        await asyncio.sleep(3600)
        tracker.reload_watches()   # drops watches above the free allowance once Pro expires


def setup_logging(cfg: Config) -> None:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file = RotatingFileHandler(cfg.db_path.parent / "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    for h in (file, console):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file, console])
    for noisy in ("websockets", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def main(argv: list[str]) -> None:
    cfg = load()
    setup_logging(cfg)
    store = Store(cfg.db_path)
    async with aiohttp.ClientSession(headers={"User-Agent": "pump-alerts/1.0"}) as http:
        rpc = Rpc(http, cfg.rpc_url)
        reports = Reports(rpc, http, store, holders_ok=cfg.rpc_url != PUBLIC_RPC, full_trades=bool(cfg.ws_url))

        if argv and argv[0] in ("check", "dev") and len(argv) > 1:
            addr = find_address(argv[1])
            if not addr:
                sys.exit("Не похоже на адрес Solana")
            Pubkey.from_string(addr)
            kind, addr = await reports.classify(addr)   # a pair address from Axiom/Photon becomes the coin
            text = await (reports.check(addr) if argv[0] == "check" else reports.dev(addr))
            sys.stdout.reconfigure(encoding="utf-8")   # emoji on a Windows console
            print(plain(text))
            return

        tg = TG(http, cfg.bot_token) if cfg.bot_token else None
        notifier = Notifier(tg, store.set_blocked) if tg else None
        app: BotApp | None = None
        tracker = Tracker(store, notifier.send if notifier else None,
                          watch_limits=lambda uid: app.watch_limit(uid) if app else 0)
        if tg:
            app = BotApp(cfg, tg, store, tracker, reports)
            tracker.reload_watches()
        else:
            log.warning("BOT_TOKEN is empty: only collecting launches into the database")
        if cfg.rpc_url == PUBLIC_RPC:
            log.warning("RPC_URL is the public endpoint: /check will show no holders")
        ws = cfg.ws_url or PUBLIC_WS
        log.info("Trades stream: %s", "public Solana websocket (partial)" if ws == PUBLIC_WS else mask_url(ws))
        tasks = [PumpPortalFeed(tracker.on_event).run(), PumpFeed(ws, tracker.on_event).run(),
                 tracker.flush_loop(), housekeeping(app, tracker)]
        if app and notifier:
            tasks += [notifier.run(), app.run()]
        try:
            await asyncio.gather(*tasks)
        finally:
            tracker.flush()


def mask_url(url: str) -> str:
    return mask(re.sub(r"(api[-_]?key=)[^&]+", r"\1***", url, flags=re.I))


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except KeyboardInterrupt:
        pass
