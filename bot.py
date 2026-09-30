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
from discord.ext import tasks

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
ledger = Ledger(
    Path(__file__).parent / CFG.get("db_file", "data/ledger.db"),
    legacy_guild_id=CFG.get("home_guild_id", 0),
    legacy_guild_name=CFG.get("home_guild_name", ""),
)
WATCH_CHANNELS: set[int] = set(CFG.get("watch_channel_ids", []))
LOOKBACK_DAYS: int = CFG.get("lookback_days", 90)
ADMIN_ROLE_IDS: set[int] = set(CFG.get("admin_role_ids", []))
# Where the daily price board lives (defaults to first watch channel)
BOARD_CHANNEL_ID: int | None = CFG.get("board_channel_id") or (WATCH_CHANNELS and next(iter(WATCH_CHANNELS)))
LISTEN_ENABLED: bool = CFG.get("listen_to_trade_chat", True)

# Item type -> board category
CATEGORIES: list[tuple[str, set[str]]] = [
    ("Weapons", {"WEAP"}),
    ("Armor & Clothing", {"ARMO"}),
    ("Materials & Misc", {"MISC", "KEYM", "LIGH"}),
    ("Ingredients", {"INGR"}),
    ("Potions & Poisons", {"ALCH"}),
    ("Soul Gems", {"SLGM"}),
    ("Scrolls", {"SCRL"}),
    ("Ammunition", {"AMMO"}),
    ("Books & Notes", {"BOOK"}),
]
_TYPE_TO_CAT: dict[str, str] = {t: name for name, types in CATEGORIES for t in types}
_NAME_TO_TYPE: dict[str, str] = {it.name: it.type for it in items.items}
PAGE_SIZE = 20

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
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.2f}"


def is_admin(inter: discord.Interaction) -> bool:
    """Manage Server perm OR a configured admin role."""
    perms = inter.user.guild_permissions if inter.guild else None
    if perms and perms.manage_guild:
        return True
    role_ids = {r.id for r in getattr(inter.user, "roles", [])}
    return bool(role_ids & ADMIN_ROLE_IDS)


# --- price board (categorized + paginated) -----------------------------------

def _category_of(item_name: str) -> str:
    return _TYPE_TO_CAT.get(_NAME_TO_TYPE.get(item_name, ""), "Materials & Misc")


async def _board_rows() -> dict[str, list]:
    """Board data grouped by category: {category: [rows]}."""
    rows = await asyncio.to_thread(ledger.board_prices)
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(_category_of(r["item_name"]), []).append(r)
    return grouped


def render_board_page(grouped: dict[str, list], category: str, page: int) -> tuple[str, int]:
    """Render one page. Returns (text, total_pages)."""
    rows = grouped.get(category, [])
    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [f"**OFFICIAL PRICE BOARD — {category}**",
             f"_Cross-server averages • Updated <t:{int(time.time())}:R>_", "",
             "```", f"{'ITEM':<30} {'BUY':>7} {'SELL':>7} {'SRV':>4}", "─" * 50]
    for r in chunk:
        lines.append(
            f"{r['item_name'][:30]:<30} {fmt_gold(r['avg_buy']):>7} {fmt_gold(r['avg_sell']):>7}"
            f" {r['n_servers']:>4}"
        )
    if not chunk:
        lines.append("(nothing priced in this category yet)")
    lines.append("```")
    lines.append(f"_Page {page + 1}/{total_pages} • SRV = servers reporting_")
    return "\n".join(lines), total_pages


class BoardView(discord.ui.View):
    """Persistent view: category dropdown + page buttons."""

    def __init__(self, category: str | None = None, page: int = 0):
        super().__init__(timeout=None)
        self.category = category
        self.page = page
        # category select is rebuilt per render (options depend on data);
        # buttons carry fixed custom_ids so the view survives restarts.

    async def _render(self, interaction: discord.Interaction):
        grouped = await _board_rows()
        if self.category is None or self.category not in grouped:
            self.category = next((c for c, _ in CATEGORIES if c in grouped), "Materials & Misc")
        text, total = render_board_page(grouped, self.category, self.page)
        view = BoardView(self.category, self.page)
        view.rebuild(grouped, total)
        await interaction.response.edit_message(content=text, view=view)

    def rebuild(self, grouped: dict[str, list], total_pages: int):
        self.clear_items()
        cats = [c for c, _ in CATEGORIES if c in grouped]
        if cats:
            select = discord.ui.Select(
                placeholder="Category",
                custom_id="ledger:board:cat",
                options=[discord.SelectOption(label=c, value=c,
                                              default=(c == self.category)) for c in cats],
            )

            async def on_cat(inter: discord.Interaction):
                self.category = select.values[0]
                self.page = 0
                await self._render(inter)

            select.callback = on_cat
            self.add_item(select)

        async def on_prev(inter: discord.Interaction):
            self.page -= 1
            await self._render(inter)

        async def on_next(inter: discord.Interaction):
            self.page += 1
            await self._render(inter)

        prev_btn = discord.ui.Button(label="◀ Prev", custom_id="ledger:board:prev",
                                     disabled=self.page <= 0)
        next_btn = discord.ui.Button(label="Next ▶", custom_id="ledger:board:next",
                                     disabled=self.page >= total_pages - 1)
        prev_btn.callback = on_prev
        next_btn.callback = on_next
        self.add_item(prev_btn)
        self.add_item(next_btn)


async def render_board_message() -> tuple[str, BoardView]:
    grouped = await _board_rows()
    if not any(grouped.values()):
        return ("**OFFICIAL PRICE BOARD**\n_No prices set yet. Admins: `/setprice`._",
                BoardView())
    first_cat = next((c for c, _ in CATEGORIES if c in grouped), "Materials & Misc")
    text, total = render_board_page(grouped, first_cat, 0)
    view = BoardView(first_cat, 0)
    view.rebuild(grouped, total)
    return text, view


async def update_board(channel: discord.TextChannel) -> None:
    text, view = await render_board_message()
    msg_id = ledger.kv_get("board_message_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(content=text, view=view)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    msg = await channel.send(text, view=view)
    ledger.kv_set("board_message_id", str(msg.id))
    try:
        await msg.pin()
    except discord.HTTPException:
        pass


@tasks.loop(hours=24)
async def daily_board():
    channel = client.get_channel(BOARD_CHANNEL_ID) if BOARD_CHANNEL_ID else None
    if channel is None:
        return
    try:
        await update_board(channel)
        log.info("daily board refreshed in #%s", channel.name)
    except Exception:
        log.exception("daily board update failed")


@daily_board.before_loop
async def _wait_ready():
    await client.wait_until_ready()


@client.event
async def on_ready():
    client.add_view(BoardView())  # re-register persistent board buttons
    await tree.sync()
    if not daily_board.is_running():
        daily_board.start()
    log.info("online as %s — %d items loaded, watching %d channels",
             client.user, len(items), len(WATCH_CHANNELS))


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not LISTEN_ENABLED:
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


@tree.command(name="price", description="Official price for an item (falls back to trade-chat average)")
@app_commands.describe(item="Item name (fuzzy matched)")
async def price_cmd(inter: discord.Interaction, item: str):
    await inter.response.defer()
    match = items.match(item)
    if match.item is None:
        sugg = ", ".join(n for n, _ in match.candidates[:3]) or "no close names"
        await inter.followup.send(f"Couldn't resolve **{item}**. Closest: {sugg}")
        return

    avg = await asyncio.to_thread(ledger.average_price, match.item.name)
    if avg is not None:
        e = discord.Embed(title=f"{match.item.name} — cross-server average")
        e.add_field(name="Avg buy (stores pay)", value=fmt_gold(avg["avg_buy"]), inline=True)
        e.add_field(name="Avg sell (stores charge)", value=fmt_gold(avg["avg_sell"]), inline=True)
        e.add_field(name="Servers reporting", value=str(avg["n_servers"]), inline=True)
        if avg["n_servers"] > 1:
            breakdown = "\n".join(
                f"**{name or 'unknown'}** — buy {fmt_gold(b)} / sell {fmt_gold(s)}"
                for name, b, s in avg["servers"]
            )
            e.add_field(name="Per-server", value=breakdown[:1024], inline=False)
        e.set_footer(text="Official prices set by each server's admins • /pricehistory for the audit trail")
        await inter.followup.send(embed=e)
        return

    # fallback: trade-chat market data
    since = int(time.time()) - LOOKBACK_DAYS * 86400
    rows = await asyncio.to_thread(ledger.prices_for, match.item.name, since)
    rep = compute_price_report(match.item.name, rows)

    e = discord.Embed(title=f"{rep.item_name} — market chatter (no official price)")
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
        e.description = "No official price and no recorded trade offers."
    await inter.followup.send(embed=e)


# --- admin: official price management ---------------------------------------

@tree.command(name="setprice", description="Set the official price for an item (admin)")
@app_commands.describe(item="Item name", buy="What the store pays (optional)", sell="What the store charges (optional)")
async def setprice_cmd(inter: discord.Interaction, item: str, buy: float | None = None, sell: float | None = None):
    if not is_admin(inter):
        await inter.response.send_message("Price-setting is restricted to ledger admins.", ephemeral=True)
        return
    if buy is None and sell is None:
        await inter.response.send_message("Give at least one of `buy` or `sell`.", ephemeral=True)
        return
    match = items.match(item)
    if match.item is None:
        sugg = ", ".join(n for n, _ in match.candidates[:3]) or "no close names"
        await inter.response.send_message(f"Couldn't resolve **{item}**. Closest: {sugg}", ephemeral=True)
        return
    await asyncio.to_thread(ledger.set_price,
                            inter.guild_id or 0,
                            inter.guild.name if inter.guild else "DM",
                            match.item.name, buy, sell,
                            str(inter.user), inter.user.id)
    await inter.response.send_message(
        f"✅ **{match.item.name}** — buy {fmt_gold(buy)} / sell {fmt_gold(sell)} (logged)")
    channel = client.get_channel(BOARD_CHANNEL_ID) if BOARD_CHANNEL_ID else None
    if channel:
        try:
            await update_board(channel)
        except Exception:
            log.exception("board refresh after setprice failed")


@tree.command(name="removeprice", description="Remove an official price (admin)")
@app_commands.describe(item="Item name")
async def removeprice_cmd(inter: discord.Interaction, item: str):
    if not is_admin(inter):
        await inter.response.send_message("Price-setting is restricted to ledger admins.", ephemeral=True)
        return
    match = items.match(item)
    name = match.item.name if match.item else item
    removed = await asyncio.to_thread(ledger.remove_price, inter.guild_id or 0, name)
    if removed:
        await inter.response.send_message(f"🗑 Removed official price for **{name}**.")
    else:
        await inter.response.send_message(f"No official price on record for **{name}**.", ephemeral=True)


@tree.command(name="pricehistory", description="Audit trail of official price changes for an item")
@app_commands.describe(item="Item name")
async def pricehistory_cmd(inter: discord.Interaction, item: str):
    match = items.match(item)
    if match.item is None:
        await inter.response.send_message(f"Couldn't resolve **{item}**.", ephemeral=True)
        return
    rows = await asyncio.to_thread(ledger.price_history_for, match.item.name, inter.guild_id)
    if not rows:
        await inter.response.send_message(f"No official price history for **{match.item.name}**.")
        return
    lines = [f"`{time.strftime('%Y-%m-%d', time.localtime(r['ts']))}` "
             f"buy **{fmt_gold(r['buy'])}** / sell **{fmt_gold(r['sell'])}** — {r['set_by']}"
             for r in rows]
    await inter.response.send_message(
        embed=discord.Embed(title=f"{match.item.name} — price changes", description="\n".join(lines)))


@tree.command(name="board", description="Post/refresh the official price board now (admin)")
async def board_cmd(inter: discord.Interaction):
    if not is_admin(inter):
        await inter.response.send_message("Restricted to ledger admins.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    channel = client.get_channel(BOARD_CHANNEL_ID) if BOARD_CHANNEL_ID else inter.channel
    await update_board(channel)
    await inter.followup.send(f"Board refreshed in {channel.mention}.", ephemeral=True)


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
