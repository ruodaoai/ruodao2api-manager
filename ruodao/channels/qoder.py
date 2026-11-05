"""Qoder channel: device auth / PAT / local import, COSY chat, campaigns and credits."""

import base64
import datetime
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import uuid

from .. import util
from ..errors import AuthExpired, UpstreamError
from ..qoder import sign
from ..qoder.fingerprint import derive_id, generate_request_id
from .base import BaseChannel

REALMS = {
    "cn": {
        "name": "国内版",
        "openapi": "https://openapi.qoder.com.cn",
        "gateway": "https://gateway.qoder.com.cn",
        "website": "https://qoder.com.cn",
        "client_id": "1c5e33e1-364d-4ce6-b02c-acaa81274a5c",
        "redirect_uri": "qoder-work-cn://",
        "domain": "qoder.com.cn",
        "ua": "QoderWork/1.1.75",
        "send_redirect_uri": True,
        "nonce_dashed": True,
        "home_dir": ".qoder-cn",
        "app_dir": "com.qodercn.app.stable",
    },
    "intl": {
        "name": "国际版",
        "openapi": "https://openapi.qoder.sh",
        "gateway": "https://api3.qoder.sh",
        "gateways": ["https://api3.qoder.sh", "https://api2.qoder.sh",
                     "https://api1.qoder.sh"],
        "website": "https://qoder.com",
        "client_id": "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        "redirect_uri": "qoder://aicoding.aicoding-agent/login-success",
        "domain": "qoder.com",
        "ua": "Qoder/1.1.75",
        "send_redirect_uri": False,
        "nonce_dashed": False,
        "home_dir": ".qoder",
        "app_dir": "com.qoder.app.stable",
    },
}

PATH_DEVICE_POLL = "/api/v1/deviceToken/poll"
PATH_DEVICE_REFRESH = "/api/v1/deviceToken/refresh"
PATH_JOB_EXCHANGE = "/api/v1/jobToken/exchange"
PATH_JOB_REFRESH = "/api/v1/jobToken/refresh"
PATH_USERINFO = "/api/v1/userinfo"
PATH_QUOTA = "/api/v2/quota/usage"
PATH_PLAN = "/api/v2/user/plan"
PATH_CHECKIN_STATUS = "/sash/api/v1/me/daily-check-in/status"
PATH_CHECKIN_CLAIM = "/sash/api/v1/me/daily-check-in/claim"
PATH_CAMPAIGNS = "/sash/api/v1/me/campaigns"
PATH_CAMPAIGN_CLAIM = "/sash/api/v1/me/campaigns/%s/claim"
PATH_CAMPAIGN_REWARD = "/sash/api/v1/me/campaigns/%s/reward"

CHAT_PATH = ("/algo/api/v2/service/pro/sse/agent_chat_generation"
             "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1")
MODELS_PATH = "/algo/api/v2/model/list?Encode=1"

DESKTOP_VERSION = (os.environ.get("RUODAO_QODER_DESKTOP_VERSION")
                   or os.environ.get("QD_DESKTOP_VERSION") or "0.4.8").strip()
CLAIM_GAP = 1.2
DEFAULT_USER_TYPE = "personal_professional_trial"
LEAK_MARKER = "[assistant 请求调用工具]"
TOOL_RESULT_MARKER = "[工具结果"

CATALOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "qoder")

_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def _realm(realm):
    return REALMS.get(realm) or REALMS["cn"]


def _load_catalog(realm):
    path = os.path.join(CATALOG_DIR, "catalog_%s.json" % ("cn" if realm == "cn" else "intl"))
    data = util.read_json(path, [])
    return [m for m in (data if isinstance(data, list) else [])
            if isinstance(m, dict) and m.get("key")]


def _display(realm, key):
    for m in _load_catalog(realm):
        if m.get("key") == key:
            return m.get("display_name") or key
    return key


def _next_checkin_window():
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)
    nxt = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if now >= nxt:
        nxt += datetime.timedelta(days=1)
    return int(nxt.timestamp()), "每天 10:00（UTC+8）"


def _stamp(account):
    account.setdefault("data", {})["last_checkin"] = time.strftime("%Y-%m-%d")
    account["data"]["last_checkin_at"] = time.time()


def _stamp_message(account, message):
    account.setdefault("data", {})["last_checkin_message"] = str(message or "")[:500]


def _tool_ids_ok(messages):
    pending = set()
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    pending.add(str(tc["id"]))
        elif m.get("role") == "tool":
            tid = str(m.get("tool_call_id") or "")
            if tid:
                pending.discard(tid)
    return not pending


def _structured_for(realm, model_key, display_name, messages):
    if realm != "cn":
        return False
    if not _tool_ids_ok(messages):
        return False
    low = "%s %s" % (str(model_key or "").lower(), str(display_name or "").lower())
    return any(tag in low for tag in ("qwen", "qmodel", "glm", "gmodel",
                                      "gm4", "gm5", "deepseek", "dmodel",
                                      "dfmodel"))


def _shrink_tool_results(messages):
    """Keep the latest tool result intact, trim older ones deterministically."""
    idxs = [i for i, m in enumerate(messages or [])
            if isinstance(m, dict) and m.get("role") == "tool"]
    protect = set(idxs[-1:])
    out = []
    for i, m in enumerate(messages or []):
        if isinstance(m, dict) and m.get("role") == "tool" and i not in protect:
            text = util.text_from_content(m.get("content"))
            if len(text) > 4500:
                m = dict(m)
                m["content"] = text[:2000] + "\n...[较早的工具结果已截断]...\n" + text[-2000:]
        out.append(m)
    return out


def _content_for_image(content):
    parts = util.content_parts(content)
    out = []
    has_image = False
    for p in parts:
        if p.get("type") == "text":
            out.append({"type": "text", "text": p.get("text") or ""})
        elif p.get("type") == "image_url":
            has_image = True
            iu = p.get("image_url")
            if isinstance(iu, str):
                iu = {"url": iu}
            out.append({"type": "image_url", "image_url": iu})
    return out, has_image


def _flatten_messages(messages, structured=False):
    system_text = None
    flat = []
    images = []
    for m in _shrink_tool_results(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("system", "developer"):
            if system_text is None:
                system_text = util.text_from_content(m.get("content"))
            continue
        if role == "tool":
            text = util.text_from_content(m.get("content"))
            if structured:
                flat.append({"role": "tool",
                             "tool_call_id": str(m.get("tool_call_id") or ""),
                             "content": text or ""})
            else:
                name = m.get("name") or ""
                flat.append({"role": "user",
                             "content": TOOL_RESULT_MARKER + ((" (%s)" % name) if name else "") +
                                        "]\n" + (text or "")})
            continue
        if role == "assistant" and m.get("tool_calls"):
            if structured:
                calls = []
                for tc in m.get("tool_calls") or []:
                    tc = tc if isinstance(tc, dict) else {}
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, (dict, list)):
                        args = util.json_dumps(args)
                    elif not isinstance(args, str):
                        args = "" if args is None else str(args)
                    calls.append({"id": str(tc.get("id") or ""),
                                  "type": tc.get("type") or "function",
                                  "function": {"name": fn.get("name") or "",
                                               "arguments": args}})
                flat.append({"role": "assistant",
                             "content": util.text_from_content(m.get("content")),
                             "tool_calls": calls})
            else:
                calls = []
                for tc in m.get("tool_calls") or []:
                    fn = (tc or {}).get("function") or {}
                    calls.append({"name": fn.get("name") or "",
                                  "arguments": fn.get("arguments") or ""})
                text = util.text_from_content(m.get("content"))
                if calls:
                    text = (text or "") + "\n\n" + LEAK_MARKER + "\n" + util.json_dumps(calls)
                flat.append({"role": "assistant", "content": text or ""})
            continue
        if role in ("user", "assistant"):
            parts, has_image = _content_for_image(m.get("content"))
            if has_image:
                for p in parts:
                    if p.get("type") == "image_url":
                        iu = p.get("image_url") or {}
                        url = iu.get("url") if isinstance(iu, dict) else ""
                        if url:
                            images.append(url)
                flat.append({"role": role, "content": parts})
            else:
                flat.append({"role": role,
                             "content": util.text_from_content(m.get("content"))})
        elif role:
            flat.append({"role": "user",
                         "content": util.text_from_content(m.get("content"))})
    return system_text, flat, images


def _normalize_tools(tools):
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            out.append(tool)
        elif tool.get("name"):
            out.append({"type": "function", "function": {
                "name": tool.get("name") or "",
                "description": tool.get("description") or "",
                "parameters": tool.get("parameters") or tool.get("input_schema") or
                              {"type": "object", "properties": {}},
            }})
    return out


def _find_catalog(realm, raw):
    raw = str(raw or "").strip()
    low = raw.lower()
    for m in _load_catalog(realm):
        if str(m.get("key") or "").lower() == low:
            return m
        if str(m.get("display_name") or "").lower() == low:
            return m
    return None


def _build_qoder_body(payload, account, model_key, meta, realm):
    base = util.read_json(os.path.join(CATALOG_DIR, "baseprompt.json"), None)
    if not isinstance(base, dict):
        raise UpstreamError("缺少 Qoder baseprompt.json", status=500)
    body = json.loads(json.dumps(base))
    messages = payload.get("messages") or []
    structured = payload.get("_qd_structured")
    if structured is None:
        structured = _structured_for(realm, model_key,
                                     (meta or {}).get("display_name") or "",
                                     messages)
    system_text, flat, images = _flatten_messages(messages, structured=bool(structured))
    tmpl_system = [m for m in (body.get("messages") or [])
                   if isinstance(m, dict) and m.get("role") == "system"]
    if system_text is None:
        body["messages"] = tmpl_system
    else:
        body["messages"] = [{"role": "system", "content": system_text}]
    body["messages"].extend(flat)

    prompt = ""
    for m in reversed(flat):
        if m.get("role") == "user" and m.get("content"):
            content = m["content"]
            if isinstance(content, list):
                prompt = "\n".join(str(p.get("text") or "") for p in content
                                   if isinstance(p, dict) and p.get("type") == "text")
            else:
                prompt = str(content)
            break

    nid = str(uuid.uuid4())
    body["request_id"] = nid
    body["chat_record_id"] = payload.get("_qd_conversation_id") or nid
    body["request_set_id"] = str(uuid.uuid4())
    body["session_id"] = payload.get("_qd_session_id") or str(uuid.uuid4())
    body["stream"] = True
    body["agent_id"] = "agent_common"
    data = account.get("data") or {}
    body["aliyun_user_type"] = data.get("user_type") or DEFAULT_USER_TYPE

    catalog_meta = meta or {}
    mc = body.get("model_config") or {}
    mc["key"] = model_key
    mc["display_name"] = catalog_meta.get("display_name") or model_key
    mc["model"] = ""
    mc["format"] = "openai"
    mc["is_vl"] = bool(catalog_meta.get("is_vl"))
    mc["is_reasoning"] = bool(catalog_meta.get("is_reasoning"))
    mc["api_key"] = ""
    mc["url"] = ""
    mc["source"] = "system"
    mc["max_input_tokens"] = catalog_meta.get("max_input_tokens") or 180000
    body["model_config"] = mc

    cc = body.get("chat_context") or {}
    txt = cc.get("text") or {}
    txt["text"] = prompt
    cc["text"] = txt
    extra = cc.get("extra") or {}
    oc = extra.get("originalContent") or {}
    oc["text"] = prompt
    extra["originalContent"] = oc
    extra["modelConfig"] = dict(mc)
    cc["extra"] = extra
    if images:
        cc["imageUrls"] = images
        body["image_urls"] = images
    body["chat_context"] = cc

    params = body.get("parameters") or {}
    max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
    if max_tokens:
        try:
            params["max_tokens"] = int(max_tokens)
        except Exception:
            pass
    effort = payload.get("reasoning_effort")
    if not effort and isinstance(payload.get("reasoning"), dict):
        effort = payload["reasoning"].get("effort")
    if effort and str(effort).lower() in ("low", "medium", "high", "xhigh"):
        params["reasoning_effort"] = str(effort).lower()
    body["parameters"] = params

    body["tools"] = _normalize_tools(payload.get("tools"))
    biz = body.get("business") or {}
    biz["id"] = payload.get("_qd_conversation_id") or str(uuid.uuid4())
    begin = payload.get("_qd_begin_at")
    try:
        biz["begin_at"] = int(begin) if begin else int(time.time() * 1000)
    except Exception:
        biz["begin_at"] = int(time.time() * 1000)
    biz["name"] = prompt[:30] if prompt else "chat"
    biz.setdefault("product", "cli")
    biz.setdefault("type", "agent")
    biz.setdefault("stage", "start")
    biz["model_key"] = model_key
    biz["version"] = sign.COSY_VERSION
    body["business"] = biz
    return body


def _clean_chunk(obj):
    if not isinstance(obj, dict):
        return None
    if obj.get("error"):
        raise UpstreamError(str(obj.get("error"))[:500], status=502)
    usage = obj.get("usage")
    if isinstance(usage, dict):
        try:
            cached = usage.get("cached_tokens")
            if cached is None:
                cached = usage.get("prompt_cache_hit_tokens")
            if cached is None:
                det = usage.get("completion_tokens_details") or {}
                cached = det.get("cached_tokens") if isinstance(det, dict) else None
            if cached:
                pdet = usage.get("prompt_tokens_details")
                pdet = pdet if isinstance(pdet, dict) else {}
                pdet.setdefault("cached_tokens", int(cached))
                usage["prompt_tokens_details"] = pdet
            obj["usage"] = util.normalize_usage(usage) or usage
        except Exception:
            pass
    choices = obj.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict):
                for key in ("content", "reasoning_content", "tool_calls",
                            "function_call"):
                    if key in delta and delta.get(key) in (None, "", []):
                        delta.pop(key, None)
        if not choices and not obj.get("usage"):
            return None
    return obj


class QoderChannel(BaseChannel):
    name = "qoder"
    label = "Qoder"
    color = "#6d5ce7"
    supports_checkin = True
    supports_oauth = True

    def __init__(self, app):
        super(QoderChannel, self).__init__(app)
        self._logins = {}
        self._campaign_cache = {}
        self._cache_lock = threading.Lock()

    # -- models -----------------------------------------------------------
    def list_models(self, account=None):
        out = []
        for realm in ("cn", "intl"):
            for m in _load_catalog(realm):
                key = str(m.get("key") or "")
                if not key:
                    continue
                out.append({
                    "id": "qoder/%s/%s" % (realm, key),
                    "raw_id": key,
                    "object": "model",
                    "channel": "qoder",
                    "channel_label": "Qoder",
                    "realm": realm,
                    "realm_label": REALMS[realm]["name"],
                    "owned_by": "Qoder %s" % REALMS[realm]["name"],
                    "display_name": m.get("display_name") or key,
                    "created": 0,
                    "context_length": util.safe_int(m.get("max_input_tokens"), 180000),
                    "capabilities": {
                        "vision": bool(m.get("is_vl")),
                        "reasoning": bool(m.get("is_reasoning")),
                        "free": bool(m.get("is_free")),
                        "tools": True,
                    },
                    "price_factor": m.get("price_factor"),
                })
        return out

    def supports_model(self, account, model_info):
        realm = (model_info or {}).get("realm")
        if realm and account.get("realm") and realm != account.get("realm"):
            return False
        return True

    def resolve_model(self, requested):
        found = self.find_model(requested)
        return found.get("raw_id") if found else None

    def find_model(self, requested):
        raw = str(requested or "").strip()
        if raw.lower().startswith("qoder/"):
            raw = raw[6:]
        realm = ""
        low = raw.lower()
        if low.startswith("cn/"):
            realm, raw = "cn", raw[3:]
        elif low.startswith("intl/"):
            realm, raw = "intl", raw[5:]
        realm_order = [realm] if realm else ["cn", "intl"]
        matches = []
        for r in realm_order:
            m = _find_catalog(r, raw)
            if m:
                matches.append((r, m))
        if not matches:
            # pass-through model key; choose the realm with accounts
            accounts = self.app.accounts.list("qoder") if getattr(self.app, "accounts", None) else []
            if realm:
                use = realm
            elif any(a.get("realm") == "cn" and a.get("enabled") for a in accounts):
                use = "cn"
            elif any(a.get("realm") == "intl" and a.get("enabled") for a in accounts):
                use = "intl"
            else:
                use = "cn"
            return {"id": "qoder/%s/%s" % (use, raw), "raw_id": raw,
                    "realm": use, "display_name": raw, "unknown": True,
                    "context_length": 180000, "capabilities": {}}
        if not realm and len(matches) > 1:
            # Prefer a realm with an enabled account.
            accounts = self.app.accounts.list("qoder") if getattr(self.app, "accounts", None) else []
            for preferred in ("cn", "intl"):
                if any(a.get("realm") == preferred and a.get("enabled") for a in accounts):
                    for r, m in matches:
                        if r == preferred:
                            return self._model_dict(r, m)
        r, m = matches[0]
        return self._model_dict(r, m)

    def _model_dict(self, realm, m):
        key = m.get("key")
        return {
            "id": "qoder/%s/%s" % (realm, key),
            "raw_id": key,
            "realm": realm,
            "display_name": m.get("display_name") or key,
            "context_length": util.safe_int(m.get("max_input_tokens"), 180000),
            "capabilities": {"vision": bool(m.get("is_vl")),
                             "reasoning": bool(m.get("is_reasoning")),
                             "free": bool(m.get("is_free")), "tools": True},
            "meta": m,
        }

    # -- inference --------------------------------------------------------
    def _session_for(self, account):
        data = account.get("data") or {}
        token = str(data.get("access_token") or "")
        if not token:
            raise AuthExpired("Qoder 账号没有 access token", account_id=account.get("id"))
        key = account.get("id") or data.get("uid") or token[:12]
        with _SESSIONS_LOCK:
            cached = _SESSIONS.get(key)
            if cached and cached[0] == token:
                return cached[1]
        sess = sign.CosySession(
            uid=str(data.get("uid") or ""),
            nickname=str(data.get("nickname") or ""),
            access_token=token,
            refresh_token=str(data.get("refresh_token") or ""),
            user_type=str(data.get("user_type") or DEFAULT_USER_TYPE),
            org_id=str(data.get("organization_id") or ""),
            org_name=str(data.get("organization_name") or ""),
        )
        with _SESSIONS_LOCK:
            _SESSIONS[key] = (token, sess)
        return sess

    def _invalidate_session(self, account):
        with _SESSIONS_LOCK:
            _SESSIONS.pop(account.get("id"), None)

    def stream_chat(self, account, body, upstream_model):
        realm = account.get("realm") or "cn"
        found = self.find_model(upstream_model)
        meta = (found or {}).get("meta") or _find_catalog(realm, upstream_model) or {}
        qbody = _build_qoder_body(body, account, upstream_model, meta, realm)
        encoded = sign.qoder_encode(util.json_dumps(qbody).encode("utf-8"))
        sess = self._session_for(account)
        hosts = [REALMS[realm]["gateway"]] + list(REALMS[realm].get("gateways") or [])
        seen = []
        hosts = [h for h in hosts if not (h in seen or seen.append(h))]
        last_error = None
        for host in hosts:
            raw_url = host + CHAT_PATH
            headers = sess.headers(encoded, raw_url, model_key=upstream_model,
                                   sse=True)
            resp = None
            try:
                resp = util.open_stream(raw_url, headers=headers,
                                        body=encoded.encode("utf-8"),
                                        timeout=self._timeout())
            except util.HttpStatusError as exc:
                detail = exc.body.decode("utf-8", "replace")
                last_error = self._http_error(exc.status, detail, account)
                if exc.status in (401, 403) or exc.status < 500:
                    raise last_error
                continue
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_error = UpstreamError("连接 Qoder 上游失败: %s" % exc,
                                           status=502, retryable=True)
                continue
            try:
                for chunk in self._iter_envelope(resp, account):
                    yield chunk
                return
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        raise last_error or UpstreamError("Qoder 上游不可达", status=502,
                                          retryable=True)

    def _http_error(self, status, detail, account=None):
        message = detail[:500]
        try:
            obj = json.loads(detail)
            message = str(obj.get("message") or obj.get("msg")
                          or (obj.get("error") or {}).get("message")
                          or obj.get("detail") or detail)[:500]
        except Exception:
            pass
        low = detail.lower()
        if status in (401, 403) or "token_expire" in low or "12153" in low \
                or "offline user session" in low:
            if account:
                account.setdefault("data", {})["needs_login"] = True
                account["data"]["auth_error"] = message
            return AuthExpired("Qoder 凭证失效，需要重新登录：%s" % message,
                               account_id=(account or {}).get("id", ""))
        if status == 429 or "frequency limit" in low or "10605" in low:
            wait = 60
            try:
                obj = json.loads(detail)
                wait = int(obj.get("retryAfterSeconds") or obj.get("retry_after") or 60)
            except Exception:
                pass
            return UpstreamError("Qoder 上游限流，请稍后重试", status=429,
                                 code="rate_limited", retryable=True)
        if status == 430 or "version" in low and "low" in low:
            return UpstreamError("客户端/网关版本过低，请升级", status=430)
        return UpstreamError(message or ("Qoder HTTP %s" % status),
                             status=status, retryable=status >= 500)

    def _iter_envelope(self, resp, account):
        for raw in resp:
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "replace")
            else:
                line = str(raw)
            data = util.strip_data_prefix(line)
            if not data:
                continue
            if data == "[DONE]":
                return
            try:
                outer = json.loads(data)
            except Exception:
                continue
            if not isinstance(outer, dict):
                continue
            status = outer.get("statusCodeValue")
            inner_body = outer.get("body")
            if status not in (None, 200, "200"):
                detail = inner_body if isinstance(inner_body, str) else util.json_dumps(outer)
                raise self._http_error(int(status), detail, account)
            if not isinstance(inner_body, str):
                continue
            if inner_body == "[DONE]":
                return
            try:
                inner = json.loads(inner_body)
            except Exception:
                continue
            cleaned = _clean_chunk(inner)
            if cleaned:
                yield cleaned

    # -- credentials ------------------------------------------------------
    def _openapi_headers(self, account, token=None):
        realm = account.get("realm") or "cn"
        cfg = _realm(realm)
        data = account.get("data") or {}
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": cfg["ua"],
            "Authorization": "Bearer " + (token or data.get("access_token") or ""),
            "X-Request-ID": generate_request_id(data.get("uid") or ""),
            "X-Machine-ID": derive_id(data.get("uid") or "", "machine"),
            "X-Session-ID": derive_id(data.get("uid") or "", "session"),
            "Origin": cfg["website"],
            "Referer": cfg["website"] + "/",
        }

    def desktop_headers(self, account):
        data = account.get("data") or {}
        return {
            "Accept": "application/json",
            "User-Agent": "Qoder",
            "Authorization": "Bearer " + str(data.get("access_token") or ""),
            "cosy-clienttype": "10",
            "cosy-version": DESKTOP_VERSION,
        }

    def refresh_account(self, account):
        data = account.get("data") or {}
        realm = account.get("realm") or "cn"
        base = _realm(realm)["openapi"]
        refresh = str(data.get("refresh_token") or "")
        personal = str(data.get("personal_token") or "")
        ok = False
        if refresh.startswith("drt-"):
            ok = self._post_token(account, base + PATH_DEVICE_REFRESH,
                                  {"refresh_token": refresh}, "device")
        elif refresh:
            ok = self._post_token(account, base + PATH_JOB_REFRESH,
                                  {"refresh_token": refresh}, "job")
        if not ok and personal:
            ok = self._post_token(account, base + PATH_JOB_EXCHANGE,
                                  {"personal_token": personal}, "job")
        if not ok:
            if not refresh and not personal:
                data["needs_login"] = True
                data["auth_error"] = "没有可用的 refresh token"
                self.store.save_account(account)
            return False
        self._invalidate_session(account)
        self.store.save_account(account)
        return True

    def _post_token(self, account, url, payload, kind):
        realm = account.get("realm") or "cn"
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json",
                   "User-Agent": _realm(realm)["ua"]}
        status, obj, raw = util.http_json(url, method="POST", headers=headers,
                                          body=payload, timeout=30)
        if status >= 400 or not isinstance(obj, dict):
            if status in (401, 403):
                account["data"]["needs_login"] = True
                account["data"]["auth_error"] = "refresh HTTP %s" % status
                self.store.save_account(account)
            return False
        token = obj.get("token") or obj.get("device_token") or ""
        if not token:
            return False
        data = account.setdefault("data", {})
        data["access_token"] = token
        if obj.get("refresh_token"):
            data["refresh_token"] = obj["refresh_token"]
        exp = 0
        if obj.get("expires_in"):
            try:
                exp = int(time.time() + int(obj["expires_in"]) / 1000)
            except Exception:
                exp = 0
        elif obj.get("expires_at"):
            exp = util.normalize_epoch(obj["expires_at"])
        elif kind == "job":
            exp = int(time.time() + 24 * 3600)
        data["expires_at"] = exp or util.normalize_epoch(data.get("expires_at"))
        data.pop("needs_login", None)
        data.pop("auth_error", None)
        return True

    # -- balance ----------------------------------------------------------
    def fetch_account_usage(self, account):
        realm = account.get("realm") or "cn"
        base = _realm(realm)["openapi"]
        status, q, raw = util.http_json(base + PATH_QUOTA, headers=self._openapi_headers(account),
                                        timeout=30)
        if status >= 400 or not isinstance(q, dict):
            if status in (401, 403):
                raise AuthExpired("额度查询被拒绝 HTTP %s" % status,
                                  account_id=account.get("id"))
            raise UpstreamError("额度查询失败 HTTP %s" % status, status=502)
        uq = q.get("userQuota") or {}
        aq = q.get("addOnQuota") or {}
        if not isinstance(uq, dict) and not isinstance(aq, dict):
            raise UpstreamError("额度响应无法识别", status=502)

        def num(d, key):
            try:
                return float((d or {}).get(key) or 0)
            except Exception:
                return 0.0

        remain = int(num(uq, "remaining") + num(aq, "remaining"))
        used = int(num(uq, "used") + num(aq, "used"))
        size = int(num(uq, "total") + num(aq, "total"))
        shared = None
        for key in ("orgResourcePackage", "sharedQuota", "teamQuota",
                    "poolQuota", "sharedCredits"):
            src = q.get(key)
            if isinstance(src, dict):
                shared = {
                    "remain": int(num(src, "remaining")),
                    "used": int(num(src, "used")),
                    "size": int(num(src, "cap") or num(src, "total")),
                    "available": src.get("available", True) not in (False, 0, "false"),
                    "source": key,
                }
                if shared["size"] < 0:
                    shared["unlimited"] = True
                break
        packages = [
            {"name": "基础额度", "remain": int(num(uq, "remaining")),
             "used": int(num(uq, "used")), "size": int(num(uq, "total"))},
            {"name": "赠送/签到额度", "remain": int(num(aq, "remaining")),
             "used": int(num(aq, "used")), "size": int(num(aq, "total"))},
        ]
        if shared:
            packages.append(dict(shared, name="共享额度"))
        shared_avail = 0
        if shared and shared.get("available") and not shared.get("unlimited"):
            shared_avail = int(shared.get("remain") or 0)
        credits = {
            "remain": remain,
            "remain_effective": int(remain + shared_avail),
            "used": used,
            "size": size,
            "exceeded": bool(q.get("isQuotaExceeded")),
            "usage_pct": q.get("totalUsagePercentage"),
            "expires_at": util.normalize_epoch(q.get("expiresAt")),
            "shared": shared,
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": util.iso_now(),
        }
        plan = str(account.get("data", {}).get("plan") or "")
        try:
            st, p, _ = util.http_json(base + PATH_PLAN,
                                      headers=self._openapi_headers(account),
                                      timeout=15)
            if st < 400 and isinstance(p, dict):
                plan = str(p.get("plan_tier_name") or p.get("user_type") or plan)
        except Exception:
            pass
        return {"credits": credits, "plan": plan}

    # -- check-in ---------------------------------------------------------
    def _campaigns(self, account, force=False):
        key = account.get("id")
        now = time.time()
        with self._cache_lock:
            hit = self._campaign_cache.get(key)
            if hit and not force and now - hit[0] < 20:
                return hit[1]
        realm = account.get("realm") or "cn"
        url = _realm(realm)["openapi"] + PATH_CAMPAIGNS
        status, obj, raw = util.http_json(url, headers=self.desktop_headers(account),
                                          timeout=20)
        if status >= 400:
            result = {"ok": False, "unavailable": status in (404, 405, 410),
                      "http": status, "error": "HTTP %s %s" % (status, raw[:200])}
        elif not isinstance(obj, dict):
            result = {"ok": False, "error": "活动列表响应无法解析", "raw": raw[:300]}
        else:
            items = []
            for c in obj.get("campaigns") or []:
                if not isinstance(c, dict):
                    continue
                benefit = c.get("benefit") if isinstance(c.get("benefit"), dict) else {}
                title_zh = title_en = desc = detail = button = ""
                for pl in c.get("placements") or []:
                    cont = (pl or {}).get("content")
                    if not isinstance(cont, dict):
                        continue
                    zh = cont.get("zh") if isinstance(cont.get("zh"), dict) else {}
                    en = cont.get("en") if isinstance(cont.get("en"), dict) else {}
                    title_zh = title_zh or str(zh.get("title") or "")
                    title_en = title_en or str(en.get("title") or "")
                    desc = desc or str(zh.get("description") or en.get("description") or "")
                    detail = detail or str(zh.get("detailUrl") or en.get("detailUrl") or "")
                    button = button or str(zh.get("buttonText") or en.get("buttonText") or "")
                    if title_zh and desc:
                        break
                items.append({
                    "campaign_id": str(c.get("campaignId") or c.get("campaign_id") or ""),
                    "campaign_key": str(c.get("campaignKey") or c.get("campaign_key") or ""),
                    "title": title_zh or title_en or str(c.get("name") or ""),
                    "title_en": title_en,
                    "description": desc,
                    "detail_url": detail,
                    "button_text": button,
                    "action_type": str(c.get("actionType") or c.get("action_type") or ""),
                    "claim_status": str(c.get("claimStatus") or c.get("claim_status") or "").upper(),
                    "benefit": {"kind": str(benefit.get("kind") or ""),
                                "amount": util.safe_int(benefit.get("amount"), 0)},
                    "required_achievement_key": str(c.get("requiredAchievementKey") or ""),
                    "achievement_completed": bool(c.get("achievementCompleted")),
                    "unavailable_reason": str(c.get("unavailableReason") or ""),
                })
            result = {"ok": True, "available": True,
                      "show_campaign": bool(obj.get("showCampaign")),
                      "claimable": bool(obj.get("claimable")),
                      "campaigns": items,
                      "campaign_url": str(obj.get("campaignUrl") or "")}
        with self._cache_lock:
            self._campaign_cache[key] = (time.time(), result)
        return result

    def _claim_campaign(self, account, campaign_id):
        realm = account.get("realm") or "cn"
        url = _realm(realm)["openapi"] + (PATH_CAMPAIGN_CLAIM % campaign_id)
        headers = self.desktop_headers(account)
        headers["Content-Type"] = "application/json"
        status, obj, raw = util.http_json(url, method="POST", headers=headers,
                                          body=b"{}", timeout=20)
        if status == 409:
            return {"ok": True, "status": "CLAIMED", "replayed": True,
                    "message": "今日已领取"}
        if status >= 400 or not isinstance(obj, dict):
            return {"ok": False, "error": "HTTP %s %s" % (status, raw[:200])}
        st = str(obj.get("status") or "").upper()
        failure = str(obj.get("failureCode") or "").upper()
        code = str(obj.get("redemptionCode") or "").strip()
        if failure == "SAME_PERSON_ALREADY_CLAIMED" or st == "BLOCKED":
            return {"ok": False, "blocked": True, "status": st or "BLOCKED",
                    "failure_code": failure or "BLOCKED",
                    "message": "同人已领取（同设备下其他账号本轮已领）"}
        if failure:
            return {"ok": False, "status": st, "failure_code": failure,
                    "redemption_code": code,
                    "message": {
                        "REDEMPTION_CODE_OUT_OF_STOCK": "今日名额已发完",
                        "ACHIEVEMENT_NOT_COMPLETED": "需先在官方桌面端完成对应任务",
                    }.get(failure, "领取失败：%s" % failure)}
        if not st and obj.get("success") is False:
            return {"ok": False, "error": str(obj.get("error") or obj.get("msg") or raw[:200])}
        if code:
            account.setdefault("data", {}).setdefault("campaign_codes", {})[campaign_id] = code
        return {"ok": st in ("CLAIMED", "GRANTED", "SUCCESS"),
                "status": st, "replayed": bool(obj.get("replayed")),
                "redemption_code": code,
                "amount": util.safe_int((obj.get("benefit") or {}).get("amount")
                                        if isinstance(obj.get("benefit"), dict)
                                        else obj.get("amount"), 0),
                "message": "领取成功" + (("，兑换码：%s" % code) if code else "")}

    def _legacy_checkin(self, account):
        realm = account.get("realm") or "cn"
        base = _realm(realm)["openapi"]
        status, obj, raw = util.http_json(base + PATH_CHECKIN_STATUS,
                                          headers=self._openapi_headers(account),
                                          timeout=15)
        if status in (404, 405, 410):
            return {"ok": True, "unavailable": True, "message": "本区域未开放旧版签到接口"}
        if status >= 400 or not isinstance(obj, dict):
            return {"ok": False, "error": "签到状态查询失败 HTTP %s" % status}
        if obj.get("today_checked_in") or obj.get("todayCheckedIn"):
            _stamp(account)
            return {"ok": True, "already": True, "message": "今日已签到",
                    "streak_days": obj.get("streak_days") or obj.get("streakDays")}
        if obj.get("active") is False or str(obj.get("status") or "").upper() == "DISABLED":
            return {"ok": True, "disabled": True,
                    "message": "官方签到活动未开放（status=%s）" % (obj.get("status") or "?")}
        status, res, raw = util.http_json(base + PATH_CHECKIN_CLAIM, method="POST",
                                          headers=self._openapi_headers(account),
                                          body=b"{}", timeout=15)
        if status == 409 or "ALREADY_CLAIMED" in raw:
            _stamp(account)
            return {"ok": True, "already": True, "message": "今日已签到"}
        if status >= 400 or not isinstance(res, dict):
            return {"ok": False, "error": "签到失败 HTTP %s" % status}
        if str(res.get("result") or "") == "ALREADY_CLAIMED":
            _stamp(account)
            return {"ok": True, "already": True, "message": "今日已签到"}
        reward = util.safe_int(res.get("rewardCredits"), 0)
        _stamp(account)
        return {"ok": True, "message": "签到成功 +%d 积分" % reward,
                "reward_credits": reward}

    def checkin(self, account):
        next_at, next_note = _next_checkin_window()
        st = self._campaigns(account, force=True)
        if not st.get("ok"):
            if st.get("unavailable"):
                legacy = self._legacy_checkin(account)
                legacy["next_available_at"] = next_at
                legacy["next_available_note"] = next_note
                return legacy
            return {"ok": False, "error": st.get("error") or "活动查询失败",
                    "next_available_at": next_at, "next_available_note": next_note}

        items = st.get("campaigns") or []
        claimed, already, blocked, pending, locked, errors, codes = [], [], [], [], [], [], []
        earned = 0
        blocked_until = account.setdefault("data", {}).setdefault("campaign_blocked_until", {})
        now = time.time()
        for c in items:
            cid = c.get("campaign_id")
            if not cid:
                continue
            label = c.get("title") or c.get("campaign_key") or cid
            status = c.get("claim_status")
            amount = util.safe_int((c.get("benefit") or {}).get("amount"), 0)
            if status == "CLAIMED":
                if amount > 0:
                    already.append(label)
                code = (account.get("data") or {}).get("campaign_codes", {}).get(cid)
                if code:
                    codes.append({"campaign": label, "code": code})
                continue
            reason = str(c.get("unavailable_reason") or "").upper()
            if status != "CLAIMABLE":
                if reason == "REDEMPTION_CODE_OUT_OF_STOCK":
                    pending.append(label)
                elif reason == "ACHIEVEMENT_NOT_COMPLETED" or c.get("achievement_completed") is False:
                    locked.append(label)
                continue
            if c.get("action_type") not in ("", "CLAIM_BENEFIT"):
                continue
            try:
                if float(blocked_until.get(cid) or 0) > now:
                    blocked.append({"campaign": label,
                                    "failure_code": "SAME_PERSON_ALREADY_CLAIMED"})
                    continue
            except Exception:
                pass
            result = self._claim_campaign(account, cid)
            time.sleep(CLAIM_GAP)
            if result.get("ok"):
                amount = result.get("amount") or amount
                if result.get("replayed"):
                    already.append(label)
                else:
                    claimed.append(label)
                    earned += amount
                if result.get("redemption_code"):
                    codes.append({"campaign": label,
                                  "code": result["redemption_code"]})
            elif result.get("blocked"):
                blocked_until[cid] = now + 6 * 3600
                blocked.append({"campaign": label,
                                "failure_code": result.get("failure_code")})
            elif result.get("failure_code") == "REDEMPTION_CODE_OUT_OF_STOCK":
                pending.append(label)
            elif result.get("failure_code") == "ACHIEVEMENT_NOT_COMPLETED":
                locked.append(label)
            else:
                errors.append("%s: %s" % (label, result.get("error") or result.get("message")))

        if claimed:
            msg = "活动领取成功 +%d Credits（%s）" % (earned, "、".join(claimed))
        elif already:
            msg = "今日活动奖励已领取（%s）" % "、".join(already)
        elif blocked:
            msg = "同人已领取：同一设备下的其他账号本轮已领过"
        elif pending:
            msg = "名额已发完，次日 10:00 后可再领：%s" % "、".join(pending)
        elif locked:
            msg = "需先在官方桌面端完成新人任务：%s" % "、".join(locked)
        elif errors:
            msg = "活动领取失败：%s" % "; ".join(errors)[:300]
        else:
            legacy = self._legacy_checkin(account)
            if legacy.get("unavailable"):
                msg = "当前账号暂无可领取的官方活动"
                if not st.get("show_campaign"):
                    msg += "（活动列表未展示，可能缺少真实机器身份）"
            elif legacy.get("ok"):
                msg = legacy.get("message") or "签到完成"
            else:
                return {"ok": False, "error": legacy.get("error") or "签到失败",
                        "next_available_at": next_at,
                        "next_available_note": next_note}

        if claimed or already:
            _stamp(account)
        _stamp_message(account, msg)
        self.store.save_account(account)
        self._campaign_cache.pop(account.get("id"), None)
        return {"ok": not errors, "claimed": claimed, "already": already,
                "blocked": blocked, "pending": pending, "locked": locked,
                "earned": earned, "codes": codes, "errors": errors,
                "message": msg, "next_available_at": next_at,
                "next_available_note": next_note}

    # -- login ------------------------------------------------------------
    def _userinfo(self, token, realm):
        url = _realm(realm)["openapi"] + PATH_USERINFO
        headers = {"Accept": "application/json",
                   "User-Agent": _realm(realm)["ua"],
                   "Authorization": "Bearer " + token}
        status, obj, raw = util.http_json(url, headers=headers, timeout=15)
        if status >= 400 or not isinstance(obj, dict):
            return {}
        return obj

    def import_token(self, realm, token, refresh_token="", uid="", nickname="",
                     source="oauth", personal_token="", expires_at=0, userinfo=None):
        realm = realm if realm in REALMS else "cn"
        cfg = _realm(realm)
        ui = userinfo if isinstance(userinfo, dict) else {}
        uid = str(ui.get("id") or uid or "")
        nickname = str(ui.get("name") or nickname or ("u" + uid[-8:]))
        data = {
            "uid": uid or ("q-" + uuid.uuid4().hex[:24]),
            "nickname": nickname,
            "email": str(ui.get("email") or ""),
            "realm": realm,
            "access_token": token,
            "refresh_token": refresh_token,
            "personal_token": personal_token,
            "expires_at": int(expires_at or (time.time() + 30 * 86400)),
            "source": source,
            "platform": "CLI",
            "user_type": str(ui.get("user_type") or DEFAULT_USER_TYPE),
            "organization_id": str(ui.get("organization_id") or ui.get("organizationId") or ""),
            "organization_name": str(ui.get("organization_name") or ui.get("organizationName") or ""),
        }
        return self.app.accounts.add("qoder", data,
                                     name=nickname, realm=realm)

    def start_oauth(self, realm="cn"):
        realm = realm if realm in REALMS else "cn"
        cfg = _realm(realm)
        verifier = base64.urlsafe_b64encode(os.urandom(48)).decode("ascii").rstrip("=")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        nonce = str(uuid.uuid4()) if cfg["nonce_dashed"] else uuid.uuid4().hex
        q = {"challenge": challenge, "challenge_method": "S256", "nonce": nonce}
        if cfg["send_redirect_uri"]:
            q["redirect_uri"] = cfg["redirect_uri"]
        q["client_id"] = cfg["client_id"]
        q["machine_id"] = self._local_machine_id(realm)
        auth_url = cfg["website"] + "/device/selectAccounts?" + urllib.parse.urlencode(q)
        state = "qd-" + uuid.uuid4().hex
        self._logins[state] = {"created": time.time(), "verifier": verifier,
                               "nonce": nonce, "realm": realm}
        return {"state": state, "authUrl": auth_url, "realm": realm}

    def _local_machine_id(self, realm):
        cfg = _realm(realm)
        path = os.path.join(os.path.expanduser("~"), cfg["home_dir"],
                            ".auth", "machine_id")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                mid = fh.read().strip()
            if mid:
                return mid
        except Exception:
            pass
        return str(uuid.uuid4())

    def poll_oauth(self, state):
        info = self._logins.get(state)
        if not info:
            return {"status": "unknown", "message": "登录会话不存在，请重新发起"}
        if time.time() - info["created"] > 600:
            self._logins.pop(state, None)
            return {"status": "expired", "message": "登录已超时，请重新发起"}
        realm = info["realm"]
        url = _realm(realm)["openapi"] + PATH_DEVICE_POLL + "?" + urllib.parse.urlencode({
            "nonce": info["nonce"], "verifier": info["verifier"],
            "challenge_method": "S256"})
        try:
            status, obj, raw = util.http_json(
                url, headers={"Accept": "application/json", "User-Agent": "QoderWork"},
                timeout=20)
        except Exception as exc:
            return {"status": "pending", "message": "轮询中：%s" % exc}
        if status in (202, 404):
            return {"status": "pending", "message": "等待浏览器完成 Qoder 授权"}
        if status >= 400:
            return {"status": "error", "message": "授权轮询失败 HTTP %s" % status}
        if not isinstance(obj, dict):
            return {"status": "pending", "message": "等待授权结果"}
        token = str(obj.get("token") or obj.get("device_token") or "")
        if not token:
            return {"status": "pending", "message": "等待授权结果"}
        exp = 0
        if obj.get("expires_in"):
            try:
                exp = int(time.time() + int(obj["expires_in"]) / 1000)
            except Exception:
                exp = 0
        elif obj.get("expires_at"):
            exp = util.normalize_epoch(obj["expires_at"])
        ui = self._userinfo(token, realm)
        account = self.import_token(realm, token,
                                    refresh_token=str(obj.get("refresh_token") or ""),
                                    uid=str(obj.get("user_id") or ""),
                                    source="oauth", expires_at=exp,
                                    userinfo=ui)
        self._logins.pop(state, None)
        return {"status": "ok", "account": self.app.accounts.public(account)}

    def cancel_oauth(self, state):
        return bool(self._logins.pop(state, None))

    def import_pat(self, pat, realm="cn"):
        pat = str(pat or "").strip()
        if not pat.startswith("pt-"):
            raise ValueError("PAT 必须以 pt- 开头")
        realm = realm if realm in REALMS else "cn"
        status, obj, raw = util.http_json(
            _realm(realm)["openapi"] + PATH_JOB_EXCHANGE, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": _realm(realm)["ua"]},
            body={"personal_token": pat}, timeout=30)
        if status >= 400 or not isinstance(obj, dict):
            raise UpstreamError("PAT 交换失败 HTTP %s: %s" % (status, raw[:200]),
                                status=400)
        token = str(obj.get("token") or "")
        if not token:
            raise UpstreamError("PAT 交换没有返回 token", status=400)
        exp = int(time.time()) + 24 * 3600
        if obj.get("expires_in"):
            try:
                exp = int(time.time() + int(obj["expires_in"]) / 1000)
            except Exception:
                pass
        ui = self._userinfo(token, realm)
        return self.import_token(realm, token,
                                 refresh_token=str(obj.get("refresh_token") or ""),
                                 source="pat", personal_token=pat,
                                 expires_at=exp, userinfo=ui)

    # -- local credentials -------------------------------------------------
    def _roaming_app_dir(self, cfg):
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, cfg["app_dir"])

    def _read_os_crypt_key(self, app_dir):
        with open(os.path.join(app_dir, "Local State"), "r", encoding="utf-8") as fh:
            state = json.load(fh)
        encrypted = (state.get("os_crypt") or {}).get("encrypted_key")
        if not encrypted:
            raise RuntimeError("Local State 缺少 os_crypt.encrypted_key")
        blob = base64.b64decode(encrypted)
        if blob[:5] != b"DPAPI":
            raise RuntimeError("encrypted_key 头不是 DPAPI")
        return sign.dpapi_unprotect(blob[5:])

    def _load_app_auth(self, realm):
        cfg = _realm(realm)
        app_dir = self._roaming_app_dir(cfg)
        key = self._read_os_crypt_key(app_dir)
        with open(os.path.join(app_dir, "auth.v1.dat"), "rb") as fh:
            blob = fh.read()
        plain = sign.chromium_decrypt_v10(blob, key)
        data = json.loads(plain.decode("utf-8"))
        if not isinstance(data, dict) or not data.get("token"):
            raise RuntimeError("auth.v1.dat 结构无法识别")
        return data

    def _load_cli_user(self, path, machine_key):
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        if raw.startswith("{"):
            return json.loads(raw)
        key = str(machine_key or "")[:16].encode("utf-8")
        if len(key) != 16:
            raise RuntimeError("machine_id 不足 16 字符")
        plain = sign.aes_cbc_decrypt(base64.b64decode(raw), key, key)
        return json.loads(plain.decode("utf-8"))

    def scan_local(self):
        found = []
        for realm in ("intl", "cn"):
            cfg = _realm(realm)
            app_path = os.path.join(self._roaming_app_dir(cfg), "auth.v1.dat")
            item = {"kind": "app", "path": app_path, "file": "auth.v1.dat",
                    "realm": realm, "realm_label": cfg["name"],
                    "readable": False, "valid": False, "uid": "", "nickname": "",
                    "expires_at": 0, "error": ""}
            try:
                data = self._load_app_auth(realm)
                user = data.get("user") or {}
                exp = util.normalize_epoch(data.get("expiresAt"))
                token = str(data.get("token") or "")
                item.update({"readable": True,
                             "valid": token.startswith(("dt-", "jt-")) or bool(data.get("refreshToken")),
                             "uid": str(user.get("id") or ""),
                             "nickname": str(user.get("name") or ""),
                             "expires_at": exp})
            except FileNotFoundError:
                item["error"] = "未找到（未登录或未安装该版本客户端）"
            except Exception as exc:
                item["error"] = str(exc)[:200]
            found.append(item)

            auth_dir = os.path.join(os.path.expanduser("~"), cfg["home_dir"], ".auth")
            if os.path.isdir(auth_dir):
                try:
                    with open(os.path.join(auth_dir, "machine_id"), "r", encoding="utf-8") as fh:
                        machine_key = fh.read().strip()
                except Exception:
                    machine_key = ""
                try:
                    names = [n for n in os.listdir(auth_dir)
                             if n == "user" or n.startswith("user.")]
                except Exception:
                    names = []
                for name in names:
                    path = os.path.join(auth_dir, name)
                    cli = {"kind": "cli", "path": path, "file": name,
                           "realm": realm, "realm_label": cfg["name"],
                           "readable": False, "valid": False, "uid": "",
                           "nickname": "", "expires_at": 0, "error": ""}
                    try:
                        data = self._load_cli_user(path, machine_key)
                        token = str(data.get("access_token") or "")
                        cli.update({"readable": True,
                                    "valid": token.startswith(("dt-", "jt-")),
                                    "uid": str(data.get("uid") or ""),
                                    "nickname": str(data.get("name") or ""),
                                    "expires_at": util.normalize_epoch(data.get("expire_time"))})
                    except Exception as exc:
                        cli["error"] = str(exc)[:200]
                    found.append(cli)
        return found

    def import_local(self, path=None, realm=None):
        scanned = self.scan_local()
        targets = [x for x in scanned if x.get("valid")]
        if path:
            match = [x for x in scanned
                     if os.path.abspath(x["path"]) == os.path.abspath(str(path))]
            if not match:
                raise UpstreamError("未在扫描结果中找到该凭证文件", status=400)
            targets = match
        if not targets:
            raise UpstreamError("没有可导入的本机凭证", status=400)
        imported = []
        errors = []
        for item in targets:
            target_realm = realm if realm in REALMS else item["realm"]
            try:
                if item["file"] == "auth.v1.dat":
                    data = self._load_app_auth(target_realm)
                    token = str(data.get("token") or "")
                    refresh = str(data.get("refreshToken") or "")
                    user = data.get("user") or {}
                    uid = str(user.get("id") or "")
                    nickname = str(user.get("name") or "")
                    exp = util.normalize_epoch(data.get("expiresAt"))
                    if not exp:
                        exp = int(time.time() + 30 * 86400)
                    personal = ""
                    source = "local-app"
                else:
                    auth_dir = os.path.dirname(item["path"])
                    try:
                        with open(os.path.join(auth_dir, "machine_id"), "r", encoding="utf-8") as fh:
                            machine_key = fh.read().strip()
                    except Exception:
                        machine_key = ""
                    data = self._load_cli_user(item["path"], machine_key)
                    token = str(data.get("access_token") or "")
                    refresh = str(data.get("refresh_token") or "")
                    uid = str(data.get("uid") or "")
                    nickname = str(data.get("name") or "")
                    exp = util.normalize_epoch(data.get("expire_time"))
                    personal = str(data.get("personal_token") or "")
                    source = "local-cli"
                if not token:
                    raise RuntimeError("凭证中没有 access token")
                ui = self._userinfo(token, target_realm)
                acc = self.import_token(target_realm, token, refresh_token=refresh,
                                        uid=uid, nickname=nickname,
                                        source=source, personal_token=personal,
                                        expires_at=exp, userinfo=ui)
                imported.append(acc)
            except Exception as exc:
                errors.append("%s: %s" % (item.get("file"), exc))
        if not imported and errors:
            raise UpstreamError("导入失败：%s" % "; ".join(errors)[:400], status=400)
        return {"imported": [self.app.accounts.public(a) for a in imported],
                "errors": errors}

    # -- validation -------------------------------------------------------
    def validate_account(self, account):
        try:
            result = self.fetch_account_usage(account)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        credits = (result or {}).get("credits") or {}
        return {"ok": True,
                "message": "余额 %s / %s" % (credits.get("remain"), credits.get("size"))}

    def account_public_extra(self, account):
        data = account.get("data") or {}
        detail_url = None
        st = self._campaign_cache.get(account.get("id"))
        if st and st[1].get("campaign_url"):
            detail_url = st[1]["campaign_url"]
        return {
            "campaign_codes": data.get("campaign_codes") or {},
            "campaign_url": detail_url,
            "credits_updated_at": data.get("credits_updated_at"),
            "credits_updated_iso": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(data.get("credits_updated_at") or 0))
            if data.get("credits_updated_at") else "",
        }
