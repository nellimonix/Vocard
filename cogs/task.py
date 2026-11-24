"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import asyncio

import voicelink
import discord
import function as func
from aiohttp import ClientConnectionError

from voicelink.config import Config
from discord.ext import commands, tasks

class Task(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.current_act = 0
        self._last_presence = None
        self.placeholder = voicelink.BotPlaceholder(bot)
        self.activity_update.start()
        self.cache_cleaner.start()

    def cog_unload(self):
        self.activity_update.cancel()
        self.cache_cleaner.cancel()

    @tasks.loop(seconds=Config().timer_settings.get("bot_activity_update", 600))
    async def activity_update(self):
        await self.bot.wait_until_ready()

        try:
            activities = Config().activity
            if not activities:
                return
            act_data = activities[self.current_act % len(activities)]
            act_type = getattr(discord.ActivityType, act_data.get("type", "").lower(), discord.ActivityType.playing)
            status_type = getattr(discord.Status, act_data.get("status", "").lower(), None)

            for delay in (0, 5, 15):
                if delay:
                    await asyncio.sleep(delay)
                act_name = self.placeholder.replace(act_data.get("name", ""))
                presence = (act_type, act_name, status_type)
                if presence != self._last_presence:
                    try:
                        async with asyncio.timeout(15):
                            await self.bot.change_presence(activity=discord.Activity(type=act_type, name=act_name), status=status_type)
                    except (ClientConnectionError, discord.ConnectionClosed, TimeoutError):
                        self._last_presence = None
                        continue
                    self._last_presence = presence
                    func.logger.info(f"Changed the bot status to {act_name}")

                self.current_act = (self.current_act + 1) % len(activities)
                return

            func.logger.warning("Discord Gateway was unavailable during the presence update")
        except Exception as e:
            func.logger.error("Error occurred while changing the bot status!", exc_info=e)

    @tasks.loop(seconds=Config().timer_settings.get("cache_cleanup", 60))
    async def cache_cleaner(self):
        await voicelink.MongoDBHandler.cleanup_cache()

async def setup(bot: commands.Bot):
    await bot.add_cog(Task(bot))
