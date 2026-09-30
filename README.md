# daedric-ledger

Discord bot that monitors Daedric Online trading channels and maintains a
community price ledger: every WTS/WTB post parsed, stored, and rolled into
decay-weighted average prices per item.

## Stack

- Python 3.13+, discord.py (slash commands), SQLite (zero-config storage)
- Item database dumped straight from the live install's plugins (ESM/ESP/ESL)
  — includes all 115 mods in the Daedric Online modlist, so modded items are
  first-class citizens.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Dump the item database from the live game install
python tools/dump_items.py \
    --data-dir "$HOME/.local/share/Steam/steamapps/common/Skyrim Special Edition/Data" \
    --out data/items.json

# 2. Configure
cp config.example.json config.json   # fill in token + channel IDs

# 3. Run
python bot.py
```

Discord side: create an app at discord.com/developers, enable the
**Message Content** intent, invite with `bot` + `applications.commands`
scopes and View Channels / Read Message History permissions.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/price <item>` | everyone | Official price (buy/sell) if set; falls back to trade-chat average |
| `/history <item>` | everyone | Last 10 recorded trade-chat offers |
| `/top` | everyone | Most traded items, 7 days |
| `/pricehistory <item>` | everyone | Audit trail of official price changes (who/when/what) |
| `/setprice <item> [buy] [sell]` | admins | Set official price — logged to the audit trail, board auto-refreshes |
| `/removeprice <item>` | admins | Remove an official price |
| `/board` | admins | Force-post/refresh the pinned price board |
| `/unmatched` | admins | Item texts the matcher couldn't resolve — feed these into aliases |

"Admins" = Manage Server permission, or any role in `admin_role_ids` in config.json.

## Price board

The bot keeps a single **pinned, self-editing price board** message in the
board channel (default: first watch channel). It updates on every `/setprice`
and re-renders every 24 hours. `tools/import_sheets.py` seeds official prices
from admin-posted price sheets already in the channel.

## Design notes

- **Decay weighting**: 14-day half-life; recent trades count more.
- **Outlier rejection**: IQR fence — a troll posting "wts sword 1 gold" can't
  crater the average.
- **Thin-market honesty**: every `/price` reports sample size; fewer than 3
  offers gets a "this is a rumor, not a law" warning.
- **Dedupe**: Discord snowflake UNIQUE constraint makes restarts and history
  backfills idempotent.
- `/unmatched` is the maintenance loop: it surfaces slang the matcher missed,
  which goes into `ALIASES` in `ledger/items.py`.

## Credit

Made by synth with blackclaw

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```
