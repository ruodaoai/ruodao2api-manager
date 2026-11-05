"""Client API key pool: creation, authentication, expiry and per-key usage."""

import hmac
import os
import time

from . import util
from .errors import UpstreamError


class ApiKeyManager(object):
    def __init__(self, store, config, log=None):
        self.store = store
        self.config = config
        self.log = log or (lambda *a, **k: None)
        self._last_touch = {}
        self._migrate()

    # -- migration --------------------------------------------------------
    def _migrate(self):
        keys = self.store.list_api_keys()
        env_key = (os.environ.get("RUDAO_API_KEY") or "").strip()
        if not keys:
            secret = env_key or self.config.api_key or util.make_api_key()
            key = self.store.insert_api_key({
                "id": util.new_id("key_"),
                "name": "默认密钥",
                "secret": secret,
                "enabled": True,
            })
            self.store.backfill_usage_key(key["id"], key["name"])
            self.store.set_setting("default_api_key_id", key["id"])
            self.config.set("api_key", secret)
            return

        default_id = self.store.get_setting("default_api_key_id")
        current = self.store.get_api_key(default_id) if default_id else None
        if not current:
            current = keys[0]
            self.store.set_setting("default_api_key_id", current["id"])
        self._sync_default_config(current)

    @staticmethod
    def _find_by_secret(keys, secret):
        target = str(secret or "").encode("utf-8")
        for key in keys:
            if hmac.compare_digest(key["secret"].encode("utf-8"), target):
                return key
        return None

    def _sync_default_config(self, current=None):
        keys = self.store.list_api_keys()
        now = time.time()
        if current is None:
            default_id = self.store.get_setting("default_api_key_id")
            current = next((k for k in keys if k["id"] == default_id), None)
        if not current or not self._usable(current, now):
            usable = [k for k in keys if self._usable(k, now)]
            if usable:
                current = usable[0]
                self.store.set_setting("default_api_key_id", current["id"])
        if current and self.config.api_key != current["secret"]:
            self.config.set("api_key", current["secret"])

    @staticmethod
    def _usable(key, now=None):
        if not key or not key.get("enabled"):
            return False
        expires_at = float(key.get("expires_at") or 0)
        return not expires_at or expires_at > float(now or time.time())

    def _count_enabled(self):
        return sum(1 for k in self.store.list_api_keys() if k["enabled"])

    def _public(self, key, usage, today):
        now = time.time()
        expires_at = float(key.get("expires_at") or 0)
        last_used = max(float(key.get("last_used_at") or 0),
                        float(usage.get("last_request_at") or 0))
        return {
            "id": key["id"],
            "name": key["name"],
            "prefix": key["secret"][:14],
            "masked": util.mask_secret(key["secret"], 12, 4),
            "channel": key.get("channel") or "",
            "enabled": bool(key["enabled"]),
            "expired": bool(expires_at and expires_at <= now),
            "expires_at": expires_at,
            "created_at": float(key.get("created_at") or 0),
            "last_used_at": last_used,
            "usage": {
                "requests": int(usage.get("requests") or 0),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "errors": int(usage.get("errors") or 0),
                "today_requests": int(today.get("requests") or 0),
                "today_total_tokens": int(today.get("total_tokens") or 0),
            },
        }

    # -- queries ----------------------------------------------------------
    def list_public(self):
        totals = self.store.usage_by_key(0)
        today = self.store.usage_by_key(time.time() - 86400)
        return [self._public(key, totals.get(key["id"]) or {},
                             today.get(key["id"]) or {})
                for key in self.store.list_api_keys()]

    def get_public(self, key_id):
        key = self._get(key_id)
        totals = self.store.usage_by_key(0)
        today = self.store.usage_by_key(time.time() - 86400)
        return self._public(key, totals.get(key["id"]) or {},
                            today.get(key["id"]) or {})

    def _get(self, key_id):
        key = self.store.get_api_key(str(key_id or ""))
        if not key:
            raise UpstreamError("API Key 不存在", status=404)
        return key

    # -- mutations --------------------------------------------------------
    def create(self, name, expires_at=0, channel=""):
        name = self._clean_name(name, allow_empty=True)
        channel = self._clean_channel(channel)
        secret = self._new_secret()
        key = self.store.insert_api_key({
            "id": util.new_id("key_"),
            "name": name,
            "secret": secret,
            "channel": channel,
            "enabled": True,
            "expires_at": self._clean_expiry(expires_at),
        })
        self.log("INFO", "创建 API Key：%s%s" % (
            name, ("（绑定 %s）" % channel) if channel else ""))
        return self.get_public(key["id"]), secret

    def update(self, key_id, patch):
        key = self._get(key_id)
        fields = {}
        if "name" in patch:
            fields["name"] = self._clean_name(patch.get("name"), allow_empty=False)
        if "enabled" in patch:
            enabled = bool(patch.get("enabled"))
            if key["enabled"] and not enabled and self._count_enabled() <= 1:
                raise ValueError("至少保留一个启用的 API Key")
            fields["enabled"] = enabled
        if "expires_at" in patch:
            fields["expires_at"] = self._clean_expiry(patch.get("expires_at"))
        if "channel" in patch:
            fields["channel"] = self._clean_channel(patch.get("channel"))
        if fields:
            self.store.update_api_key(key["id"], fields)
            self._sync_default_config()
        return self.get_public(key["id"])

    def delete(self, key_id):
        key = self._get(key_id)
        if key["enabled"] and self._count_enabled() <= 1:
            raise ValueError("至少保留一个启用的 API Key")
        self.store.delete_api_key(key["id"])
        if self.store.get_setting("default_api_key_id") == key["id"]:
            self.store.set_setting("default_api_key_id", "")
        self._sync_default_config()
        self.log("INFO", "删除 API Key：%s" % key["name"])
        return True

    def reveal(self, key_id):
        return self._get(key_id)["secret"]

    def rotate(self, key_id):
        key = self._get(key_id)
        secret = self._new_secret()
        self.store.update_api_key(key["id"], {"secret": secret})
        self._sync_default_config()
        self.log("INFO", "重置 API Key：%s" % key["name"])
        return self.get_public(key["id"]), secret

    def default_secret(self):
        self._sync_default_config()
        default_id = self.store.get_setting("default_api_key_id")
        key = self.store.get_api_key(default_id)
        if not key:
            keys = self.store.list_api_keys()
            key = keys[0] if keys else None
        if not key:
            raise ValueError("没有可用的 API Key")
        return key["secret"]

    def rotate_default(self):
        default_id = self.store.get_setting("default_api_key_id")
        keys = self.store.list_api_keys()
        key = next((k for k in keys if k["id"] == default_id), None) or \
            (keys[0] if keys else None)
        if not key:
            raise ValueError("没有可重置的 API Key")
        public, secret = self.rotate(key["id"])
        return secret

    # -- authentication ---------------------------------------------------
    def authenticate(self, secret):
        supplied = str(secret or "")
        if not supplied:
            return None, "无效的 API Key"
        key = self._find_by_secret(self.store.list_api_keys(), supplied)
        if not key:
            return None, "无效的 API Key"
        if not key["enabled"]:
            return None, "API Key 已停用"
        expires_at = float(key.get("expires_at") or 0)
        if expires_at and expires_at <= time.time():
            return None, "API Key 已过期"
        self._touch(key)
        return key, ""

    def _touch(self, key):
        now = time.time()
        previous = self._last_touch.get(key["id"]) or 0
        if now - previous < 20:
            return
        self._last_touch[key["id"]] = now
        try:
            self.store.touch_api_key(key["id"], now)
        except Exception:
            pass

    # -- helpers ----------------------------------------------------------
    def _new_secret(self):
        for _ in range(8):
            secret = util.make_api_key()
            if not self._find_by_secret(self.store.list_api_keys(), secret):
                return secret
        raise RuntimeError("无法生成唯一的 API Key")

    @staticmethod
    def _clean_name(name, allow_empty):
        text = str(name or "").strip()
        if not text:
            if allow_empty:
                return "未命名密钥"
            raise ValueError("密钥名称不能为空")
        return text[:60]

    @staticmethod
    def _clean_expiry(value):
        if value in (None, "", 0, "0"):
            return 0
        try:
            return max(0, int(float(value)))
        except Exception:
            raise ValueError("有效期格式不正确")

    @staticmethod
    def _clean_channel(value):
        text = str(value or "").strip().lower()
        if text in ("", "all", "*"):
            return ""
        from .runtime import CHANNEL_META
        if text not in CHANNEL_META:
            raise ValueError("渠道不存在：%s" % value)
        return text
