"""SQLite ledger: every parsed trade offer, raw and structured."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .parser import TradeOffer

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,           -- unix timestamp
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,
    message_id    INTEGER NOT NULL UNIQUE,    -- dedupe on Discord snowflake
    author_id     INTEGER NOT NULL,
    author_name   TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('sell','buy')),
    item_name     TEXT,                        -- canonical name (NULL if unresolved)
    item_text     TEXT NOT NULL,               -- raw parsed item text
    match_score   REAL NOT NULL DEFAULT 0,
    quantity      INTEGER NOT NULL DEFAULT 1,
    unit_price    REAL NOT NULL,
    raw_message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_item_ts ON offers(item_name, ts);
CREATE INDEX IF NOT EXISTS idx_offers_ts ON offers(ts);

CREATE TABLE IF NOT EXISTS unmatched (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    message_id    INTEGER NOT NULL UNIQUE,
    author_name   TEXT NOT NULL,
    item_text     TEXT NOT NULL,
    raw_message   TEXT NOT NULL
);

-- Official (admin-set) prices, PER SERVER: each guild keeps its own books.
-- The cross-server average is computed from these rows.
CREATE TABLE IF NOT EXISTS prices (
    guild_id      INTEGER NOT NULL,
    guild_name    TEXT NOT NULL DEFAULT '',
    item_name     TEXT NOT NULL,
    buy           REAL,                 -- what the crown/store pays
    sell          REAL,                 -- what the crown/store charges
    set_by        TEXT NOT NULL,
    set_by_id     INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    PRIMARY KEY (guild_id, item_name)
);

-- Full audit trail: every price change, who did it, when, in which server.
CREATE TABLE IF NOT EXISTS price_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    item_name     TEXT NOT NULL,
    buy           REAL,
    sell          REAL,
    set_by        TEXT NOT NULL,
    set_by_id     INTEGER NOT NULL,
    ts            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_item ON price_history(item_name, ts);

-- tiny kv store for board message id etc.
CREATE TABLE IF NOT EXISTS kv (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);
"""


class Ledger:
    def __init__(self, path: str | Path, legacy_guild_id: int = 0, legacy_guild_name: str = ""):
        # check_same_thread=False: discord.py handlers offload DB calls via
        # asyncio.to_thread. The lock serializes writers.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self._migrate(legacy_guild_id, legacy_guild_name)

    def _migrate(self, legacy_guild_id: int, legacy_guild_name: str) -> None:
        """v1 -> v2: global prices table gains guild_id. Existing rows are
        attributed to the home guild (the server that ran the bot first)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(prices)")}
        if "guild_id" in cols or not cols:
            return
        self.conn.execute("ALTER TABLE prices RENAME TO prices_v1")
        self.conn.execute("ALTER TABLE price_history RENAME TO price_history_v1")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            """INSERT INTO prices (guild_id, guild_name, item_name, buy, sell, set_by, set_by_id, ts)
               SELECT ?, ?, item_name, buy, sell, set_by, set_by_id, ts FROM prices_v1""",
            (legacy_guild_id, legacy_guild_name),
        )
        self.conn.execute(
            """INSERT INTO price_history (guild_id, item_name, buy, sell, set_by, set_by_id, ts)
               SELECT ?, item_name, buy, sell, set_by, set_by_id, ts FROM price_history_v1""",
            (legacy_guild_id,),
        )
        self.conn.execute("DROP TABLE prices_v1")
        self.conn.execute("DROP TABLE price_history_v1")
        self.conn.commit()

    def record(
        self,
        *,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_id: int,
        author_name: str,
        offer: TradeOffer,
        item_name: str | None,
        match_score: float,
    ) -> bool:
        """Insert an offer. Returns False if the message was already recorded."""
        try:
            with self._lock:
                self.conn.execute(
                    """INSERT INTO offers
                       (ts, guild_id, channel_id, message_id, author_id, author_name,
                        side, item_name, item_text, match_score, quantity, unit_price, raw_message)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        int(time.time()), guild_id, channel_id, message_id, author_id,
                        author_name, offer.side.value, item_name, offer.item_text,
                        match_score, offer.quantity, offer.unit_price, offer.raw,
                    ),
                )
                self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def record_unmatched(self, *, message_id: int, author_name: str, item_text: str, raw: str) -> None:
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO unmatched (ts, message_id, author_name, item_text, raw_message) VALUES (?,?,?,?,?)",
                    (int(time.time()), message_id, author_name, item_text, raw),
                )
                self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def prices_for(self, item_name: str, since_ts: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT side, unit_price, quantity, ts, author_name, item_text
               FROM offers
               WHERE item_name = ? AND ts >= ?
               ORDER BY ts ASC""",
            (item_name, since_ts),
        ).fetchall()

    def history(self, item_name: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT side, unit_price, quantity, ts, author_name
               FROM offers WHERE item_name = ?
               ORDER BY ts DESC LIMIT ?""",
            (item_name, limit),
        ).fetchall()

    def top_traded(self, days: int = 7, limit: int = 15) -> list[sqlite3.Row]:
        since = int(time.time()) - days * 86400
        return self.conn.execute(
            """SELECT item_name, COUNT(*) AS n, AVG(unit_price) AS avg_price
               FROM offers
               WHERE item_name IS NOT NULL AND ts >= ?
               GROUP BY item_name ORDER BY n DESC LIMIT ?""",
            (since, limit),
        ).fetchall()

    def unmatched_report(self, limit: int = 25) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT item_text, COUNT(*) AS n FROM unmatched GROUP BY item_text ORDER BY n DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # --- official prices (per-guild, cross-server averaged) -----------------

    def set_price(self, guild_id: int, guild_name: str, item_name: str,
                  buy: float | None, sell: float | None,
                  set_by: str, set_by_id: int) -> None:
        ts = int(time.time())
        with self._lock:
            self.conn.execute(
                """INSERT INTO prices (guild_id, guild_name, item_name, buy, sell, set_by, set_by_id, ts)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(guild_id, item_name) DO UPDATE SET
                       guild_name=excluded.guild_name,
                       buy=excluded.buy, sell=excluded.sell,
                       set_by=excluded.set_by, set_by_id=excluded.set_by_id, ts=excluded.ts""",
                (guild_id, guild_name, item_name, buy, sell, set_by, set_by_id, ts),
            )
            self.conn.execute(
                """INSERT INTO price_history (guild_id, item_name, buy, sell, set_by, set_by_id, ts)
                   VALUES (?,?,?,?,?,?,?)""",
                (guild_id, item_name, buy, sell, set_by, set_by_id, ts),
            )
            self.conn.commit()

    def remove_price(self, guild_id: int, item_name: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM prices WHERE guild_id = ? AND item_name = ?",
                (guild_id, item_name),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def get_prices(self, item_name: str) -> list[sqlite3.Row]:
        """Every server's official price for one item."""
        return self.conn.execute(
            "SELECT * FROM prices WHERE item_name = ? ORDER BY guild_name",
            (item_name,),
        ).fetchall()

    def average_price(self, item_name: str) -> dict | None:
        """Cross-server average: mean buy / mean sell across all guilds
        reporting a price. Each server counts once — no whale server can
        dominate by listing more items."""
        rows = self.get_prices(item_name)
        if not rows:
            return None
        buys = [r["buy"] for r in rows if r["buy"] is not None]
        sells = [r["sell"] for r in rows if r["sell"] is not None]
        return {
            "item_name": item_name,
            "avg_buy": sum(buys) / len(buys) if buys else None,
            "avg_sell": sum(sells) / len(sells) if sells else None,
            "n_servers": len({r["guild_id"] for r in rows}),
            "servers": [(r["guild_name"], r["buy"], r["sell"]) for r in rows],
        }

    def board_prices(self) -> list[sqlite3.Row]:
        """One row per item: cross-server average + reporting server count."""
        return self.conn.execute(
            """SELECT item_name,
                      AVG(buy) AS avg_buy, AVG(sell) AS avg_sell,
                      COUNT(DISTINCT guild_id) AS n_servers,
                      MAX(ts) AS last_change
               FROM prices GROUP BY item_name ORDER BY item_name"""
        ).fetchall()

    def price_history_for(self, item_name: str, guild_id: int | None = None,
                          limit: int = 10) -> list[sqlite3.Row]:
        if guild_id is not None:
            return self.conn.execute(
                "SELECT * FROM price_history WHERE item_name = ? AND guild_id = ? ORDER BY ts DESC LIMIT ?",
                (item_name, guild_id, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM price_history WHERE item_name = ? ORDER BY ts DESC LIMIT ?",
            (item_name, limit),
        ).fetchall()

    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()
