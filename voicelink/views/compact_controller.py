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
READOUT_ICONS = {
    "ym_yamusic": 1374258185359196191,
    "ym_start_100_v2": 1574308845839327282,
    "ym_mid_100_v2": 1574308847638679592,
    "ym_end_100_v2": 1574308849337376798,
    "ym_start_0_v2": 1574308983395590296,
    "ym_mid_0_v2": 1574308985421566013,
    "ym_end_0_v2": 1574308987183046798,
}
PROGRESS_SEGMENTS = ("start", *("mid",) * 8, "end")


def _application_icon(bot, name):
    if registered := getattr(bot, "compact_app_emojis", {}).get(name):
        return f"<:{name}:{registered}>"
    return ""


def compact_icon(bot, action):
    name, _, fallback = ICONS[action]
    export_name = f"{name.removesuffix('_v2')}_x125"
    return _application_icon(bot, export_name) or _application_icon(bot, name) or fallback


def _progress_bar(bot, elapsed, duration):
    segments = len(PROGRESS_SEGMENTS)
    filled = round(segments * elapsed / duration)
    icons = [_application_icon(bot, f"ym_{segment}_{100 if index < filled else 0}_v2")
             for index, segment in enumerate(PROGRESS_SEGMENTS)]
    if all(icons):
        return "".join(icons)
    return "▰" * filled + "▱" * (segments - filled)


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
        self.add_item(discord.ui.Container(line))


class CompactController(ControllerInteractions, discord.ui.LayoutView):
    def __init__(self, player, embed):
        super().__init__(timeout=None)
        self.bind_player(player)
        self.frame = discord.ui.Container()
        self.add_item(self.frame)
        self._add_track_header(embed)
        self.progress = None
        if track := self.player.current:
            self.progress = discord.ui.TextDisplay(self.progress_text(track))
            self.frame.add_item(self.progress)
            self.frame.add_item(discord.ui.Separator(visible=False))
        self.metadata = discord.ui.TextDisplay(self.metadata_text())
        self.frame.add_item(self.metadata)
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

    def metadata_text(self):
        track = self.player.current
        entries = [f"{self.player.queue.count} в очереди"]
        if track is None:
            return "-# " + entries[0]
        if track.source == "yandexmusic":
            icon = _application_icon(self.player.bot, "ym_yamusic")
            entries.insert(0, f"{icon} Яндекс Музыка".lstrip())
        elif track.source:
            entries.insert(0, _metadata(track.source))
        if track.requester:
            entries.append(f"Добавлен: {track.requester.mention}")
        return "-# " + "  ·  ".join(entries)

    def progress_text(self, track, position=None):
        elapsed = max(0, self.player.position if position is None else position)
        if track.is_stream:
            return self.player.get_msg("common.status.live")
        if not track.length:
            return f"`{format_ms(elapsed)}`"
        elapsed = min(track.length, elapsed)
        bar = _progress_bar(self.player.bot, elapsed, track.length)
        return f"{format_ms(elapsed)}  {bar}  {format_ms(track.length)}"

    def refresh_readout(self, position=None):
        if self.progress is not None and (track := self.player.current):
            self.progress.content = self.progress_text(track, position)
        self.metadata.content = self.metadata_text()
        self.volume_readout.label = f"{self.player.volume}%"
