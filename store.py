"""SQLite storage: every Pump.fun launch with per-token aggregates, plus bot users, watches and payments."""
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens(
    mint TEXT PRIMARY KEY, creator TEXT, name TEXT, symbol TEXT, uri TEXT,
    created_at REAL, standard INT,
    dev_buy_sol REAL DEFAULT 0, dev_buy_tokens REAL DEFAULT 0,
    dev_sold_tokens REAL DEFAULT 0, dev_sold_sol REAL DEFAULT 0, dev_first_sell_at REAL,
    peak_mcap REAL DEFAULT 0, last_mcap REAL DEFAULT 0, last_trade_at REAL,
    buys INT DEFAULT 0, sells INT DEFAULT 0, vol_sol REAL DEFAULT 0,
    migrated_at REAL, pool TEXT);
CREATE INDEX IF NOT EXISTS tokens_creator ON tokens(creator, created_at);
CREATE INDEX IF NOT EXISTS tokens_created ON tokens(created_at);
CREATE TABLE IF NOT EXISTS bundles(
    mint TEXT PRIMARY KEY, checked_at REAL, create_slot INT,
    wallets TEXT, bundle_tokens REAL, bundle_sol REAL, sniper_tokens REAL);
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT, first_seen REAL,
    pro_until REAL DEFAULT 0, used_day TEXT, used INT DEFAULT 0,
    alerts INT DEFAULT 0, blocked INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS watches(
    user_id INT, wallet TEXT, label TEXT, added_at REAL, PRIMARY KEY(user_id, wallet));
CREATE TABLE IF NOT EXISTS payments(
    charge_id TEXT PRIMARY KEY, user_id INT, stars INT, days INT, at REAL, refunded INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        if "pool" not in {r["name"] for r in self.q("PRAGMA table_info(tokens)")}:
            self.db.execute("ALTER TABLE tokens ADD COLUMN pool TEXT")   # databases made before LetsBonk support
            self.db.commit()

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args=()) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def commit(self) -> None:
        self.db.commit()

    # ---- meta
    def meta(self, k: str) -> str | None:
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return r["v"] if r else None

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
        self.db.commit()

    def coverage_start(self) -> float | None:
        r = self.one("SELECT MIN(created_at) m FROM tokens")
        return r["m"] if r and r["m"] else None

    # ---- tokens
    def add_token(self, ev: dict) -> bool:
        """Insert a launch; returns False when it was already known."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO tokens(mint, creator, name, symbol, uri, created_at, standard,"
            " dev_buy_sol, dev_buy_tokens, peak_mcap, last_mcap, pool) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev["mint"], ev["traderPublicKey"], ev.get("name", ""), ev.get("symbol", ""), ev.get("uri", ""),
             ev["t"], ev.get("standard"), ev.get("solAmount") or 0.0, ev.get("initialBuy") or 0.0,
             ev.get("marketCapSol") or 0.0, ev.get("marketCapSol") or 0.0, ev.get("pool") or "pump"))
        return cur.rowcount > 0

    def token(self, mint: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM tokens WHERE mint=?", (mint,))

    def dev_tokens(self, creator: str, before: float | None = None) -> list[sqlite3.Row]:
        if before is None:
            return self.q("SELECT * FROM tokens WHERE creator=? ORDER BY created_at DESC", (creator,))
        return self.q("SELECT * FROM tokens WHERE creator=? AND created_at<? ORDER BY created_at DESC",
                      (creator, before))

    def recent_creators(self, since: float) -> list[sqlite3.Row]:
        return self.q("SELECT mint, creator, dev_buy_tokens FROM tokens WHERE created_at>=?", (since,))

    # ---- users
    def user(self, uid: int, username: str = "") -> sqlite3.Row:
        self.db.execute("INSERT OR IGNORE INTO users(id, username, first_seen) VALUES(?,?,?)",
                        (uid, username, time.time()))
        if username:
            self.db.execute("UPDATE users SET username=?, blocked=0 WHERE id=?", (username, uid))
        self.db.commit()
        return self.one("SELECT * FROM users WHERE id=?", (uid,))

    def is_pro(self, uid: int) -> bool:
        r = self.one("SELECT pro_until FROM users WHERE id=?", (uid,))
        return bool(r and r["pro_until"] > time.time())

    def use_lookup(self, uid: int, limit: int) -> bool:
        """Count one lookup against today's limit; False when the limit is reached."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        r = self.user(uid)
        used = r["used"] if r["used_day"] == day else 0
        if used >= limit:
            return False
        self.db.execute("UPDATE users SET used_day=?, used=? WHERE id=?", (day, used + 1, uid))
        self.db.commit()
        return True

    def extend_pro(self, uid: int, days: int) -> float:
        r = self.user(uid)
        until = max(r["pro_until"], time.time()) + days * 86400
        self.db.execute("UPDATE users SET pro_until=? WHERE id=?", (until, uid))
        self.db.commit()
        return until

    def set_alerts(self, uid: int, on: bool) -> None:
        self.db.execute("UPDATE users SET alerts=? WHERE id=?", (int(on), uid))
        self.db.commit()

    def set_blocked(self, uid: int) -> None:
        self.db.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
        self.db.commit()

    def alert_users(self) -> list[int]:
        return [r["id"] for r in self.q(
            "SELECT id FROM users WHERE alerts=1 AND blocked=0 AND pro_until>?", (time.time(),))]

    # ---- watches
    def watches(self, uid: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM watches WHERE user_id=? ORDER BY added_at", (uid,))

    def add_watch(self, uid: int, wallet: str, label: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO watches VALUES(?,?,?,?)", (uid, wallet, label, time.time()))
        self.db.commit()

    def remove_watch(self, uid: int, wallet: str) -> bool:
        cur = self.db.execute("DELETE FROM watches WHERE user_id=? AND wallet=?", (uid, wallet))
        self.db.commit()
        return cur.rowcount > 0

    def all_watches(self) -> list[sqlite3.Row]:
        """Watches of users who may still use them (Pro, or within the free allowance)."""
        return self.q("SELECT w.user_id, w.wallet, w.label FROM watches w JOIN users u ON u.id=w.user_id"
                      " WHERE u.blocked=0 ORDER BY w.added_at")

    # ---- payments
    def add_payment(self, charge_id: str, uid: int, stars: int, days: int) -> bool:
        cur = self.db.execute("INSERT OR IGNORE INTO payments(charge_id, user_id, stars, days, at)"
                              " VALUES(?,?,?,?,?)", (charge_id, uid, stars, days, time.time()))
        self.db.commit()
        return cur.rowcount > 0
