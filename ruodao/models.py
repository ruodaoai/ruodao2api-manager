"""Persistent model catalog snapshot filled by manual refreshes."""

import copy
import os
import threading
import time

from . import util
from .config import DATA_DIR

CACHE_PATH = os.path.join(str(DATA_DIR), "models_cache.json")


class ModelCatalog(object):
    def __init__(self, path=None):
        self.path = str(path or CACHE_PATH)
        self._lock = threading.RLock()
        self._updated_at = 0.0
        self._channels = {}
        loaded = util.read_json(self.path, {})
        if isinstance(loaded, dict):
            try:
                self._updated_at = float(loaded.get("updated_at") or 0)
            except Exception:
                self._updated_at = 0.0
            channels = loaded.get("channels")
            if isinstance(channels, dict):
                for name, section in channels.items():
                    if isinstance(section, dict):
                        self._channels[str(name)] = section

    def _save(self):
        payload = {"updated_at": self._updated_at, "channels": self._channels}
        try:
            util.atomic_write_json(self.path, payload)
        except Exception:
            pass

    def rows(self, channel, realm=None):
        with self._lock:
            section = self._channels.get(str(channel)) or {}
            if realm is None:
                rows = []
                for item in section.values():
                    rows.extend((item or {}).get("models") or [])
            else:
                rows = (section.get(str(realm)) or {}).get("models") or []
            return copy.deepcopy(rows)

    def replace(self, channel, rows, source="live"):
        groups = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            groups.setdefault(str(row.get("realm") or ""), []).append(row)
        if not groups:
            return
        now = time.time()
        with self._lock:
            section = dict(self._channels.get(str(channel)) or {})
            for realm, items in groups.items():
                section[realm] = {"at": now, "source": str(source or "live"),
                                  "models": list(items)}
            self._channels[str(channel)] = section
            self._updated_at = now
            self._save()

    def touch(self):
        with self._lock:
            self._updated_at = time.time()
            self._save()

    def updated_at(self):
        with self._lock:
            return self._updated_at

    def meta(self):
        with self._lock:
            channels = {}
            for name, section in self._channels.items():
                realms = {}
                total = 0
                at = 0.0
                source = ""
                for realm, item in (section or {}).items():
                    item = item or {}
                    models = item.get("models") or []
                    total += len(models)
                    at = max(at, float(item.get("at") or 0))
                    source = source or str(item.get("source") or "")
                    realms[realm] = {"at": float(item.get("at") or 0),
                                     "count": len(models),
                                     "source": str(item.get("source") or "")}
                if total:
                    channels[name] = {"at": at, "count": total,
                                      "source": source, "realms": realms}
            return {"updated_at": self._updated_at, "channels": channels}
