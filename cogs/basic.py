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

import re
import asyncio
import discord
import voicelink

from io import StringIO
from validators import url
from discord import app_commands
from discord.ext import commands
from function import (
    cooldown_check,
    get_aliases,
    logger
)

from voicelink import MongoDBHandler, LangHandler, Config
from voicelink.views import SearchView, QueueView, LinkView, LyricsView, HelpView
from voicelink.utils import format_ms, format_to_ms, truncate_string, dispatch_message, send_localized_message

async def nowplay(ctx: commands.Context, player: voicelink.Player):
    track = player.current
    if not track:
        return await send_localized_message(ctx, 'player.errors.noTrackPlaying', ephemeral=True)

    texts = await LangHandler.get_lang(ctx.guild.id, "player.playback.nowplayingDesc", "player.playback.nowplayingField", "player.playback.nowplayingLink")
    upnext = "\n".join(f"`{index}.` `[{track.formatted_length}]` [{truncate_string(track.title)}]({track.uri})" for index, track in enumerate(player.queue.tracks()[:2], start=2))

    embed = discord.Embed(description=texts[0].format(track.title), color=Config().embed_color)
    embed.set_author(
        name=track.requester.display_name,
        icon_url=track.requester.display_avatar.url
    )
    embed.set_thumbnail(url=track.thumbnail)

    if upnext:
        embed.add_field(name=texts[1], value=upnext)

    pbar = "".join(":radio_button:" if i == round(player.position // round(track.length // 15)) else "▬" for i in range(15))
    icon = ":red_circle:" if track.is_stream else (":pause_button:" if player.is_paused else ":arrow_forward:")
    embed.add_field(name="\u2800", value=f"{icon} {pbar} **[{format_ms(player.position)}/{track.formatted_length}]**", inline=False)

    return await dispatch_message(ctx, embed, view=LinkView(texts[2].format(track.source.title()), track.emoji, track.uri))

class Basic(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.description = "cogDescBasic"
        self.ctx_menu = app_commands.ContextMenu(
            name="play",
            callback=self._play
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    async def help_autocomplete(self, interaction: discord.Interaction, current: str) -> list:
        return [app_commands.Choice(name=c.capitalize(), value=c) for c in self.bot.cogs if c not in ["Nodes", "Task"] and current in c]

    async def play_autocomplete(self, interaction: discord.Interaction, current: str) -> list:
        if voicelink.pool.URL_REGEX.match(current):
            return []

        if current:
            node = voicelink.NodePool.get_node()
            if not node:
                return []

            try:
                tracks = await asyncio.wait_for(node.get_tracks(current, requester=interaction.user), timeout=2)
            except (asyncio.TimeoutError, voicelink.VoicelinkException):
                return []
            if not tracks:
                return []

            if isinstance(tracks, voicelink.Playlist):
                tracks = tracks.tracks

            return [app_commands.Choice(name=truncate_string(f"🎵 [{track.formatted_length}] {track.author} - {track.title}", 100), value=track.uri) for track in tracks if track.uri and len(track.uri) <= 100][:25]

        history = {track["identifier"]: track for track_id in reversed(await MongoDBHandler.get_user(interaction.user.id, d_type="history")) if (track := voicelink.Track.decode(track_id))["uri"]}
        return [app_commands.Choice(name=truncate_string(f"🕒 [{format_ms(track['length'])}] {track['author']} - {track['title']}", 100), value=track['uri']) for track in history.values() if len(track['uri']) <= 100][:25]

    @commands.hybrid_command(name='connect', aliases=get_aliases('connect'), description=app_commands.locale_str('Connect to a voice channel.', key='basic.connect'))
    @app_commands.describe(channel=app_commands.locale_str('Provide a channel to connect to.', key='basic.connect.channel'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def connect(self, ctx: commands.Context, channel: discord.VoiceChannel = None) -> None:
        'Connect to a voice channel.'
        try:
            player = await voicelink.connect_channel(ctx, channel)
        except discord.errors.ClientException:
            return await send_localized_message(ctx, "voice.connection.alreadyConnected")

        await send_localized_message(ctx, "voice.connection.connect", player.channel)

    @commands.hybrid_command(name='play', aliases=get_aliases('play'), description=app_commands.locale_str('Loads your input and adds it to the queue.', key='basic.play'))
    @app_commands.describe(query=app_commands.locale_str('Enter a query or a searchable link.', key='basic.play.query'), start=app_commands.locale_str('Specify a start time, e.g. 1:00.', key='basic.play.start'), end=app_commands.locale_str('Specify an end time, e.g. 4:00.', key='basic.play.end'))
    @app_commands.autocomplete(query=play_autocomplete)
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def play(self, ctx: commands.Context, *, query: str, start: str = "0", end: str = "0") -> None:
        'Loads your input and adds it to the queue.'
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            player = await voicelink.connect_channel(ctx)

        if not player.is_user_join(ctx.author):
            return await send_localized_message(ctx, "voice.connection.notInChannel", ctx.author.mention, player.channel.mention, ephemeral=True)

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.interaction.response.defer()

        tracks = await player.get_tracks(query, requester=ctx.author)
        if not tracks:
            return await send_localized_message(ctx, "player.errors.noTrackFound")

        try:
            if isinstance(tracks, voicelink.Playlist):
                index = await player.add_track(tracks.tracks, start_time=format_to_ms(start), end_time=format_to_ms(end))
                await send_localized_message(ctx, "player.playback.playlistLoad", tracks.name, index)
            else:
                position = await player.add_track(tracks[0], start_time=format_to_ms(start), end_time=format_to_ms(end))
                texts = await LangHandler.get_lang(ctx.guild.id, "common.status.live", "player.playback.trackLoadPos", "player.playback.trackLoad")
                stream_content = f"`{texts[0]}`" if tracks[0].is_stream else ""
                additional_content = texts[1] if position >= 1 and player.is_playing else texts[2]

                await dispatch_message(
                    ctx,
                    stream_content + additional_content,
                    tracks[0].title, tracks[0].uri, tracks[0].author, tracks[0].formatted_length,
                    position if position >= 1 and player.is_playing else None
                )
        finally:
            if not player.is_playing:
                await player.do_next()

    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def _play(self, interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer()
        query = ""

        if message.content:
            url = re.findall(r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+", message.content)
            if url:
                query = url[0]

        elif message.attachments:
            query = message.attachments[0].url

        if not query:
            return await send_localized_message(interaction, "player.errors.noPlaySource", ephemeral=True)

        player: voicelink.Player = interaction.guild.voice_client
        if not player:
            player = await voicelink.connect_channel(interaction)

        if not player.is_user_join(interaction.user):
            return await send_localized_message(interaction, "voice.connection.notInChannel", interaction.user.mention, player.channel.mention, ephemeral=True)

        tracks = await player.get_tracks(query, requester=interaction.user)
        if not tracks:
            return await send_localized_message(interaction, "player.errors.noTrackFound")

        try:
            if isinstance(tracks, voicelink.Playlist):
                index = await player.add_track(tracks.tracks)
                await send_localized_message(interaction, "player.playback.playlistLoad", tracks.name, index)
            else:
                position = await player.add_track(tracks[0])
                texts = await LangHandler.get_lang(interaction.guild.id, "common.status.live", "player.playback.trackLoadPos", "player.playback.trackLoad")

                stream_content = f"`{texts[0]}`" if tracks[0].is_stream else ""
                additional_content = texts[1] if position >= 1 and player.is_playing else texts[2]

                await dispatch_message(
                    interaction,
                    stream_content + additional_content,
                    tracks[0].title, tracks[0].uri, tracks[0].author, tracks[0].formatted_length,
                    position if position >= 1 and player.is_playing else None
                )
        finally:
            if not player.is_playing:
                await player.do_next()




    @commands.hybrid_command(name='pause', aliases=get_aliases('pause'), description=app_commands.locale_str('Pause the music.', key='basic.pause'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def pause(self, ctx: commands.Context):
        'Pause the music.'
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if player.is_paused:
            return await send_localized_message(ctx, "player.controls.pause.error", ephemeral=True)

        if not player.is_privileged(ctx.author):
            if ctx.author in player.pause_votes:
                return await send_localized_message(ctx, "voting.voted", ephemeral=True)

            player.pause_votes.add(ctx.author)
            if len(player.pause_votes) < (required := player.required()):
                return await send_localized_message(ctx, "player.controls.pause.vote", ctx.author, len(player.pause_votes), required)

        await player.set_pause(True, ctx.author)
        await send_localized_message(ctx, "player.controls.pause.success", ctx.author)

    @commands.hybrid_command(name='resume', aliases=get_aliases('resume'), description=app_commands.locale_str('Resume the music.', key='basic.resume'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def resume(self, ctx: commands.Context):
        'Resume the music.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_paused:
            return await send_localized_message(ctx, "player.controls.resume.error")

        if not player.is_privileged(ctx.author):
            if ctx.author in player.resume_votes:
                return await send_localized_message(ctx, "voting.voted", ephemeral=True)

            player.resume_votes.add(ctx.author)
            if len(player.resume_votes) < (required := player.required()):
                return await send_localized_message(ctx, "player.controls.resume.vote", ctx.author, len(player.resume_votes), required)

        await player.set_pause(False, ctx.author)
        await send_localized_message(ctx, "player.controls.resume.success", ctx.author)

    @commands.hybrid_command(name='skip', aliases=get_aliases('skip'), description=app_commands.locale_str('Skip to the next song, or to a specific position.', key='basic.skip'))
    @app_commands.describe(index=app_commands.locale_str('Enter the position number you want to skip to.', key='basic.skip.index'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def skip(self, ctx: commands.Context, index: int = 0):
        'Skip to the next song, or to a specific position.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.node._available:
            return await send_localized_message(ctx, "player.errors.nodeReconnect")

        if not player.is_playing:
            return await send_localized_message(ctx, "player.controls.skip.error", ephemeral=True)

        if not player.is_privileged(ctx.author):
            if ctx.author == player.current.requester:
                pass
            elif ctx.author in player.skip_votes:
                return await send_localized_message(ctx, "voting.voted", ephemeral=True)
            else:
                player.skip_votes.add(ctx.author)
                if len(player.skip_votes) < (required := player.required()):
                    return await send_localized_message(ctx, "player.controls.skip.vote", ctx.author, len(player.skip_votes), required)

        if index:
            player.queue.skipto(index)

        await send_localized_message(ctx, "player.controls.skip.success", ctx.author)
        if player.queue._repeat.mode == voicelink.LoopType.TRACK:
            await player.set_repeat(voicelink.LoopType.OFF)

        await player.stop()

    @commands.hybrid_command(name='back', aliases=get_aliases('back'), description=app_commands.locale_str('Go back to the previous track, or to a specific previous position.', key='basic.back'))
    @app_commands.describe(index=app_commands.locale_str('Enter the position number you want to return to.', key='basic.back.index'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def back(self, ctx: commands.Context, index: int = 1):
        'Go back to the previous track, or to a specific previous position.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.node._available:
            return await send_localized_message(ctx, "player.errors.nodeReconnect")

        if not player.is_privileged(ctx.author):
            if ctx.author in player.previous_votes:
                return await send_localized_message(ctx, "voting.voted", ephemeral=True)

            player.previous_votes.add(ctx.author)
            if len(player.previous_votes) < (required := player.required()):
                return await send_localized_message(ctx, "player.controls.back.vote", ctx.author, len(player.previous_votes), required)

        if not player.is_playing:
            player.queue.backto(index)
            await player.do_next()
        else:
            player.queue.backto(index + 1)
            await player.stop()

        await send_localized_message(ctx, "player.controls.back.success", ctx.author)
        if player.queue._repeat.mode == voicelink.LoopType.TRACK:
            await player.set_repeat(voicelink.LoopType.OFF)

    @commands.hybrid_command(name='seek', aliases=get_aliases('seek'), description=app_commands.locale_str('Change the player position.', key='basic.seek'))
    @app_commands.describe(position=app_commands.locale_str('Enter the position. Example: 1:20.', key='basic.seek.position'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def seek(self, ctx: commands.Context, position: str):
        'Change the player position.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_privileged(ctx.author):
            return await send_localized_message(ctx, "permissions.missingPosition", ephemeral=True)

        if not player.current or player.position == 0:
            return await send_localized_message(ctx, "player.errors.noTrackPlaying", ephemeral=True)

        if not (num := format_to_ms(position)):
            return await send_localized_message(ctx, "time.formatError", ephemeral=True)

        await player.seek(num, ctx.author)
        await send_localized_message(ctx, "player.controls.seek", position)

    @commands.hybrid_group(name='queue', aliases=get_aliases('queue'), fallback='list', invoke_without_command=True, description=app_commands.locale_str('Show songs in the queue.', key='basic.queue'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def queue(self, ctx: commands.Context):
        'Show songs in the queue.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_user_join(ctx.author):
            return await send_localized_message(ctx, "voice.connection.notInChannel", ctx.author.mention, player.channel.mention, ephemeral=True)

        if player.queue.is_empty:
            return await nowplay(ctx, player)
        view = QueueView(player=player, author=ctx.author)
        view.response = await dispatch_message(ctx, await view.build_embed(), view=view)



    @commands.hybrid_command(name='history', aliases=get_aliases('history'), description=app_commands.locale_str('Show previously played songs from your history.', key='basic.history'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def history(self, ctx: commands.Context):
        'Show previously played songs from your history.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_user_join(ctx.author):
            return await send_localized_message(ctx, "voice.connection.notInChannel", ctx.author.mention, player.channel.mention, ephemeral=True)

        if not player.queue.history():
            return await nowplay(ctx, player)

        view = QueueView(player=player, author=ctx.author, is_queue=False)
        view.response = await dispatch_message(ctx, await view.build_embed(), view=view)

    @commands.hybrid_command(name='leave', aliases=get_aliases('leave'), description=app_commands.locale_str('Disconnects the bot from your voice channel and clears the queue.', key='basic.leave'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def leave(self, ctx: commands.Context):
        'Disconnects the bot from your voice channel and clears the queue.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_privileged(ctx.author):
            if ctx.author in player.stop_votes:
                return await send_localized_message(ctx, "voting.voted", ephemeral=True)
            else:
                player.stop_votes.add(ctx.author)
                if len(player.stop_votes) >= (required := player.required(leave=True)):
                    pass
                else:
                    return await send_localized_message(ctx, "player.controls.leave.vote", ctx.author, len(player.stop_votes), required)

        await send_localized_message(ctx, "player.controls.leave.success", ctx.author)
        await player.teardown()


    @commands.hybrid_command(name='loop', aliases=get_aliases('loop'), description=app_commands.locale_str('Change the loop mode.', key='basic.loop'))
    @app_commands.describe(mode=app_commands.locale_str('Select a loop mode.', key='basic.loop.mode'))
    @app_commands.choices(mode=[
        app_commands.Choice(name=loop_type.name.title(), value=loop_type.name)
        for loop_type in voicelink.LoopType
    ])
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def loop(self, ctx: commands.Context, mode: str):
        'Change the loop mode.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_privileged(ctx.author):
            return await send_localized_message(ctx, "permissions.missingMode", ephemeral=True)

        await player.set_repeat(voicelink.LoopType[mode] if mode in voicelink.LoopType.__members__ else voicelink.LoopType.OFF, ctx.author)
        await send_localized_message(ctx, "player.controls.repeat", mode.capitalize())

    @commands.hybrid_command(name='clear', aliases=get_aliases('clear'), description=app_commands.locale_str('Remove all tracks from the queue or history.', key='basic.clear'))
    @app_commands.describe(queue=app_commands.locale_str('Select the queue you want to clear.', key='basic.clear.queue_param'))
    @app_commands.choices(queue=[
        app_commands.Choice(name='Queue', value='queue'),
        app_commands.Choice(name='History', value='history')
    ])
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def clear(self, ctx: commands.Context, queue: str = "queue"):
        'Remove all tracks from the queue or history.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_privileged(ctx.author):
            return await send_localized_message(ctx, "permissions.missingQueue", ephemeral=True)

        await player.clear_queue(queue, ctx.author)
        await send_localized_message(ctx, "queue.management.cleared", queue.capitalize())


    @commands.hybrid_command(name='forward', aliases=get_aliases('forward'), description=app_commands.locale_str('Seek forward by a given amount in the current track. Default: 10 seconds.', key='basic.forward'))
    @app_commands.describe(position=app_commands.locale_str('Enter how far to seek forward. Example: 1:20', key='basic.forward.position'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def forward(self, ctx: commands.Context, position: str = "10"):
        'Seek forward by a given amount in the current track. Default: 10 seconds.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_privileged(ctx.author):
            return await send_localized_message(ctx, "permissions.missingPosition", ephemeral=True)

        if not player.current:
            return await send_localized_message(ctx, "player.errors.noTrackPlaying", ephemeral=True)

        if not (num := format_to_ms(position)):
            return await send_localized_message(ctx, "time.formatError", ephemeral=True)

        await player.seek(int(player.position + num))
        await send_localized_message(ctx, "player.controls.forward", format_ms(player.position + num))

    @commands.hybrid_command(name='rewind', aliases=get_aliases('rewind'), description=app_commands.locale_str('Seek backward by a given amount in the current track. Default: 10 seconds.', key='basic.rewind'))
    @app_commands.describe(position=app_commands.locale_str('Enter how far to seek backward. Example: 1:20', key='basic.rewind.position'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def rewind(self, ctx: commands.Context, position: str = "10"):
        'Seek backward by a given amount in the current track. Default: 10 seconds.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_privileged(ctx.author):
            return await send_localized_message(ctx, "permissions.missingPosition", ephemeral=True)

        if not player.current:
            return await send_localized_message(ctx, "player.errors.noTrackPlaying", ephemeral=True)

        if not (num := format_to_ms(position)):
            return await send_localized_message(ctx, "time.formatError", ephemeral=True)

        await player.seek(int(player.position - num))
        await send_localized_message(ctx, "player.controls.rewind", format_ms(player.position - num))

    @commands.hybrid_command(name='replay', aliases=get_aliases('replay'), description=app_commands.locale_str('Restart the currently playing track.', key='basic.replay'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def replay(self, ctx: commands.Context):
        'Restart the currently playing track.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_privileged(ctx.author):
            return await send_localized_message(ctx, "permissions.missingPosition", ephemeral=True)

        if not player.current:
            return await send_localized_message(ctx, "player.errors.noTrackPlaying", ephemeral=True)

        await player.seek(0)
        await send_localized_message(ctx, "player.controls.replay")

    @commands.hybrid_command(name='shuffle', aliases=get_aliases('shuffle'), description=app_commands.locale_str('Shuffle the tracks in the queue.', key='basic.shuffle'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def shuffle(self, ctx: commands.Context):
        'Shuffle the tracks in the queue.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_privileged(ctx.author):
            if ctx.author in player.shuffle_votes:
                return await send_localized_message(ctx, "voting.voted", ephemeral=True)

            player.shuffle_votes.add(ctx.author)
            if len(player.shuffle_votes) < (required := player.required()):
                return await send_localized_message(ctx, "player.controls.shuffle.vote", ctx.author, len(player.shuffle_votes), required)

        await player.shuffle("queue", ctx.author)
        await send_localized_message(ctx, "player.controls.shuffle.success")




    @commands.hybrid_command(name='swapdj', aliases=get_aliases('swapdj'), description=app_commands.locale_str('Transfer the DJ role to another member.', key='basic.swapdj'))
    @app_commands.describe(member=app_commands.locale_str('Select the member to receive the DJ role.', key='basic.swapdj.member'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def swapdj(self, ctx: commands.Context, member: discord.Member):
        'Transfer the DJ role to another member.'
        player: voicelink.Player = ctx.guild.voice_client
        if not player:
            return await send_localized_message(ctx, "player.errors.noPlayer", ephemeral=True)

        if not player.is_user_join(ctx.author):
            return await send_localized_message(ctx, "voice.connection.notInChannel", ctx.author.mention, player.channel.mention, ephemeral=True)

        if player.dj.id != ctx.author.id or player.settings.get('dj', False):
            return await send_localized_message(ctx, "permissions.notDj", f"<@&{player.settings['dj']}>" if player.settings.get('dj') else player.dj.mention, ephemeral=True)

        if player.dj.id == member.id or member.bot:
            return await send_localized_message(ctx, "permissions.djToSelf", ephemeral=True)

        if member not in player.channel.members:
            return await send_localized_message(ctx, "permissions.djNotInChannel", member, ephemeral=True)

        player.dj = member
        await send_localized_message(ctx, "permissions.djSwapped", member)


    @commands.hybrid_command(name='help', aliases=get_aliases('help'), description=app_commands.locale_str('All available commands in YaMusic.', key='basic.help'))
    @app_commands.autocomplete(category=help_autocomplete)
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def help(self, ctx: commands.Context, category: str = "News") -> None:
        'All available commands in YaMusic.'
        if category not in self.bot.cogs:
            category = "News"
        view = HelpView(self.bot, ctx.author)
        embed = view.build_embed(category)
        view.response = await dispatch_message(ctx, embed, view=view)

    @commands.hybrid_command(name='ping', aliases=get_aliases('ping'), description=app_commands.locale_str('Check whether the bot is responsive and the latency between your commands and its response.', key='basic.ping'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def ping(self, ctx: commands.Context):
        'Check whether the bot is responsive and the latency between your commands and its response.'
        player: voicelink.Player = ctx.guild.voice_client

        value = await LangHandler.get_lang(ctx.guild.id, "ping.title1", "ping.field1", "ping.title2", "ping.field2")

        embed = discord.Embed(color=Config().embed_color)
        embed.add_field(
            name=value[0],
            value=value[1].format(
                "0", "0", self.bot.latency, '😭' if self.bot.latency > 5 else ('😨' if self.bot.latency > 1 else '👌'), "St Louis, MO, United States"
        ))

        if player:
            embed.add_field(
                name=value[2],
                value=value[3].format(
                    player.node._identifier, player.ping, player.node.player_count, player.channel.rtc_region),
                    inline=False
            )

        await dispatch_message(ctx, embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Basic(bot))
