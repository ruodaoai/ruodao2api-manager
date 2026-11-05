"""WorkBuddy / CodeBuddy channel: QR login, token refresh, chat, credits, check-in."""

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import uuid

from .. import util
from ..errors import AuthExpired, UpstreamError
from ..qoder import sign
from .base import BaseChannel

REALMS = {
    "cn": {
        "name": "国内版",
        "chat_base": "https://copilot.tencent.com",
        "billing_base": "https://www.codebuddy.cn",
        "origin": "https://www.codebuddy.cn",
        "platform": "WorkBuddy",
        "checkin": True,
    },
    "global": {
        "name": "国际版",
        "chat_base": "https://www.workbuddy.ai",
        "billing_base": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "platform": "WorkBuddy AI",
        "checkin": False,
    },
}

PATH_LOGIN_STATE = "/v2/plugin/auth/state"
PATH_LOGIN_TOKEN = "/v2/plugin/auth/token"
PATH_LOGIN_ACCOUNT = "/v2/plugin/login/account"
PATH_REFRESH = "/v2/plugin/auth/token/refresh"
PATH_ENTERPRISE_MODELS = "/console/enterprises/personal/models"
PATH_V3_CONFIG = "/v3/config"
PATH_ENTERPRISE_USAGE = "/v2/billing/meter/get-enterprise-user-usage"

CLIENT_VERSION = os.environ.get("RUODAO_WORKBUDDY_VERSION", "5.5.4").strip() or "5.5.4"
CLI_VERSION = os.environ.get("RUODAO_WORKBUDDY_CLI_VERSION", "2.137.1").strip() or "2.137.1"
LOGIN_TTL = 900
MODEL_TTL = 300

ALREADY_MARKERS = ("已签到", "已经签到", "already checked in",
                   "already signed in", "already checkin")

FALLBACK_MODELS = {
    "cn": [
        ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash", 128000),
        ("deepseek-v4-flash", "DeepSeek V4 Flash", 128000),
        ("deepseek-v4-pro", "DeepSeek V4 Pro", 128000),
        ("glm-5.3", "GLM-5.3", 200000),
        ("glm-5.3-flash", "GLM-5.3 Flash", 200000),
        ("glm-5.2", "GLM-5.2", 200000),
        ("glm-5.1", "GLM-5.1", 200000),
        ("glm-5v-turbo", "GLM-5V Turbo", 128000),
        ("hy3", "混元 3", 128000),
        ("hy3-x", "混元 3 X", 128000),
        ("hy4-preview", "混元 4 Preview", 128000),
        ("hy4-preview-x", "混元 4 Preview X", 128000),
        ("kimi-k3-1", "Kimi K3.1", 200000),
        ("kimi-k2.7", "Kimi K2.7", 200000),
        ("kimi-k2.6", "Kimi K2.6", 200000),
        ("minimax-m3", "MiniMax M3", 200000),
    ],
    "global": [
        ("fast-model", "Fast Model", 128000),
        ("balanced-model", "Balanced Model", 128000),
        ("primary-model", "Primary Model", 200000),
        ("gpt-6-astra", "GPT-6 Astra", 400000),
        ("gpt-5.6-sol", "GPT-5.6 Sol", 400000),
        ("gpt-5.6-terra", "GPT-5.6 Terra", 400000),
        ("gpt-5.6-luna", "GPT-5.6 Luna", 400000),
        ("gpt-5.5", "GPT-5.5", 400000),
        ("gpt-5.4", "GPT-5.4", 400000),
        ("gpt-5.3-codex", "GPT-5.3 Codex", 400000),
        ("gemini-3.5-flash", "Gemini 3.5 Flash", 1000000),
        ("glm-5.3", "GLM-5.3", 200000),
        ("glm-5.2", "GLM-5.2", 200000),
        ("kimi-k3", "Kimi K3", 200000),
        ("kimi-k2.6", "Kimi K2.6", 200000),
        ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash", 128000),
        ("hy4-preview-f", "混元 4 Preview F", 128000),
        ("hy3", "混元 3", 128000),
    ],
}

_MODEL_CACHE = {"cn": {"at": 0.0, "models": []},
                "global": {"at": 0.0, "models": []}}
_MODEL_LOCK = threading.Lock()


def _realm(realm):
    return REALMS.get(realm) or REALMS["cn"]


def _realm_of(data):
    realm = str((data or {}).get("realm") or "").strip().lower()
    if realm in REALMS:
        return realm
    domain = str((data or {}).get("domain") or "").strip().lower()
    if domain == "workbuddy.ai" or domain.endswith(".workbuddy.ai"):
        return "global"
    return "cn"


def _ua(realm):
    cfg = _realm(realm)
    return "WorkBuddy/%s %s/%s CLI/%s" % (
        CLIENT_VERSION, cfg["platform"], CLIENT_VERSION, CLI_VERSION)


def _billing_ua(realm):
    return "WorkBuddy/%s" % CLIENT_VERSION


def _fingerprint(uid):
    uid = str(uid or "").strip()
    if not uid:
        return {}

    def derive(purpose):
        blob = ("wb2a:%s:%s" % (purpose, uid)).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:36]

    return {"X-Machine-ID": derive("machine"), "X-Session-ID": derive("session")}


def _base_headers(realm, token=None, uid=None):
    cfg = _realm(realm)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": "en-US" if realm == "global" else "zh-CN",
        "X-Requested-With": "XMLHttpRequest",
        "X-CodeBuddy-Request": "1",
        "User-Agent": _ua(realm),
        "Origin": cfg["origin"],
        "Referer": cfg["origin"] + "/",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    headers.update(_fingerprint(uid))
    return headers


def _billing_headers(account):
    data = account.get("data") or {}
    realm = _realm_of(data)
    headers = _base_headers(realm, str(data.get("access_token") or ""),
                            str(data.get("uid") or ""))
    headers["User-Agent"] = _billing_ua(realm)
    uid = str(data.get("uid") or "")
    if uid:
        headers["X-User-Id"] = uid
    enterprise = str(data.get("enterprise_id") or "")
    if enterprise:
        headers["X-Enterprise-Id"] = enterprise
        headers["X-Tenant-Id"] = enterprise
    domain = str(data.get("domain") or "")
    if domain:
        headers["X-Domain"] = domain
    device_token = str(data.get("device_token") or "")
    if device_token:
        headers["X-Device-Token"] = device_token
    return headers


def _chat_headers(account, body, model):
    data = account.get("data") or {}
    realm = _realm_of(data)
    uid = str(data.get("uid") or "")
    token = str(data.get("access_token") or "")
    headers = _base_headers(realm, token, uid)
    headers["Accept"] = "application/json, text/event-stream"
    if uid:
        headers["X-User-Id"] = uid
    else:
        headers["X-No-User-Id"] = "1"
    enterprise = str(data.get("enterprise_id") or "")
    domain = str(data.get("domain") or "")
    if realm == "global":
        headers["X-No-Enterprise-Id"] = "1"
        headers["X-Domain"] = "www.workbuddy.ai"
    else:
        if enterprise:
            headers["X-Enterprise-Id"] = enterprise
        else:
            headers["X-No-Enterprise-Id"] = "1"
        if domain:
            headers["X-Domain"] = domain
        else:
            headers["X-No-Department-Info"] = "1"
    headers["X-Product"] = "WorkBuddy"
    headers.update({
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Version": CLIENT_VERSION,
        "X-Product": "WorkBuddy",
    })
    device_token = str(data.get("device_token") or "")
    if device_token:
        headers["X-Device-Token"] = device_token
    seed = str(body.get("conversationId") or body.get("conversation_id") or "")
    if not seed:
        for message in body.get("messages") or []:
            if isinstance(message, dict) and message.get("role") == "user":
                seed = util.text_from_content(message.get("content"))[:2000]
                if seed:
                    break
    if not seed:
        seed = "%s:%s" % (model, len(body.get("messages") or []))
    conversation = hashlib.sha256(
        ("codebuddy:%s:%s" % (uid, seed)).encode("utf-8")).hexdigest()[:32]
    headers.update({
        "X-Conversation-ID": conversation,
        "X-Conversation-Request-ID": uuid.uuid4().hex,
        "X-Conversation-Message-ID": uuid.uuid4().hex,
        "X-Root-Request-ID": uuid.uuid4().hex,
        "X-Trace-ID": uuid.uuid4().hex,
    })
    return headers


def _envelope(status, obj):
    if isinstance(obj, dict) and "code" in obj:
        try:
            return int(obj.get("code"))
        except Exception:
            return status
    return int(status or 0)


def _is_channel_check(code, raw):
    text = str(code or "")
    if "11-128" not in text and "11128" not in text:
        return False
    low = str(raw or "").lower()
    return ("渠道校验" in low or "channel verification" in low
            or "official client" in low or "conversation id missing" in low)


def _is_already(code, message):
    if code != 10001:
        return False
    message = str(message or "").strip().lower()
    if not message:
        return True
    return any(marker in message for marker in ALREADY_MARKERS)


def _round_credits(value):
    try:
        value = float(value)
    except Exception:
        return 0
    return int(value) if value.is_integer() else round(value, 2)


def _num(item, key):
    value = (item or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _package_remain(item):
    size = _num(item, "CycleCapacitySize")
    if size > 0:
        remain = min(max(_num(item, "CycleCapacityRemain"), 0.0), size)
        used = size - remain
        cycle_used = _num(item, "CycleCapacityUsed")
        if cycle_used > used:
            used = cycle_used
            if size >= used:
                remain = size - used
        return remain
    return _num(item, "CapacityRemain")


def _resource_accounts(data):
    current = data
    for _ in range(4):
        if isinstance(current, dict):
            if isinstance(current.get("Accounts"), list):
                return current["Accounts"]
            for key in ("Response", "Data", "data", "response"):
                if isinstance(current.get(key), (dict, list)):
                    current = current[key]
                    break
            else:
                return None
        elif isinstance(current, list):
            return current
        else:
            return None
    return None


def _parse_models(data, realm):
    if isinstance(data, list):
        items = []
        for raw in data:
            mid = str(raw or "").strip()
            if mid:
                items.append({"id": mid, "name": mid})
        return items
    if not isinstance(data, dict):
        return []
    raw_models = data.get("models") if isinstance(data.get("models"), list) else []
    agents = data.get("agents") if isinstance(data.get("agents"), list) else []
    allowed = []
    if realm == "cn":
        for agent in agents:
            if isinstance(agent, dict) and agent.get("name") == "cli":
                ids = agent.get("models")
                if isinstance(ids, list):
                    allowed = [str(x) for x in ids if x]
                break
    by_id = {}
    order = []
    for model in raw_models:
        if not isinstance(model, dict) or not model.get("id"):
            continue
        mid = str(model["id"])
        if model.get("disabled"):
            continue
        tags = [str(t) for t in (model.get("tags") or []) if t]
        low = mid.lower()
        if realm == "cn":
            if low.startswith(("nes-", "completion-", "codewise-")):
                continue
            if "text-to-image" in tags:
                continue
            if 0 < util.safe_int(model.get("maxOutputTokens")) <= 256:
                continue
        reasoning = model.get("reasoning") if isinstance(model.get("reasoning"), dict) else {}
        efforts = reasoning.get("supportedEfforts")
        by_id[mid] = {
            "id": mid,
            "name": str(model.get("name") or mid).strip(),
            "context_length": util.safe_int(model.get("maxInputTokens"), 128000),
            "max_output_tokens": util.safe_int(model.get("maxOutputTokens"), 0),
            "efforts": [str(x) for x in efforts if x] if isinstance(efforts, list) else [],
            "tags": tags,
            "vendor": str(model.get("vendor") or ""),
            "credits": str(model.get("credits") or ""),
            "supports_images": (model.get("supportsImages")
                                if type(model.get("supportsImages")) is bool else None),
            "supports_tool_call": bool(model.get("supportsToolCall", True)),
            "description": str(model.get("descriptionZh") or ""),
        }
        order.append(mid)
    if allowed:
        ids = [mid for mid in allowed if mid in by_id]
    else:
        ids = order
    return [by_id[mid] for mid in ids if mid in by_id]


class CodeBuddyChannel(BaseChannel):
    name = "codebuddy"
    label = "WorkBuddy"
    color = "#2f6fed"
    supports_checkin = True
    supports_oauth = True

    def __init__(self, app):
        super(CodeBuddyChannel, self).__init__(app)
        self._logins = {}
        self._login_lock = threading.Lock()

    # -- models -----------------------------------------------------------
    def _catalog(self, realm):
        with _MODEL_LOCK:
            slot = _MODEL_CACHE.get(realm) or {}
            models = list(slot.get("models") or [])
        if models:
            return models
        return [{
            "id": mid,
            "name": name,
            "context_length": context,
            "max_output_tokens": 0,
            "efforts": [],
            "tags": [],
            "vendor": "",
            "credits": "",
            "supports_images": None,
            "supports_tool_call": True,
            "description": "",
        } for mid, name, context in FALLBACK_MODELS.get(realm, [])]

    def refresh_catalog(self, account=None):
        accounts = [account] if account else [
            a for a in self.app.accounts.list("codebuddy") if a.get("enabled")]
        for acc in accounts:
            realm = _realm_of(acc.get("data") or {})
            try:
                models = self._fetch_models(acc)
            except Exception as exc:
                self.app.log("WARN", "WorkBuddy 模型目录刷新失败: %s" % exc)
                continue
            if models:
                with _MODEL_LOCK:
                    _MODEL_CACHE[realm] = {"at": time.time(), "models": models}
        return self.list_models()

    def list_models(self, account=None):
        if account:
            realm = _realm_of(account.get("data") or {})
            with _MODEL_LOCK:
                stale = time.time() - float(_MODEL_CACHE[realm].get("at") or 0)
            if stale > MODEL_TTL:
                try:
                    models = self._fetch_models(account)
                    if models:
                        with _MODEL_LOCK:
                            _MODEL_CACHE[realm] = {"at": time.time(), "models": models}
                except Exception:
                    pass
        out = []
        for realm in ("cn", "global"):
            for model in self._catalog(realm):
                mid = str(model.get("id") or "")
                if not mid:
                    continue
                out.append({
                    "id": "codebuddy/%s/%s" % (realm, mid),
                    "raw_id": mid,
                    "object": "model",
                    "channel": "codebuddy",
                    "channel_label": "WorkBuddy",
                    "realm": realm,
                    "realm_label": REALMS[realm]["name"],
                    "owned_by": "WorkBuddy %s" % REALMS[realm]["name"],
                    "display_name": model.get("name") or mid,
                    "created": 0,
                    "context_length": model.get("context_length") or 128000,
                    "capabilities": {
                        "vision": model.get("supports_images") is True,
                        "reasoning": bool(model.get("efforts")),
                        "tools": bool(model.get("supports_tool_call", True)),
                        "free": False,
                    },
                    "efforts": model.get("efforts") or [],
                    "description": model.get("description") or "",
                })
        return out

    def _fetch_models(self, account):
        data = account.get("data") or {}
        realm = _realm_of(data)
        token = str(data.get("access_token") or "")
        if not token:
            raise AuthExpired("WorkBuddy 账号没有 access token")
        uid = str(data.get("uid") or "")
        paths = [PATH_ENTERPRISE_MODELS]
        if realm == "global":
            paths = ["/v2/enterprises/personal/models", PATH_ENTERPRISE_MODELS]
        found = {}
        for path in paths:
            status, obj, raw = util.http_json(
                _realm(realm)["chat_base"] + path, method="GET",
                headers=_base_headers(realm, token, uid), timeout=25)
            if status >= 400:
                continue
            models = _parse_models(obj.get("data") if isinstance(obj, dict) else obj, realm)
            for model in models:
                found.setdefault(model["id"], model)
            if found:
                break
        status, obj, raw = util.http_json(
            _realm(realm)["chat_base"] + PATH_V3_CONFIG, method="GET",
            headers=_base_headers(realm, token, uid), timeout=25)
        if status < 400:
            for model in _parse_models(obj.get("data") if isinstance(obj, dict) else obj, realm):
                found[model["id"]] = model
        return list(found.values())

    def supports_model(self, account, model_info):
        realm = (model_info or {}).get("realm")
        if realm and realm != _realm_of(account.get("data") or {}):
            return False
        return True

    def find_model(self, requested):
        raw = str(requested or "").strip()
        if raw.lower().startswith("codebuddy/"):
            raw = raw[10:]
        realm = ""
        low = raw.lower()
        if low.startswith("cn/"):
            realm, raw = "cn", raw[3:]
        elif low.startswith(("global/", "intl/")):
            realm, raw = "global", low[7:] if low.startswith("global/") else raw[5:]
        realms = [realm] if realm else ["cn", "global"]
        for candidate in realms:
            for model in self._catalog(candidate):
                mid = str(model.get("id") or "")
                if raw == mid or raw.lower() == str(model.get("name") or "").lower():
                    return self._model_dict(candidate, model)
        if not realm:
            accounts = self.app.accounts.list("codebuddy")
            realm = "cn"
            for candidate in ("cn", "global"):
                if any(_realm_of(a.get("data") or {}) == candidate and a.get("enabled")
                       for a in accounts):
                    realm = candidate
                    break
        return {"id": "codebuddy/%s/%s" % (realm, raw), "raw_id": raw,
                "realm": realm, "display_name": raw, "unknown": True,
                "context_length": 128000, "capabilities": {}}

    def _model_dict(self, realm, model):
        mid = model.get("id") or ""
        return {
            "id": "codebuddy/%s/%s" % (realm, mid),
            "raw_id": mid,
            "realm": realm,
            "display_name": model.get("name") or mid,
            "context_length": model.get("context_length") or 128000,
            "capabilities": {
                "vision": model.get("supports_images") is True,
                "reasoning": bool(model.get("efforts")),
                "tools": bool(model.get("supports_tool_call", True)),
            },
            "meta": model,
        }

    def resolve_model(self, requested):
        return self.find_model(requested).get("raw_id")

    # -- inference --------------------------------------------------------
    def stream_chat(self, account, body, upstream_model):
        data = account.get("data") or {}
        realm = _realm_of(data)
        payload = dict(body or {})
        payload["model"] = upstream_model or payload.get("model")
        payload["stream"] = True
        payload.pop("stream_options", None)
        payload.pop("conversationId", None)
        payload.pop("conversation_id", None)
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise UpstreamError("消息列表为空", status=400)
        messages = [dict(m) if isinstance(m, dict) else m for m in messages]
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "developer":
            messages[0]["role"] = "system"
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": "You are a helpful assistant."})
        payload["messages"] = messages
        headers = _chat_headers(account, payload, str(payload.get("model") or ""))
        paths = ["/v2/chat/completions", "/console/chat/completions"]
        timeout = self._timeout()
        last_error = None
        for index, path in enumerate(paths):
            url = _realm(realm)["chat_base"] + path
            try:
                resp = util.open_stream(url, headers=headers,
                                        body=util.json_dumps(payload).encode("utf-8"),
                                        timeout=timeout)
            except util.HttpStatusError as exc:
                detail = exc.body.decode("utf-8", "replace")
                try:
                    obj = json.loads(detail)
                except Exception:
                    obj = None
                code = _envelope(exc.status, obj)
                fallback = (exc.status in (404, 405)
                            or _is_channel_check(code, detail))
                if fallback and index < len(paths) - 1:
                    continue
                raise self._http_error(exc.status, detail, obj, account)
            except Exception as exc:
                raise UpstreamError("连接 WorkBuddy 失败: %s" % exc,
                                    status=502, retryable=True)
            yielded = False
            try:
                from .base import iter_sse_json
                for chunk in iter_sse_json(resp):
                    yielded = True
                    yield chunk
            except UpstreamError as exc:
                if (not yielded and index < len(paths) - 1
                        and _is_channel_check(exc.code, exc.message)):
                    last_error = exc
                    continue
                raise
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            return
        if last_error:
            raise last_error
        raise UpstreamError("WorkBuddy 所有候选路径均不可用", status=502,
                            retryable=True)

    def _http_error(self, status, detail, obj, account):
        code = _envelope(status, obj)
        message = ""
        if isinstance(obj, dict):
            message = str(obj.get("msg") or obj.get("message") or "")
            err = obj.get("error")
            if isinstance(err, dict):
                message = message or str(err.get("message") or "")
        message = message or detail[:300] or ("HTTP %s" % status)
        if status in (401, 403) or str(code) in ("12153", "401", "403"):
            return AuthExpired("WorkBuddy 登录态失效: %s" % message,
                               account_id=account.get("id"))
        if status == 429 or str(code) in ("6004", "429"):
            return UpstreamError("WorkBuddy 上游限流: %s" % message, status=429,
                                 code=str(code), retryable=True)
        if status == 402 or str(code) in ("14018", "402"):
            return UpstreamError("WorkBuddy 额度不足: %s" % message, status=429,
                                 code=str(code), retryable=True)
        return UpstreamError("WorkBuddy 上游错误: %s" % message, status=status,
                             code=str(code), retryable=status >= 500)

    # -- account lifecycle -------------------------------------------------
    def refresh_account(self, account):
        data = account.get("data") or {}
        refresh = str(data.get("refresh_token") or "").strip()
        if not refresh:
            raise AuthExpired("WorkBuddy 账号没有 refreshToken，需要重新登录",
                              account_id=account.get("id"))
        realm = _realm_of(data)
        uid = str(data.get("uid") or "")
        headers = _base_headers(realm, str(data.get("access_token") or ""), uid)
        headers["X-Refresh-Token"] = refresh
        headers["X-Auth-Refresh-Source"] = "plugin"
        enterprise = str(data.get("enterprise_id") or "")
        if enterprise:
            headers["X-Enterprise-Id"] = enterprise
        if uid:
            headers["X-User-Id"] = uid
        status, obj, raw = util.http_json(
            _realm(realm)["chat_base"] + PATH_REFRESH, method="POST",
            headers=headers, body={}, timeout=30)
        if status == 429:
            raise UpstreamError("WorkBuddy 刷新接口限流", status=429, retryable=True)
        if status >= 400:
            if status in (401, 403):
                raise AuthExpired("WorkBuddy refreshToken 已失效",
                                  account_id=account.get("id"))
            raise UpstreamError("WorkBuddy 刷新失败 HTTP %s: %s"
                                % (status, raw[:200]), status=502)
        body = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else obj
        if not isinstance(body, dict):
            raise UpstreamError("WorkBuddy 刷新响应无法解析", status=502)
        access = str(body.get("accessToken") or "")
        if not access:
            raise AuthExpired("WorkBuddy 刷新响应缺少 accessToken",
                              account_id=account.get("id"))
        data["access_token"] = access
        if body.get("refreshToken"):
            data["refresh_token"] = str(body["refreshToken"])
        expires_in = body.get("expiresIn")
        if isinstance(expires_in, (int, float)) and 0 < expires_in < 10 * 365 * 86400:
            data["expires_at"] = int(time.time() + expires_in)
        if body.get("domain"):
            data["domain"] = str(body["domain"])
        data.pop("needs_login", None)
        data.pop("auth_error", None)
        data["refreshed_at"] = time.time()
        self.save(account)
        return True

    def fetch_account_usage(self, account):
        data = account.get("data") or {}
        realm = _realm_of(data)
        headers = _billing_headers(account)
        enterprise = str(data.get("enterprise_id") or "")
        if enterprise:
            return self._enterprise_usage(realm, headers)
        now = time.time()
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "PackageEndTimeRangeEnd": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now + 365 * 101 * 24 * 3600)),
        }
        paths = (["/billing/meter/get-user-resource",
                  "/v2/billing/meter/get-user-resource"]
                 if realm == "global"
                 else ["/v2/billing/meter/get-user-resource"])
        obj = None
        status = 0
        raw = ""
        for path in paths:
            status, obj, raw = util.http_json(
                _realm(realm)["billing_base"] + path, method="POST",
                headers=headers, body=body, timeout=30)
            if status != 404:
                break
        code = _envelope(status, obj)
        if status >= 400 or code != 0 or not isinstance(obj, dict):
            if status in (401, 403):
                raise AuthExpired("WorkBuddy 额度查询被拒绝",
                                  account_id=account.get("id"))
            raise UpstreamError("WorkBuddy 额度查询失败 code=%s HTTP %s"
                                % (code, status), status=502)
        accounts = _resource_accounts(obj.get("data") if isinstance(obj, dict) else obj)
        if accounts is None:
            raise UpstreamError("WorkBuddy 额度响应结构无法识别", status=502)
        total = 0.0
        used = 0.0
        size = 0.0
        packages = []
        for item in accounts:
            if not isinstance(item, dict):
                continue
            remain = max(0.0, _package_remain(item))
            total += remain
            cycle_size = _num(item, "CycleCapacitySize")
            capacity_used = _num(item, "CycleCapacityUsed")
            used += cycle_size - remain if cycle_size > 0 else _num(item, "CapacityUsed")
            size += cycle_size or _num(item, "CapacitySize")
            packages.append({
                "name": str(item.get("PackageName") or item.get("ProductName")
                            or item.get("Name") or "套餐"),
                "remain": _round_credits(remain),
                "used": _round_credits(cycle_size - remain if cycle_size > 0
                                       else _num(item, "CapacityUsed")),
                "size": _round_credits(cycle_size or _num(item, "CapacitySize")),
                "expires_at": self._expiry(item.get("CycleEndTime")),
            })
        credits = {
            "remain": _round_credits(total),
            "used": _round_credits(used),
            "size": _round_credits(size),
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": util.iso_now(),
        }
        return {"credits": credits, "plan": str(data.get("plan") or "")}

    def _enterprise_usage(self, realm, headers):
        status, obj, raw = util.http_json(
            _realm(realm)["billing_base"] + PATH_ENTERPRISE_USAGE, method="POST",
            headers=headers, body={}, timeout=30)
        code = _envelope(status, obj)
        data = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else obj
        if status >= 400 or code != 0 or not isinstance(data, dict):
            raise UpstreamError("WorkBuddy 企业额度查询失败 code=%s" % code, status=502)
        limit = data.get("limitNum")
        try:
            limit = float(limit)
        except Exception:
            limit = 0.0
        if limit < 0:
            return {"credits": {"unlimited": True, "remain": None,
                                "updated_at": time.time(), "updated_iso": util.iso_now()},
                    "plan": "企业不限量"}
        used = float(data.get("credit") or 0.0)
        remain = max(0.0, limit - used)
        return {"credits": {
            "remain": _round_credits(remain),
            "used": _round_credits(used),
            "size": _round_credits(limit),
            "packages": [],
            "updated_at": time.time(),
            "updated_iso": util.iso_now(),
        }, "plan": "企业额度"}

    @staticmethod
    def _expiry(raw):
        text = str(raw or "").strip()
        if not text:
            return 0
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                import calendar
                st = time.strptime(text[:19], fmt)
                return int(calendar.timegm(st) - 8 * 3600)
            except Exception:
                continue
        return util.normalize_epoch(text)

    def checkin(self, account):
        data = account.get("data") or {}
        realm = _realm_of(data)
        if not _realm(realm)["checkin"]:
            data["last_checkin_message"] = "国际版无签到体系，已跳过"
            self.save(account)
            return {"ok": True, "skipped": True,
                    "message": "国际版无签到体系，已跳过"}
        status, obj, raw = util.http_json(
            _realm(realm)["billing_base"] + "/v2/billing/meter/daily-checkin",
            method="POST", headers=_billing_headers(account), body={}, timeout=30)
        code = _envelope(status, obj)
        message = str((obj or {}).get("msg") or "") if isinstance(obj, dict) else ""
        if status >= 400 and code not in (10001, 0):
            if status in (401, 403):
                raise AuthExpired("WorkBuddy 签到登录态失效",
                                  account_id=account.get("id"))
            data["last_checkin_message"] = "签到失败 HTTP %s" % status
            self.save(account)
            return {"ok": False, "error": "签到失败 HTTP %s" % status}
        if code == 0:
            data["last_checkin"] = time.strftime("%Y-%m-%d")
            data["last_checkin_at"] = time.time()
            data["last_checkin_message"] = message or "签到成功"
            self.save(account)
            return {"ok": True, "message": message or "签到成功"}
        if _is_already(code, message):
            data["last_checkin"] = time.strftime("%Y-%m-%d")
            data["last_checkin_at"] = time.time()
            data["last_checkin_message"] = "今日已签到"
            self.save(account)
            return {"ok": True, "already": True, "message": "今日已签到"}
        data["last_checkin_message"] = message or "签到返回 code=%s" % code
        self.save(account)
        return {"ok": False, "error": message or "签到返回 code=%s" % code,
                "code": code}

    # -- login ------------------------------------------------------------
    def start_oauth(self, realm="cn"):
        realm = realm if realm in REALMS else "cn"
        status, obj, raw = util.http_json(
            _realm(realm)["chat_base"] + PATH_LOGIN_STATE + "?platform=CLI",
            method="POST", headers=_base_headers(realm), body={}, timeout=25)
        code = _envelope(status, obj)
        data = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else {}
        state = str(data.get("state") or "")
        auth_url = str(data.get("authUrl") or data.get("auth_url") or "")
        if status >= 400 or code != 0 or not state:
            raise UpstreamError("获取 WorkBuddy 登录链接失败 code=%s HTTP %s"
                                % (code, status), status=502)
        with self._login_lock:
            self._logins[state] = {"created": time.time(), "realm": realm}
        return {"state": state, "authUrl": auth_url, "realm": realm}

    def poll_oauth(self, state):
        with self._login_lock:
            info = self._logins.get(state)
        if not info:
            return {"status": "unknown", "message": "登录会话不存在，请重新发起"}
        if time.time() - info["created"] > LOGIN_TTL:
            with self._login_lock:
                self._logins.pop(state, None)
            return {"status": "expired", "message": "登录已超时，请重新发起"}
        realm = info["realm"]
        base = _realm(realm)["chat_base"]
        status, obj, raw = util.http_json(
            base + PATH_LOGIN_TOKEN + "?state=" + urllib.parse.quote(state),
            headers=_base_headers(realm), timeout=25)
        code = _envelope(status, obj)
        data = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else {}
        access = str(data.get("accessToken") or "")
        if status >= 400 or code != 0 or not access:
            if status in (401, 403) and code:
                return {"status": "pending", "message": "等待扫码授权"}
            return {"status": "pending", "message": "等待浏览器完成 WorkBuddy 授权"}
        status, acct_obj, _ = util.http_json(
            base + PATH_LOGIN_ACCOUNT + "?state=" + urllib.parse.quote(state),
            headers=_base_headers(realm, access), timeout=25)
        acct = acct_obj.get("data") if isinstance(acct_obj, dict) and isinstance(acct_obj.get("data"), dict) else {}
        uid = str(acct.get("uid") or acct.get("id") or "")
        if not uid:
            return {"status": "pending", "message": "等待账号信息返回"}
        expires_in = data.get("expiresIn")
        try:
            expires_at = int(time.time() + int(expires_in or 3600))
        except Exception:
            expires_at = int(time.time() + 3600)
        new_data = {
            "uid": uid,
            "nickname": str(acct.get("nickname") or acct.get("name") or ""),
            "realm": realm,
            "access_token": access,
            "refresh_token": str(data.get("refreshToken") or ""),
            "expires_at": expires_at,
            "domain": str(data.get("domain") or ""),
            "enterprise_id": str(acct.get("enterpriseId") or acct.get("enterprise_id") or ""),
            "source": "oauth",
        }
        account = self.app.accounts.add("codebuddy", new_data,
                                        name=new_data["nickname"] or uid,
                                        realm=realm)
        with self._login_lock:
            self._logins.pop(state, None)
        return {"status": "ok", "account": self.app.accounts.public(account)}

    def cancel_oauth(self, state):
        with self._login_lock:
            return bool(self._logins.pop(state, None))

    def import_token(self, access_token, refresh_token="", realm="cn",
                     uid="", nickname="", expires_at=0, domain="",
                     enterprise_id="", source="manual"):
        access_token = str(access_token or "").strip()
        if not access_token:
            raise ValueError("accessToken 不能为空")
        realm = realm if realm in REALMS else "cn"
        data = {
            "uid": str(uid or ("cb-" + uuid.uuid4().hex[:24])),
            "nickname": str(nickname or ""),
            "realm": realm,
            "access_token": access_token,
            "refresh_token": str(refresh_token or ""),
            "expires_at": int(expires_at or (time.time() + 3600)),
            "domain": str(domain or ""),
            "enterprise_id": str(enterprise_id or ""),
            "source": source,
        }
        return self.app.accounts.add("codebuddy", data,
                                     name=nickname or data["uid"], realm=realm)

    # -- local client import ---------------------------------------------
    def _state_paths(self):
        appdata = os.environ.get("APPDATA") or ""
        if not appdata:
            return []
        names = ["WorkBuddy", "WorkBuddy CN", "CodeBuddy CN", "CodeBuddy"]
        paths = []
        for name in names:
            base = os.path.join(appdata, name)
            state = os.path.join(base, "User", "globalStorage", "state.vscdb")
            local_state = os.path.join(base, "Local State")
            if os.path.isfile(state) and os.path.isfile(local_state):
                paths.append((name, base, state, local_state))
        return paths

    @staticmethod
    def _decode_buffer(value):
        if isinstance(value, bytes):
            return value
        if isinstance(value, list):
            return bytes(value)
        if isinstance(value, str):
            try:
                obj = json.loads(value)
            except Exception:
                return b""
            if isinstance(obj, dict) and obj.get("type") == "Buffer":
                try:
                    return bytes(obj.get("data") or [])
                except Exception:
                    return b""
        return b""

    def _accounts_from_state(self, app, base, state_path, local_state_path):
        import base64
        found = []
        try:
            with open(local_state_path, "r", encoding="utf-8") as fh:
                local_state = json.load(fh)
            encrypted_key = base64.b64decode(
                (local_state.get("os_crypt") or {}).get("encrypted_key") or "")
            if not encrypted_key.startswith(b"DPAPI"):
                return found
            key = sign.dpapi_unprotect(encrypted_key[5:])
        except Exception:
            return found
        try:
            uri = "file:" + state_path.replace(os.sep, "/") + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=3)
            try:
                rows = con.execute(
                    "select key, value from ItemTable where key like '%coding-copilot%'"
                ).fetchall()
            finally:
                con.close()
        except Exception:
            return found
        for key_name, raw_value in rows:
            if "accessToken" not in str(key_name):
                continue
            blob = self._decode_buffer(raw_value)
            if not blob.startswith(b"v10"):
                continue
            try:
                plain = sign.chromium_decrypt_v10(blob, key)
                obj = json.loads(plain.decode("utf-8", "replace"))
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            auth = obj.get("auth") if isinstance(obj.get("auth"), dict) else {}
            account = obj.get("account") if isinstance(obj.get("account"), dict) else {}
            access = str(obj.get("accessToken") or auth.get("accessToken") or "")
            if not access:
                continue
            refresh = str(obj.get("refreshToken") or auth.get("refreshToken") or "")
            domain = str(obj.get("domain") or auth.get("domain") or "")
            realm = "global" if ("workbuddy.ai" in domain.lower()) else "cn"
            expires = util.normalize_epoch(obj.get("expiresAt") or auth.get("expiresAt"))
            uid = str(account.get("uid") or obj.get("id") or auth.get("uid") or "")
            found.append({
                "app": app,
                "path": state_path,
                "realm": realm,
                "uid": uid,
                "nickname": str(account.get("nickname") or account.get("name") or ""),
                "expires_at": expires,
                "has_refresh_token": bool(refresh),
                "domain": domain,
                "enterprise_id": str(account.get("enterpriseId")
                                     or account.get("enterprise_id") or ""),
                "access_token": access,
                "refresh_token": refresh,
                "source": "local",
            })
        return found

    def scan_local(self):
        items = []
        for app, base, state_path, local_state_path in self._state_paths():
            items.extend(self._accounts_from_state(app, base, state_path,
                                                   local_state_path))
        return items

    def public_scan(self):
        out = []
        for item in self.scan_local():
            out.append({
                "app": item["app"],
                "path": item["path"],
                "realm": item["realm"],
                "uid": item["uid"],
                "nickname": item["nickname"],
                "expires_at": item["expires_at"],
                "has_refresh_token": item["has_refresh_token"],
                "domain": item["domain"],
            })
        return out

    def import_local(self, realm=None, **kwargs):
        imported = []
        errors = []
        for item in self.scan_local():
            use_realm = realm if realm in REALMS else item["realm"]
            try:
                account = self.import_token(
                    item["access_token"], item["refresh_token"],
                    realm=use_realm, uid=item["uid"], nickname=item["nickname"],
                    expires_at=item["expires_at"], domain=item["domain"],
                    enterprise_id=item["enterprise_id"],
                    source="local:%s" % item["app"])
                imported.append(account)
            except Exception as exc:
                errors.append("%s: %s" % (item.get("app"), exc))
        return {"imported": [self.app.accounts.public(a) for a in imported],
                "errors": errors}

    def validate_account(self, account):
        try:
            result = self.fetch_account_usage(account)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        credits = (result or {}).get("credits") or {}
        return {"ok": True, "message": "凭证有效",
                "credits": credits.get("remain")}

    def account_public_extra(self, account):
        data = account.get("data") or {}
        realm = _realm_of(data)
        return {
            "realm_label": REALMS[realm]["name"],
            "checkin_supported": _realm(realm)["checkin"],
            "source_label": "本地客户端" if str(data.get("source") or "").startswith("local")
                            else ("扫码登录" if data.get("source") == "oauth" else "手动导入"),
        }
