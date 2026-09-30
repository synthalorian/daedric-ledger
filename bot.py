"""daedric-ledger bot — Discord front end.

Commands (slash):
    /price <item>     — decay-weighted average with sample size
    /history <item>   — last N recorded offers
    /top              — most traded items this week
    /unmatched        — item texts the parser couldn't resolve (admin)

Setup:
    1. Create a bot at https://discord.com/developers/applications
    2. Enable the MESSAGE CONTENT privileged intent.
    3. Invite with scopes: bot, applications.commands
       Permissions: View Channels, Read Message History.
    4. Copy config.example.json -> config.json and fill in token + channels.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import discord
from discord import app_commands

from ledger.db import Ledger
from ledger.items import ItemDB
from ledger.parser import parse_trade
from ledger.pricing import compute_price_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("daedric-ledger")

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH} — copy config.example.json and fill it in.")
    return json.loads(CONFIG_PATH.read_text())


CFG = load_config()
items = ItemDB.load(Path(__file__).parent / CFG.get("items_file", "data/items.json"))
ledger = Ledger(Path(__file__).parent / CFG.get("db_file", "data/ledger.db"))
WATCH_CHANNELS: set[int] = set(CFG.get("watch_channel_ids", []))
LOOKBACK_DAYS: int = CFG.get("lookback_days", 90)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def fmt_gold(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}m"
    if v >= 10_000:
        return f"{v/1_000:.1f}k"
    return f"{v:,.0f}"


@client.event
async def on_ready():
    await tree.sync()
    log.info("online as %s — %d items loaded, watching %d channels",
             client.user, len(items), len(WATCH_CHANNELS))


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return
    if WATCH_CHANNELS and msg.channel.id not in WATCH_CHANNELS:
        return

    offer = parse_trade(msg.content)
    if offer is None:
        return

    match = items.match(offer.item_text)
    canonical = match.item.name if match.item else None
    if match.item is None:
        ledger.record_unmatched(
            message_id=msg.id, author_name=str(msg.author),
            item_text=offer.item_text, raw=msg.content,
        )
        log.info("unmatched: %r (best %.0f: %s)", offer.item_text, match.score,
                 match.candidates[:2])

    inserted = ledger.record(
        message_id=msg.id,
        guild_id=msg.guild.id if msg.guild else 0,
        channel_id=msg.channel.id,
        author_id=msg.author.id,
        author_name=str(msg.author),
        offer=offer,
        item_name=canonical,
        match_score=match.score,
    )
    if inserted:
        label = canonical or f"??{offer.item_text}??"
        log.info("%s %s x%d @ %s — %s", offer.side.value.upper(), label,
                 offer.quantity, fmt_gold(offer.unit_price), msg.author)


@tree.command(name="price", description="Decay-weighted average price for an item")
@app_commands.describe(item="Item name (fuzzy matched)")
async def price_cmd(inter: discord.Interaction, item: str):
    await inter.response.defer()
    match = items.match(item)
    if match.item is None:
        sugg = ", ".join(n for n, _ in match.candidates[:3]) or "no close names"
        await inter.followup.send(f"Couldn't resolve **{item}**. Closest: {sugg}")
        return

    since = int(time.time()) - LOOKBACK_DAYS * 86400
    rows = await asyncio.to_thread(ledger.prices_for, match.item.name, since)
    rep = compute_price_report(match.item.name, rows)

    e = discord.Embed(title=rep.item_name)
    e.add_field(name="Sellers ask (avg)", value=fmt_gold(rep.sell_avg), inline=True)
    e.add_field(name="Buyers bid (avg)", value=fmt_gold(rep.buy_avg), inline=True)
    e.add_field(name="Midpoint", value=fmt_gold(rep.midpoint), inline=True)
    total = rep.sell_count + rep.buy_count
    sample = f"{total} offers ({rep.sell_count} sell / {rep.buy_count} buy)"
    if rep.oldest_days is not None:
        sample += f", oldest {rep.oldest_days:.0f}d"
    if rep.rejected_outliers:
        sample += f", {rep.rejected_outliers} outliers dropped"
    e.add_field(name="Sample", value=sample, inline=False)
    if not rep.reliable and total > 0:
        e.set_footer(text="⚠ Thin data — treat this average as a rumor, not a law.")
    elif total == 0:
        e.description = "No recorded offers in the lookback window."
    await inter.followup.send(embed=e)


@tree.command(name="history", description="Recent recorded offers for an item")
@app_commands.describe(item="Item name")
async def history_cmd(inter: discord.Interaction, item: str):
    match = items.match(item)
    if match.item is None:
        await inter.response.send_message(f"Couldn't resolve **{item}**.", ephemeral=True)
        return
    rows = ledger.history(match.item.name, limit=10)
    if not rows:
        await inter.response.send_message(f"No offers recorded for **{match.item.name}**.")
        return
    lines = []
    for r in rows:
        when = time.strftime("%m-%d", time.localtime(r["ts"]))
        arrow = "🔴 WTS" if r["side"] == "sell" else "🟢 WTB"
        qty = f"x{r['quantity']} " if r["quantity"] > 1 else ""
        lines.append(f"`{when}` {arrow} {qty}@ **{fmt_gold(r['unit_price'])}** — {r['author_name']}")
    await inter.response.send_message(
        embed=discord.Embed(title=f"{match.item.name} — recent offers", description="\n".join(lines))
    )


@tree.command(name="top", description="Most traded items recently")
async def top_cmd(inter: discord.Interaction):
    rows = ledger.top_traded(days=7)
    if not rows:
        await inter.response.send_message("Nothing traded in the last 7 days.")
        return
    lines = [f"`{i+1:>2}.` **{r['item_name']}** — {r['n']} offers, avg {fmt_gold(r['avg_price'])}"
             for i, r in enumerate(rows)]
    await inter.response.send_message(
        embed=discord.Embed(title="Top traded (7 days)", description="\n".join(lines))
    )


@tree.command(name="unmatched", description="Item texts the matcher couldn't resolve (admin)")
@app_commands.default_permissions(manage_guild=True)
async def unmatched_cmd(inter: discord.Interaction):
    rows = ledger.unmatched_report()
    if not rows:
        await inter.response.send_message("No unmatched item texts. Matcher is clean. ⚫")
        return
    lines = [f"`{r['n']:>3}×` {r['item_text']}" for r in rows]
    await inter.response.send_message(
        embed=discord.Embed(title="Unmatched item texts", description="\n".join(lines)),
        ephemeral=True,
    )


if __name__ == "__main__":
    client.run(CFG["token"])
