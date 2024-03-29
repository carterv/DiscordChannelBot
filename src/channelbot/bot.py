from __future__ import annotations

import asyncio
import os
from collections import Counter
from contextlib import suppress
from datetime import timedelta, datetime
from functools import wraps, lru_cache
from string import Template
from typing import Sequence

from cattr import unstructure, structure
from discord import (
    Message,
    Member,
    VoiceState,
    VoiceChannel,
    Guild,
    PermissionOverwrite,
    Game,
    Intents,
    NotFound,
    ActivityType,
)
from discord.ext import commands
from discord.ext.commands import Context, CommandNotFound

from channelbot.data import ManagedChannelType, ChannelConfig, ManagedChannel
from channelbot.db import ChannelDatabase


async def spawn_channel(spawner: ManagedChannel, guild: Guild, db: ChannelDatabase, *members: Member) -> ManagedChannel:
    source_channel = spawner.voice_channel(guild)

    channel_numbers = [el.config.channel_number for el in db.get_children(spawner)]
    channel_number = len(channel_numbers) + 1
    for i in range(1, channel_number):
        if i not in channel_numbers:
            channel_number = i
            break

    new_config = structure(unstructure(spawner.config), ChannelConfig)
    new_config.channel_type = ManagedChannelType.CHILD
    new_config.spawner = (spawner.guild_id, spawner.channel_id)
    new_config.channel_number = channel_number

    game_status = "General"
    for activity in members[0].activities:  # FIXME: Use all members
        if not all((activity, isinstance(activity, Game))):
            continue
        game: Game = activity
        game_status = game.name

    channel_name = new_config.make_channel_name(game=game_status)
    new_channel: VoiceChannel = await source_channel.clone(name=channel_name)
    await new_channel.edit(position=source_channel.position + 1)
    managed_channel = ManagedChannel(guild.id, new_channel.id, new_config)
    db.insert_channel(managed_channel)
    for member in members:
        await member.move_to(new_channel)
    return managed_channel


def async_loop(*, hours: int = 0, minutes: int = 0, seconds: int = 0):
    seconds = timedelta(hours=hours, minutes=minutes, seconds=seconds).seconds
    if seconds <= 0:
        raise ValueError("Loop time must be greater than zero")

    def wrapper(func):
        @wraps(func)
        async def wrapped(self, *args, **kwargs):
            while not self.bot.is_ready():
                await asyncio.sleep(1)

            while not self.bot.is_closed():
                await func(self, *args, **kwargs)
                await asyncio.sleep(seconds)

        return wrapped

    return wrapper


def channel_only_command(command_prefix: str):
    def wrapper(func):
        @wraps(func)
        async def wrapped(self, ctx, *args, **kwargs):
            message: Message = ctx.message
            guild: Guild = ctx.guild
            try:
                _ = guild.id
            except AttributeError:
                await message.author.send(f"Error: !{command_prefix} cannot be used in a private message")
                return
            await func(self, ctx, *args, **kwargs)

        return wrapped

    return wrapper


def game_status_from_members(members: Sequence[Member]) -> str:
    c = Counter()
    for member in members:
        for activity in member.activities:
            if activity.type == ActivityType.playing or activity.type == ActivityType.streaming:
                c[activity.name] += 1
                break
        else:
            c["General"] += 1
    most_common = c.most_common(1)
    if most_common:
        name, count = most_common[0]
        return name
    return "General"


async def update_channel_name(guild: Guild, channel: ManagedChannel):
    voice_channel = channel.voice_channel(guild)
    game_status = game_status_from_members(voice_channel.members)
    new_channel_name = channel.config.make_channel_name(game=game_status)
    if voice_channel.name != new_channel_name:
        await voice_channel.edit(name=new_channel_name)


@lru_cache(maxsize=64)
def lock(guild_id: int, channel_id: int):
    return asyncio.Lock()


class ChannelBot:
    def __init__(self):
        self.db: ChannelDatabase = ChannelDatabase()
        intents = Intents.default()
        intents.voice_states = True
        intents.presences = True
        intents.members = True
        self.bot = commands.Bot(command_prefix="!", intents=intents)

        self.bot.event(self.on_voice_state_update)
        self.bot.event(self.on_command_error)
        self.bot.event(self.on_ready)
        self.bot.command(name="cbspawner", help="Create a new dynamic channel")(self.create_spawner)
        self.bot.command(name="cbrename", help="Rename your current channel")(self.rename)
        self.bot.command(
            name="cbtemplate",
            help=(
                "Update the template for the connected channel. "
                "Currently supported variables are '${no}' (number) and '${game}'. "
            ),
        )(self.update_template)
        self.bot.command(
            name="cblimit", help="Set the user limit for your current channel. Set to zero to remove the limit."
        )(self.limit_channel)
        self.bot.command(name="cbhold", help="Configure the current channel to stay alive even if it has no members")(
            self.hold_channel
        )
        self.bot.command(name="cbimport", help="Import a channel")(self.import_channel)
        self.bot.command(name="cborphan", help="Orphan a channel")(self.orphan_channel)
        self.bot.loop.create_task(self.update_loop())

    def run(self):
        token = os.getenv("DISCORD_TOKEN")
        self.bot.run(token)

    async def on_ready(self):
        await self.update_task()

    async def on_command_error(self, ctx: Context, error: BaseException):
        if isinstance(error, CommandNotFound):
            return
        raise error

    async def on_channel_join(self, member: Member, channel: VoiceChannel):
        guild: Guild = channel.guild
        try:
            managed_channel: ManagedChannel = self.db.get_channel(guild.id, channel.id)
        except KeyError:
            return

        if managed_channel.config.channel_type == ManagedChannelType.SPAWNER:
            async with lock(guild.id, channel.id):
                await spawn_channel(managed_channel, guild, self.db, *channel.members)

    async def on_channel_leave(self, member: Member, channel: VoiceChannel):
        guild: Guild = member.guild
        try:
            managed_channel = self.db.get_channel(guild.id, channel.id)
        except KeyError:
            return

        if managed_channel.config.channel_type != ManagedChannelType.CHILD:
            return

        async with lock(guild.id, channel.id):
            if channel.members:
                await update_channel_name(guild, managed_channel)
                return

            if not managed_channel.config.is_expired:
                return

            with suppress(NotFound):
                await channel.delete()
            self.db.remove_channel(managed_channel)

    async def on_voice_state_update(self, member: Member, before: VoiceState, after: VoiceState):
        if before.channel == after.channel:
            return

        if before.channel is not None:
            await self.on_channel_leave(member, before.channel)

        if after.channel is not None:
            await self.on_channel_join(member, after.channel)

    @channel_only_command("cbspawner")
    async def create_spawner(self, ctx: Context, *args: str):
        message: Message = ctx.message
        guild: Guild = ctx.guild
        author: Member = message.author

        if not author.guild_permissions.manage_channels:
            await message.channel.send("Error: You do not have permissions to manage channels")
            return

        overwrites = {
            guild.me: PermissionOverwrite(connect=True, manage_channels=True, move_members=True, view_channel=True)
        }
        channel: VoiceChannel = await guild.create_voice_channel("+ Spawn Channel", overwrites=overwrites)

        new_spawner = ManagedChannel(
            guild_id=guild.id, channel_id=channel.id, config=ChannelConfig(channel_type=ManagedChannelType.SPAWNER)
        )
        self.db.insert_channel(new_spawner)

        await message.channel.send(f"New channel spawner created")

    @channel_only_command("cbtemplate")
    async def update_template(self, ctx: Context, *args: str):
        message: Message = ctx.message
        guild: Guild = ctx.guild
        author: Member = message.author
        channel = author.voice.channel

        if not author.guild_permissions.manage_channels:
            await message.channel.send("Error: You do not have permissions to manage channels")
            return

        if not channel:
            await message.channel.send("Error: !cbtemplate can only be used when connected to a voice channel")
            return

        try:
            managed_channel = self.db.get_channel(guild.id, channel.id)
        except KeyError:
            await message.channel.send("Error: Channel is not managed by ChannelBot")
            return
        spawner = managed_channel.get_spawner(self.db)

        spawner.config.template = " ".join(args)
        try:
            Template(spawner.config.template).substitute(no=1, game="General")
        except (KeyError, ValueError):
            await message.channel.send("Error: Invalid template provided")
            return

        self.db.insert_channel(spawner)
        await message.channel.send("Template updated")

    @channel_only_command("cbrename")
    async def rename(self, ctx: Context, *args: str):
        message: Message = ctx.message
        guild: Guild = ctx.guild
        author: Member = message.author
        channel = author.voice.channel

        if not channel:
            await message.channel.send("Error: !cbrename can only be used when connected to a voice channel")
            return

        try:
            managed_channel = self.db.get_channel(guild.id, channel.id)
        except KeyError:
            await message.channel.send("Error: Channel is not managed by ChannelBot")
            return

        if managed_channel.config.channel_type not in {ManagedChannelType.CHILD, ManagedChannelType.IMPORT}:
            await message.channel.send("Error: Channel must be a child or imported channel")
            return

        managed_channel.config.template = " ".join(args)
        try:
            Template(managed_channel.config.template).substitute(no=1, game="General")
        except (KeyError, ValueError):
            await message.channel.send("Error: Invalid template provided")
            return

        self.db.insert_channel(managed_channel)
        await message.channel.send("Template updated")
        await update_channel_name(guild, managed_channel)


    @channel_only_command("cblimit")
    async def limit_channel(self, ctx: Context, *args: str):
        message: Message = ctx.message
        guild: Guild = ctx.guild
        author: Member = message.author
        channel: VoiceChannel = author.voice.channel

        try:
            managed_channel = self.db.get_channel(guild.id, channel.id)
        except KeyError:
            await message.channel.send("Error: You must be in a ChannelBot-managed session to set limit")
            return

        if not managed_channel.config.channel_type == ManagedChannelType.CHILD:
            await message.channel.send("Error: Command not allowed in non-child channels")
            return

        if len(args) != 1:
            await message.channel.send("Usage: !cblimit <limit>")
            return

        try:
            limit = int(args[0])
        except ValueError:
            limit = args[0]

        if not isinstance(limit, int) or limit < 0:
            await message.channel.send("Limit must be positive integer")
            return

        await channel.edit(user_limit=limit)

    @channel_only_command("cbhold")
    async def hold_channel(self, ctx: Context, *args: str):
        message: Message = ctx.message
        guild: Guild = ctx.guild
        author: Member = message.author
        channel: VoiceChannel = author.voice.channel

        try:
            managed_channel = self.db.get_channel(guild.id, channel.id)
        except KeyError:
            await message.channel.send("Error: You must be in a ChannelBot-managed session to set limit")
            return

        if not managed_channel.config.channel_type == ManagedChannelType.CHILD:
            await message.channel.send("Error: Command not allowed in non-child channels")
            return

        if len(args) != 1:
            await message.channel.send("Usage: !cbhold <DD:HH:MM>")
            return

        try:
            days, hours, minutes = [int(p) for p in args[0].split(":")]
        except ValueError:
            await message.channel.send("Usage: !cbhold <DD:HH:MM>")
            return

        if managed_channel.config.hold_until is None:
            managed_channel.config.hold_until = (
                datetime.utcnow() + timedelta(days=days, hours=hours, minutes=minutes)
            ).timestamp()
        else:
            managed_channel.config.hold_until = (
                datetime.utcfromtimestamp(managed_channel.config.hold_until)
                + timedelta(days=days, hours=hours, minutes=minutes)
            ).timestamp()

        self.db.insert_channel(managed_channel)
        await message.channel.send(
            "Hold will expire at {} UTC".format(
                datetime.utcfromtimestamp(managed_channel.config.hold_until).strftime("%Y-%m-%dT%H:%M:%S")
            )
        )

    @channel_only_command("cbimport")
    async def import_channel(self, ctx: Context, *args: str):
        message: Message = ctx.message
        guild: Guild = ctx.guild
        author: Member = message.author
        channel: VoiceChannel = author.voice.channel

        if not author.guild_permissions.manage_channels:
            await message.channel.send("Error: You do not have permissions to manage channels")
            return

        try:
            _ = self.db.get_channel(guild.id, channel.id)
        except KeyError:
            pass
        else:
            await message.channel.send("Error: The channel you are in is already ChannelBot-managed")
            return

        imported_channel = ManagedChannel(
            guild_id=guild.id,
            channel_id=channel.id,
            config=ChannelConfig(channel_type=ManagedChannelType.IMPORT, template=channel.name, channel_number=0),
        )
        self.db.insert_channel(imported_channel)
        await message.channel.send("Channel imported")

    @channel_only_command("cborphan")
    async def orphan_channel(self, ctx: Context, *args: str):
        message: Message = ctx.message
        guild: Guild = ctx.guild
        author: Member = message.author
        channel: VoiceChannel = author.voice.channel

        if not author.guild_permissions.manage_channels:
            await message.channel.send("Error: You do not have permissions to manage channels")
            return

        try:
            managed_channel = self.db.get_channel(guild.id, channel.id)
        except KeyError:
            await message.channel.send("Error: The channel you are in is already not managed by ChannelBot")
            return

        self.db.remove_channel(managed_channel)
        await message.channel.send("Channel orphaned")

    @async_loop(minutes=1)
    async def update_loop(self):
        await self.update_task()

    async def update_task(self):
        all_managed_channels = list(self.db.scan())
        for channel in all_managed_channels:
            guild = self.bot.get_guild(channel.guild_id)
            if guild is None:
                print("Removing invalid channel (no guild)")
                with suppress(KeyError):
                    self.db.remove_channel(channel)
                continue
            voice_channel = channel.voice_channel(guild)
            if voice_channel is None:
                print("Removing invalid channel (no channel)")
                with suppress(KeyError):
                    self.db.remove_channel(channel)
                continue

            async with lock(guild.id, voice_channel.id):
                if (
                    channel.config.channel_type == ManagedChannelType.CHILD
                    and len(voice_channel.members) == 0
                    and channel.config.is_expired
                ):
                    with suppress(NotFound, KeyError):
                        await voice_channel.delete(reason="Automated channel cleanup")
                        self.db.remove_channel(channel)
                    continue

                if channel.config.channel_type in {ManagedChannelType.CHILD, ManagedChannelType.IMPORT}:
                    await update_channel_name(guild, channel)
                    continue

                if channel.config.channel_type == ManagedChannelType.SPAWNER:
                    if len(voice_channel.members) > 0:
                        await spawn_channel(channel, guild, self.db, *voice_channel.members)
