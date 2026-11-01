import sys
import time
from collections import OrderedDict
from collections.abc import MutableMapping


class BoundedTTLCache(MutableMapping):
    def __init__(self, *, ttl=300, max_entries=1000, max_bytes=64 * 1024 * 1024, max_entry_bytes=1024 * 1024, clock=None):
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_entry_bytes = min(max_entry_bytes, max_bytes)
        self._clock = clock or time.monotonic
        self._entries = OrderedDict()
        self._bytes = 0

    def _discard(self, key):
        entry = self._entries.pop(key)
        self._bytes -= entry[2]
        return entry[0]

    def _measure(self, value):
        pending = [value]
        seen = set()
        size = 0
        while pending:
            item = pending.pop()
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
            size += sys.getsizeof(item)
            if size > self.max_entry_bytes:
                break
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple, set, frozenset)):
                pending.extend(item)
        return size

    def expire(self):
        now = self._clock()
        removed = 0
        while self._entries:
            key = next(iter(self._entries))
            if self._entries[key][1] > now:
                break
            self._discard(key)
            removed += 1
        return removed

    def __getitem__(self, key):
        entry = self._entries[key]
        now = self._clock()
        if entry[1] <= now:
            self._discard(key)
            raise KeyError(key)
        self._entries[key] = (entry[0], now + self.ttl, entry[2])
        self._entries.move_to_end(key)
        return entry[0]

    def __setitem__(self, key, value):
        self.expire()
        if key in self._entries:
            self._discard(key)
        size = self._measure(value)
        if size > self.max_entry_bytes or self.max_entries <= 0:
            return
        self._entries[key] = (value, self._clock() + self.ttl, size)
        self._bytes += size
        while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
            self._discard(next(iter(self._entries)))

    def __delitem__(self, key):
        self._discard(key)

    def __iter__(self):
        self.expire()
        return iter(self._entries)

    def __len__(self):
        self.expire()
        return len(self._entries)

    def clear(self):
        self._entries.clear()
        self._bytes = 0

    @property
    def bytes_used(self):
        self.expire()
        return self._bytes
