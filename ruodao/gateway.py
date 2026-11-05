"""HTTP gateway: OpenAI Chat, Anthropic Messages, Responses API and admin UI."""

import hashlib
import json
import mimetypes
import os
import queue
import threading
import time
import traceback
import urllib.parse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import util
from .errors import UpstreamError
from .protocols.responses import ResponseStore, ResponsesStreamTranslator
from .protocols.translate import (
    AnthropicStreamTranslator,
    anthropic_to_openai,
    normalize_messages,
    openai_to_anthropic,
    openai_to_responses,
    responses_to_openai,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "static")
API_KEY_HEADERS = ("authorization", "x-api-key", "api-key")
MAX_ATTEMPTS_PER_CHANNEL = 3


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, runtime):
        self.runtime = runtime
        self.responses = ResponseStore()
        ThreadingHTTPServer.__init__(self, address, handler)


class GatewayService(object):
    """Start/stop the HTTP gateway without blocking the caller."""

    def __init__(self, runtime, host=None, port=None):
        self.runtime = runtime
        self.host = host or runtime.config.host
        self.port = int(port or runtime.config.port)
        self.server = None
        self.thread = None

    @property
    def url(self):
        host = self.host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return "http://%s:%d" % (host, self.port)

    def alive(self):
        return bool(self.server and self.thread and self.thread.is_alive())

    def start(self):
        if self.alive():
            return self.url
        server = GatewayHTTPServer((self.host, self.port), GatewayHandler,
                                   self.runtime)
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever,
                                       kwargs={"poll_interval": 0.5},
                                       name="ruodao-gateway", daemon=True)
        self.thread.start()
        self.runtime.log("INFO", "网关已启动：%s（OpenAI: %s/v1）"
                         % (self.url, self.url))
        return self.url

    def stop(self):
        server = self.server
        self.server = None
        if not server:
            return
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        self.thread = None
        self.runtime.log("INFO", "网关已停止")


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ruodao2api/0.1"
    protocol_version = "HTTP/1.1"
    timeout = 300
    api_key_id = ""
    api_key_name = ""
    api_key_channel = ""

    @property
    def runtime(self):
        return self.server.runtime

    # -- logging ----------------------------------------------------------
    def log_message(self, fmt, *args):
        try:
            line = fmt % args if args else str(fmt)
        except Exception:
            line = str(fmt)
        lowered = line.strip().lower()
        if lowered.startswith(('"get /api/', '"get /static/', '"get /favicon',
                               '"options ')):
            return
        try:
            self.runtime.log("INFO", "%s %s" % (self.client_address[0], line))
        except Exception:
            pass

    def log_error(self, fmt, *args):
        pass

    # -- helpers ----------------------------------------------------------
    def _loopback(self):
        addr = str(self.client_address[0] if self.client_address else "")
        return addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _read_body(self):
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        limit = int(self.runtime.config.get("max_body_mb") or 32) * 1024 * 1024
        if length > limit:
            self.close_connection = True
            raise UpstreamError("请求体过大", status=413)
        if length <= 0:
            raw = b""
        else:
            raw = self.rfile.read(length)
        self._body_cache = raw
        return raw

    def _read_json(self):
        raw = self._read_body()
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            raise UpstreamError("请求体不是合法 JSON", status=400)
        if not isinstance(obj, dict):
            raise UpstreamError("请求体必须是 JSON 对象", status=400)
        return obj

    def _json(self, status, obj, headers=None):
        payload = util.json_dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _admin_error(self, status, message):
        self._json(status, {"ok": False, "error": message})

    def _api_error(self, status, message, protocol="openai", retry_after=None):
        headers = {}
        if retry_after:
            headers["Retry-After"] = str(max(1, int(retry_after)))
        if protocol == "anthropic":
            self._json(status, {
                "type": "error",
                "error": {"type": "api_error" if status >= 500
                          else "invalid_request_error", "message": message},
            }, headers=headers)
        else:
            self._json(status, {
                "error": {"message": message, "type": "upstream_error",
                          "code": str(status)},
            }, headers=headers)

    def _authorize_api(self):
        supplied = ""
        for name in API_KEY_HEADERS:
            value = self.headers.get(name) or ""
            if value:
                supplied = value.strip()
                break
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        key, error = self.runtime.api_keys.authenticate(supplied)
        if not key:
            self._api_error(401, error or "无效的 API Key")
            return False
        self.api_key_id = key["id"]
        self.api_key_name = key["name"]
        self.api_key_channel = key.get("channel") or ""
        return True

    def _admin_guard(self, mutating=False):
        if not self._loopback():
            self._admin_error(403, "管理接口仅允许本机访问")
            return False
        if mutating and self.headers.get("X-Ruodao-Admin") != "1":
            self._admin_error(403, "缺少管理请求头")
            return False
        return True

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _write(self, raw):
        self.wfile.write(raw)
        self.wfile.flush()

    def _sse_data(self, data):
        if not isinstance(data, str):
            data = util.json_dumps(data)
        self._write(("data: %s\n\n" % data).encode("utf-8"))

    def _sse_event(self, name, data):
        if not isinstance(data, str):
            data = util.json_dumps(data)
        self._write(("event: %s\ndata: %s\n\n" % (name, data)).encode("utf-8"))

    def _sse_ping(self):
        try:
            self._write(b": ping\n\n")
        except (BrokenPipeError, ConnectionResetError):
            raise

    # -- routing ----------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, X-Api-Key, Api-Key, X-Ruodao-Admin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        self._body_cache = None
        path = util.path_only(self)
        try:
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                # 必须在路由前读完请求体，否则 keep-alive 连接上残留的
                # body 会被下一次请求解析成请求行，导致莫名其妙的 501。
                self._read_body()
            if path.startswith("/api/"):
                if not self._admin_guard(mutating=method != "GET"):
                    return
                self._handle_admin(method, path)
                return
            if path in ("/", "/index.html", "/favicon.ico") or path.startswith("/static/"):
                self._serve_static(path)
                return
            if path == "/health":
                self._json(200, {"status": "ok", "version": "0.1.0",
                                 "time": int(time.time())})
                return
            if path in ("/v1/models", "/models") and method == "GET":
                if not self._authorize_api():
                    return
                self._handle_models()
                return
            if path.startswith("/v1/models/") and method == "GET":
                if not self._authorize_api():
                    return
                self._handle_model(path[len("/v1/models/"):])
                return
            if path in ("/v1/chat/completions", "/chat/completions") and method == "POST":
                if not self._authorize_api():
                    return
                self._handle_chat("openai")
                return
            if path in ("/v1/messages", "/messages") and method == "POST":
                if not self._authorize_api():
                    return
                self._handle_chat("anthropic")
                return
            if path in ("/v1/messages/count_tokens", "/messages/count_tokens") \
                    and method == "POST":
                if not self._authorize_api():
                    return
                self._handle_count_tokens()
                return
            if path in ("/v1/responses", "/responses") and method == "POST":
                if not self._authorize_api():
                    return
                self._handle_chat("responses")
                return
            self._api_error(404, "接口不存在：%s" % path)
        except UpstreamError as exc:
            try:
                self._api_error(exc.status, exc.message,
                                retry_after=getattr(exc, "wait", None))
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self.runtime.log("ERROR", "请求处理异常：%s\n%s"
                                 % (exc, traceback.format_exc()[:3000]))
            except Exception:
                pass
            try:
                self._api_error(500, "网关内部错误：%s" % exc)
            except Exception:
                pass

    # -- models -----------------------------------------------------------
    def _model_payload(self, bound_channel=""):
        models = self.runtime.all_models()
        if bound_channel:
            data = []
            seen = set()
            for model in models:
                if model.get("channel") != bound_channel:
                    continue
                entry = dict(model)
                entry.pop("meta", None)
                raw = str(entry.get("raw_id") or entry.get("id") or "")
                if not raw or raw in seen:
                    continue
                seen.add(raw)
                entry["id"] = raw
                data.append(entry)
            return data
        data = []
        seen = set()
        raw_counts = {}
        for model in models:
            raw = str(model.get("raw_id") or "")
            raw_counts[raw] = raw_counts.get(raw, 0) + 1
        for model in models:
            entry = dict(model)
            entry.pop("meta", None)
            data.append(entry)
            seen.add(entry.get("id"))
            raw = str(entry.get("raw_id") or "")
            if raw and raw_counts.get(raw) == 1 and raw not in seen:
                alias = dict(entry)
                alias["id"] = raw
                alias["alias"] = True
                data.append(alias)
                seen.add(raw)
        return data

    def _handle_models(self):
        self._json(200, {"object": "list",
                         "data": self._model_payload(self.api_key_channel)})

    def _handle_model(self, model_id):
        model_id = urllib.parse.unquote(model_id)
        for model in self._model_payload(self.api_key_channel):
            if model.get("id") == model_id:
                self._json(200, model)
                return
        self._api_error(404, "模型不存在：%s" % model_id)

    # -- inference --------------------------------------------------------
    def _handle_count_tokens(self):
        payload = self._read_json()
        tokens = util.estimate_tokens(payload.get("messages") or payload)
        self._json(200, {"input_tokens": tokens})

    def _affinity_key(self, protocol, payload, extra=""):
        if extra:
            return str(extra)
        if protocol == "openai":
            user = payload.get("user")
            if user:
                return "u:" + str(user)
        messages = payload.get("messages") or []
        seed = ""
        for msg in messages[:3]:
            if isinstance(msg, dict):
                seed += str(msg.get("role") or "") + util.text_from_content(msg.get("content"))
        if not seed:
            seed = util.json_dumps(payload)[:2000]
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def _candidates_for(self, model):
        bound = self.api_key_channel
        candidates = self.runtime.find_models(model, channel=bound)
        if candidates:
            return candidates
        if bound:
            raise UpstreamError(
                "模型 %s 不属于该密钥绑定的渠道「%s」，请改用该渠道的模型 ID"
                % (model, self._channel_label(bound)), status=404)
        raise UpstreamError("未找到模型：%s，请检查 /v1/models" % model, status=404)

    def _channel_label(self, name):
        for item in self.runtime.channel_list():
            if item.get("name") == name:
                return item.get("label") or name
        return name

    def _run(self, candidates, payload, affinity_key):
        last_error = None
        for model_info in candidates:
            channel_name = model_info.get("channel") or ""
            channel = self.runtime.channels.get(channel_name)
            if not channel:
                continue
            raw_model = str(model_info.get("raw_id") or payload.get("model") or "")
            exclude = set()
            for attempt in range(MAX_ATTEMPTS_PER_CHANNEL):
                try:
                    account = self.runtime.accounts.pick(
                        channel_name, model=raw_model, exclude=exclude,
                        affinity_key=affinity_key, model_info=model_info)
                except UpstreamError as exc:
                    if not str(getattr(last_error, "code", "") or "").startswith("guard_"):
                        last_error = exc
                    break
                lease = None
                try:
                    self.runtime.accounts.ensure_ready(account)
                    lease = self.runtime.guard.acquire(account)
                    iterator = channel.stream_chat(account, payload, raw_model)
                    first = next(iterator, None)
                    if first is None:
                        raise UpstreamError("上游没有返回内容", status=502,
                                            retryable=True)
                    return {
                        "account": account,
                        "channel": channel,
                        "channel_name": channel_name,
                        "model_info": model_info,
                        "iterator": iterator,
                        "first": first,
                        "lease": lease,
                    }
                except UpstreamError as exc:
                    if lease:
                        lease.release()
                    last_error = exc
                    exclude.add(account["id"])
                    guard_code = str(getattr(exc, "code", "") or "")
                    if guard_code.startswith("guard_"):
                        if guard_code == "guard_global":
                            break
                        continue
                    self.runtime.accounts.mark_error(
                        account, exc,
                        cooldown=self.runtime.guard.cooldown_for(exc))
                    if exc.status in (401, 403):
                        continue
                    if exc.retryable and attempt + 1 < MAX_ATTEMPTS_PER_CHANNEL:
                        time.sleep(0.2)
                        continue
                    break
                except Exception as exc:
                    if lease:
                        lease.release()
                    last_error = UpstreamError(
                        "上游调用异常：%s" % exc, status=502, retryable=True)
                    self.runtime.accounts.mark_error(
                        account, last_error,
                        cooldown=self.runtime.guard.cooldown_for(last_error))
                    exclude.add(account["id"])
                    continue
        raise last_error or UpstreamError("没有可用账号", status=503)

    @staticmethod
    def _release_lease(result):
        lease = (result or {}).get("lease")
        if lease is not None:
            try:
                lease.release()
            except Exception:
                pass

    def _record(self, result, model, usage, status, latency_ms, error=""):
        try:
            account = result.get("account") if result else None
            usage = util.normalize_usage(usage) or {}
            self.runtime.store.add_usage({
                "channel": (result or {}).get("channel_name") or "",
                "account_id": (account or {}).get("id") or "",
                "account_name": (account or {}).get("name") or "",
                "api_key_id": self.api_key_id,
                "api_key_name": self.api_key_name,
                "model": model,
                "prompt_tokens": usage.get("prompt_tokens") or 0,
                "completion_tokens": usage.get("completion_tokens") or 0,
                "total_tokens": usage.get("total_tokens") or 0,
                "status": status,
                "latency_ms": latency_ms,
                "error": error,
            })
            if result and account:
                if status < 400:
                    self.runtime.accounts.mark_ok(account, model=model, usage=usage)
                else:
                    self.runtime.accounts.mark_error(account, UpstreamError(
                        error or "请求失败", status=status))
        except Exception:
            pass

    def _estimate_usage(self, payload, output_text):
        prompt = util.estimate_tokens(payload.get("messages") or payload)
        completion = max(1, len(output_text or "") // 4)
        return {"prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion}

    def _handle_chat(self, protocol):
        started = time.time()
        request = self._read_json()
        try:
            if protocol == "anthropic":
                payload = anthropic_to_openai(request)
                stream = bool(request.get("stream"))
                model = str(request.get("model") or payload.get("model") or "")
                affinity = self._affinity_key(
                    "anthropic", payload,
                    (request.get("metadata") or {}).get("user_id")
                    if isinstance(request.get("metadata"), dict) else "")
                include_thinking = isinstance(request.get("thinking"), dict) and \
                    request["thinking"].get("type") == "enabled"
            elif protocol == "responses":
                payload = responses_to_openai(request)
                stream = bool(request.get("stream"))
                model = str(request.get("model") or payload.get("model") or "")
                previous_id = str(request.get("previous_response_id") or "")
                if previous_id:
                    stored = self.server.responses.get(previous_id)
                    if stored:
                        payload["messages"] = stored + payload["messages"]
                payload["_previous_response_id"] = previous_id
                affinity = self._affinity_key("responses", payload, previous_id)
                include_thinking = False
            else:
                payload = dict(request)
                stream = bool(payload.get("stream"))
                model = str(payload.get("model") or "")
                affinity = self._affinity_key("openai", payload)
                include_thinking = False
        except UpstreamError as exc:
            self._api_error(exc.status, exc.message, protocol)
            return

        payload["messages"] = normalize_messages(payload.get("messages"))
        if not model:
            self._api_error(400, "缺少 model 参数", protocol)
            return
        try:
            candidates = self._candidates_for(model)
            result = self._run(candidates, payload, affinity)
        except UpstreamError as exc:
            self._api_error(exc.status, exc.message, protocol,
                            retry_after=getattr(exc, "wait", None))
            self._record(None, model, None, exc.status, int((time.time() - started) * 1000),
                         exc.message)
            return

        channel_name = result["channel_name"]
        extra_headers = {"X-Ruodao-Channel": channel_name,
                         "X-Ruodao-Model": str(result["model_info"].get("raw_id") or "")}
        try:
            if stream:
                self._stream_result(protocol, result, payload, model, started,
                                    include_thinking, extra_headers)
            else:
                self._nonstream_result(protocol, result, payload, model, started,
                                       extra_headers)
        finally:
            self._release_lease(result)

    def _collect(self, result):
        chunks = [result["first"]]
        for chunk in result["iterator"]:
            chunks.append(chunk)
        return chunks

    def _nonstream_result(self, protocol, result, payload, model, started,
                          headers):
        try:
            chunks = self._collect(result)
            aggregate = util.aggregate_chunks(chunks, model=model)
            usage = util.normalize_usage(aggregate.get("usage")) or {}
            latency = int((time.time() - started) * 1000)
            self._record(result, model, usage, 200, latency)
            if protocol == "anthropic":
                body = openai_to_anthropic(aggregate, model=model)
            elif protocol == "responses":
                previous_id = self._previous_id(payload)
                body = openai_to_responses(aggregate, model=model,
                                           previous_response_id=previous_id,
                                           store=True)
                self._store_response(body.get("id"), payload, aggregate)
            else:
                body = aggregate
            self._json(200, body, headers=headers)
        except UpstreamError as exc:
            latency = int((time.time() - started) * 1000)
            self._record(result, model, None, exc.status, latency, exc.message)
            self._api_error(exc.status, exc.message, protocol)
        except Exception as exc:
            latency = int((time.time() - started) * 1000)
            self._record(result, model, None, 500, latency, str(exc))
            self._api_error(500, "上游响应处理失败：%s" % exc, protocol)

    @staticmethod
    def _previous_id(payload):
        return str(payload.get("_previous_response_id") or "")

    def _store_response(self, response_id, payload, aggregate):
        if not response_id:
            return
        try:
            message = ((aggregate.get("choices") or [{}])[0]).get("message") or {}
            messages = list(payload.get("messages") or [])
            if message:
                messages.append(message)
            self.server.responses.put(response_id, messages)
        except Exception:
            pass

    def _stream_result(self, protocol, result, payload, model, started,
                       include_thinking, headers):
        self._sse_start()
        # Send routing headers after headers are on the wire is too late for
        # clients; they are informational only, so skip them on streams.
        del headers
        translator = None
        if protocol == "anthropic":
            translator = AnthropicStreamTranslator(model=model,
                                                   include_thinking=include_thinking)
            for event in translator._start():
                self._sse_event(event["event"], event["data"])
        elif protocol == "responses":
            previous_id = self._previous_response_id(payload)
            translator = ResponsesStreamTranslator(
                model=model, previous_response_id=previous_id, store=True,
                messages=list(payload.get("messages") or []))
            for event in translator.start_events():
                self._sse_event(event["event"], event["data"])

        q = queue.Queue()
        closed = threading.Event()
        iterator = result["iterator"]
        first = result["first"]

        def producer():
            try:
                q.put(("chunk", first))
                for chunk in iterator:
                    if closed.is_set():
                        break
                    q.put(("chunk", chunk))
                q.put(("done", None))
            except Exception as exc:
                q.put(("error", exc))

        thread = threading.Thread(target=producer, daemon=True)
        thread.start()
        usage = {}
        output_text = []
        error = None
        try:
            while True:
                try:
                    kind, value = q.get(timeout=15)
                except queue.Empty:
                    self._sse_ping()
                    continue
                if kind == "chunk":
                    if value.get("usage"):
                        usage = util.normalize_usage(value["usage"]) or usage
                    if protocol == "openai":
                        self._sse_data(value)
                    else:
                        for event in translator.feed(value):
                            self._sse_event(event["event"], event["data"])
                    delta, _ = util.chunk_delta(value)
                    if isinstance(delta.get("content"), str):
                        output_text.append(delta["content"])
                elif kind == "done":
                    break
                else:
                    error = value
                    break
        except (BrokenPipeError, ConnectionResetError):
            closed.set()
            try:
                iterator.close()
            except Exception:
                pass
            latency = int((time.time() - started) * 1000)
            self._record(result, model, usage, 499, latency, "客户端断开")
            return
        except Exception as exc:
            error = exc

        latency = int((time.time() - started) * 1000)
        if error is not None:
            closed.set()
            message = str(getattr(error, "message", None) or error)
            status = int(getattr(error, "status", 502) or 502)
            self.runtime.accounts.mark_error(result["account"],
                                             UpstreamError(message, status=status))
            self._record(result, model, usage, status, latency, message)
            if protocol == "openai":
                self._sse_data({"error": {"message": message,
                                          "type": "upstream_error",
                                          "code": str(status)}})
                self._sse_data("[DONE]")
            elif protocol == "anthropic":
                self._sse_event("error", {"type": "error",
                                          "error": {"type": "api_error",
                                                    "message": message}})
            else:
                failed = translator._base_response("failed")
                failed["error"] = {"code": "upstream_error", "message": message}
                self._sse_event("response.failed",
                                {"type": "response.failed", "response": failed})
            return

        if protocol == "openai":
            self._sse_data("[DONE]")
        elif protocol == "anthropic":
            for event in translator.finish():
                self._sse_event(event["event"], event["data"])
        else:
            for event in translator.finish():
                self._sse_event(event["event"], event["data"])
            try:
                body = translator.final_response()
                self._store_response(body.get("id"), payload,
                                     {"choices": [{"message": translator.assistant_message()}]})
            except Exception:
                pass
        if not usage:
            usage = self._estimate_usage(payload, "".join(output_text))
        self._record(result, model, usage, 200, latency)

    def _previous_response_id(self, payload):
        return str(payload.get("_previous_response_id") or "")

    # -- static files -----------------------------------------------------
    def _serve_static(self, path):
        rel = "index.html" if path in ("/", "/index.html") else path[len("/static/"):]
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        target = os.path.abspath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(os.path.abspath(STATIC_DIR)):
            self._admin_error(403, "禁止访问")
            return
        if not os.path.isfile(target):
            self._admin_error(404, "文件不存在")
            return
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
                "application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        try:
            with open(target, "rb") as fh:
                payload = fh.read()
        except Exception as exc:
            self._admin_error(500, "读取文件失败：%s" % exc)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- admin API --------------------------------------------------------
    def _handle_admin(self, method, path):
        try:
            if path == "/api/status" and method == "GET":
                self._json(200, self.runtime.status())
                return
            if path == "/api/channels" and method == "GET":
                self._json(200, {"channels": self.runtime.channel_list()})
                return
            if path == "/api/models" and method == "GET":
                self._json(200, {"object": "list", "data": self._model_payload()})
                return
            if path == "/api/accounts" and method == "GET":
                channel = (util.query_string(self).get("channel") or [""])[0]
                accounts = self.runtime.accounts.public_list(channel or None)
                self._json(200, {"accounts": accounts})
                return
            if path == "/api/accounts" and method == "POST":
                self._admin_create_account()
                return
            if path.startswith("/api/accounts/") and method == "PATCH":
                self._admin_update_account(path)
                return
            if path.startswith("/api/accounts/") and method == "DELETE":
                account_id = path[len("/api/accounts/"):].strip("/")
                ok = self.runtime.accounts.delete(account_id)
                self._json(200 if ok else 404, {"ok": ok})
                return
            if path.startswith("/api/accounts/") and method == "POST":
                self._admin_account_action(path)
                return
            if path == "/api/keys" and method == "GET":
                self._json(200, {"keys": self.runtime.api_keys.list_public()})
                return
            if path == "/api/keys" and method == "POST":
                body = self._read_json()
                key, secret = self.runtime.api_keys.create(
                    body.get("name"), body.get("expires_at"),
                    channel=body.get("channel"))
                self._json(200, {"ok": True, "key": key, "secret": secret})
                return
            if path.startswith("/api/keys/"):
                self._admin_api_key(method, path)
                return
            if path.startswith("/api/scan/") and method == "GET":
                self._admin_scan(path[len("/api/scan/"):])
                return
            if path.startswith("/api/import/") and method == "POST":
                self._admin_import_local(path[len("/api/import/"):])
                return
            if path.startswith("/api/login/") and method == "POST" \
                    and path.endswith("/cancel"):
                self._admin_login_cancel(path)
                return
            if path.startswith("/api/login/") and method == "POST":
                self._admin_login_start(path)
                return
            if path.startswith("/api/login/") and method == "GET":
                self._admin_login_poll(path)
                return
            if path == "/api/usage" and method == "GET":
                qs = util.query_string(self)
                try:
                    since = float((qs.get("since") or ["0"])[0])
                except Exception:
                    since = 0
                if not since:
                    since = time.time() - 86400
                self._json(200, {
                    "summary": self.runtime.store.usage_summary(since),
                    "recent": self.runtime.store.usage_recent(
                        int((qs.get("limit") or ["50"])[0])),
                })
                return
            if path == "/api/events" and method == "GET":
                qs = util.query_string(self)
                self._json(200, {"events": self.runtime.store.events(
                    int((qs.get("limit") or ["100"])[0]))})
                return
            if path == "/api/settings" and method == "GET":
                self._json(200, self._settings_view())
                return
            if path == "/api/settings" and method == "PUT":
                self._admin_update_settings()
                return
            if path == "/api/guard" and method == "GET":
                self._json(200, self.runtime.guard.snapshot())
                return
            if path == "/api/guard" and method == "PUT":
                self._admin_update_guard()
                return
            if path == "/api/guard/reset" and method == "POST":
                self.runtime.guard.reset_stats()
                self._json(200, self.runtime.guard.snapshot())
                return
            if path == "/api/config/reveal" and method == "POST":
                self._json(200, {"api_key": self.runtime.api_keys.default_secret()})
                return
            if path == "/api/key/rotate" and method == "POST":
                key = self.runtime.api_keys.rotate_default()
                self.runtime.log("INFO", "API Key 已轮换")
                self._json(200, {"api_key": key})
                return
            self._admin_error(404, "管理接口不存在：%s" % path)
        except UpstreamError as exc:
            self._admin_error(exc.status, exc.message)
        except ValueError as exc:
            self._admin_error(400, str(exc))
        except Exception as exc:
            self.runtime.log("ERROR", "管理接口异常：%s\n%s"
                             % (exc, traceback.format_exc()[:3000]))
            self._admin_error(500, "管理接口异常：%s" % exc)

    def _admin_api_key(self, method, path):
        rest = path[len("/api/keys/"):].strip("/")
        parts = [urllib.parse.unquote(p) for p in rest.split("/") if p]
        if not parts:
            self._admin_error(404, "管理接口不存在：%s" % path)
            return
        key_id = parts[0]
        action = parts[1] if len(parts) > 1 else ""
        if not action and method == "PATCH":
            body = self._read_json()
            patch = {k: body[k] for k in ("name", "channel", "enabled", "expires_at")
                     if k in body}
            key = self.runtime.api_keys.update(key_id, patch)
            self._json(200, {"ok": True, "key": key})
            return
        if not action and method == "DELETE":
            self.runtime.api_keys.delete(key_id)
            self._json(200, {"ok": True})
            return
        if action == "reveal" and method == "POST":
            self._json(200, {"ok": True,
                             "key": self.runtime.api_keys.reveal(key_id)})
            return
        if action == "rotate" and method == "POST":
            key, secret = self.runtime.api_keys.rotate(key_id)
            self._json(200, {"ok": True, "key": key, "secret": secret})
            return
        self._admin_error(404, "管理接口不存在：%s" % path)

    def _settings_view(self):
        cfg = self.runtime.config.public()
        cfg.pop("api_key", None)
        cfg["api_key_masked"] = util.mask_secret(
            self.runtime.api_keys.default_secret(), 10, 4)
        return cfg

    GUARD_KEYS = ("guard_enabled", "guard_global_rpm", "guard_account_rpm",
                  "guard_daily_limit", "guard_max_concurrency",
                  "guard_global_concurrency", "guard_min_interval_ms",
                  "guard_jitter_ms", "guard_queue_timeout",
                  "guard_cooldown_429", "guard_cooldown_5xx")

    def _admin_update_settings(self):
        body = self._read_json()
        allowed = ("checkin_enabled", "checkin_times", "refresh_time",
                   "credits_refresh_minutes", "scheduler_enabled",
                   "request_timeout", "max_body_mb") + self.GUARD_KEYS
        patch = {k: body[k] for k in allowed if k in body}
        self.runtime.config.update(patch)
        if self.runtime.scheduler:
            self.runtime.scheduler.wake()
        self._json(200, {"ok": True, "settings": self._settings_view()})

    def _admin_update_guard(self):
        body = self._read_json()
        patch = {k: body[k] for k in self.GUARD_KEYS if k in body}
        self.runtime.config.update(patch)
        self.runtime.log("INFO", "防护参数已更新")
        self._json(200, {"ok": True, "guard": self.runtime.guard.snapshot()})

    def _admin_create_account(self):
        body = self._read_json()
        channel_name = str(body.get("channel") or "").strip()
        channel = self.runtime.channels.get(channel_name)
        if not channel:
            raise UpstreamError("未知渠道：%s" % channel_name, status=400)
        realm = str(body.get("realm") or "").strip()
        if channel_name == "qoder":
            token = str(body.get("access_token") or body.get("token") or "").strip()
            if not token:
                raise UpstreamError("缺少 access_token", status=400)
            if token.startswith("pt-"):
                account = channel.import_pat(token, realm=realm or "cn")
            else:
                account = channel.import_token(
                    realm or "cn", token,
                    refresh_token=str(body.get("refresh_token") or ""),
                    uid=str(body.get("uid") or ""),
                    nickname=str(body.get("nickname") or ""),
                    source=str(body.get("source") or "manual"))
        elif channel_name == "codebuddy":
            token = str(body.get("access_token") or body.get("token") or "").strip()
            if not token:
                raise UpstreamError("缺少 access_token", status=400)
            account = channel.import_token(
                token,
                refresh_token=str(body.get("refresh_token") or ""),
                realm=realm or "cn",
                uid=str(body.get("uid") or ""),
                nickname=str(body.get("nickname") or ""),
                expires_at=util.normalize_epoch(body.get("expires_at")),
                domain=str(body.get("domain") or ""),
                enterprise_id=str(body.get("enterprise_id") or ""),
                source="manual")
        elif channel_name == "zcode":
            account = channel.import_token(
                realm=realm or "zai",
                oauth_access_token=str(body.get("oauth_access_token")
                                       or body.get("token") or ""),
                jwt_token=str(body.get("jwt_token") or ""),
                api_key=str(body.get("api_key") or ""),
                uid=str(body.get("uid") or ""),
                nickname=str(body.get("nickname") or ""),
                source="manual")
        elif channel_name == "gpt":
            account = channel.add_account(
                str(body.get("base_url") or ""),
                str(body.get("api_key") or ""),
                name=str(body.get("name") or ""),
                models=body.get("models"),
                model_map=body.get("model_map"),
                extra_headers=body.get("extra_headers"),
                organization=str(body.get("organization") or ""),
                project=str(body.get("project") or ""),
                realm=realm or "custom")
        else:
            raise UpstreamError("该渠道不支持手动添加", status=400)
        self._json(200, {"ok": True,
                         "account": self.runtime.accounts.public(account)})

    def _admin_update_account(self, path):
        account_id = path[len("/api/accounts/"):].strip("/")
        if not account_id:
            raise UpstreamError("账号 ID 为空", status=400)
        body = self._read_json()
        account = self.runtime.accounts.update_fields(
            account_id,
            data_patch=body.get("data") if isinstance(body.get("data"), dict) else None,
            name=body.get("name"),
            enabled=body.get("enabled"))
        self._json(200, {"ok": True,
                         "account": self.runtime.accounts.public(account)})

    def _admin_account_action(self, path):
        parts = [p for p in path[len("/api/accounts/"):].split("/") if p]
        if len(parts) < 2:
            raise UpstreamError("参数不完整", status=400)
        account_id, action = parts[0], parts[1]
        account = self.runtime.accounts.get(account_id)
        if not account:
            raise UpstreamError("账号不存在", status=404)
        channel = self.runtime.channels.get(account["channel"])
        if not channel:
            raise UpstreamError("渠道不存在", status=400)
        if action == "checkin":
            result = channel.checkin(account)
            self.runtime.accounts.save(account)
            self._json(200, {"ok": True, "result": result,
                             "account": self.runtime.accounts.public(account)})
            return
        if action == "refresh":
            changed = self.runtime.accounts.refresh_account(account, force=True)
            usage = self.runtime.accounts.refresh_balance(account, force=True)
            self._json(200, {"ok": True, "changed": changed,
                             "usage": usage,
                             "account": self.runtime.accounts.public(account)})
            return
        if action == "test":
            result = channel.validate_account(account)
            self._json(200, {"ok": bool(result.get("ok")), "result": result,
                             "account": self.runtime.accounts.public(account)})
            return
        raise UpstreamError("未知操作：%s" % action, status=404)

    def _admin_scan(self, channel_name):
        channel_name = channel_name.strip("/")
        channel = self.runtime.channels.get(channel_name)
        if not channel or not hasattr(channel, "public_scan"):
            raise UpstreamError("该渠道不支持本地扫描", status=400)
        self._json(200, {"items": channel.public_scan()})

    def _admin_import_local(self, channel_name):
        channel_name = channel_name.strip("/")
        channel = self.runtime.channels.get(channel_name)
        if not channel or not hasattr(channel, "import_local"):
            raise UpstreamError("该渠道不支持本地导入", status=400)
        body = self._read_json()
        try:
            result = channel.import_local(realm=body.get("realm") or None)
        except TypeError:
            result = channel.import_local()
        self._json(200, result)

    def _admin_login_start(self, path):
        channel_name = path[len("/api/login/"):].strip("/")
        if channel_name.endswith("/cancel") or channel_name.endswith("/poll"):
            raise UpstreamError("登录接口路径错误", status=404)
        channel = self.runtime.channels.get(channel_name)
        if not channel or not getattr(channel, "supports_oauth", False):
            raise UpstreamError("该渠道不支持 OAuth 登录", status=400)
        body = self._read_json()
        started = channel.start_oauth(str(body.get("realm") or ""))
        self._json(200, started)

    def _admin_login_poll(self, path):
        channel_name = path[len("/api/login/"):].strip("/")
        if channel_name.endswith("/poll"):
            channel_name = channel_name[:-len("/poll")]
        channel = self.runtime.channels.get(channel_name)
        if not channel or not getattr(channel, "supports_oauth", False):
            raise UpstreamError("该渠道不支持 OAuth 登录", status=400)
        state = (util.query_string(self).get("state") or [""])[0]
        if not state:
            raise UpstreamError("缺少 state", status=400)
        result = channel.poll_oauth(state)
        self._json(200, result)

    def _admin_login_cancel(self, path):
        channel_name = path[len("/api/login/"):].strip("/")[:-len("/cancel")]
        channel = self.runtime.channels.get(channel_name)
        body = self._read_json()
        if channel and getattr(channel, "supports_oauth", False):
            channel.cancel_oauth(str(body.get("state") or ""))
        self._json(200, {"ok": True})


def serve(runtime, host=None, port=None, open_browser=False):
    service = GatewayService(runtime, host, port)
    service.start()
    if open_browser:
        try:
            import webbrowser
            threading.Timer(0.8, lambda: webbrowser.open(service.url)).start()
        except Exception:
            pass
    try:
        while service.alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        runtime.log("INFO", "收到退出信号")
    finally:
        service.stop()
    return service
