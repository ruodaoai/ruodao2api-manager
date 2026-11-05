"""ZCode channel: OAuth CLI login, coding-plan credentials, GLM chat, quota, claims."""

import base64
import hashlib
import json
import os
import platform
import threading
import time
import urllib.parse
import uuid

from .. import util
from ..errors import AuthExpired, UpstreamError
from .base import BaseChannel, stream_openai_passthrough

REALMS = {
    "zai": {
        "name": "Z.AI",
        "provider": "zai",
        "coding_origin": "https://api.z.ai",
        "openai_base": "https://api.z.ai/api/coding/paas/v4",
        "biz_host": "https://api.z.ai",
        "website": "https://z.ai",
    },
    "bigmodel": {
        "name": "BigModel",
        "provider": "bigmodel",
        "coding_origin": "https://open.bigmodel.cn",
        "openai_base": "https://open.bigmodel.cn/api/coding/paas/v4",
        "biz_host": "https://bigmodel.cn",
        "website": "https://bigmodel.cn",
    },
}

ZCODE_ORIGIN = "https://zcode.z.ai"
PATH_OAUTH_INIT = "/api/v1/oauth/cli/init"
PATH_OAUTH_POLL = "/api/v1/oauth/cli/poll/%s"
PATH_BALANCE = "/api/v1/zcode-plan/billing/balance"
PATH_PREVIEW = "/api/v1/zcode-plan/billing/preview"
PATH_CLAIM = "/api/v1/zcode-plan/billing/claim"
PATH_QUOTA = "/api/monitor/usage/quota/limit"
PATH_CUSTOMER = "/api/biz/customer/getCustomerInfo"

ZCODE_APP_VERSION = (os.environ.get("RUODAO_ZCODE_VERSION") or "3.17.1").strip() or "3.17.1"
LOGIN_TTL = 900
API_KEY_NAME = "zcode-api-key"
DEFAULT_ORG_MARKER = "\u9ED8\u8BA4\u673A\u6784"
DEFAULT_PROJECT_MARKER = "\u9ED8\u8BA4\u9879\u76EE"

MODELS = [
    {"id": "glm-5.3", "name": "GLM 5.3", "context": 1000000, "max_output": 128000,
     "reasoning": True},
    {"id": "glm-5.3-flash", "name": "GLM 5.3 Flash", "context": 1000000,
     "max_output": 128000, "reasoning": True, "vision": True},
    {"id": "glm-5.2", "name": "GLM 5.2", "context": 1000000, "max_output": 128000,
     "reasoning": True},
    {"id": "glm-5.1", "name": "GLM 5.1", "context": 200000, "max_output": 64000,
     "reasoning": True},
    {"id": "glm-5", "name": "GLM 5", "context": 200000, "max_output": 64000,
     "reasoning": True},
    {"id": "glm-5-turbo", "name": "GLM 5 Turbo", "context": 200000,
     "max_output": 64000, "reasoning": True},
    {"id": "glm-5v-turbo", "name": "GLM 5V Turbo", "context": 200000,
     "max_output": 131072, "vision": True},
    {"id": "glm-4.7", "name": "GLM 4.7", "context": 200000, "max_output": 131072,
     "reasoning": True},
    {"id": "glm-4.6", "name": "GLM 4.6", "context": 200000, "max_output": 131072,
     "reasoning": True},
    {"id": "glm-4.6v", "name": "GLM 4.6V", "context": 131072, "max_output": 32768,
     "vision": True},
    {"id": "glm-4.5-air", "name": "GLM 4.5 Air", "context": 131072,
     "max_output": 98304, "reasoning": True},
]
_MODEL_BY_ID = {m["id"]: m for m in MODELS}


def _realm(realm):
    return REALMS.get(realm) or REALMS["zai"]


def _realm_of(data):
    realm = str((data or {}).get("realm") or "").strip().lower()
    return realm if realm in REALMS else "zai"


def _platform_arch():
    raw = (os.environ.get("RUODAO_ZCODE_PLATFORM") or "").strip()
    if raw:
        return raw
    system = platform.system()
    sysname = "win32" if system == "Windows" else (
        "darwin" if system == "Darwin" else "linux")
    arch = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64",
            "arm64": "arm64", "i386": "ia32", "x86": "ia32"}.get(arch, arch or "x64")
    return "%s-%s" % (sysname, arch)


def _os_category():
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    return "linux"


def _printable(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return text if all("\x20" <= ch <= "\x7e" for ch in text) else ""


def _identity_headers():
    version = _printable(ZCODE_APP_VERSION) or "3.17.1"
    return {
        "User-Agent": "ZCode/%s" % version,
        "HTTP-Referer": ZCODE_ORIGIN + "/",
        "X-Title": "Z Code@cli",
        "X-ZCode-App-Version": version,
        "X-Release-Channel": "production",
        "X-Client-Language": _printable(os.environ.get("RUODAO_ZCODE_LANGUAGE"))
                              or "zh-CN",
        "X-Client-Timezone": _printable(os.environ.get("RUODAO_ZCODE_TIMEZONE"))
                              or "Asia/Shanghai",
        "X-ZCode-Agent": "glm",
        "X-Platform": _platform_arch(),
        "X-Os-Category": _os_category(),
    }


def _billing_headers(data, jwt=None):
    headers = _identity_headers()
    headers.pop("X-ZCode-Agent", None)
    headers["Accept"] = "application/json"
    token = str(jwt if jwt is not None else (data or {}).get("jwt_token") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    device = str((data or {}).get("device_mid") or "").strip()
    if device:
        headers["X-Device-Mid"] = device
    return headers


def _envelope_ok(obj):
    if not isinstance(obj, dict):
        return False
    if obj.get("success") is False:
        return False
    code = obj.get("code")
    return code is None or code in (0, 200, "0", "200")


def _envelope_message(obj, status, raw=""):
    if isinstance(obj, dict):
        msg = obj.get("msg") or obj.get("message")
        if msg:
            return str(msg)
        data = obj.get("data")
        if isinstance(data, dict) and (data.get("msg") or data.get("message")):
            return str(data.get("msg") or data.get("message"))
    text = str(raw or "").strip()
    return text[:300] if text else "HTTP %s" % status


def _num(value):
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _round(value):
    try:
        value = float(value)
    except Exception:
        return 0
    return int(value) if abs(value - int(value)) < 1e-9 else round(value, 2)


def _jwt_claims(token):
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        obj = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _claims_uid(claims):
    for key in ("user_id", "userId", "sub", "uid"):
        value = claims.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _claims_name(claims):
    for key in ("name", "nickname", "email", "account"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _stamp(account, message):
    data = account.setdefault("data", {})
    data["last_checkin"] = time.strftime("%Y-%m-%d")
    data["last_checkin_at"] = time.time()
    data["last_checkin_message"] = str(message or "")[:500]


def _biz_data(obj):
    if not isinstance(obj, dict):
        return {}
    data = obj.get("data")
    if isinstance(data, (dict, list)):
        return data
    return obj


def _api_key_list(obj):
    data = _biz_data(obj)
    if isinstance(data, list):
        return data
    for key in ("apiKeys", "api_keys", "items", "list"):
        items = data.get(key)
        if isinstance(items, list):
            return items
    return []


class ZCodeChannel(BaseChannel):
    name = "zcode"
    label = "ZCode"
    color = "#c4552d"
    supports_checkin = True
    supports_oauth = True

    def __init__(self, app):
        super(ZCodeChannel, self).__init__(app)
        self._logins = {}
        self._lock = threading.Lock()

    # -- models -----------------------------------------------------------
    def list_models(self, account=None):
        out = []
        for realm in ("zai", "bigmodel"):
            for model in MODELS:
                out.append(self._model_dict(realm, model))
        return out

    def _model_dict(self, realm, model):
        cfg = _realm(realm)
        mid = model["id"]
        return {
            "id": "zcode/%s/%s" % (realm, mid),
            "raw_id": mid,
            "object": "model",
            "channel": "zcode",
            "channel_label": "ZCode",
            "realm": realm,
            "realm_label": cfg["name"],
            "owned_by": "ZCode %s" % cfg["name"],
            "display_name": model.get("name") or mid,
            "created": 0,
            "context_length": model.get("context") or 200000,
            "max_output_tokens": model.get("max_output") or 64000,
            "capabilities": {
                "vision": bool(model.get("vision")),
                "reasoning": bool(model.get("reasoning")),
                "tools": True,
            },
        }

    def supports_model(self, account, model_info):
        realm = (model_info or {}).get("realm")
        if realm and account.get("realm") and realm != account.get("realm"):
            return False
        return True

    def find_model(self, requested):
        raw = str(requested or "").strip()
        if raw.lower().startswith("zcode/"):
            raw = raw[6:]
        realm = ""
        low = raw.lower()
        if low.startswith("zai/"):
            realm, raw = "zai", raw[4:]
        elif low.startswith("bigmodel/"):
            realm, raw = "bigmodel", raw[9:]
        model = _MODEL_BY_ID.get(raw)
        if not realm:
            accounts = self.app.accounts.list("zcode") if getattr(self.app, "accounts", None) else []
            if any(a.get("realm") == "zai" and a.get("enabled") for a in accounts):
                realm = "zai"
            elif any(a.get("realm") == "bigmodel" and a.get("enabled") for a in accounts):
                realm = "bigmodel"
            else:
                realm = "zai"
        if model:
            out = self._model_dict(realm, model)
            out["meta"] = model
            return out
        return {
            "id": "zcode/%s/%s" % (realm, raw),
            "raw_id": raw,
            "realm": realm,
            "display_name": raw,
            "unknown": True,
            "context_length": 200000,
            "capabilities": {},
        }

    def resolve_model(self, requested):
        found = self.find_model(requested)
        return found.get("raw_id") if found else None

    # -- inference --------------------------------------------------------
    def stream_chat(self, account, body, upstream_model):
        data = account.get("data") or {}
        credential = self._ensure_credential(account)
        if not credential:
            raise AuthExpired(
                "ZCode 账号缺少 coding-plan API Key，请在面板重新登录或手动导入 API Key",
                account_id=account.get("id"))
        realm = _realm_of(data)
        payload = dict(body or {})
        payload["model"] = upstream_model
        payload["stream"] = True
        headers = _identity_headers()
        headers["Authorization"] = "Bearer " + credential
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"
        headers["Accept-Encoding"] = "identity"
        url = _realm(realm)["openai_base"] + "/chat/completions"
        try:
            for chunk in stream_openai_passthrough(url, headers, payload,
                                                   timeout=self._timeout()):
                yield chunk
        except UpstreamError as exc:
            low = (exc.message or "").lower()
            if "insufficient balance" in low or "no resource package" in low \
                    or '"1113"' in low or "code=1113" in low:
                raise UpstreamError(
                    "%s 账号没有可用的 Coding Plan API 资源包。面板里的 ZCode 计划额度"
                    "不等于 API 额度，请为该项目开通 GLM Coding Plan，或在客户端切换到"
                    "其他渠道。" % _realm(realm)["name"],
                    status=402, code="no_resource_package", retryable=False)
            if exc.status in (401, 403) or "verify_" in low:
                data["auth_error"] = str(exc.message)[:300]
                if not credential:
                    data["needs_login"] = True
                self.save(account)
            raise

    # -- account lifecycle -------------------------------------------------
    def refresh_account(self, account):
        data = account.get("data") or {}
        token = str(data.get("oauth_access_token") or "").strip()
        if not token:
            return False
        kind = str(data.get("api_key_kind") or "")
        expires = _num(data.get("api_key_expires_at"))
        if data.get("api_key") and kind not in ("", "pat"):
            return False
        if kind == "pat" and expires and expires - time.time() > 300:
            return False
        resolved = self._resolve_credentials(_realm_of(data), token)
        data.update(resolved)
        data.pop("needs_login", None)
        data.pop("auth_error", None)
        self.save(account)
        return True

    def _ensure_credential(self, account):
        data = account.get("data") or {}
        credential = str(data.get("api_key") or "").strip()
        kind = str(data.get("api_key_kind") or "")
        expires = _num(data.get("api_key_expires_at"))
        refresh = not credential or kind == ""
        if kind == "pat":
            refresh = not expires or expires - time.time() <= 120
        if not refresh:
            return credential
        oauth = str(data.get("oauth_access_token") or "").strip()
        if not oauth:
            return credential
        resolved = self._resolve_credentials(_realm_of(data), oauth)
        data.update(resolved)
        data.pop("needs_login", None)
        data.pop("auth_error", None)
        self.save(account)
        return str(data.get("api_key") or "")

    def fetch_account_usage(self, account):
        data = account.get("data") or {}
        realm = _realm_of(data)
        errors = []
        quota = None
        balances = None
        preview = None

        credential = str(data.get("api_key") or "").strip()
        if data.get("oauth_access_token"):
            try:
                credential = self._ensure_credential(account)
            except Exception as exc:
                errors.append("credential: %s" % exc)
        if credential:
            try:
                quota = self._quota(realm, credential)
            except UpstreamError as exc:
                if exc.status in (401, 403):
                    raise
                errors.append("coding: %s" % exc.message)
            except Exception as exc:
                errors.append("coding: %s" % exc)

        if data.get("jwt_token"):
            try:
                balances, preview = self._billing_snapshot(data)
            except UpstreamError as exc:
                if exc.status in (401, 403):
                    raise
                errors.append("billing: %s" % exc.message)
            except Exception as exc:
                errors.append("billing: %s" % exc)

        if quota is None and balances is None:
            if errors:
                raise UpstreamError("ZCode 额度查询失败：%s" % "; ".join(errors)[:400],
                                    status=502)
            raise UpstreamError("ZCode 账号缺少可用凭证", status=401)

        packages = []
        remain = used = size = 0.0
        unit_type = ""
        for item in balances or []:
            item_remain = _num(item.get("remaining_units"))
            item_used = _num(item.get("used_units"))
            item_size = _num(item.get("total_units"))
            remain += item_remain
            used += item_used
            size += item_size
            unit_type = unit_type or str(item.get("unit_type") or "")
            packages.append({
                "name": str(item.get("show_name") or "额度"),
                "remain": _round(item_remain),
                "used": _round(item_used),
                "size": _round(item_size),
                "expires_at": util.normalize_epoch(
                    item.get("expires_at") if item.get("expires_at") is not None
                    else item.get("expiresAt")),
            })
        limits = []
        exceeded = False
        if quota:
            for item in quota.get("limits") or []:
                limit = {
                    "type": item.get("type") or "",
                    "unit": item.get("unit") or "",
                    "total": _round(item.get("total")),
                    "used": _round(item.get("used")),
                    "remaining": _round(item.get("remaining")),
                    "percentage": _num(item.get("percentage")),
                    "next_reset_at": util.normalize_epoch(item.get("next_reset_at")),
                }
                limits.append(limit)
                if limit["type"].upper() == "TOKENS_LIMIT" \
                        and limit["percentage"] >= 100:
                    exceeded = True
        credits = {
            "remain": _round(remain),
            "remain_effective": _round(remain),
            "used": _round(used),
            "size": _round(size),
            "unit": unit_type,
            "packages": packages,
            "limits": limits,
            "exceeded": exceeded,
            "level": (quota or {}).get("level") or "",
            "claimable_plans": preview or [],
            "errors": errors,
            "updated_at": time.time(),
            "updated_iso": util.iso_now(),
        }
        plan = str((quota or {}).get("level") or data.get("plan")
                   or data.get("org_name") or "")
        return {"credits": credits, "plan": plan}

    def _quota(self, realm, credential):
        url = _realm(realm)["coding_origin"] + PATH_QUOTA
        status, obj, raw = util.http_json(
            url, headers={"authorization": credential, "accept": "application/json"},
            timeout=30)
        if status in (401, 403) or (isinstance(obj, dict)
                                    and str(obj.get("code")) in ("401", "403",
                                                                  "1001", "3012")):
            raise AuthExpired("ZCode API Key 被拒绝")
        if status >= 400 or not _envelope_ok(obj):
            raise UpstreamError("ZCode 额度查询失败 code=%s HTTP %s"
                                % ((obj or {}).get("code"), status), status=502)
        data = (obj.get("data") if isinstance(obj.get("data"), dict) else obj) or {}
        limits = []
        for item in data.get("limits") or []:
            if not isinstance(item, dict):
                continue
            total = item.get("number")
            used = item.get("usage")
            remaining = item.get("remaining")
            if total is None and used is None and remaining is None:
                continue
            limits.append({
                "type": str(item.get("type") or ""),
                "unit": str(item.get("unit") or ""),
                "total": _num(total),
                "used": _num(used),
                "remaining": _num(remaining),
                "percentage": _num(item.get("percentage")),
                "next_reset_at": util.normalize_epoch(
                    item.get("next_reset_time") or item.get("nextResetTime")),
            })
        return {"level": str(data.get("level") or ""), "limits": limits}

    def _billing_snapshot(self, data):
        realm = _realm_of(data)
        platform_name = _platform_arch()
        headers = _billing_headers(data)
        app_version = urllib.parse.quote(ZCODE_APP_VERSION, safe="")
        query = "?app_version=%s&platform=%s" % (
            app_version, urllib.parse.quote(platform_name, safe=""))
        balances = []
        preview = []
        last_error = None
        for path in (PATH_BALANCE, PATH_PREVIEW):
            url = ZCODE_ORIGIN + path + query
            status, obj, raw = util.http_json(url, headers=headers, timeout=30)
            if status in (401, 403) or (isinstance(obj, dict)
                                        and str(obj.get("code")) in ("401", "3012")):
                raise AuthExpired("ZCode 计划凭据已失效，请重新登录")
            if status >= 400 or not _envelope_ok(obj):
                last_error = UpstreamError(
                    "ZCode 账单查询失败 code=%s HTTP %s"
                    % ((obj or {}).get("code"), status), status=502)
                continue
            body = (obj.get("data") if isinstance(obj.get("data"), dict) else {}) or {}
            if path == PATH_BALANCE:
                for item in body.get("balances") or []:
                    if isinstance(item, dict):
                        balances.append(item)
            else:
                preview = self._parse_plans(body.get("plans") or [])
        if last_error and not balances and not preview:
            raise last_error
        return balances, preview

    @staticmethod
    def _parse_plans(plans):
        out = []
        for item in plans:
            if not isinstance(item, dict):
                continue
            plan_id = str(item.get("plan_id") or "").strip()
            if not plan_id:
                continue
            entitlements = []
            for ent in item.get("entitlements") or []:
                if not isinstance(ent, dict):
                    continue
                entitlements.append({
                    "name": str(ent.get("show_name") or ""),
                    "units": _num(ent.get("grant_units")),
                    "unit_type": str(ent.get("unit_type") or ""),
                    "effective_at": util.normalize_epoch(ent.get("effective_at")),
                })
            out.append({
                "plan_id": plan_id,
                "name": str(item.get("name") or plan_id),
                "description": str(item.get("description") or ""),
                "priority": _num(item.get("priority")),
                "entitlements": entitlements,
                "starts_at": util.normalize_epoch(item.get("starts_at")),
                "ends_at": util.normalize_epoch(item.get("ends_at")),
            })
        return out

    # -- check-in / trial claims -------------------------------------------
    def checkin(self, account):
        data = account.get("data") or {}
        if not data.get("jwt_token"):
            message = "ZCode 签到需要 OAuth 登录（计划 JWT），纯 API Key 账号请手动在客户端领取"
            data["last_checkin_message"] = message
            self.save(account)
            return {"ok": True, "skipped": True, "message": message}
        try:
            status, obj, raw = util.http_json(
                ZCODE_ORIGIN + PATH_PREVIEW + "?app_version=%s&platform=%s" % (
                    urllib.parse.quote(ZCODE_APP_VERSION, safe=""),
                    urllib.parse.quote(_platform_arch(), safe="")),
                headers=_billing_headers(data), timeout=30)
        except Exception as exc:
            message = "ZCode 领取活动查询失败：%s" % exc
            data["last_checkin_message"] = message
            self.save(account)
            return {"ok": False, "error": message}
        if status in (401, 403) or (isinstance(obj, dict)
                                    and str(obj.get("code")) in ("401", "3012")):
            message = "ZCode 登录态已失效，请重新登录"
            data["last_checkin_message"] = message
            self.save(account)
            return {"ok": False, "error": message}
        if status in (404, 405):
            message = "ZCode 领取接口未开放"
            data["last_checkin_message"] = message
            self.save(account)
            return {"ok": True, "skipped": True, "message": message}
        if status >= 400 or not _envelope_ok(obj):
            message = _envelope_message(obj, status, raw)
            data["last_checkin_message"] = "领取活动查询失败：" + message
            self.save(account)
            return {"ok": False, "error": "领取活动查询失败：" + message}
        body = (obj.get("data") if isinstance(obj.get("data"), dict) else {}) or {}
        plans = self._parse_plans(body.get("plans") or [])
        if not plans:
            message = "当前没有可领取的 ZCode 试用套餐"
            data["last_checkin_message"] = message
            self.save(account)
            return {"ok": True, "skipped": True, "message": message}

        claimed, already, unavailable, errors = [], [], [], []
        for plan in sorted(plans, key=lambda p: -(_num(p.get("priority")))):
            plan_id = plan["plan_id"]
            name = plan.get("name") or plan_id
            result = self._claim_plan(data, plan_id)
            if result.get("ok"):
                claimed.append(name)
            elif result.get("already"):
                already.append(name)
            elif result.get("ineligible"):
                unavailable.append("%s: %s" % (name, result.get("message") or "不满足领取条件"))
            elif result.get("captcha"):
                errors.append("%s: 需要图形验证码，请在 ZCode 客户端手动领取" % name)
            else:
                errors.append("%s: %s" % (name, result.get("message") or "领取失败"))

        if claimed:
            message = "ZCode 试用套餐领取成功：%s" % "、".join(claimed)
        elif already:
            message = "ZCode 今日试用套餐已领取"
        elif unavailable:
            message = "ZCode 暂无可自动领取的套餐（%s）" % "; ".join(unavailable)[:200]
        elif errors:
            message = "ZCode 领取失败：%s" % "; ".join(errors)[:300]
        else:
            message = "ZCode 暂无可领取的套餐"
        _stamp(account, message)
        self.save(account)
        return {"ok": not errors, "claimed": claimed, "already": already,
                "message": message, "errors": errors}

    def _claim_plan(self, data, plan_id):
        headers = _billing_headers(data)
        headers["Content-Type"] = "application/json"
        status, obj, raw = util.http_json(
            ZCODE_ORIGIN + PATH_CLAIM, method="POST", headers=headers,
            body={"plan_id": plan_id}, timeout=30)
        code = (obj or {}).get("code") if isinstance(obj, dict) else None
        if status >= 200 and status < 300 and code in (0, 200, "0", "200", None) \
                and _envelope_ok(obj):
            return {"ok": True}
        message = _envelope_message(obj, status, raw)
        if str(code) == "1003":
            return {"already": True, "message": message}
        if str(code) == "1004":
            return {"ineligible": True, "message": message}
        if str(code) == "3007" or "captcha" in message.lower() \
                or "verify" in message.lower():
            return {"captcha": True, "message": message}
        if str(code) == "1005":
            return {"message": "名额已领完"}
        return {"message": message}

    # -- login ------------------------------------------------------------
    def start_oauth(self, realm="zai"):
        realm = realm if realm in REALMS else "zai"
        poll_token = uuid.uuid4().hex + uuid.uuid4().hex
        status, obj, raw = util.http_json(
            ZCODE_ORIGIN + PATH_OAUTH_INIT, method="POST",
            headers={"Authorization": "Bearer " + poll_token,
                     "Content-Type": "application/json"},
            body={"provider": realm}, timeout=30)
        data = (obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict)
                else {}) or {}
        flow_id = str(data.get("flow_id") or "")
        auth_url = str(data.get("authorize_url") or "")
        if status >= 400 or not _envelope_ok(obj) or not flow_id or not auth_url:
            raise UpstreamError("ZCode 登录初始化失败：%s"
                                % _envelope_message(obj, status, raw), status=502)
        try:
            url = auth_url
            interstitial = urllib.parse.urlparse(ZCODE_ORIGIN + "/app/oauth/login")
            query = urllib.parse.parse_qs(interstitial.query)
            query["redirect"] = ["zcode://oauth/callback"]
            query["app_version"] = [ZCODE_APP_VERSION]
            redirect_value = urllib.parse.urlunparse(
                interstitial._replace(query=urllib.parse.urlencode(query, doseq=True)))
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            params["redirect_uri" if realm == "zai" else "redirect"] = [redirect_value]
            url = urllib.parse.urlunparse(parsed._replace(
                query=urllib.parse.urlencode(params, doseq=True)))
        except Exception:
            url = auth_url
        state = "zc-" + uuid.uuid4().hex
        with self._lock:
            self._logins[state] = {
                "created": time.time(),
                "realm": realm,
                "flow_id": flow_id,
                "poll_token": poll_token,
                "interval": max(1, int(data.get("poll_interval_sec") or 2)),
                "expires_at": util.normalize_epoch(data.get("expires_at")),
                "tokens": None,
            }
        return {"state": state, "authUrl": url, "realm": realm}

    def poll_oauth(self, state):
        with self._lock:
            info = self._logins.get(state)
        if not info:
            return {"status": "unknown", "message": "登录会话不存在，请重新发起"}
        if time.time() - info["created"] > LOGIN_TTL:
            with self._lock:
                self._logins.pop(state, None)
            return {"status": "expired", "message": "登录已超时，请重新发起"}
        realm = info["realm"]

        if not info.get("tokens"):
            status, obj, raw = util.http_json(
                ZCODE_ORIGIN + PATH_OAUTH_POLL % urllib.parse.quote(info["flow_id"]),
                headers={"Authorization": "Bearer " + info["poll_token"]},
                timeout=25)
            if status in (202, 404):
                return {"status": "pending", "message": "等待浏览器完成 ZCode 授权"}
            if status >= 500:
                return {"status": "pending", "message": "上游暂时不可用，继续等待"}
            if status >= 400:
                return {"status": "error", "message": "授权轮询失败 HTTP %s" % status}
            if not _envelope_ok(obj):
                return {"status": "error", "message": _envelope_message(obj, status, raw)}
            data = (obj.get("data") if isinstance(obj, dict)
                    and isinstance(obj.get("data"), dict) else {}) or {}
            flow_status = str(data.get("status") or "")
            if flow_status == "failed":
                return {"status": "error", "message": "浏览器授权被拒绝或已取消"}
            if flow_status != "ready":
                return {"status": "pending", "message": "等待浏览器完成 ZCode 授权"}
            provider_payload = data.get(realm) if isinstance(data.get(realm), dict) else {}
            access_token = str(provider_payload.get("access_token") or "").strip()
            if not access_token:
                return {"status": "error",
                        "message": "ZCode 授权结果缺少 provider access_token"}
            user = data.get("user") if isinstance(data.get("user"), dict) else {}
            info["tokens"] = {
                "access_token": access_token,
                "jwt_token": str(data.get("token") or "").strip(),
                "user": user,
            }

        tokens = info["tokens"]
        uid = str(tokens["user"].get("user_id")
                  or tokens["user"].get("userId") or "")
        nickname = str(tokens["user"].get("name")
                       or tokens["user"].get("nickname")
                       or tokens["user"].get("email") or "")
        try:
            resolved = self._resolve_credentials(realm, tokens["access_token"])
        except Exception as exc:
            return {"status": "error",
                    "message": "登录成功但换取 coding-plan API Key 失败：%s" % exc}
        account = self._store_account(
            realm, resolved, oauth_token=tokens["access_token"],
            jwt_token=tokens["jwt_token"], uid=uid, nickname=nickname,
            source="oauth")
        with self._lock:
            self._logins.pop(state, None)
        return {"status": "ok", "account": self.app.accounts.public(account)}

    def cancel_oauth(self, state):
        with self._lock:
            return bool(self._logins.pop(state, None))

    def _store_account(self, realm, resolved, oauth_token="", jwt_token="",
                       uid="", nickname="", source="manual"):
        claims = _jwt_claims(jwt_token)
        uid = uid or _claims_uid(claims)
        nickname = nickname or _claims_name(claims) or resolved.get("org_name") or ""
        if not uid:
            seed = resolved.get("api_key") or oauth_token or uuid.uuid4().hex
            uid = "zc-" + hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:20]
        nickname = nickname or ("ZCode " + _realm(realm)["name"] + " " + uid[-6:])
        data = {
            "uid": uid,
            "nickname": nickname,
            "realm": realm,
            "api_key": resolved.get("api_key") or "",
            "api_key_id": resolved.get("api_key_id") or "",
            "api_key_kind": resolved.get("api_key_kind") or "",
            "api_key_expires_at": resolved.get("api_key_expires_at") or 0,
            "jwt_token": jwt_token or "",
            "oauth_access_token": oauth_token or "",
            "device_mid": str(uuid.uuid4()),
            "org_id": resolved.get("org_id") or "",
            "project_id": resolved.get("project_id") or "",
            "org_name": resolved.get("org_name") or "",
            "source": source,
            "expires_at": 0,
            "needs_login": False,
        }
        account = self.app.accounts.add("zcode", data, name=nickname,
                                        realm=realm)
        account["data"].pop("needs_login", None)
        account["data"].pop("auth_error", None)
        self.store.save_account(account)
        return account

    def import_token(self, realm="zai", oauth_access_token="", jwt_token="",
                     api_key="", uid="", nickname="", userinfo=None,
                     source="manual"):
        realm = realm if realm in REALMS else "zai"
        api_key = str(api_key or "").strip()
        jwt_token = str(jwt_token or "").strip()
        if api_key:
            return self.import_api_key(api_key, realm=realm, nickname=nickname,
                                       uid=uid, jwt_token=jwt_token, source=source)
        token = str(oauth_access_token or "").strip()
        if not token:
            raise ValueError("oauth_access_token 或 api_key 至少提供一个")
        resolved = self._resolve_credentials(realm, token)
        ui = userinfo if isinstance(userinfo, dict) else {}
        return self._store_account(
            realm, resolved, oauth_token=token, jwt_token=jwt_token,
            uid=str(ui.get("user_id") or ui.get("userId") or uid or ""),
            nickname=str(ui.get("name") or ui.get("nickname")
                         or ui.get("email") or nickname or ""),
            source=source)

    def import_api_key(self, api_key, realm="zai", nickname="", uid="",
                       jwt_token="", source="apikey"):
        api_key = str(api_key or "").strip()
        if not api_key:
            raise ValueError("ZCode API Key 不能为空")
        realm = realm if realm in REALMS else "zai"
        try:
            quota = self._quota(realm, api_key)
        except AuthExpired:
            raise ValueError("ZCode API Key 无效或已过期")
        except UpstreamError:
            quota = {"level": "", "limits": []}
        resolved = {
            "api_key": api_key,
            "api_key_id": api_key.split(".", 1)[0] if "." in api_key else "",
            "api_key_kind": "apikey",
            "api_key_expires_at": 0,
            "org_name": "",
        }
        uid = str(uid or "").strip()
        if not uid:
            uid = "zk-" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:20]
        account = self._store_account(
            realm, resolved, jwt_token=jwt_token, uid=uid,
            nickname=nickname or ("ZCode %s %s" % (_realm(realm)["name"], uid[-6:])),
            source=source)
        if quota.get("level"):
            account["data"]["plan"] = quota["level"]
            self.store.save_account(account)
        return account

    # -- key resolution (mirrors ZCode KeyResolver) ------------------------
    def _login_token(self, realm, oauth_access_token):
        token = str(oauth_access_token or "").strip()
        if not token:
            raise UpstreamError("ZCode OAuth access token 为空", status=401)
        if realm != "zai":
            return token
        status, obj, raw = util.http_json(
            "https://api.z.ai/api/auth/z/login", method="POST",
            headers={"Content-Type": "application/json"},
            body={"token": token}, timeout=30)
        data = (obj.get("data") if isinstance(obj, dict)
                and isinstance(obj.get("data"), dict) else (obj or {}))
        biz_token = str(data.get("access_token") or data.get("accessToken") or "")
        if status >= 400 or not biz_token:
            raise UpstreamError("Z.AI 登录交换失败 HTTP %s：%s"
                                % (status, _envelope_message(obj, status, raw)),
                                status=502)
        return biz_token

    def _mint_project_token(self, list_url, key_headers, api_key_id):
        """Mint the coding-plan PAT ZCode 3.x uses, instead of /copy secrets."""
        url = "%s/%s/access_tokens" % (
            list_url, urllib.parse.quote(api_key_id, safe=""))
        status, obj, raw = util.http_json(
            url, method="POST", headers=key_headers,
            body={"clientType": "zcode", "clientVersion": ZCODE_APP_VERSION},
            timeout=30)
        data = _biz_data(obj)
        token = ""
        expires_at = 0
        if status < 400 and _envelope_ok(obj) and isinstance(data, dict) \
                and data.get("enabled") is not False:
            token = str(data.get("accessToken") or data.get("access_token") or "")
            expires_in = _num(data.get("expiresIn"))
            if expires_in > 0:
                expires_at = int(time.time() + expires_in)
            provided = util.normalize_epoch(data.get("expiresAt"))
            if provided > 0:
                expires_at = min(expires_at, provided) if expires_at else provided
        if not token:
            return "", 0, "HTTP %s：%s" % (status, _envelope_message(obj, status, raw))
        return token, expires_at, ""

    def _resolve_credentials(self, realm, oauth_access_token):
        cfg = _realm(realm)
        login_token = self._login_token(realm, oauth_access_token)
        host = cfg["biz_host"]
        # 客户信息接口接收原始登录令牌（zai 为业务令牌），不要加 Bearer。
        status, obj, raw = util.http_json(
            host + PATH_CUSTOMER,
            headers={"Authorization": login_token,
                     "Content-Type": "application/json"}, timeout=30)
        if status >= 400:
            raise UpstreamError("ZCode 客户信息查询失败 HTTP %s：%s"
                                % (status, _envelope_message(obj, status, raw)),
                                status=502)
        info = _biz_data(obj)
        if not isinstance(info, dict):
            info = {}
        orgs = info.get("organizations") or info.get("orgs") or []
        if not isinstance(orgs, list) or not orgs:
            raise UpstreamError("ZCode 账号没有可用机构", status=502)

        # 与 ZCode 内置 KeyResolver 一致：个人 Key 只建在非团队项目上
        # （projectType == "2" 是团队项目），并优先「默认机构 / 默认项目」。
        options = []
        for row in orgs:
            if not isinstance(row, dict):
                continue
            row_org_id = str(row.get("organizationId") or row.get("id")
                             or row.get("orgId") or "")
            projects = [p for p in (row.get("projects") or [])
                        if isinstance(p, dict)
                        and str(p.get("projectId") or p.get("id") or "")
                        and str(p.get("projectType") or "") != "2"]
            if not row_org_id or not projects:
                continue
            project = next((p for p in projects
                            if DEFAULT_PROJECT_MARKER in str(
                                p.get("projectName") or p.get("name") or "")),
                           projects[0])
            options.append((row, row_org_id, project))
        if not options:
            raise UpstreamError(
                "ZCode 账号没有可创建个人 Key 的项目（可能只有团队项目）；"
                "请先在 ZCode 客户端登录并确认已开通 GLM Coding Plan", status=502)
        named = [item for item in options
                 if DEFAULT_ORG_MARKER in str(
                     item[0].get("organizationName") or item[0].get("name") or "")]
        org, org_id, project = named[0] if named else options[0]
        project_id = str(project.get("projectId") or project.get("id") or "")
        if not org_id or not project_id:
            raise UpstreamError("ZCode 机构/项目 ID 解析失败", status=502)

        key_headers = {
            "Authorization": ("Bearer " + login_token) if realm == "zai" else login_token,
            "Content-Type": "application/json",
        }
        list_url = "%s/api/biz/v1/organization/%s/projects/%s/api_keys" % (
            host, urllib.parse.quote(org_id, safe=""),
            urllib.parse.quote(project_id, safe=""))
        status, obj, raw = util.http_json(
            list_url, headers=key_headers, timeout=30)
        if status >= 400:
            raise UpstreamError("ZCode API Key 列表查询失败 HTTP %s：%s"
                                % (status, _envelope_message(obj, status, raw)),
                                status=502)

        api_key = ""
        for item in _api_key_list(obj):
            if isinstance(item, dict) and item.get("name") == API_KEY_NAME \
                    and str(item.get("apiKey") or ""):
                api_key = str(item["apiKey"])
                break
        if not api_key:
            status, obj, raw = util.http_json(
                list_url, method="POST", headers=key_headers,
                body={"name": API_KEY_NAME, "usageScene": 1}, timeout=30)
            item = _biz_data(obj)
            if isinstance(item, list) and item:
                item = item[0]
            if isinstance(item, dict):
                api_key = str(item.get("apiKey") or "")
            if status >= 400 or not api_key:
                raise UpstreamError("ZCode API Key 创建失败 HTTP %s：%s"
                                    % (status, _envelope_message(obj, status, raw)),
                                    status=502)
        api_key_id = api_key.split(".", 1)[0]

        pat, pat_expires, pat_error = self._mint_project_token(
            list_url, key_headers, api_key_id)
        if pat:
            credential, kind, expires_at = pat, "pat", pat_expires
        else:
            # 旧版服务端仍可用 /copy 取 <id>.<secret>，作为兜底。
            copy_url = "%s/copy/%s" % (
                list_url, urllib.parse.quote(api_key_id, safe=""))
            status, obj, raw = util.http_json(
                copy_url, headers=key_headers, timeout=30)
            payload = _biz_data(obj)
            secret = ""
            if status < 400 and isinstance(payload, dict):
                secret = str(payload.get("secretKey") or payload.get("secret_key")
                             or payload.get("secret") or "")
            if not secret:
                raise UpstreamError(
                    "%s 项目令牌获取失败（%s；copy HTTP %s：%s）"
                    % (_realm(realm)["name"], pat_error, status,
                       _envelope_message(obj, status, raw)), status=502)
            credential, kind, expires_at = "%s.%s" % (api_key_id, secret), "key", 0
        return {
            "api_key": credential,
            "api_key_id": api_key_id,
            "api_key_kind": kind,
            "api_key_expires_at": expires_at,
            "org_id": org_id,
            "project_id": project_id,
            "org_name": str(org.get("organizationName") or org.get("name") or ""),
        }

    # -- local client scan (read-only) ------------------------------------
    def _credentials_path(self):
        return os.path.join(os.path.expanduser("~"), ".zcode", "v2",
                            "credentials.json")

    def _plaintext_entry(self, value):
        text = str(value or "").strip()
        if not text:
            return None
        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except Exception:
                return None
            if isinstance(obj, dict):
                token = obj.get("access_token") or obj.get("accessToken")
                api_key = obj.get("apiKey") or obj.get("api_key")
                jwt = obj.get("token") or obj.get("jwt") or obj.get("jwtToken")
                if any(isinstance(x, str) and x for x in (token, api_key, jwt)):
                    return {"access_token": token or "", "api_key": api_key or "",
                            "jwt_token": jwt or ""}
            return None
        parts = text.split(".")
        if len(parts) == 3:
            try:
                head = base64.urlsafe_b64decode(
                    parts[0] + "=" * (-len(parts[0]) % 4)).decode("utf-8")
                if head.startswith("{") and '"alg"' in head:
                    return {"access_token": "", "api_key": "", "jwt_token": text}
            except Exception:
                pass
        if len(parts) == 2 and len(parts[0]) >= 8 and len(parts[1]) >= 8:
            return {"access_token": "", "api_key": text, "jwt_token": ""}
        return None

    def scan_local(self):
        path = self._credentials_path()
        if not os.path.isfile(path):
            return []
        obj = util.read_json(path, {})
        if not isinstance(obj, dict):
            return []
        items = []
        for key, value in obj.items():
            if not isinstance(value, str):
                continue
            realm = ""
            key_text = str(key)
            if "api.z.ai" in key_text or key_text.startswith("oauth:zai"):
                realm = "zai"
            elif "bigmodel" in key_text:
                realm = "bigmodel"
            else:
                continue
            plain = self._plaintext_entry(value)
            items.append({
                "app": "ZCode",
                "path": path,
                "realm": realm,
                "key": urllib.parse.unquote(key_text)[:160],
                "encrypted": plain is None,
                "access_token": (plain or {}).get("access_token") or "",
                "api_key": (plain or {}).get("api_key") or "",
                "jwt_token": (plain or {}).get("jwt_token") or "",
            })
        return items

    def public_scan(self):
        out = []
        for item in self.scan_local():
            out.append({
                "app": item["app"],
                "path": item["path"],
                "realm": item["realm"],
                "encrypted": item["encrypted"],
                "importable": bool(item.get("api_key") or item.get("access_token")),
                "has_jwt": bool(item.get("jwt_token")),
            })
        return out

    def import_local(self, realm=None, **kwargs):
        imported = []
        errors = []
        items = self.scan_local()
        if not items:
            return {"imported": [], "errors": ["未找到 ZCode 客户端凭证文件"]}
        encrypted_note = "ZCode 桌面端凭证为加密存储，请使用 OAuth 登录或手动导入 API Key"
        for item in items:
            use_realm = realm if realm in REALMS else item["realm"]
            if item["encrypted"]:
                if encrypted_note not in errors:
                    errors.append(encrypted_note)
                continue
            try:
                if item.get("api_key"):
                    account = self.import_api_key(
                        item["api_key"], realm=use_realm,
                        jwt_token=item.get("jwt_token") or "",
                        source="local:ZCode")
                elif item.get("access_token"):
                    account = self.import_token(
                        realm=use_realm, oauth_access_token=item["access_token"],
                        jwt_token=item.get("jwt_token") or "",
                        source="local:ZCode")
                else:
                    continue
                imported.append(account)
            except Exception as exc:
                errors.append("ZCode: %s" % exc)
        return {"imported": [self.app.accounts.public(a) for a in imported],
                "errors": errors}

    def validate_account(self, account):
        try:
            result = self.fetch_account_usage(account)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        credits = (result or {}).get("credits") or {}
        return {"ok": True,
                "message": "额度可用：%s / %s"
                           % (credits.get("remain"), credits.get("size"))}

    def account_public_extra(self, account):
        data = account.get("data") or {}
        realm = _realm_of(data)
        return {
            "realm_label": _realm(realm)["name"],
            "checkin_supported": True,
            "has_jwt": bool(data.get("jwt_token")),
            "has_api_key": bool(data.get("api_key")),
            "source_label": {
                "oauth": "OAuth 登录",
                "apikey": "API Key 导入",
                "manual": "手动导入",
            }.get(str(data.get("source") or "").split(":")[0], "本地导入"),
            "credits_updated_at": data.get("credits_updated_at"),
            "credits_updated_iso": time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime(data.get("credits_updated_at") or 0))
            if data.get("credits_updated_at") else "",
        }
