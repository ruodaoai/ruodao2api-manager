"""GPT / generic OpenAI-compatible channel: base URL + bearer key."""

import hashlib
import threading
import time
import urllib.parse

from .. import util
from ..errors import AuthExpired, UpstreamError
from .base import BaseChannel, stream_openai_passthrough

DEFAULT_MODELS = [
    "gpt-5.6", "gpt-5.5", "gpt-5.4", "gpt-5.3", "gpt-5.3-codex",
    "gpt-5.2", "gpt-5.1", "gpt-5", "gpt-4.1", "gpt-4o", "o3", "o4-mini",
]
MODEL_TTL = 300
_SECRET_HEADER_MARKERS = ("auth", "token", "key", "cookie", "secret",
                          "credential", "password")


def _base_url(value):
    url = str(value or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[:-len("/chat/completions")]
    return url


def _models_of(account):
    data = account.get("data") or {}
    raw = data.get("models")
    if isinstance(raw, str):
        items = [x.strip() for x in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    else:
        items = []
    return [x for x in items if x]


def _model_map_of(account):
    mapping = (account.get("data") or {}).get("model_map")
    if isinstance(mapping, dict):
        return {str(k): str(v) for k, v in mapping.items() if k and v}
    return {}


def _headers_for(account):
    data = account.get("data") or {}
    headers = {
        "Authorization": "Bearer " + str(data.get("api_key") or ""),
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Encoding": "identity",
    }
    extra = data.get("extra_headers")
    if isinstance(extra, dict):
        for key, value in extra.items():
            low = str(key).lower()
            if any(marker in low for marker in _SECRET_HEADER_MARKERS):
                continue
            headers[str(key)] = str(value)
    org = str(data.get("organization") or "").strip()
    if org:
        headers["OpenAI-Organization"] = org
    project = str(data.get("project") or "").strip()
    if project:
        headers["OpenAI-Project"] = project
    return headers


class OpenAICompatChannel(BaseChannel):
    name = "gpt"
    label = "GPT / OpenAI 兼容"
    color = "#2f8f6b"
    supports_checkin = False
    supports_oauth = False

    def __init__(self, app):
        super(OpenAICompatChannel, self).__init__(app)
        self._catalog = {}
        self._lock = threading.Lock()

    # -- models -----------------------------------------------------------
    def list_models(self, account=None):
        accounts = [account] if account else self.app.accounts.list("gpt")
        seen = set()
        out = []
        for acc in accounts:
            if not acc.get("enabled"):
                continue
            models = self._account_models(acc)
            for model in models:
                if model in seen:
                    continue
                seen.add(model)
                out.append({
                    "id": "gpt/%s" % model,
                    "raw_id": model,
                    "object": "model",
                    "channel": "gpt",
                    "channel_label": self.label,
                    "realm": acc.get("realm") or "",
                    "realm_label": acc.get("name") or "自定义",
                    "owned_by": str((acc.get("data") or {}).get("name")
                                    or "OpenAI Compatible"),
                    "display_name": model,
                    "created": 0,
                    "context_length": 128000,
                    "capabilities": {"tools": True},
                })
        return out

    def _account_models(self, account):
        acc_id = account.get("id") or ""
        with self._lock:
            hit = self._catalog.get(acc_id)
        if hit and time.time() - hit[0] < MODEL_TTL:
            return hit[1]
        models = _models_of(account)
        if not models:
            models = self._fetch_models(account)
        if not models:
            models = list(DEFAULT_MODELS)
        with self._lock:
            self._catalog[acc_id] = (time.time(), models)
        return models

    def _fetch_models(self, account):
        base = _base_url((account.get("data") or {}).get("base_url"))
        if not base:
            return []
        try:
            status, obj, raw = util.http_json(
                base + "/models", headers={
                    "Authorization": "Bearer " + str((account.get("data") or {}).get("api_key") or ""),
                    "Accept": "application/json",
                }, timeout=20)
        except Exception:
            return []
        if status >= 400 or not isinstance(obj, (dict, list)):
            return []
        items = obj.get("data") if isinstance(obj, dict) else obj
        out = []
        for item in items or []:
            if isinstance(item, dict) and item.get("id"):
                out.append(str(item["id"]))
            elif isinstance(item, str) and item:
                out.append(item)
        return out

    def find_model(self, requested):
        raw = str(requested or "").strip()
        if raw.lower().startswith("gpt/"):
            raw = raw[4:]
        accounts = self.app.accounts.list("gpt") if getattr(self.app, "accounts", None) else []
        matched = None
        for acc in accounts:
            if not acc.get("enabled"):
                continue
            alias = _model_map_of(acc).get(raw)
            if alias:
                matched = (acc, alias)
                break
            if raw in self._account_models(acc):
                matched = (acc, raw)
                break
        realm = matched[0].get("realm") if matched else ""
        return {
            "id": "gpt/%s" % raw,
            "raw_id": raw,
            "realm": realm or "",
            "display_name": raw,
            "context_length": 128000,
            "capabilities": {},
            "unknown": matched is None,
            "account_id": matched[0]["id"] if matched else "",
        }

    def resolve_model(self, requested):
        found = self.find_model(requested)
        return found.get("raw_id") if found else str(requested or "")

    def supports_model(self, account, model_info):
        raw = str((model_info or {}).get("raw_id") or "")
        if not raw:
            return True
        models = _models_of(account)
        if models and raw not in models and raw not in _model_map_of(account):
            return bool((model_info or {}).get("unknown"))
        return True

    # -- inference --------------------------------------------------------
    def stream_chat(self, account, body, upstream_model):
        data = account.get("data") or {}
        base = _base_url(data.get("base_url"))
        api_key = str(data.get("api_key") or "").strip()
        if not base:
            raise UpstreamError("GPT 账号缺少 Base URL", status=503)
        if not api_key:
            raise AuthExpired("GPT 账号缺少 API Key", account_id=account.get("id"))
        model = _model_map_of(account).get(upstream_model, upstream_model)
        payload = dict(body or {})
        payload["model"] = model
        payload["stream"] = True
        url = base + "/chat/completions"
        try:
            for chunk in stream_openai_passthrough(url, _headers_for(account),
                                                   payload,
                                                   timeout=self._timeout()):
                yield chunk
        except UpstreamError as exc:
            # Some compatible servers reject `stream_options`; retry once
            # without it instead of failing the whole request.
            if exc.status == 400 and "stream_options" in (exc.message or "").lower() \
                    and "stream_options" in payload:
                retry = dict(payload)
                retry.pop("stream_options", None)
                for chunk in stream_openai_passthrough(url, _headers_for(account),
                                                       retry,
                                                       timeout=self._timeout()):
                    yield chunk
                return
            raise

    # -- account lifecycle -------------------------------------------------
    def refresh_account(self, account):
        return False

    def fetch_account_usage(self, account):
        return None

    def validate_account(self, account):
        data = account.get("data") or {}
        base = _base_url(data.get("base_url"))
        api_key = str(data.get("api_key") or "")
        if not base or not api_key:
            return {"ok": False, "message": "缺少 Base URL 或 API Key"}
        try:
            status, obj, raw = util.http_json(
                base + "/models", headers={"Authorization": "Bearer " + api_key,
                                           "Accept": "application/json"},
                timeout=20)
        except Exception as exc:
            return {"ok": False, "message": "连接失败：%s" % exc}
        if status in (401, 403):
            return {"ok": False, "message": "API Key 被拒绝 HTTP %s" % status}
        if status >= 400:
            return {"ok": False, "message": "模型列表查询失败 HTTP %s" % status}
        models = self._fetch_models(account)
        if models:
            with self._lock:
                self._catalog[account.get("id") or ""] = (time.time(), models)
        return {"ok": True, "message": "凭证有效，可用模型 %d 个" % len(models)}

    def add_account(self, base_url, api_key, name="", models=None,
                    model_map=None, extra_headers=None, organization="",
                    project="", realm="custom"):
        base = _base_url(base_url)
        api_key = str(api_key or "").strip()
        if not base or not api_key:
            raise ValueError("Base URL 和 API Key 不能为空")
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        safe_headers = {}
        if isinstance(extra_headers, dict):
            for key, value in extra_headers.items():
                low = str(key).lower()
                if any(marker in low for marker in _SECRET_HEADER_MARKERS):
                    continue
                safe_headers[str(key)] = str(value)
        if isinstance(models, str):
            models = [x.strip() for x in models.replace("\n", ",").split(",") if x.strip()]
        data = {
            "uid": "gpt-" + hashlib.sha256(
                (base + "\n" + api_key).encode("utf-8")).hexdigest()[:20],
            "nickname": str(name or parsed.netloc),
            "name": str(name or parsed.netloc),
            "base_url": base,
            "api_key": api_key,
            "organization": str(organization or ""),
            "project": str(project or ""),
            "realm": realm or "custom",
            "source": "manual",
            "needs_login": False,
        }
        if models:
            data["models"] = list(models)
        if model_map:
            data["model_map"] = dict(model_map)
        if safe_headers:
            data["extra_headers"] = safe_headers
        return self.app.accounts.add("gpt", data,
                                     name=str(name or parsed.netloc),
                                     realm=realm or "custom")

    def account_public_extra(self, account):
        data = account.get("data") or {}
        return {
            "base_url": util.redact_url(str(data.get("base_url") or "")),
            "model_count": len(_models_of(account)),
            "source_label": "手动配置",
        }
