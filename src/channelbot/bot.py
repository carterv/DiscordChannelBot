from __future__ import annotations

import asyncio
import os
from collections import Counter
from enum import Enum
from string import Template
from typing import Optional, Tuple, Iterator, Iterable

from attr import attrs, attrib
from cattr import unstructure, structure
from discord import Message, Member, VoiceState, VoiceChannel, Guild, PermissionOverwrite, Game, Intents, NotFound
from discord.ext import commands
from discord.ext.commands import Context, CommandNotFound
from tinydb import TinyDB, where


class ManagedChannelType(Enum):
    SPAWNER = "SPAWNER"
    CHILD = "CHILD"


@attrs
class ChannelConfig:
    template = attrib(default="#${no} Talk [${game}]", type=str)
    channel_type = attrib(default=ManagedChannelType.SPAWNER, type=ManagedChannelType)
    # Child only attributes
    spawner = attrib(type=Optional[Tuple[int, int]], default=None)
    channel_number = attrib(type=Optional[int], default=None)


@attrs
class ManagedChannel:
    guild_id = attrib(type=int)
    channel_id = attrib(type=int)
    config = attrib(type=ChannelConfig)

    def voice_channel(self, guild: Guild) -> VoiceChannel:
        return guild.get_channel(self.channel_id)

    def get_spawner(self, db: ChannelDatabase):
        if self.config.channel_type == ManagedChannelType.SPAWNER:
            return self
        return db.get_channel(*self.config.spawner)

    async def spawn_channel(self, guild: Guild, member: Member, db: ChannelDatabase) -> ManagedChannel:
        source_channel = self.voice_channel(guild)

        channel_numbers = [el.config.channel_number for el in db.get_children(self)]
        channel_number = len(channel_numbers) + 1
        for i in range(1, channel_number):
            if i not in channel_numbers:
                channel_number = i
                break

        new_config = structure(unstructure(self.config), ChannelConfig)
        new_config.channel_type = ManagedChannelType.CHILD
        new_config.spawner = (self.guild_id, self.channel_id)
        new_config.channel_number = channel_number

        game_status = "General"
        for activity in member.activities:
            if not all((activity, isinstance(activity, Game))):
                continue
            game: Game = activity
            game_status = game.name

        try:
            channel_name = Template(self.config.template).substitute(
                no=channel_number,
                game=game_status,
            )
        except (ValueError, KeyError):
            channel_name = self.config.template

        new_channel: VoiceChannel = await source_channel.clone(name=channel_name)
        await new_channel.edit(position=source_channel.position+1)
        managed_channel = ManagedChannel(guild.id, new_channel.id, new_config)
        db.insert_channel(managed_channel)
        await member.move_to(new_channel)
        return managed_channel


class ChannelDatabase:
    def __init__(self):
        self._db: TinyDB = TinyDB(os.getenv("CHANNEL_DB_PATH"))

    def insert_channel(self, channel: ManagedChannel):
        try:
            self.remove_channel(channel)
        except KeyError:
            pass
        self._db.insert(unstructure(channel))

    def remove_channel(self, channel: ManagedChannel):
        el = self._db.get((where("channel_id") == channel.channel_id) & (where("guild_id") == channel.guild_id))
        if el:
            self._db.remove(doc_ids=[el.doc_id])
        else:
            raise KeyError(channel.guild_id, channel.channel_id)

    def get_channel(self, guild_id: int, channel_id: int) -> ManagedChannel:
        el = self._db.get((where("channel_id") == channel_id) & (where("guild_id") == guild_id))
        if not el:
            raise KeyError(guild_id, channel_id)
        return structure(el, ManagedChannel)

    def get_children(self, spawner: ManagedChannel) -> Iterator[ManagedChannel]:
        all_raw_channels = self._db.search(where("guild_id") == spawner.guild_id)
        all_channels = (structure(el, ManagedChannel) for el in all_raw_channels)
        children = (c for c in all_channels if c.config.spawner and c.config.spawner[1] == spawner.channel_id)
        yield from sorted(children, key=lambda el: el.config.channel_number)

    def scan(self) -> Iterator[ManagedChannel]:
        yield from (structure(el, ManagedChannel) for el in self._db.search(where("channel_id") > 0))


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
        self.bot.command(name="dcspawn", help="Create a new dynamic channel")(self.create_spawner)
        self.bot.command(
            name="dctemplate",
            help=(
                "Update the template for the connected channel. "
                "Currently supported variables are '${no}' (number) and '${game}'. "
            ),
        )(self.update_template)
        self.bot.loop.create_task(self.update_loop())

    def run(self):
        token = os.getenv("DISCORD_TOKEN")
        self.bot.run(token)

    async def on_command_error(self, ctx: Context, error: BaseException):
        if isinstance(error, CommandNotFound):
            return
        raise error

    async def on_voice_state_update(self, member: Member, before: VoiceState, after: VoiceState):
        guild: Guild = member.guild
        if before.channel == after.channel:
            return

        if before.channel is not None:
            channel: VoiceChannel = before.channel
            if not channel.members:
                try:
                    managed_channel = self.db.get_channel(guild.id, channel.id)
                except KeyError:
                    pass
                else:
                    if managed_channel.config.channel_type == ManagedChannelType.CHILD:
                        try:
                            await channel.delete()
                        except NotFound:
                            pass
                        self.db.remove_channel(managed_channel)

        if after.channel is not None:
            try:
                channel: ManagedChannel = self.db.get_channel(member.guild.id, after.channel.id)
            except KeyError:
                pass
            else:
                if channel.config.channel_type == ManagedChannelType.SPAWNER:
                    await channel.spawn_channel(guild, member, self.db)

    async def create_spawner(self, ctx: Context):
        message: Message = ctx.message
        guild: Guild = ctx.guild

        try:
            _ = message.guild.id
        except AttributeError:
            await message.author.send("Error: !dccreate cannot be used in a private message")
            return

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

    async def update_template(self, ctx: Context, *args: str):
        message: Message = ctx.message
        guild: Guild = ctx.guild

        try:
            _ = guild.id
        except AttributeError:
            await message.author.send("Error: !dctemplate cannot be used in a private message")
            return

        author: Member = message.author
        channel = author.voice.channel

        if not author.guild_permissions.manage_channels:
            await message.channel.send("Error: You do not have permissions to manage channels")
            return

        if not channel:
            await message.channel.send("Error: !dctemplate can only be used when connected to a voice channel")
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

    def game_status_from_members(self, members: Iterable[Member]) -> str:
        c = Counter()
        for member in members:
            for activity in member.activities:
                if isinstance(activity, Game):
                    c[activity.name] += 1
        most_common = c.most_common(1)
        if most_common:
            return most_common[0][0]
        else:
            return "General"

    async def update_loop(self):
        while not self.bot.is_ready():
            await asyncio.sleep(1)

        while not self.bot.is_closed():
            all_managed_channels = list(self.db.scan())
            for channel in all_managed_channels:
                guild = self.bot.get_guild(channel.guild_id)
                if guild is None:
                    print("Removing invalid channel (no guild)")
                    self.db.remove_channel(channel)
                    continue
                voice_channel = channel.voice_channel(guild)
                if voice_channel is None:
                    print("Removing invalid channel (no channel)")
                    self.db.remove_channel(channel)
                    continue

                if channel.config.channel_type == ManagedChannelType.CHILD and len(voice_channel.members) == 0:
                    try:
                        await voice_channel.delete(reason="Automated channel cleanup")
                    except NotFound:
                        pass
                    self.db.remove_channel(channel)
                    continue

                if channel.config.channel_type == ManagedChannelType.CHILD:
                    game_status = self.game_status_from_members(voice_channel.members)
                    await voice_channel.edit(
                        name=Template(channel.config.template).substitute(
                            no=channel.config.channel_number, game=game_status
                        )
                    )
                    continue

            await asyncio.sleep(60)
