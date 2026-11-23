from urllib.parse import urlsplit

import discord

from ..config import Config
from ..exceptions import VoicelinkException
from ..language import LangHandler
from ..utils import dispatch_message, format_ms, send_localized_message
from .controller import ADD_TRACK_ID, BUTTON_TYPE, ControlButton, ControllerInteractions, StopPlayback


ICON_ROWS = (
    ("back", "rewind", "play-pause", "forward", "skip"),
    ("volumedown", "volumeup", "shuffle", "loop"),
    ("queue", "add-track", "lyrics", "stop", "leave"),
)
ICONS = {
    "back": ("ym_back_v2", 1574291224532357150, "⏮️"),
    "rewind": ("ym_forward_back_v2", 1574291239279534192, "⏪"),
    "pause": ("ym_pause_v2", 1574291222640594944, "⏸️"),
    "play": ("ym_play_v2", 1574292346986561679, "▶️"),
    "forward": ("ym_forward_next_v2", 1574291233734398032, "⏩"),
    "skip": ("ym_next_v2", 1574291226331578409, "⏭️"),
    "volumedown": ("ym_volume_down_v2", 1574291243880419429, "🔉"),
    "volumeup": ("ym_volume_up_v2", 1574291245667451022, "🔊"),
    "shuffle": ("ym_shuffle_v2", 1574291247248576512, "🔀"),
    "loop": ("ym_repeat_v2", 1574291228252708874, "🔁"),
    "queue": ("ym_playlist_v2", 1574291235575697559, "📋"),
    "add-track": ("ym_plus_v2", 1574291231821926461, "➕"),
    "lyrics": ("ym_lyrics_v2", 1574291237660401674, "📜"),
    "stop": ("ym_stop_v2", 1574291241082953748, "⏹️"),
    "leave": ("ym_logout_v2", 1574291229980762162, "🚪"),
}


class AddTrackModal(discord.ui.Modal):
    def __init__(self, player, locale):
        super().__init__(title="/play")
        self.player = player
        key = "basic.play.query"
        fallback = LangHandler._local_langs.get("EN-US", {}).get(key, "Enter a query or a searchable link.")
        label = LangHandler._local_langs.get(str(locale).upper(), {}).get(key, fallback)
        self.query = discord.ui.TextInput(label=label if len(label) <= 45 else fallback, max_length=1024)
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        from cogs.basic import enqueue_search

        player = self.player
        if player._closing or not player.channel or interaction.guild.voice_client is not player:
            return await send_localized_message(interaction, "player.errors.noPlayer", ephemeral=True)
        if interaction.user.id not in Config().bot_access_user and not player.is_user_join(interaction.user):
            return await send_localized_message(interaction, "voice.connection.notInChannel", interaction.user.mention,
                                                player.channel.mention, ephemeral=True)
        query = self.query.value.strip()
        if not query:
            return await send_localized_message(interaction, "player.errors.noTrackFound", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            was_playing = player.is_playing
            await enqueue_search(interaction, player, query, interaction.user, ephemeral=True)
            if was_playing and player.is_playing:
                await player.invoke_controller()
        except VoicelinkException as error:
            await dispatch_message(interaction, str(error), ephemeral=True)


class OpenAddTrack(ControlButton):
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddTrackModal(self.player, interaction.locale))


def compact_icon(bot, action):
    name, emoji_id, fallback = ICONS[action]
    if emoji_id in getattr(bot, "compact_app_emojis", ()):
        return f"<:{name}:{emoji_id}>"
    return fallback


def button_settings(bot, action):
    if action == "play-pause":
        return {"states": {"pause": {"emoji": compact_icon(bot, "pause"), "style": "primary"},
                           "resume": {"emoji": compact_icon(bot, "play"), "style": "primary"}}}
    icon = compact_icon(bot, action)
    if action == "loop":
        return {"states": {state: {"emoji": icon, "style": style} for state, style in
                           (("off", "secondary"), ("track", "primary"), ("queue", "success"))}}
    return {"emoji": icon, "style": {"leave": "danger", "add-track": "success"}.get(action, "secondary")}


def _https_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(char in value for char in "<>\n\r"):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _metadata(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(" ".join((value or "").split())[:180]))


def _idle_text(embed, fallback):
    return "\n".join(value for value in (embed.title, embed.description) if value)[:3500] or fallback


class CompactIdle(discord.ui.LayoutView):
    def __init__(self, embed, fallback):
        super().__init__(timeout=None)
        line = discord.ui.TextDisplay(_idle_text(embed, fallback))
        self.add_item(discord.ui.Container(line, accent_colour=embed.colour))


class CompactController(ControllerInteractions, discord.ui.LayoutView):
    def __init__(self, player, embed):
        super().__init__(timeout=None)
        self.bind_player(player)
        self.frame = discord.ui.Container(accent_colour=embed.colour)
        self.add_item(self.frame)
        self._add_track_header(embed)
        self.readout = discord.ui.TextDisplay(self.readout_text())
        self.frame.add_item(self.readout)
        self.frame.add_item(discord.ui.Separator())
        self._add_controls()

    def _add_track_header(self, embed):
        track = self.player.current
        if track is None:
            self.frame.add_item(discord.ui.TextDisplay(_idle_text(embed, self.player.get_msg("player.errors.noTrackPlaying"))))
            return
        title = _metadata(track.title) or "Unknown"
        url = _https_url(track.uri)
        header = discord.ui.TextDisplay(f"**[{title}](<{url}>)**" if url else f"**{title}**")
        artist = _metadata(track.author)
        lines = [header]
        if artist:
            lines.append(discord.ui.TextDisplay(artist))
        if artwork := _https_url(track.thumbnail):
            self.frame.add_item(discord.ui.Section(*lines, accessory=discord.ui.Thumbnail(artwork, description=title)))
        else:
            for line in lines:
                self.frame.add_item(line)

    def _add_controls(self):
        for names in ICON_ROWS:
            row = discord.ui.ActionRow()
            for name in names:
                button = {"stop": StopPlayback, "add-track": OpenAddTrack}.get(name) or BUTTON_TYPE[name]
                kwargs = {"custom_id": ADD_TRACK_ID} if name == "add-track" else {}
                item = button(player=self.player, btn_data=button_settings(self.player.bot, name), icon_only=True, **kwargs)
                if name in ("forward", "rewind") and (not self.player.current or not self.player.current.is_seekable):
                    item.disabled = True
                row.add_item(item)
            self.frame.add_item(row)

    def readout_text(self, position=None):
        track = self.player.current
        entries = [f"🔊 {self.player.volume}%", f"📋 {self.player.queue.count}"]
        if track is None:
            return "-# " + "  ·  ".join(entries)
        if track.source:
            entries.insert(0, _metadata(track.source))
        if track.requester:
            entries.append(track.requester.mention)
        elapsed = max(0, self.player.position if position is None else position)
        if track.is_stream:
            times = self.player.get_msg("common.status.live")
        elif track.length:
            elapsed = min(track.length, elapsed)
            bars = round(12 * elapsed / track.length)
            times = f"`{format_ms(elapsed)}`  {'━' * bars}{'─' * (12 - bars)}  `{format_ms(track.length)}`"
        else:
            times = f"`{format_ms(elapsed)}`"
        return times + "\n-# " + "  ·  ".join(entries)

    def refresh_readout(self, position=None):
        self.readout.content = self.readout_text(position)
