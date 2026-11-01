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
import voicelink

from function import (
    get_aliases,
    cooldown_check
)
from discord import app_commands
from discord.ext import commands
from voicelink import LangHandler
from voicelink.utils import send_localized_message


async def check_access(ctx: commands.Context):
    player: voicelink.Player = ctx.guild.voice_client
    if not player:
        text = await LangHandler.get_lang(ctx.guild.id, "player.errors.noPlayer")
        raise voicelink.exceptions.VoicelinkException(text)

    if ctx.author not in player.channel.members:
        if not ctx.author.guild_permissions.manage_guild:
            text = await LangHandler.get_lang(ctx.guild.id, "voice.connection.notInChannel")
            raise voicelink.exceptions.VoicelinkException(text.format(ctx.author.mention, player.channel.mention))

    return player

class Effect(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.description = "cogDescEffect"

    async def effect_autocomplete(self, interaction: discord.Interaction, current: str) -> list:
        player: voicelink.Player = interaction.guild.voice_client
        if not player:
            return []
        if current:
            return [app_commands.Choice(name=effect.tag, value=effect.tag) for effect in player.filters.get_filters() if current in effect.tag]
        return [app_commands.Choice(name=effect.tag, value=effect.tag) for effect in player.filters.get_filters()]

    @commands.hybrid_command(name='speed', aliases=get_aliases('speed'), description=app_commands.locale_str('Set the playback speed of the player.', key='effect.speed'))
    @app_commands.describe(value=app_commands.locale_str('The value to set the speed to. Default is `1.0`.', key='effect.speed.value'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def speed(self, ctx: commands.Context, value: commands.Range[float, 0, 2]):
        'Set the playback speed of the player.'
        player = await check_access(ctx)

        if player.filters.has_filter(filter_tag="speed"):
            player.filters.remove_filter(filter_tag="speed")

        effect = voicelink.Timescale(tag="speed", speed=value)
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='karaoke', aliases=get_aliases('karaoke'), description=app_commands.locale_str('Use equalization to eliminate part of the frequency range, typically targeting vocals.', key='effect.karaoke'))
    @app_commands.describe(level=app_commands.locale_str('The karaoke level. Default is `1.0`.', key='effect.karaoke.level'), monolevel=app_commands.locale_str('The karaoke mono level. Default is `1.0`.', key='effect.karaoke.monolevel'), filterband=app_commands.locale_str('The karaoke filter band. Default is `220.0`.', key='effect.karaoke.filterband'), filterwidth=app_commands.locale_str('The karaoke filter width. Default is `100.0`.', key='effect.karaoke.filterwidth'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def karaoke(self, ctx: commands.Context, level: commands.Range[float, 0, 2] = 1.0, monolevel: commands.Range[float, 0, 2] = 1.0, filterband: commands.Range[float, 100, 300] = 220.0, filterwidth: commands.Range[float, 50, 150] = 100.0) -> None:
        'Use equalization to eliminate part of the frequency range, typically targeting vocals.'
        player = await check_access(ctx)

        if player.filters.has_filter(filter_tag="karaoke"):
            player.filters.remove_filter(filter_tag="karaoke")

        effect = voicelink.Karaoke(tag="karaoke", level=level, mono_level=monolevel, filter_band=filterband, filter_width=filterwidth)
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='tremolo', aliases=get_aliases('tremolo'), description=app_commands.locale_str('Create a trembling effect where the volume oscillates rapidly.', key='effect.tremolo'))
    @app_commands.describe(frequency=app_commands.locale_str('The tremolo frequency. Default is `2.0`.', key='effect.tremolo.frequency'), depth=app_commands.locale_str('The tremolo depth. Default is `0.5`.', key='effect.tremolo.depth'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def tremolo(self, ctx: commands.Context, frequency: commands.Range[float, 0, 10] = 2.0, depth: commands.Range[float, 0, 1] = 0.5) -> None:
        'Create a trembling effect where the volume oscillates rapidly.'
        player = await check_access(ctx)

        if player.filters.has_filter(filter_tag="tremolo"):
            player.filters.remove_filter(filter_tag="tremolo")

        effect = voicelink.Tremolo(tag="tremolo", frequency=frequency, depth=depth)
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='vibrato', aliases=get_aliases('vibrato'), description=app_commands.locale_str('Similar to tremolo, but changes pitch instead of volume.', key='effect.vibrato'))
    @app_commands.describe(frequency=app_commands.locale_str('The vibrato frequency. Default is `2.0`.', key='effect.vibrato.frequency'), depth=app_commands.locale_str('The vibrato depth. Default is `0.5`.', key='effect.vibrato.depth'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def vibrato(self, ctx: commands.Context, frequency: commands.Range[float, 0, 14] = 2.0, depth: commands.Range[float, 0, 1] = 0.5) -> None:
        'Similar to tremolo, but changes pitch instead of volume.'
        player = await check_access(ctx)

        if player.filters.has_filter(filter_tag="vibrato"):
            player.filters.remove_filter(filter_tag="vibrato")

        effect = voicelink.Vibrato(tag="vibrato", frequency=frequency, depth=depth)
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='rotation', aliases=get_aliases('rotation'), description=app_commands.locale_str('Rotates the sound around stereo channels / headphones, also known as audio panning.', key='effect.rotation'))
    @app_commands.describe(hertz=app_commands.locale_str('The rotation frequency. Default is `0.2`.', key='effect.rotation.hertz'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def rotation(self, ctx: commands.Context, hertz: commands.Range[float, 0, 2] = 0.2) -> None:
        'Rotates the sound around stereo channels / headphones, also known as audio panning.'
        player = await check_access(ctx)

        if player.filters.has_filter(filter_tag="rotation"):
            player.filters.remove_filter(filter_tag="rotation")

        effect = voicelink.Rotation(tag="rotation", rotation_hertz=hertz)
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='distortion', aliases=get_aliases('distortion'), description=app_commands.locale_str('Distortion effect. It can generate quite unique sound effects.', key='effect.distortion'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def distortion(self, ctx: commands.Context) -> None:
        'Distortion effect. It can generate quite unique sound effects.'
        player = await check_access(ctx)

        if player.filters.has_filter(filter_tag="distortion"):
            player.filters.remove_filter(filter_tag="distortion")

        effect = voicelink.Distortion(tag="distortion", sin_offset=0.0, sin_scale=1.0, cos_offset=0.0, cos_scale=1.0, tan_offset=0.0, tan_scale=1.0, offset=0.0, scale=1.0)
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='lowpass', aliases=get_aliases('lowpass'), description=app_commands.locale_str('A filter that suppresses higher frequencies and passes lower ones.', key='effect.lowpass'))
    @app_commands.describe(smoothing=app_commands.locale_str('The level of the low pass filter. Default is `20.0`.', key='effect.lowpass.smoothing'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def lowpass(self, ctx: commands.Context, smoothing: commands.Range[float, 10, 30] = 20.0) -> None:
        'A filter that suppresses higher frequencies and passes lower ones.'
        player = await check_access(ctx)

        if player.filters.has_filter(filter_tag="lowpass"):
            player.filters.remove_filter(filter_tag="lowpass")

        effect = voicelink.LowPass(tag="lowpass", smoothing=smoothing)
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='channelmix', aliases=get_aliases('channelmix'), description=app_commands.locale_str('A filter that manually adjusts the audio panning.', key='effect.channelmix'))
    @app_commands.describe(left_to_left=app_commands.locale_str('Left to left sound. Default is `1.0`.', key='effect.channelmix.ltl'), right_to_right=app_commands.locale_str('Right to right sound. Default is `1.0`.', key='effect.channelmix.rtr'), left_to_right=app_commands.locale_str('Left to right sound. Default is `0.0`.', key='effect.channelmix.ltr'), right_to_left=app_commands.locale_str('Right to left sound. Default is `0.0`.', key='effect.channelmix.rtl'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def channelmix(self, ctx: commands.Context, left_to_left: commands.Range[float, 0, 1] = 1.0, right_to_right: commands.Range[float, 0, 1] = 1.0, left_to_right: commands.Range[float, 0, 1] = 0.0, right_to_left: commands.Range[float, 0, 1] = 0.0) -> None:
        'A filter that manually adjusts the audio panning.'
        player = await check_access(ctx)

        if player.filters.has_filter(filter_tag="channelmix"):
            player.filters.remove_filter(filter_tag="channelmix")

        effect = voicelink.ChannelMix(tag="channelmix", left_to_left=left_to_left, right_to_right=right_to_right, left_to_right=left_to_right, right_to_left=right_to_left)
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='nightcore', aliases=get_aliases('nightcore'), description=app_commands.locale_str('Add a nightcore filter to your player.', key='effect.nightcore'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def nightcore(self, ctx: commands.Context) -> None:
        'Add a nightcore filter to your player.'
        player = await check_access(ctx)

        effect = voicelink.Timescale.nightcore()
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='8d', aliases=get_aliases('8d'), description=app_commands.locale_str('Add an 8D filter to your player.', key='effect.8d'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def eightD(self, ctx: commands.Context) -> None:
        'Add an 8D filter to your player.'
        player = await check_access(ctx)

        effect = voicelink.Rotation.nightD()
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='vaporwave', aliases=get_aliases('vaporwave'), description=app_commands.locale_str('Add a vaporwave filter to your player.', key='effect.vaporwave'))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def vaporwave(self, ctx: commands.Context) -> None:
        'Add a vaporwave filter to your player.'
        player = await check_access(ctx)

        effect = voicelink.Timescale.vaporwave()
        await player.add_filter(effect, ctx.author)
        await send_localized_message(ctx, "effects.added", effect.tag)

    @commands.hybrid_command(name='cleareffect', aliases=get_aliases('cleareffect'), description=app_commands.locale_str('Clear all or specific sound effects.', key='effect.cleareffect'))
    @app_commands.describe(effect=app_commands.locale_str('Remove specific sound effects.', key='effect.cleareffect.effect'))
    @app_commands.autocomplete(effect=effect_autocomplete)
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def cleareffect(self, ctx: commands.Context, effect: str = None) -> None:
        'Clear all or specific sound effects.'
        player = await check_access(ctx)

        if effect:
            await player.remove_filter(effect)
        else:
            await player.reset_filter()

        await send_localized_message(ctx, "effects.cleared")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Effect(bot))
