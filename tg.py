"""Telegram Bot API over aiohttp (long polling) and a paced outgoing message queue."""
import asyncio
import logging
import re
import time

import aiohttp

log = logging.getLogger("tg")

GLOBAL_PER_SECOND = 25      # Telegram allows ~30 messages/s per bot
PER_CHAT_SECONDS = 1.05     # and ~1 message/s per chat
QUEUE_LIMIT = 2000


def mask(text: str) -> str:
    return re.sub(r"bot\d+:[\w-]+", "bot***", str(text))


class TGError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


class TG:
    def __init__(self, http: aiohttp.ClientSession, token: str):
        self.http = http
        self.base = f"https://api.telegram.org/bot{token}/"

    async def call(self, method: str, **params):
        timeout = aiohttp.ClientTimeout(total=(params.get("timeout") or 0) + 20)
        for _ in range(3):
            try:
                async with self.http.post(self.base + method, json=params, timeout=timeout) as r:
                    d = await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                log.warning("Telegram %s failed: %s", method, mask(type(e).__name__))
                await asyncio.sleep(3)
                continue
            if d.get("ok"):
                return d["result"]
            retry = (d.get("parameters") or {}).get("retry_after")
            if retry:
                await asyncio.sleep(float(retry))
                continue
            raise TGError(d.get("error_code", 0), mask(d.get("description", "")))
        raise TGError(0, "network")

    async def send(self, chat_id: int, text: str, **extra):
        return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                               link_preview_options={"is_disabled": True}, **extra)


class Notifier:
    """Alerts go through one queue so bursts never hit Telegram's flood limits."""

    def __init__(self, tg: TG, on_blocked):
        self.tg = tg
        self.on_blocked = on_blocked
        self.queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self.last_sent: dict[int, float] = {}
        self.dropped = 0

    async def send(self, chat_id: int, text: str) -> None:
        if self.queue.qsize() >= QUEUE_LIMIT:
            self.dropped += 1
            return
        self.queue.put_nowait((chat_id, text))

    async def run(self) -> None:
        while True:
            chat_id, text = await self.queue.get()
            wait = self.last_sent.get(chat_id, 0) + PER_CHAT_SECONDS - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await self.tg.send(chat_id, text)
            except TGError as e:
                if e.code == 403:
                    self.on_blocked(chat_id)   # the user blocked the bot
                else:
                    log.warning("Alert to %s not sent: %s", chat_id, e)
            self.last_sent[chat_id] = time.time()
            if len(self.last_sent) > 10000:
                cut = time.time() - 60
                self.last_sent = {k: v for k, v in self.last_sent.items() if v > cut}
            await asyncio.sleep(1 / GLOBAL_PER_SECOND)
