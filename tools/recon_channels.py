#!/usr/bin/env python3
"""One-shot recon: list guilds and text channels the bot can see."""
import asyncio
import json
from pathlib import Path

import discord

CFG = json.loads((Path(__file__).parent.parent / "config.json").read_text())


async def main():
    client = discord.Client(intents=discord.Intents.default())

    @client.event
    async def on_ready():
        print(f"online as {client.user} (id {client.user.id})")
        for g in client.guilds:
            print(f"\nGUILD: {g.name} ({g.id})  members={g.member_count}")
            for ch in g.text_channels:
                perms = ch.permissions_for(g.me)
                if perms.read_messages:
                    print(f"  #{ch.name:<28} {ch.id}")
        await client.close()

    await client.start(CFG["token"])


asyncio.run(main())
