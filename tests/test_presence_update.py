import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import _bootstrap
import discord
from aiohttp import ClientConnectionResetError
from discord.ext import commands

from cogs.task import Task
from voicelink import Config


class PresenceUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(wait_until_ready=AsyncMock(), change_presence=AsyncMock())
        self.task = SimpleNamespace(bot=self.bot, current_act=0, _last_presence=None,
                                    placeholder=SimpleNamespace(replace=Mock(side_effect=lambda value: value)))

    async def test_gateway_reset_retries_with_a_fresh_player_count(self):
        self.bot.change_presence.side_effect = [ClientConnectionResetError(), None]
        self.task.placeholder.replace.side_effect = ["56 Players", "36 Players"]
        with patch.object(Config(), "activity", [{"name": "@@players@@ Players"}, {"name": "Music"}]), \
             patch("cogs.task.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await Task.activity_update.coro(self.task)

        sleep.assert_awaited_once_with(5)
        self.assertEqual([entry.kwargs["activity"].name for entry in self.bot.change_presence.await_args_list],
                         ["56 Players", "36 Players"])
        self.assertEqual(self.task._last_presence[1], "36 Players")
        self.assertEqual(self.task.current_act, 1)

    async def test_exhausted_retries_leave_the_status_pending_for_the_next_cycle(self):
        self.bot.change_presence.side_effect = [ClientConnectionResetError()] * 3 + [None]
        with patch.object(Config(), "activity", [{"name": "Music"}, {"name": "More music"}]), \
             patch("cogs.task.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await Task.activity_update.coro(self.task)
            self.assertEqual(self.task.current_act, 0)
            self.assertIsNone(self.task._last_presence)
            await Task.activity_update.coro(self.task)

        self.assertEqual(sleep.await_args_list, [call(5), call(15)])
        self.assertEqual(self.bot.change_presence.await_count, 4)
        self.assertEqual(self.task._last_presence[1], "Music")
        self.assertEqual(self.task.current_act, 1)

    async def test_repeated_activity_does_not_stop_rotation(self):
        activities = [{"name": "Music"}, {"name": "Music"}, {"name": "36 Players"}]
        with patch.object(Config(), "activity", activities):
            for _ in activities:
                await Task.activity_update.coro(self.task)

        self.assertEqual([entry.kwargs["activity"].name for entry in self.bot.change_presence.await_args_list],
                         ["Music", "36 Players"])
        self.assertEqual(self.task.current_act, 0)

    async def test_task_extension_can_be_reloaded_in_the_same_bot(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        async with bot:
            await bot.load_extension("cogs.task")
            previous = bot.get_cog("Task")
            await bot.reload_extension("cogs.task")
            current = bot.get_cog("Task")
            await asyncio.sleep(0)
            self.assertIsNot(current, previous)
            self.assertFalse(previous.activity_update.is_running())
            self.assertTrue(current.activity_update.is_running())
