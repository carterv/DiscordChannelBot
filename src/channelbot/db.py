from __future__ import annotations

import os
from typing import Iterator

from cattr import unstructure, structure
from tinydb import TinyDB, where

from channelbot.data import ManagedChannel


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