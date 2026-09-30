#!/usr/bin/env python3
"""One-shot import: parse admin price sheets already posted in the ledger
channel and seed the official prices table.

Targets rows like the BITS AND PIECES sheet:

    MATERIAL              BUY     SELL
     Iron Ore                 1       2
     Leather Strips       .25   1

A row qualifies when it has a name followed by exactly two numeric columns.
Single-number lines ("Iron Mine: 8,000 Septims") are quantities/costs, not
buy/sell prices, and are skipped.

Uses REST only (no gateway) so it's safe to run while the bot is live.

    python tools/import_sheets.py --channel 1550350919240261724 [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ledger.db import Ledger
from ledger.items import ItemDB

CFG = json.loads((Path(__file__).parent.parent / "config.json").read_text())

# "  Iron Ore                 1       2" / "Leather Strips       .25   1"
_ROW_RE = re.compile(r"^\s*(?P<name>[A-Za-z][A-Za-z '\-]+?)\s{2,}(?P<buy>\d*\.?\d+)\s+(?P<sell>\d*\.?\d+)\s*$")


def fetch_all_messages(channel_id: int) -> list[dict]:
    token = CFG["token"]
    msgs: list[dict] = []
    before = None
    while True:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=100"
        if before:
            url += f"&before={before}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "daedric-ledger (https://localhost, 1.0)",
        })
        with urllib.request.urlopen(req) as resp:
            batch = json.loads(resp.read())
        if not batch:
            break
        msgs.extend(batch)
        before = batch[-1]["id"]
        if len(batch) < 100:
            break
    return msgs


def parse_sheet(content: str) -> list[tuple[str, float, float]]:
    rows = []
    for line in content.splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        # skip headers and markdown noise
        if name.lower() in {"material", "buy", "sell", "item", "price"} or name.startswith(("#", ">", "*")):
            continue
        buy, sell = float(m.group("buy")), float(m.group("sell"))
        if buy > 1_000_000 or sell > 1_000_000:
            continue
        rows.append((name, buy, sell))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items = ItemDB.load(Path(__file__).parent.parent / CFG.get("items_file", "data/items.json"))
    ledger = Ledger(Path(__file__).parent.parent / CFG.get("db_file", "data/ledger.db"))
    guild_id = CFG.get("home_guild_id", 0)
    guild_name = CFG.get("home_guild_name", "")

    msgs = fetch_all_messages(args.channel)
    print(f"fetched {len(msgs)} messages")

    imported = skipped = 0
    for msg in msgs:
        author = msg["author"]["username"]
        for name, buy, sell in parse_sheet(msg["content"]):
            match = items.match(name)
            canonical = match.item.name if match.item else None
            if canonical is None:
                print(f"  ?? {name!r} (buy {buy} sell {sell}) — no item match, skipped")
                skipped += 1
                continue
            print(f"  {canonical}: buy {buy} sell {sell}  (from {author})")
            if not args.dry_run:
                ledger.set_price(guild_id, guild_name, canonical, buy, sell,
                                 f"{author} (sheet import)", 0)
            imported += 1

    print(f"\n{'would import' if args.dry_run else 'imported'} {imported} prices, skipped {skipped}")
    ledger.close()


if __name__ == "__main__":
    main()
