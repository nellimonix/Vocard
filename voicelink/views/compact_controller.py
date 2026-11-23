from urllib.parse import urlsplit

import discord

from ..utils import format_ms
from .controller import BUTTON_TYPE, ControllerInteractions, StopPlayback


ICON_ROWS = (
    ("shuffle", "volumedown", "volume-readout", "volumeup", "loop"),
    ("rewind", "back", "play-pause", "skip", "forward"),
    ("add-track", "queue", "lyrics", "stop", "leave"),
)
ICONS = {
    "back": ("ym_back_v2", 1574291224532357150, "⏮️"),
    "rewind": ("ym_forward_back_v2", 1574291239279534192, "⏪"),
    "pause": ("ym_pause_v2", 1574291222640594944, "⏸️"),
    "play": ("ym_play_v2", 1574292346986561679, "▶️"),
    "forward": ("ym_forward_next30_v2", 1574295953220042752, "⏩"),
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


def compact_icon(bot, action):
    name, _, fallback = ICONS[action]
    if registered := getattr(bot, "compact_app_emojis", {}).get(name):
        return f"<:{name}:{registered}>"
    return fallback


def button_settings(bot, action):
    if action == "play-pause":
        return {"states": {"pause": {"emoji": compact_icon(bot, "pause"), "style": "primary"},
                           "resume": {"emoji": compact_icon(bot, "play"), "style": "primary"}}}
    icon = compact_icon(bot, action)
    if action == "loop":
        return {"states": {state: {"emoji": icon, "style": style} for state, style in
                           (("off", "secondary"), ("track", "primary"), ("queue", "success"))}}
    return {"emoji": icon, "style": "danger" if action == "leave" else "secondary"}


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
        header = discord.ui.TextDisplay(f"### [{title}](<{url}>)" if url else f"### {title}")
        status = self.player.get_msg("player.playback.nowplayingDesc").splitlines()[0].replace("**", "")
        artist = _metadata(track.author)
        lines = [discord.ui.TextDisplay(f"-# {status}"), header]
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
                if name == "volume-readout":
                    self.volume_readout = discord.ui.Button(label=f"{self.player.volume}%", disabled=True,
                                                            style=discord.ButtonStyle.secondary)
                    row.add_item(self.volume_readout)
                    continue
                if name == "add-track":
                    row.add_item(discord.ui.Button(emoji=compact_icon(self.player.bot, name), disabled=True,
                                                   style=discord.ButtonStyle.success))
                    continue
                button = StopPlayback if name == "stop" else BUTTON_TYPE[name]
                item = button(player=self.player, btn_data=button_settings(self.player.bot, name), icon_only=True)
                if name in ("forward", "rewind") and (not self.player.current or not self.player.current.is_seekable):
                    item.disabled = True
                row.add_item(item)
            self.frame.add_item(row)

    def readout_text(self, position=None):
        track = self.player.current
        entries = [f"{compact_icon(self.player.bot, 'queue')} {self.player.queue.count}"]
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
            bars = round(14 * elapsed / track.length)
            times = f"{format_ms(elapsed)}  {'▰' * bars}{'▱' * (14 - bars)}  {format_ms(track.length)}"
        else:
            times = f"`{format_ms(elapsed)}`"
        return times + "\n-# " + "  ·  ".join(entries)

    def refresh_readout(self, position=None):
        self.readout.content = self.readout_text(position)
        self.volume_readout.label = f"{self.player.volume}%"
