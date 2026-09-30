"""SQLite ledger: every parsed trade offer, raw and structured."""

from __future__ import annotations

import sqlite3
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
"""


class Ledger:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
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

    def close(self) -> None:
        self.conn.close()
