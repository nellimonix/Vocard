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

import discord
from discord.ext import commands

from ..config import Config
from ..language import LangHandler
from ..mongodb import MongoDBHandler

def _get_lang(guild_id: int, *keys):
    settings = MongoDBHandler.get_cached_settings(guild_id)
    return LangHandler._get_lang(settings.get("lang", LangHandler._default_lang), *keys)


_HELP_FALLBACK = {
    "helpSelectCategory": "Select Category!",
    "helpNewsLabel": "News",
    "helpNewsDesc": "View new updates of YaMusic.",
    "helpTutorialLabel": "Tutorial",
    "helpTutorialDesc": "How to use YaMusic.",
    "helpCategoryLabel": "{0} Commands",
    "helpCategoryDesc": "This is {0} Category.",
    "helpMenuTitle": "YaMusic Help Menu",
    "helpAvailableCategories": "Available Categories: [{0}]",
    "helpInformation": "📰 Information:",
    "helpNewsInfo": "YaMusic is a simple music bot. It leads to a comfortable experience which is user-friendly, It supports YouTube, Soundcloud, Spotify, Twitch and more!",
    "helpGetStarted": "Get Started",
    "helpGetStartedValue": "```Join a voice channel and /play {Song/URL} a song. (Names, Youtube Video Links or Playlist links or Spotify links are supported on YaMusic)```",
    "helpCategoryTitle": "Category: {0}",
    "helpCategories": "Categories: [{0}]",
    "helpTutorialDescFull": "How can use YaMusic? Some simple commands you should know now after watching this video.",
    "helpCategoryCommands": "{0} Commands: [{1}]",
    "helpDashboardBtn": "Панель управления",
    "helpDonateBtn": "Donate",
}

def _get_text(guild_id: int, key: str, *args) -> str:
    value = _get_lang(guild_id, key) if guild_id else None
    if not value or value == "Not found!":
        value = _HELP_FALLBACK.get(key, key)
    if args:
        try:
            return value.format(*args)
        except Exception:
            return value
    return value

class HelpDropdown(discord.ui.Select):
    def __init__(self, categories: list, guild_id: int = 0):
        self.view: HelpView

        options = [
            discord.SelectOption(
                emoji="🆕",
                label=_get_text(guild_id, "helpNewsLabel"),
                value="News",
                description=_get_text(guild_id, "helpNewsDesc")
            ),
            discord.SelectOption(
                emoji="🕹️",
                label=_get_text(guild_id, "helpTutorialLabel"),
                value="Tutorial",
                description=_get_text(guild_id, "helpTutorialDesc")
            ),
        ]
        for category, emoji in zip(categories, ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]):
            options.append(
                discord.SelectOption(
                    emoji=emoji,
                    label=_get_text(guild_id, "helpCategoryLabel", category),
                    value=category,
                    description=_get_text(guild_id, "helpCategoryDesc", category.lower())
                )
            )

        super().__init__(
            placeholder=_get_text(guild_id, "helpSelectCategory"),
            min_values=1, max_values=1,
            options=options,
            custom_id="select"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        embed = self.view.build_embed(self.values[0])
        await interaction.response.edit_message(embed=embed)

class HelpView(discord.ui.View):
    def __init__(self, bot: commands.Bot, author: discord.Member) -> None:
        super().__init__(timeout=60)

        self.author: discord.Member = author
        self.bot: commands.Bot = bot
        self.response: discord.Message = None
        self.categories: list[str] = [ name.capitalize() for name, cog in bot.cogs.items() if len([c for c in cog.walk_commands()]) ]

        guild_id = self.author.guild.id if self.author.guild else 0

        self.add_item(discord.ui.Button(
            label=_get_text(guild_id, "helpDashboardBtn"),
            emoji='🌎',
            url='https://dashboard.yamusic.app'
        ))
        self.add_item(discord.ui.Button(
            label=_get_text(guild_id, "helpDonateBtn"),
            emoji=':patreon:913397909024800878',
            url='https://pay.cloudtips.ru/p/d6bf9f92'
        ))
        self.add_item(HelpDropdown(self.categories, guild_id))

    async def on_timeout(self) -> None:
        for child in self.children:
            if child.custom_id == "select":
                child.disabled = True
        try:
            await self.response.edit(view=self)
        except:
            pass


    async def on_timeout(self) -> None:
        for child in self.children:
            if child.custom_id == "select":
                child.disabled = True
        try:
            await self.response.edit(view=self)
        except:
            pass

    async def interaction_check(self, interaction: discord.Interaction) -> None:
        return interaction.user == self.author

    def build_embed(self, category: str, guild_id: int = None) -> discord.Embed:
        if guild_id is None:
            guild_id = self.author.guild.id if self.author.guild else 0

        news_label = _get_text(guild_id, "helpNewsLabel")
        tutorial_label = _get_text(guild_id, "helpTutorialLabel")

        category = category.lower()
        if category == "news":
            embed = discord.Embed(
                title=_get_text(guild_id, "helpMenuTitle"),
                url="https://discord.com/channels/811542332678996008/811909963718459392/1069971173116481636",
                color=Config().embed_color
            )
            embed.add_field(
                name=_get_text(guild_id, "helpAvailableCategories", 2 + len(self.categories)),
                value="```py\n👉 {0}\n2. {1}\n{2}```".format(
                    news_label,
                    tutorial_label,
                    "".join(f"{i}. {c}\n" for i, c in enumerate(self.categories, start=3))
                ),
                inline=True
            )

            update = _get_text(guild_id, "helpNewsInfo")
            embed.add_field(name=_get_text(guild_id, "helpInformation"), value=update, inline=True)
            embed.add_field(
                name=_get_text(guild_id, "helpGetStarted"),
                value=_get_text(guild_id, "helpGetStartedValue"),
                inline=False
            )

            return embed

        embed = discord.Embed(
            title=_get_text(guild_id, "helpCategoryTitle", category.capitalize()),
            color=Config().embed_color
        )

        category_list = [news_label, tutorial_label] + self.categories
        current_display = category.capitalize()
        current_localized = {
            "news": news_label,
            "tutorial": tutorial_label,
        }.get(category, current_display)

        embed.add_field(
            name=_get_text(guild_id, "helpCategories", 2 + len(self.categories)),
            value="```py\n" + "\n".join(
                ("👉 " if c == current_localized else f"{i}. ") + c
                for i, c in enumerate(category_list, start=1)
            ) + "```",
            inline=True
        )

        if category == 'tutorial':
            embed.description = _get_text(guild_id, "helpTutorialDescFull")
            embed.set_image(url="https://cdn.discordapp.com/attachments/674788144931012638/917656288899514388/final_61aef3aa7836890135c6010c_669380.gif")
        else:
            cog = [c for _, c in self.bot.cogs.items() if _.lower() == category][0]

            commands = [command for command in cog.walk_commands()]
            embed.description = _get_text(guild_id, cog.description) if cog.description.startswith('cogDesc') else cog.description
            embed.add_field(
                name=_get_text(guild_id, "helpCategoryCommands", category.capitalize(), len(commands)),
                value="```{}```".format("".join(f"/{command.qualified_name}\n" for command in commands if not command.qualified_name == cog.qualified_name))
            )

        return embed
