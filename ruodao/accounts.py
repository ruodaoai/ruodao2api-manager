"""Unified account pool: storage, rotation, cooldown and public views."""

import hashlib
import threading
import time

from . import util
from .errors import UpstreamError

CHANNEL_LABELS = {
    "qoder": "Qoder",
    "codebuddy": "WorkBuddy",
    "zcode": "ZCode",
    "gpt": "GPT / OpenAI 兼容",
}

REALM_LABELS = {
    "cn": "国内版",
    "intl": "国际版",
    "global": "国际版",
    "zai": "Z.AI",
    "bigmodel": "BigModel",
}

_SECRET_MARKERS = ("token", "secret", "apikey", "api_key", "password",
                   "private", "credential")


class AccountManager(object):
    def __init__(self, store, channels, log=None):
        self.store = store
        self.channels = channels
        self.log = log or (lambda *a, **k: None)
        self.lock = threading.RLock()
        self._rr = {}
        self._affinity = {}
        self._cooldown = {}
        self._strikes = {}
        self._last_used = {}
        self._last_balance_refresh = {}
        self._oauth = {}

    # -- basic CRUD -------------------------------------------------------
    def add(self, channel, data, name="", realm="", account_id=None):
        data = dict(data or {})
        uid = str(data.get("uid") or data.get("id") or "")
        existing = self.store.find_account(channel, uid) if uid else None
        now = time.time()
        if existing:
            merged = dict(existing.get("data") or {})
            for key, value in data.items():
                if value not in (None, ""):
                    merged[key] = value
            existing["data"] = merged
            existing["name"] = name or existing.get("name") or merged.get("nickname") or uid or channel
            existing["realm"] = realm or existing.get("realm") or ""
            existing["enabled"] = True
            self.store.save_account(existing)
            self.log("INFO", "更新账号 %s/%s" % (channel, existing["name"]))
            return existing
        acc = {
            "id": account_id or util.new_id("acc_"),
            "channel": channel,
            "name": name or data.get("nickname") or data.get("name") or uid or channel,
            "realm": realm or data.get("realm") or "",
            "enabled": True,
            "data": data,
            "created_at": now,
            "updated_at": now,
        }
        self.store.save_account(acc)
        self.log("INFO", "添加账号 %s/%s" % (channel, acc["name"]))
        return acc

    def get(self, account_id):
        return self.store.get_account(account_id)

    def list(self, channel=None):
        return self.store.list_accounts(channel)

    def delete(self, account_id):
        acc = self.store.get_account(account_id)
        if not acc:
            return False
        self.store.delete_account(account_id)
        with self.lock:
            self._cooldown.pop(account_id, None)
            self._strikes.pop(account_id, None)
            for key in list(self._affinity):
                if self._affinity[key].get("account_id") == account_id:
                    self._affinity.pop(key, None)
        self.log("INFO", "删除账号 %s/%s" % (acc["channel"], acc["name"]))
        return True

    def save(self, account):
        self.store.save_account(account)

    def update_fields(self, account_id, data_patch=None, name=None, enabled=None):
        acc = self.store.get_account(account_id)
        if not acc:
            raise UpstreamError("账号不存在", status=404)
        if data_patch:
            acc["data"].update(data_patch)
        if name is not None:
            acc["name"] = str(name)
        if enabled is not None:
            acc["enabled"] = bool(enabled)
        self.store.save_account(acc)
        return acc

    # -- credentials ------------------------------------------------------
    def credential(self, account):
        return str(account.get("data", {}).get("access_token")
                   or account.get("data", {}).get("api_key") or "")

    def has_credential(self, account):
        return bool(self.credential(account))

    def refresh_account(self, account, force=False):
        channel = self.channels.get(account.get("channel"))
        if not channel:
            return False
        data = account.get("data") or {}
        exp = util.normalize_epoch(data.get("expires_at"))
        if not force and exp and exp > time.time() + 600:
            return False
        try:
            changed = channel.refresh_account(account)
        except UpstreamError as exc:
            self.log("WARN", "刷新 %s 失败: %s" % (account.get("name"), exc.message))
            if exc.status in (401, 403):
                account["data"]["auth_error"] = exc.message
                account["data"]["needs_login"] = True
                self.store.save_account(account)
            return False
        except Exception as exc:
            self.log("WARN", "刷新 %s 异常: %s" % (account.get("name"), exc))
            return False
        if changed:
            account["data"].pop("needs_login", None)
            account["data"].pop("auth_error", None)
            self.store.save_account(account)
        return changed

    def ensure_ready(self, account):
        data = account.get("data") or {}
        exp = util.normalize_epoch(data.get("expires_at"))
        if exp and exp <= time.time() + 300:
            self.refresh_account(account, force=True)
        if data.get("needs_login"):
            raise UpstreamError("账号 %s 需要重新登录" % account.get("name"),
                                status=401, code="auth_expired")
        if not self.has_credential(account):
            raise UpstreamError("账号 %s 没有可用凭证" % account.get("name"),
                                status=401, code="no_credential")
        return account

    # -- rotation ---------------------------------------------------------
    def _affinity_key(self, channel, key):
        if not key:
            return ""
        blob = ("%s:%s" % (channel, key)).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def bind_affinity(self, channel, key, account_id, ttl=6 * 3600):
        if not key:
            return
        k = self._affinity_key(channel, key)
        with self.lock:
            self._affinity[k] = {"account_id": account_id, "at": time.time(),
                                 "ttl": ttl}

    def _affinity_hit(self, channel, key):
        if not key:
            return None
        k = self._affinity_key(channel, key)
        with self.lock:
            item = self._affinity.get(k)
            if not item:
                return None
            if time.time() - item["at"] > item["ttl"]:
                self._affinity.pop(k, None)
                return None
            return item["account_id"]

    def _is_cooling(self, account_id):
        item = self._cooldown.get(account_id)
        if not item:
            return 0
        remain = item[0] - time.time()
        if remain <= 0:
            self._cooldown.pop(account_id, None)
            return 0
        return remain

    def cooldown_remaining(self, account_id):
        with self.lock:
            return int(self._is_cooling(str(account_id or "")))

    def mark_error(self, account, error, cooldown=None):
        if not account:
            return
        status = getattr(error, "status", 502) if error else 502
        account_id = account["id"]
        if status == 429:
            with self.lock:
                strikes = min(4, self._strikes.get(account_id, 0) + 1)
                self._strikes[account_id] = strikes
            if cooldown is None:
                cooldown = min(600, max(30, int(getattr(error, "wait", 60))))
            cooldown = min(1800, int(cooldown) * (2 ** (strikes - 1)))
        elif cooldown is None:
            if status == 401:
                cooldown = 0
            elif status >= 500:
                cooldown = 15
            else:
                cooldown = 5
        if cooldown > 0:
            with self.lock:
                self._cooldown[account_id] = (time.time() + cooldown,
                                              str(getattr(error, "message", error) or "")[:200])
        if status in (401, 403):
            account["data"]["needs_login"] = True
            account["data"]["auth_error"] = str(getattr(error, "message", error) or "")[:300]
            self.store.save_account(account)

    def mark_ok(self, account, model="", usage=None):
        if not account:
            return
        with self.lock:
            self._cooldown.pop(account["id"], None)
            self._strikes.pop(account["id"], None)
            self._last_used[account["id"]] = time.time()
            if model:
                by_model = account["data"].setdefault("usage_models", {})
                by_model[model] = int(by_model.get(model) or 0) + 1
        if usage:
            account["data"]["last_usage"] = usage
            account["data"]["last_used_at"] = time.time()

    def pick(self, channel, model="", exclude=None, affinity_key="",
             model_info=None):
        exclude = set(exclude or [])
        accounts = [a for a in self.list(channel) if a["enabled"]
                    and a["id"] not in exclude]
        if not accounts:
            raise UpstreamError("渠道 %s 没有可用账号，请先在面板添加"
                                % CHANNEL_LABELS.get(channel, channel), status=503)
        channel_obj = self.channels.get(channel)
        ready = []
        for acc in accounts:
            if not self.has_credential(acc):
                continue
            data = acc.get("data") or {}
            if data.get("needs_login"):
                continue
            if self._is_cooling(acc["id"]):
                continue
            if channel_obj and model_info is not None:
                try:
                    if not channel_obj.supports_model(acc, model_info):
                        continue
                except Exception:
                    pass
            ready.append(acc)
        if not ready:
            cooling = [(self._is_cooling(a["id"]), a) for a in accounts]
            cooling = [x for x in cooling if x[0] > 0]
            if cooling:
                wait = int(min(x[0] for x in cooling)) + 1
                raise UpstreamError("该渠道账号都在冷却中，请 %d 秒后重试" % wait,
                                    status=429, code="cooling", retryable=True)
            raise UpstreamError("渠道 %s 的账号缺少凭证或需要重新登录"
                                % CHANNEL_LABELS.get(channel, channel), status=503)

        hit = self._affinity_hit(channel, affinity_key)
        if hit:
            for acc in ready:
                if acc["id"] == hit:
                    return acc
        with self.lock:
            idx = self._rr.get(channel, 0)
            self._rr[channel] = (idx + 1) % max(1, len(ready))
        return ready[idx % len(ready)]

    # -- status / views ---------------------------------------------------
    def status_of(self, account):
        data = account.get("data") or {}
        if not account.get("enabled"):
            return "disabled", "已停用"
        if data.get("needs_login"):
            return "expired", "需重新登录"
        if not self.has_credential(account):
            return "expired", "无凭证"
        exp = util.normalize_epoch(data.get("expires_at"))
        can_refresh = bool(data.get("refresh_token") or data.get("personal_token"))
        if exp and exp <= time.time() and not can_refresh:
            return "expired", "已过期"
        remain = self._is_cooling(account["id"])
        if remain:
            return "cooling", "冷却 %d 秒" % int(remain)
        if exp and exp <= time.time() + 300:
            return "warn", "即将过期"
        return "ready", "可用"

    def _masked_data(self, account):
        data = account.get("data") or {}
        out = {}
        for key, value in data.items():
            low = key.lower()
            if any(marker in low for marker in _SECRET_MARKERS):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = value
            elif isinstance(value, list):
                out[key] = value[:20]
            elif isinstance(value, dict):
                if key in ("credits", "last_checkin", "last_usage"):
                    out[key] = value
                elif key in ("usage_models",):
                    out[key] = value
                else:
                    out[key] = {k: v for k, v in value.items()
                                if not any(m in str(k).lower() for m in _SECRET_MARKERS)}
        return out

    def public(self, account):
        channel = self.channels.get(account.get("channel"))
        status, status_label = self.status_of(account)
        data = self._masked_data(account)
        raw = account.get("data") or {}
        view = {
            "id": account["id"],
            "channel": account["channel"],
            "channel_label": CHANNEL_LABELS.get(account["channel"],
                                                account["channel"]),
            "name": account.get("name") or "",
            "realm": account.get("realm") or "",
            "realm_label": REALM_LABELS.get(account.get("realm"), account.get("realm") or ""),
            "enabled": bool(account.get("enabled")),
            "status": status,
            "status_label": status_label,
            "created_at": account.get("created_at"),
            "updated_at": account.get("updated_at"),
            "source": raw.get("source") or "",
            "plan": raw.get("plan") or "",
            "uid": raw.get("uid") or raw.get("id") or "",
            "nickname": raw.get("nickname") or "",
            "org_name": raw.get("organization_name") or raw.get("org_name") or "",
            "expires_at": util.normalize_epoch(raw.get("expires_at")),
            "access_token": util.mask_secret(raw.get("access_token")
                                             or raw.get("api_key") or ""),
            "has_refresh_token": bool(raw.get("refresh_token")),
            "credits": raw.get("credits"),
            "last_checkin": raw.get("last_checkin"),
            "last_checkin_message": raw.get("last_checkin_message") or "",
            "data": data,
            "checkin_supported": bool(channel and channel.supports_checkin),
            "recheck_needed": bool(raw.get("needs_login")),
        }
        if channel:
            try:
                view.update(channel.account_public_extra(account) or {})
            except Exception:
                pass
        return view

    def public_list(self, channel=None):
        return [self.public(a) for a in self.list(channel)]

    def checkin_eligible(self, account):
        if not account.get("enabled"):
            return False
        channel = self.channels.get(account.get("channel"))
        if not channel or not channel.supports_checkin:
            return False
        if not self.has_credential(account):
            return False
        data = account.get("data") or {}
        if data.get("needs_login"):
            return False
        return True

    # -- balances ---------------------------------------------------------
    def refresh_balance(self, account, force=False):
        channel = self.channels.get(account.get("channel"))
        if not channel:
            return None
        last = self._last_balance_refresh.get(account["id"], 0)
        if not force and time.time() - last < 30:
            return None
        try:
            result = channel.fetch_account_usage(account)
        except Exception as exc:
            self.log("WARN", "刷新余额 %s 失败: %s" % (account.get("name"), exc))
            return None
        self._last_balance_refresh[account["id"]] = time.time()
        if result:
            data = account.get("data") or {}
            if result.get("credits") is not None:
                data["credits"] = result["credits"]
            if result.get("plan"):
                data["plan"] = result["plan"]
            data["credits_updated_at"] = time.time()
            self.store.save_account(account)
        return result

    def refresh_all_balances(self, force=False):
        for account in self.list():
            if not account.get("enabled"):
                continue
            try:
                self.refresh_balance(account, force=force)
            except Exception:
                continue

    def summary(self):
        accounts = self.list()
        out = {"total": len(accounts), "enabled": 0, "ready": 0,
               "cooling": 0, "expired": 0, "channels": {}}
        for acc in accounts:
            view = self.public(acc)
            if view["enabled"]:
                out["enabled"] += 1
            if view["status"] == "ready":
                out["ready"] += 1
            elif view["status"] == "cooling":
                out["cooling"] += 1
            elif view["status"] == "expired":
                out["expired"] += 1
            ch = view["channel"]
            slot = out["channels"].setdefault(ch, {
                "label": CHANNEL_LABELS.get(ch, ch), "total": 0, "ready": 0,
                "accounts": 0})
            slot["total"] += 1
            if view["status"] == "ready":
                slot["ready"] += 1
        return out

    # -- oauth state ------------------------------------------------------
    def remember_oauth(self, channel, state, payload):
        with self.lock:
            self._oauth[state] = {"channel": channel, "at": time.time(),
                                  "payload": payload}

    def take_oauth(self, state):
        with self.lock:
            return self._oauth.get(state)

    def forget_oauth(self, state):
        with self.lock:
            self._oauth.pop(state, None)

    def login_state(self, state):
        with self.lock:
            item = self._oauth.get(state)
            if not item:
                return None
            if time.time() - item["at"] > 900:
                self._oauth.pop(state, None)
                return None
            return item
