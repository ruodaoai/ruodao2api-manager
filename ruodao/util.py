"""Small stdlib-only helpers shared by the gateway and channels."""

import datetime
import json
import os
import re
import secrets
import string
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_UA = "ruodao2api-manager/0.1"


class HttpStatusError(Exception):
    """HTTP response with a non-2xx status."""

    def __init__(self, status, body, headers=None, url=""):
        self.status = int(status or 0)
        self.body = body if isinstance(body, bytes) else str(body or "").encode("utf-8", "replace")
        self.headers = headers or {}
        self.url = url
        super(HttpStatusError, self).__init__("HTTP %s %s" % (self.status, self.body[:200]))


def now():
    return time.time()


def iso_now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def json_dumps(obj, ensure_ascii=False):
    return json.dumps(obj, ensure_ascii=ensure_ascii, separators=(",", ":"))


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = "%s.tmp.%s" % (path, secrets.token_hex(4))
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def new_id(prefix=""):
    return (prefix or "") + secrets.token_hex(12)


def make_api_key():
    return "sk-ruodao-" + secrets.token_urlsafe(24)


def mask_secret(value, keep_prefix=6, keep_suffix=4):
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= keep_prefix + keep_suffix:
        return "*" * len(value)
    return value[:keep_prefix] + "…" + value[-keep_suffix:]


def normalize_epoch(value):
    """Best-effort epoch seconds from number / ISO string / millisecond value."""
    if value is None or value == "":
        return 0
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            if v > 1e12:
                v /= 1000.0
            return int(v)
    except Exception:
        return 0
    text = str(value).strip()
    try:
        if text.isdigit():
            v = float(text)
            if v > 1e12:
                v /= 1000.0
            return int(v)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(text[:19], fmt)
            return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
        except Exception:
            continue
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            dt = datetime.datetime.strptime(text, fmt)
            return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
        except Exception:
            continue
    try:
        return int(datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def human_delta(seconds):
    try:
        seconds = int(seconds)
    except Exception:
        return ""
    if seconds <= 0:
        return "0 秒"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return "%d 天 %d 小时" % (days, hours)
    if hours:
        return "%d 小时 %d 分" % (hours, minutes)
    return "%d 分" % max(1, minutes)


def strip_data_prefix(line):
    line = line.strip()
    if not line:
        return ""
    if line.startswith("data:"):
        return line[5:].strip()
    return ""


def normalize_usage(usage):
    """Return OpenAI-style usage dict or None."""
    if not isinstance(usage, dict) or not usage:
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    if completion is None:
        completion = usage.get("output_tokens")
    try:
        prompt = int(prompt or 0)
        completion = int(completion or 0)
    except Exception:
        return None
    total = usage.get("total_tokens")
    try:
        total = int(total if total is not None else prompt + completion)
    except Exception:
        total = prompt + completion
    out = {"prompt_tokens": prompt, "completion_tokens": completion,
           "total_tokens": total}
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        out["prompt_tokens_details"] = details
    return out


def open_stream(url, method="POST", headers=None, body=None, timeout=600):
    """Open a streaming HTTP response. Raises HttpStatusError for non-2xx."""
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(2 * 1024 * 1024)
        except Exception:
            raw = b""
        raise HttpStatusError(exc.code, raw, dict(exc.headers or {}), url)


def http_request(url, method="GET", headers=None, body=None, timeout=60):
    """Small non-streaming request helper; returns (status, headers, bytes)."""
    headers = dict(headers or {})
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers or {}), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(2 * 1024 * 1024)
        except Exception:
            raw = b""
        return exc.code, dict(exc.headers or {}), raw


def http_json(url, method="GET", headers=None, body=None, timeout=60):
    status, hdrs, raw = http_request(url, method=method, headers=headers,
                                     body=body, timeout=timeout)
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        obj = None
    return status, obj, raw.decode("utf-8", "replace")


def text_from_content(content):
    """Flatten OpenAI message content into plain text (images are ignored)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if p.get("type") in ("text", "input_text", "output_text"):
                    parts.append(str(p.get("text") or ""))
                elif p.get("type") == "image_url":
                    parts.append("[image]")
        return "".join(parts)
    return str(content)


def content_parts(content):
    """Normalize content into a list of OpenAI-style parts."""
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, str):
                out.append({"type": "text", "text": p})
            elif isinstance(p, dict):
                if p.get("type") in ("text", "input_text", "output_text"):
                    out.append({"type": "text", "text": str(p.get("text") or "")})
                elif p.get("type") == "image_url":
                    out.append(p)
                elif "image_url" in p:
                    out.append({"type": "image_url", "image_url": p.get("image_url")})
        return out
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def aggregate_chunks(chunks, model="", response_id="", created=None):
    """Fold OpenAI streaming chunks into one chat.completion object."""
    content = []
    reasoning = []
    tool_calls = {}
    finish = None
    usage = None
    resp_id = response_id
    resp_model = model
    created = created or int(time.time())
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if chunk.get("id"):
            resp_id = resp_id or chunk["id"]
        if chunk.get("model"):
            resp_model = chunk.get("model") or resp_model
        if chunk.get("created"):
            created = chunk["created"]
        if chunk.get("usage"):
            usage = normalize_usage(chunk["usage"]) or usage
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            elif isinstance(delta.get("content"), list):
                for p in delta["content"]:
                    if isinstance(p, dict) and p.get("type") in ("text", "output_text"):
                        content.append(str(p.get("text") or ""))
            if isinstance(delta.get("reasoning_content"), str):
                reasoning.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index")
                if idx is None:
                    idx = len(tool_calls)
                try:
                    idx = int(idx)
                except Exception:
                    idx = len(tool_calls)
                slot = tool_calls.setdefault(idx, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tc.get("id"):
                    slot["id"] = str(tc["id"])
                if tc.get("type"):
                    slot["type"] = str(tc["type"])
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += str(fn["name"])
                if fn.get("arguments") is not None:
                    slot["function"]["arguments"] += str(fn["arguments"])
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        calls = []
        for idx in sorted(tool_calls):
            call = tool_calls[idx]
            if not call["id"]:
                call["id"] = "call_" + secrets.token_hex(12)
            calls.append(call)
        message["tool_calls"] = calls
        message["content"] = message["content"] or None
        finish = finish or "tool_calls"
    return {
        "id": resp_id or ("chatcmpl-" + secrets.token_hex(12)),
        "object": "chat.completion",
        "created": created,
        "model": resp_model or model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish or "stop",
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0},
    }


def chunk_delta(chunk):
    """Extract the first choice's delta/finish_reason from an OpenAI chunk."""
    for choice in (chunk or {}).get("choices") or []:
        if isinstance(choice, dict):
            return choice.get("delta") or {}, choice.get("finish_reason")
    return {}, None


def estimate_tokens(value):
    try:
        return max(1, len(json.dumps(value, ensure_ascii=False)) // 4)
    except Exception:
        return 1


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def query_string(handler):
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    except Exception:
        return {}


def path_only(handler):
    return urllib.parse.urlparse(handler.path).path


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(name):
    return _SAFE_NAME.sub("_", str(name or ""))[:80]


def redact_url(url):
    try:
        p = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(p.query)
        for key in list(query):
            if any(s in key.lower() for s in ("key", "token", "secret", "auth")):
                query[key] = ["***"]
        return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(query, doseq=True)))
    except Exception:
        return str(url)
