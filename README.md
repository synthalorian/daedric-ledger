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

| Command | What it does |
|---|---|
| `/price <item>` | Decay-weighted average (sell ask / buy bid / midpoint) with sample size |
| `/history <item>` | Last 10 recorded offers |
| `/top` | Most traded items, 7 days |
| `/unmatched` | (admin) Item texts the matcher couldn't resolve — feed these into aliases |

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
