"""Protocol translation helpers (Anthropic / Responses <-> internal OpenAI chat)."""

import json
import time

from .. import util


# ---------------------------------------------------------------------------
# Anthropic Messages -> OpenAI Chat
# ---------------------------------------------------------------------------

def _text_blocks(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(p.get("text") or "") for p in value
                       if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return str(value or "")


_ROLES = ("system", "user", "assistant", "tool")


def normalize_messages(messages):
    """Flatten driver-specific roles into the system/user/assistant/tool set.

    Responses clients (Codex and friends) emit ``developer`` messages and can
    leave system prompts in the middle of a replayed history. Z.AI/BigModel
    reject both with ``Incorrect role information``, so system text is hoisted
    to the front and orphaned tool results degrade to user messages.
    """
    out = []
    system_texts = []
    pending_calls = set()
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").lower()
        if role == "developer":
            role = "system"
        if role == "system":
            text = _text_blocks(msg.get("content"))
            if text:
                system_texts.append(text)
            continue
        if role not in _ROLES:
            role = "user"
        item = dict(msg)
        item["role"] = role
        if role == "tool":
            call_id = str(item.get("tool_call_id") or "")
            if not call_id or call_id not in pending_calls:
                text = _text_blocks(item.get("content"))
                out.append({"role": "user",
                            "content": ("[工具结果] " + text) if text else "[工具结果]"})
                continue
            pending_calls.discard(call_id)
        elif role == "assistant":
            pending_calls = set()
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    pending_calls.add(str(call["id"]))
        else:
            pending_calls = set()
        out.append(item)
    merged_parts = []
    seen_system = set()
    for text in system_texts:
        if text and text not in seen_system:
            seen_system.add(text)
            merged_parts.append(text)
    if merged_parts:
        out.insert(0, {"role": "system", "content": "\n\n".join(merged_parts)})
    if not out:
        out.append({"role": "user", "content": ""})
    return out


def _image_block(block):
    source = block.get("source") or {}
    if not isinstance(source, dict):
        return None
    media = str(source.get("media_type") or "image/png")
    data = str(source.get("data") or "")
    url = str(source.get("url") or "")
    if data:
        return {"type": "image_url",
                "image_url": {"url": "data:%s;base64,%s" % (media, data)}}
    if url:
        return {"type": "image_url", "image_url": {"url": url}}
    return None


def anthropic_to_openai(payload):
    messages = []
    system = payload.get("system")
    if system:
        messages.append({"role": "system", "content": _text_blocks(system)})

    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        blocks = content if isinstance(content, list) else []
        if role == "assistant":
            texts = []
            tool_calls = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    texts.append(str(block.get("text") or ""))
                elif kind == "tool_use":
                    tool_calls.append({
                        "id": str(block.get("id") or util.new_id("call_")),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": util.json_dumps(block.get("input") or {}),
                        },
                    })
            item = {"role": "assistant", "content": "".join(texts)}
            if tool_calls:
                item["tool_calls"] = tool_calls
                item["content"] = item["content"] or None
            messages.append(item)
            continue

        parts = []
        tool_results = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append({"type": "text", "text": str(block.get("text") or "")})
            elif kind == "image":
                image = _image_block(block)
                if image:
                    parts.append(image)
            elif kind == "tool_result":
                tool_results.append(block)
        for block in tool_results:
            content = block.get("content")
            text = _text_blocks(content)
            if isinstance(content, list):
                extra = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image":
                        image = _image_block(part)
                        if image:
                            extra.append(image)
                if extra:
                    value = [{"type": "text", "text": text}] + extra
                    messages.append({"role": "tool",
                                     "tool_call_id": str(block.get("tool_use_id") or ""),
                                     "content": value})
                    continue
            messages.append({"role": "tool",
                             "tool_call_id": str(block.get("tool_use_id") or ""),
                             "content": text})
        if parts:
            messages.append({"role": role, "content": parts})

    out = {"model": payload.get("model") or "",
           "messages": messages}
    for src, dst in (("max_tokens", "max_tokens"), ("temperature", "temperature"),
                     ("top_p", "top_p"), ("stop_sequences", "stop"),
                     ("top_k", "top_k")):
        if payload.get(src) is not None:
            out[dst] = payload[src]
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        converted = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "")
            if not name:
                continue
            converted.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema") or {"type": "object",
                                                               "properties": {}},
                },
            })
        if converted:
            out["tools"] = converted
    choice = payload.get("tool_choice")
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind == "tool" and choice.get("name"):
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": str(choice["name"])}}
        elif kind == "any":
            out["tool_choice"] = "required"
        elif kind == "none":
            out["tool_choice"] = "none"
        elif kind == "auto":
            out["tool_choice"] = "auto"
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled" \
            and thinking.get("budget_tokens"):
        out["reasoning_effort"] = "medium" if int(thinking["budget_tokens"]) < 8192 else "high"
    return out


def openai_to_anthropic(obj, model=""):
    choice = (obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []
    if isinstance(message.get("content"), str) and message["content"]:
        content.append({"type": "text", "text": message["content"]})
    elif isinstance(message.get("content"), list):
        for part in message["content"]:
            if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                content.append({"type": "text", "text": str(part.get("text") or "")})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        arguments = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except Exception:
            parsed = {"_raw": str(arguments)}
        content.append({
            "type": "tool_use",
            "id": str(call.get("id") or util.new_id("toolu_")),
            "name": str(fn.get("name") or ""),
            "input": parsed if isinstance(parsed, dict) else {"value": parsed},
        })
    finish = choice.get("finish_reason")
    stop_reason = {"stop": "end_turn", "length": "max_tokens",
                   "tool_calls": "tool_use", "content_filter": "end_turn"}.get(finish)
    usage = util.normalize_usage(obj.get("usage")) or {}
    return {
        "id": "msg_" + util.new_id("")[:24],
        "type": "message",
        "role": "assistant",
        "model": model or str(obj.get("model") or ""),
        "content": content,
        "stop_reason": stop_reason or "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


class AnthropicStreamTranslator(object):
    """OpenAI stream chunks -> Anthropic Messages SSE events."""

    def __init__(self, model="", include_thinking=False):
        self.model = model
        self.include_thinking = include_thinking
        self.message_id = "msg_" + util.new_id("")[:24]
        self.started = False
        self.finished = False
        self.block_index = -1
        self.open_kind = ""
        self.tool_blocks = {}
        self.finish_reason = None
        self.usage = {}
        self.thinking_index = None
        self.text_index = None

    def _event(self, name, data):
        return {"event": name, "data": data}

    def _start(self):
        if self.started:
            return []
        self.started = True
        return [self._event("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id, "type": "message", "role": "assistant",
                "model": self.model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })]

    def _close_open(self):
        if self.open_kind:
            event = self._event("content_block_stop", {
                "type": "content_block_stop", "index": self.block_index})
            self.open_kind = ""
            return [event]
        return []

    def _open_block(self, kind, block):
        events = self._close_open()
        self.block_index += 1
        self.open_kind = kind
        events.append(self._event("content_block_start", {
            "type": "content_block_start", "index": self.block_index,
            "content_block": block,
        }))
        return events

    def feed(self, chunk):
        events = self._start()
        if chunk.get("usage"):
            self.usage = util.normalize_usage(chunk["usage"]) or self.usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning and self.include_thinking:
                if self.open_kind != "thinking":
                    events += self._open_block("thinking", {
                        "type": "thinking", "thinking": "", "signature": ""})
                    self.thinking_index = self.block_index
                events.append(self._event("content_block_delta", {
                    "type": "content_block_delta", "index": self.block_index,
                    "delta": {"type": "thinking_delta", "thinking": reasoning}}))
            text = delta.get("content")
            if isinstance(text, str) and text:
                if self.open_kind != "text":
                    events += self._open_block("text", {"type": "text", "text": ""})
                    self.text_index = self.block_index
                events.append(self._event("content_block_delta", {
                    "type": "content_block_delta", "index": self.block_index,
                    "delta": {"type": "text_delta", "text": text}}))
            elif isinstance(text, list):
                joined = "".join(str(p.get("text") or "") for p in text
                                 if isinstance(p, dict)
                                 and p.get("type") in ("text", "output_text"))
                if joined:
                    if self.open_kind != "text":
                        events += self._open_block("text", {"type": "text", "text": ""})
                        self.text_index = self.block_index
                    events.append(self._event("content_block_delta", {
                        "type": "content_block_delta", "index": self.block_index,
                        "delta": {"type": "text_delta", "text": joined}}))
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                idx = call.get("index")
                try:
                    idx = int(idx)
                except Exception:
                    idx = 0
                slot = self.tool_blocks.get(idx)
                fn = call.get("function") or {}
                if slot is None:
                    slot = {
                        "id": str(call.get("id") or util.new_id("toolu_")),
                        "name": str(fn.get("name") or ""),
                        "arguments": "",
                    }
                    self.tool_blocks[idx] = slot
                if fn.get("name") and not slot["name"]:
                    slot["name"] = str(fn["name"])
                arguments = fn.get("arguments")
                if arguments is not None:
                    slot["arguments"] += str(arguments)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return events

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        events = self._start()
        events += self._close_open()
        # Anthropic content blocks are sequential; buffer tool arguments and
        # emit each tool_use block in order at the end of the stream.
        for idx in sorted(self.tool_blocks):
            slot = self.tool_blocks[idx]
            self.block_index += 1
            events.append(self._event("content_block_start", {
                "type": "content_block_start", "index": self.block_index,
                "content_block": {"type": "tool_use", "id": slot["id"],
                                  "name": slot["name"], "input": {}},
            }))
            if slot["arguments"]:
                events.append(self._event("content_block_delta", {
                    "type": "content_block_delta", "index": self.block_index,
                    "delta": {"type": "input_json_delta",
                              "partial_json": slot["arguments"]},
                }))
            events.append(self._event("content_block_stop", {
                "type": "content_block_stop", "index": self.block_index}))
        stop_reason = {"stop": "end_turn", "length": "max_tokens",
                       "tool_calls": "tool_use",
                       "content_filter": "end_turn"}.get(self.finish_reason)
        if not stop_reason and self.tool_blocks:
            stop_reason = "tool_use"
        usage = self.usage or {}
        events.append(self._event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason or "end_turn",
                      "stop_sequence": None},
            "usage": {"output_tokens": usage.get("completion_tokens") or 0},
        }))
        events.append(self._event("message_stop", {"type": "message_stop"}))
        return events


# ---------------------------------------------------------------------------
# OpenAI Responses -> OpenAI Chat
# ---------------------------------------------------------------------------

def _responses_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append({"type": "text", "text": item})
            elif isinstance(item, dict):
                kind = item.get("type")
                if kind in ("input_text", "output_text", "text"):
                    parts.append({"type": "text", "text": str(item.get("text") or "")})
                elif kind == "input_image":
                    url = item.get("image_url")
                    if isinstance(url, dict):
                        url = url.get("url")
                    if url:
                        parts.append({"type": "image_url", "image_url": {"url": url}})
        return parts
    return str(value or "")


def responses_to_openai(payload):
    messages = []
    instructions = payload.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": _text_blocks(instructions)})
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if not isinstance(item, dict):
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                continue
            kind = item.get("type")
            if kind == "message" or (kind is None and item.get("role")):
                role = str(item.get("role") or "user")
                content = _responses_content(item.get("content"))
                messages.append({"role": role, "content": content})
            elif kind == "function_call":
                call = {
                    "id": str(item.get("call_id") or item.get("id") or util.new_id("call_")),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }
                if messages and messages[-1].get("role") == "assistant" \
                        and isinstance(messages[-1].get("tool_calls"), list):
                    messages[-1]["tool_calls"].append(call)
                else:
                    messages.append({"role": "assistant", "content": None,
                                     "tool_calls": [call]})
            elif kind == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": _text_blocks(item.get("output")),
                })
            elif kind == "reasoning":
                continue
    if not messages:
        messages.append({"role": "user", "content": ""})

    out = {"model": payload.get("model") or "", "messages": messages}
    for src, dst in (("max_output_tokens", "max_tokens"),
                     ("temperature", "temperature"), ("top_p", "top_p"),
                     ("parallel_tool_calls", "parallel_tool_calls")):
        if payload.get(src) is not None:
            out[dst] = payload[src]
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        converted = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") not in (None, "function"):
                continue
            name = str(tool.get("name") or "")
            if not name:
                continue
            converted.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("parameters") or {"type": "object",
                                                             "properties": {}},
                },
            })
        if converted:
            out["tools"] = converted
    choice = payload.get("tool_choice")
    if isinstance(choice, str):
        out["tool_choice"] = choice
    elif isinstance(choice, dict):
        if choice.get("type") == "function" and choice.get("name"):
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": str(choice["name"])}}
        elif choice.get("type") in ("auto", "none", "required"):
            out["tool_choice"] = choice["type"]
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        out["reasoning_effort"] = str(reasoning["effort"])
    text = payload.get("text")
    if isinstance(text, dict) and isinstance(text.get("format"), dict):
        fmt = text["format"]
        if fmt.get("type") == "json_object":
            out["response_format"] = {"type": "json_object"}
        elif fmt.get("type") == "json_schema" and fmt.get("schema"):
            out["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": str(fmt.get("name") or "output"),
                    "schema": fmt.get("schema"),
                    "strict": bool(fmt.get("strict")),
                },
            }
    return out


def _responses_usage(usage):
    usage = util.normalize_usage(usage) or {}
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": usage.get("total_tokens") or (prompt + completion),
    }


def openai_to_responses(obj, model="", messages=None, previous_response_id=None,
                        store=True):
    choice = (obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output = []
    text = message.get("content")
    if isinstance(text, list):
        text = "".join(str(p.get("text") or "") for p in text
                       if isinstance(p, dict))
    if text:
        output.append({
            "id": "msg_" + util.new_id("")[:24],
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": str(text),
                         "annotations": []}],
        })
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        output.append({
            "id": "fc_" + util.new_id("")[:24],
            "type": "function_call",
            "status": "completed",
            "arguments": str(fn.get("arguments") or "{}"),
            "call_id": str(call.get("id") or util.new_id("call_")),
            "name": str(fn.get("name") or ""),
        })
    response_id = "resp_" + util.new_id("")[:24]
    finish = choice.get("finish_reason")
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": ({"reason": "max_output_tokens"}
                               if finish == "length" else None),
        "instructions": None,
        "max_output_tokens": None,
        "model": model or str(obj.get("model") or ""),
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": previous_response_id,
        "reasoning": {"effort": None, "summary": None},
        "store": bool(store),
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": _responses_usage(obj.get("usage")),
        "user": None,
        "metadata": {},
    }
