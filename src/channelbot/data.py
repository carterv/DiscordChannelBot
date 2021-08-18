from __future__ import annotations

from datetime import datetime
from enum import Enum
from string import Template
from typing import Optional, Tuple, TYPE_CHECKING

from attr import attrs, attrib
from discord import Guild, VoiceChannel

if TYPE_CHECKING:
    from channelbot.db import ChannelDatabase


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
    hold_until = attrib(type=Optional[float], default=None)

    def make_channel_name(self, *, game: str = "General"):
        try:
            return Template(self.template).substitute(no=self.channel_number, game=game)
        except (ValueError, KeyError):
            return self.template

    @property
    def is_expired(self) -> bool:
        return self.hold_until is None or datetime.utcnow().timestamp() >= self.hold_until


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
