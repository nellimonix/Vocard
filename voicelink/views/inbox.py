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

import discord, time
from ..config import Config
from ..language import LangHandler
from ..mongodb import MongoDBHandler

from typing import Any

def _get_lang(guild_id: int, *keys):
    settings = MongoDBHandler.get_cached_settings(guild_id)
    return LangHandler._get_lang(settings.get("lang", LangHandler._default_lang), *keys)


class Select_message(discord.ui.Select):
    def __init__(self, inbox, guild_id: int = 0):
        self.view: InboxView
        options = [discord.SelectOption(label=f"{index}. {mail['title'][:50]}", description=mail['type'], emoji='✉️' if mail['type'] == 'invite' else '📢') for index, mail in enumerate(inbox, start=1) ]

        placeholder = _get_lang(guild_id, "inboxSelectPlaceholder") if guild_id else "Select a message to view .."

        super().__init__(
            placeholder=placeholder,
            options=options, custom_id='select'
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.current = self.view.inbox[int(self.values[0].split(". ")[0]) - 1]
        await self.view.button_change(interaction)

class InboxView(discord.ui.View):
    def __init__(self, author: discord.Member, inbox: list[dict[str, Any]]):
        super().__init__(timeout=60)
        self.inbox: list[dict[str, Any]] = inbox
        self.new_playlist = []

        self.author: discord.Member = author
        self.response: discord.Message = None
        self.current = None

        self.add_item(Select_message(inbox, author.guild.id if author.guild else 0))

        self._apply_button_labels()

    def _apply_button_labels(self) -> None:
        labels = _get_lang(
            self.author.guild.id if self.author.guild else 0,
            "inboxBtnAccept", "inboxBtnDismiss", "inboxBtnSave"
        )
        fallback = ["Accept", "Dismiss", "Click Me To Save The Changes"]
        mapping = {"accept": 0, "dismiss": 1, "save": 2}

        for child in self.children:
            custom_id = getattr(child, "custom_id", None)
            if custom_id in mapping:
                idx = mapping[custom_id]
                value = labels[idx] if labels and labels[idx] != "Not found!" else fallback[idx]
                child.label = value

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.response.edit(view=self)
        except:
            pass

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user == self.author

    def build_embed(self) -> discord.Embed:
        texts = _get_lang(
            self.author.guild.id if self.author.guild else 0,
            "inboxTitle", "inboxMaxMessages", "inboxColId", "inboxColTitle", "inboxMessageInfo",
            "inboxSenderId", "inboxPlaylistId", "inboxInviteTime"
        )
        fallback = [
            "📭 All {0}'s Inbox",
            "Max Messages: {0}/10",
            "ID:",
            "Title:",
            "Message Info:",
            "Sender ID: {0}",
            "Playlist ID: {0}",
            "Invite Time: {0}"
        ]
        texts = [texts[i] if texts and texts[i] != "Not found!" else fallback[i] for i in range(len(fallback))]

        embed=discord.Embed(
            title=texts[0].format(self.author.display_name),
            description=texts[1].format(len(self.inbox)) + '```%0s %2s %20s\n' % ("   ", texts[2], texts[3]) + '\n'.join('%0s %2s. %35s'% ('✉️' if mail['type'] == 'invite' else '📢', index, mail['title'][:35] + "...") for index, mail in enumerate(self.inbox, start=1)) + '```',
            color=Config().embed_color
        )

        if self.current:
            info_value = (
                f"```{self.current['description']}\n"
                f"{texts[5].format(self.current['sender'])}\n"
                f"{texts[6].format(self.current['referId'])}\n"
                f"{texts[7].format(time.strftime('%d-%m %H:%M:%S', time.gmtime(int(self.current['time']))))}```"
            )
            embed.add_field(name=texts[4], value=info_value)
        return embed

    async def button_change(self, interaction: discord.Interaction):
        for child in self.children:
            if child.custom_id in ['accept', 'dismiss']:
                child.disabled = True if self.current is None else False
            elif child.custom_id == "select":
                child.options = [discord.SelectOption(label=f"{index}. {mail['title'][:50]}", description=mail['type'], emoji='✉️' if mail['type'] == 'invite' else '📢') for index, mail in enumerate(self.inbox, start=1) ]

        if not self.inbox:
            await interaction.response.edit_message(embed=self.build_embed(), view=None)
            return self.stop()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.response.edit(view=self)
        except:
            pass

    @discord.ui.button(label='Accept', style=discord.ButtonStyle.green, custom_id="accept", disabled=True)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.new_playlist.append(self.current)
        self.inbox.remove(self.current)
        self.current = None
        await self.button_change(interaction)

    @discord.ui.button(label='Dismiss', style=discord.ButtonStyle.red, custom_id="dismiss", disabled=True)
    async def dismiss_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.inbox.remove(self.current)
        self.current = None
        await self.button_change(interaction)

    @discord.ui.button(label='Click Me To Save The Changes', style=discord.ButtonStyle.blurple, custom_id="save")
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.response.edit(view=None)
        self.stop()
