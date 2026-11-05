"""Application runtime: configuration, storage, channels and account pool."""

import sys
import time

from . import util
from .accounts import AccountManager
from .apikeys import ApiKeyManager
from .channels.codebuddy import CodeBuddyChannel
from .channels.openai_compat import OpenAICompatChannel
from .channels.qoder import QoderChannel
from .channels.zcode import ZCodeChannel
from .config import DB_PATH, Config
from .guard import Guard
from .store import Store


CHANNEL_META = {
    "qoder": {"label": "Qoder", "color": "#6d5ce7",
              "realm_options": [["cn", "国内版"], ["intl", "国际版"]],
              "login": "oauth", "local_import": True},
    "codebuddy": {"label": "WorkBuddy", "color": "#2f7de1",
                  "realm_options": [["cn", "国内版"], ["global", "国际版"]],
                  "login": "oauth", "local_import": True},
    "zcode": {"label": "ZCode", "color": "#c4552d",
              "realm_options": [["zai", "Z.AI"], ["bigmodel", "BigModel"]],
              "login": "oauth", "local_import": True},
    "gpt": {"label": "GPT / OpenAI 兼容", "color": "#2f8f6b",
            "realm_options": [], "login": "manual", "local_import": False},
}


class Runtime(object):
    def __init__(self):
        self.config = Config()
        self.store = Store(DB_PATH)
        self.started_at = time.time()
        self._log_sink = []
        self.api_keys = ApiKeyManager(self.store, self.config, self.log)
        self.channels = {
            "qoder": QoderChannel(self),
            "codebuddy": CodeBuddyChannel(self),
            "zcode": ZCodeChannel(self),
            "gpt": OpenAICompatChannel(self),
        }
        self.accounts = AccountManager(self.store, self.channels, self.log)
        self.guard = Guard(self)
        self.scheduler = None

    # -- logging ----------------------------------------------------------
    def log(self, level, message):
        line = str(message or "")
        try:
            self.store.add_event(level, line)
        except Exception:
            pass
        try:
            print("[%s] %s %s" % (util.iso_now(), str(level).upper(), line),
                  flush=True)
        except Exception:
            pass

    # -- views ------------------------------------------------------------
    def channel(self, name):
        return self.channels.get(name)

    def channel_list(self):
        out = []
        counts = {}
        for acc in self.accounts.list():
            slot = counts.setdefault(acc["channel"], {"total": 0, "ready": 0})
            slot["total"] += 1
            if self.accounts.status_of(acc)[0] == "ready":
                slot["ready"] += 1
        for name, meta in CHANNEL_META.items():
            channel = self.channels.get(name)
            slot = counts.get(name) or {"total": 0, "ready": 0}
            out.append({
                "name": name,
                "label": meta["label"],
                "color": meta["color"],
                "realm_options": meta["realm_options"],
                "login": meta["login"],
                "local_import": meta["local_import"],
                "supports_checkin": bool(channel and channel.supports_checkin),
                "total": slot["total"],
                "ready": slot["ready"],
            })
        return out

    def all_models(self):
        out = []
        for channel in self.channels.values():
            try:
                out.extend(channel.list_models())
            except Exception as exc:
                self.log("WARN", "读取 %s 模型失败: %s" % (channel.label, exc))
        return out

    def find_model(self, requested, channel=None):
        found = self._find_models(requested, channel)
        return found[0] if found else None

    def find_models(self, requested, channel=None):
        return self._find_models(requested, channel)

    def _find_models(self, requested, channel=None):
        name = str(requested or "").strip()
        if not name:
            name = "auto"
        lowered = name.lower()
        bound = channel if channel in self.channels else ""
        for channel_name, item in self.channels.items():
            if lowered.startswith(channel_name + "/"):
                if bound and channel_name != bound:
                    return []
                found = item.find_model(name)
                if found:
                    found["channel"] = channel_name
                    return [found]
                return []
        if bound:
            matches = []
            seen = set()
            try:
                for model in self.channels[bound].list_models():
                    raw = str(model.get("raw_id") or model.get("id") or "")
                    if lowered not in (str(model.get("id") or "").lower(),
                                       raw.lower()):
                        continue
                    found = self.channels[bound].find_model(
                        model.get("id") or raw)
                    if not found:
                        continue
                    marker = (found.get("realm") or "", raw)
                    if marker not in seen:
                        seen.add(marker)
                        found["channel"] = bound
                        matches.append(found)
            except Exception:
                pass
            if matches:
                return matches
            found = self.channels[bound].find_model(name)
            if found:
                found["channel"] = bound
                found["unknown"] = True
                return [found]
            return []
        matches = []
        seen = set()
        for channel_name, channel in self.channels.items():
            try:
                for model in channel.list_models():
                    raw = str(model.get("raw_id") or model.get("id") or "")
                    if lowered in (str(model.get("id") or "").lower(),
                                   raw.lower()):
                        found = channel.find_model(
                            model.get("id") or model.get("raw_id"))
                        if found and raw not in seen:
                            seen.add(raw)
                            found["channel"] = channel_name
                            matches.append(found)
                        break
            except Exception:
                continue
        if matches:
            return matches
        # bare unknown model: choose the most plausible channel family
        preferred = "gpt"
        if lowered.startswith(("glm-", "glm.")):
            preferred = "zcode"
        elif lowered.startswith(("deepseek", "kimi", "hy", "hunyuan",
                                 "minimax", "gemini-")):
            preferred = "codebuddy"
        elif lowered.startswith(("qwen", "qwen2", "qwen3")):
            preferred = "qoder"
        channel = self.channels.get(preferred)
        found = channel.find_model(name) if channel else None
        if not found:
            return []
        found["channel"] = preferred
        found["unknown"] = True
        return [found]

    # -- status -----------------------------------------------------------
    def status(self):
        summary = self.accounts.summary()
        usage = self.store.usage_summary()
        return {
            "version": "0.1.0",
            "uptime": int(time.time() - self.started_at),
            "started_at": self.started_at,
            "host": self.config.host,
            "port": self.config.port,
            "base_url": self.config.base_url,
            "openai_base": self.config.base_url + "/v1",
            "api_key": util.mask_secret(self.config.api_key, 10, 4),
            "data_dir": str(self.store.path),
            "accounts": summary,
            "usage": usage,
            "channels": self.channel_list(),
            "scheduler": bool(self.scheduler and self.scheduler.alive()),
            "checkin_enabled": bool(self.config.get("checkin_enabled", True)),
            "settings": {
                "checkin_times": self.config.get("checkin_times"),
                "refresh_time": self.config.get("refresh_time"),
                "credits_refresh_minutes": self.config.get("credits_refresh_minutes"),
                "scheduler_enabled": self.config.get("scheduler_enabled"),
            },
        }

    def close(self):
        if self.scheduler:
            self.scheduler.stop()
        try:
            self.store.close()
        except Exception:
            pass


def create_runtime():
    if sys.version_info < (3, 9):
        raise SystemExit("需要 Python 3.9 或更高版本")
    return Runtime()
