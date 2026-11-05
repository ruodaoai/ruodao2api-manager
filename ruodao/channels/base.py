"""Base channel adapter and shared upstream helpers."""

import json
import time

from .. import util
from ..errors import UpstreamError


class BaseChannel(object):
    name = ""
    label = ""
    color = ""
    supports_checkin = False
    supports_oauth = False

    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.config = app.config

    # -- models -----------------------------------------------------------
    def list_models(self, account=None):
        """Return a list of model dicts in gateway shape."""
        return []

    def resolve_model(self, requested):
        """Map a client-facing model name (without channel prefix) to upstream id."""
        for m in self.list_models():
            if requested == m.get("id") or requested == m.get("raw_id"):
                return m.get("raw_id") or requested
        return requested

    def supports_model(self, account, model_info):
        """Whether an account can serve a resolved model (realm/capability filter)."""
        return True

    # -- inference --------------------------------------------------------
    def stream_chat(self, account, body, upstream_model):
        """Yield OpenAI-format chunk dicts. Must raise before/inside iteration."""
        raise UpstreamError("渠道 %s 不支持推理" % self.label, status=501)

    # -- account lifecycle -------------------------------------------------
    def refresh_account(self, account):
        """Refresh short-lived credentials. Return True when changed/saved."""
        return False

    def fetch_account_usage(self, account):
        """Return {'credits': {...}, 'plan': str} or None."""
        return None

    def checkin(self, account):
        return {"ok": True, "skipped": True, "message": "该渠道无需签到"}

    def validate_account(self, account):
        return {"ok": True, "message": "ok"}

    # -- helpers ----------------------------------------------------------
    def save(self, account):
        if account.get("data"):
            account["data"].setdefault("updated_at", time.time())
        self.store.save_account(account)

    def account_public_extra(self, account):
        return {}

    def _timeout(self):
        try:
            return int(self.config.get("request_timeout") or 600)
        except Exception:
            return 600


def extract_upstream_error(obj, status=502):
    """Build UpstreamError from an arbitrary upstream JSON object."""
    if not isinstance(obj, dict):
        return UpstreamError("上游返回异常响应", status=status)
    err = obj.get("error")
    code = ""
    message = ""
    if isinstance(err, dict):
        code = str(err.get("code") or err.get("type") or "")
        message = str(err.get("message") or "")
    elif isinstance(err, str):
        message = err
    if not message:
        message = str(obj.get("message") or obj.get("msg") or "")
    if not code:
        code = str(obj.get("code") or "")
    if not message:
        message = util.json_dumps(obj)[:300]
    retryable = status in (429, 500, 502, 503, 504) or code in ("429", "rate_limit_exceeded")
    return UpstreamError(message, status=status, code=code, retryable=retryable)


def unwrap_chunk(obj):
    """Unwrap common `{code,msg,data}` envelopes and surface errors."""
    if not isinstance(obj, dict):
        return None
    if obj.get("error"):
        raise extract_upstream_error(obj, status=502)
    code = obj.get("code")
    if code is not None and code not in (0, 200, "0", "200", "success"):
        # Some OpenAI-compatible servers include a `code` only in errors.
        if "choices" not in obj:
            status = 401 if str(code) in ("401", "403", "10001") else 502
            raise extract_upstream_error(obj, status=status)
    if "choices" in obj:
        return obj
    data = obj.get("data")
    if isinstance(data, dict):
        if data.get("error"):
            raise extract_upstream_error(data, status=502)
        if "choices" in data:
            if data.get("usage") is None and obj.get("usage"):
                data["usage"] = obj.get("usage")
            return data
    if isinstance(data, str):
        try:
            inner = json.loads(data)
            if isinstance(inner, dict) and "choices" in inner:
                return inner
        except Exception:
            return None
    if obj.get("type") == "error":
        raise extract_upstream_error(obj, status=502)
    # Usage-only / role-only chunks are legal.
    if "usage" in obj and not obj.get("choices"):
        return obj
    return None


def iter_sse_json(resp):
    """Yield JSON objects from an SSE response, tolerating JSON-lines."""
    content_type = ""
    try:
        content_type = str(resp.headers.get("Content-Type") or "").lower()
    except Exception:
        pass
    if "application/json" in content_type and "event-stream" not in content_type:
        raw = resp.read()
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            raise UpstreamError("上游返回了无法解析的 JSON", status=502)
        chunk = unwrap_chunk(obj)
        if chunk:
            yield chunk
        return
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
            obj = json.loads(data)
        except Exception:
            continue
        chunk = unwrap_chunk(obj)
        if chunk:
            yield chunk


def stream_openai_passthrough(url, headers, body, timeout=600):
    """POST to an OpenAI-compatible SSE endpoint and yield chunks."""
    out_headers = dict(headers or {})
    out_headers.setdefault("Content-Type", "application/json")
    out_headers.setdefault("Accept", "text/event-stream")
    out_headers.setdefault("Accept-Encoding", "identity")
    payload = util.json_dumps(body).encode("utf-8")
    try:
        resp = util.open_stream(url, headers=out_headers, body=payload,
                                timeout=timeout)
    except util.HttpStatusError as exc:
        detail = exc.body.decode("utf-8", "replace")
        try:
            obj = json.loads(detail)
            raise extract_upstream_error(obj, status=exc.status)
        except UpstreamError:
            raise
        except Exception:
            msg = detail[:500] or ("HTTP %s" % exc.status)
            raise UpstreamError(msg, status=exc.status, retryable=exc.status >= 500)
    except Exception as exc:
        raise UpstreamError("连接上游失败: %s" % exc, status=502, retryable=True)
    try:
        for chunk in iter_sse_json(resp):
            yield chunk
    finally:
        try:
            resp.close()
        except Exception:
            pass
