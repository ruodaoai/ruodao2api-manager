"""OpenAI Responses API translation state (streaming + previous_response_id)."""

import json
import threading
import time

from .. import util
from .translate import openai_to_responses


class ResponseStore(object):
    """Bounded in-memory memory of canonical chat messages per response id."""

    def __init__(self, max_entries=1000, ttl=24 * 3600):
        self.max_entries = max(1, int(max_entries))
        self.ttl = max(60, int(ttl))
        self._items = {}
        self._order = []
        self._lock = threading.Lock()

    def get(self, response_id):
        if not response_id:
            return None
        with self._lock:
            item = self._items.get(response_id)
            if not item:
                return None
            if time.time() - item["at"] > self.ttl:
                self._items.pop(response_id, None)
                return None
            return list(item["messages"])

    def put(self, response_id, messages):
        if not response_id:
            return
        with self._lock:
            self._items[response_id] = {"at": time.time(),
                                        "messages": list(messages or [])}
            self._order.append(response_id)
            while len(self._order) > self.max_entries:
                old = self._order.pop(0)
                self._items.pop(old, None)

    def clear(self):
        with self._lock:
            self._items.clear()
            self._order = []


class ResponsesStreamTranslator(object):
    """OpenAI stream chunks -> Responses API SSE events."""

    def __init__(self, model="", previous_response_id=None, store=True,
                 messages=None):
        self.model = model
        self.previous_response_id = previous_response_id
        self.store = store
        self.messages = messages or []
        self.response_id = "resp_" + util.new_id("")[:24]
        self.created_at = int(time.time())
        self.seq = 0
        self.text = []
        self.reasoning = []
        self.tools = {}
        self.text_item_id = None
        self.text_started = False
        self.usage = {}
        self.finish_reason = None
        self.completed = False

    def _event(self, name, data):
        self.seq += 1
        data = dict(data or {})
        data.setdefault("type", name)
        data["sequence_number"] = self.seq
        return {"event": name, "data": data}

    def _base_response(self, status="in_progress"):
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "max_output_tokens": None,
            "model": self.model,
            "output": [],
            "parallel_tool_calls": True,
            "previous_response_id": self.previous_response_id,
            "reasoning": {"effort": None, "summary": None},
            "store": bool(self.store),
            "temperature": None,
            "text": {"format": {"type": "text"}},
            "tool_choice": "auto",
            "tools": [],
            "top_p": None,
            "truncation": "disabled",
            "usage": None,
            "user": None,
            "metadata": {},
        }

    def start_events(self):
        return [self._event("response.created", {"response": self._base_response()}),
                self._event("response.in_progress",
                            {"response": self._base_response()})]

    def _ensure_text_item(self, events):
        if self.text_started:
            return
        self.text_started = True
        self.text_item_id = "msg_" + util.new_id("")[:24]
        events.append(self._event("response.output_item.added", {
            "output_index": 0,
            "item": {"id": self.text_item_id, "type": "message",
                     "status": "in_progress", "role": "assistant",
                     "content": []},
        }))
        events.append(self._event("response.content_part.added", {
            "item_id": self.text_item_id, "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        }))

    def feed(self, chunk):
        events = []
        if chunk.get("usage"):
            self.usage = util.normalize_usage(chunk["usage"]) or self.usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                self.reasoning.append(reasoning)
            text = delta.get("content")
            if isinstance(text, list):
                text = "".join(str(p.get("text") or "") for p in text
                               if isinstance(p, dict))
            if isinstance(text, str) and text:
                self._ensure_text_item(events)
                self.text.append(text)
                events.append(self._event("response.output_text.delta", {
                    "item_id": self.text_item_id, "output_index": 0,
                    "content_index": 0, "delta": text,
                }))
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                try:
                    idx = int(call.get("index"))
                except Exception:
                    idx = 0
                slot = self.tools.get(idx)
                fn = call.get("function") or {}
                if slot is None:
                    slot = {
                        "id": "fc_" + util.new_id("")[:24],
                        "call_id": str(call.get("id") or util.new_id("call_")),
                        "name": str(fn.get("name") or ""),
                        "arguments": "",
                        "output_index": len(self.tools) + (1 if self.text_started else 0),
                    }
                    self.tools[idx] = slot
                    events.append(self._event("response.output_item.added", {
                        "output_index": slot["output_index"],
                        "item": {"id": slot["id"], "type": "function_call",
                                 "status": "in_progress", "arguments": "",
                                 "call_id": slot["call_id"],
                                 "name": slot["name"]},
                    }))
                if fn.get("name") and not slot["name"]:
                    slot["name"] = str(fn["name"])
                arguments = fn.get("arguments")
                if arguments is not None:
                    slot["arguments"] += str(arguments)
                    events.append(self._event("response.function_call_arguments.delta", {
                        "item_id": slot["id"], "output_index": slot["output_index"],
                        "delta": str(arguments),
                    }))
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return events

    def finish(self):
        if self.completed:
            return []
        self.completed = True
        events = []
        output = []
        if self.text_started:
            text = "".join(self.text)
            events.append(self._event("response.output_text.done", {
                "item_id": self.text_item_id, "output_index": 0,
                "content_index": 0, "text": text,
            }))
            events.append(self._event("response.content_part.done", {
                "item_id": self.text_item_id, "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            }))
            item = {"id": self.text_item_id, "type": "message",
                    "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": text,
                                 "annotations": []}]}
            events.append(self._event("response.output_item.done", {
                "output_index": 0, "item": item}))
            output.append(item)
        for idx in sorted(self.tools):
            slot = self.tools[idx]
            events.append(self._event("response.function_call_arguments.done", {
                "item_id": slot["id"], "output_index": slot["output_index"],
                "arguments": slot["arguments"],
            }))
            item = {"id": slot["id"], "type": "function_call",
                    "status": "completed", "arguments": slot["arguments"],
                    "call_id": slot["call_id"], "name": slot["name"]}
            events.append(self._event("response.output_item.done", {
                "output_index": slot["output_index"], "item": item}))
            output.append(item)
        response = self._base_response("completed")
        response["output"] = output
        response["usage"] = {
            "input_tokens": self.usage.get("prompt_tokens") or 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": self.usage.get("completion_tokens") or 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": self.usage.get("total_tokens") or 0,
        }
        if self.finish_reason == "length":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        events.append(self._event("response.completed", {"response": response}))
        return events

    def assistant_message(self):
        message = {"role": "assistant", "content": "".join(self.text) or None}
        if self.tools:
            calls = []
            for idx in sorted(self.tools):
                slot = self.tools[idx]
                calls.append({"id": slot["call_id"], "type": "function",
                              "function": {"name": slot["name"],
                                           "arguments": slot["arguments"]}})
            message["tool_calls"] = calls
        return message

    def final_response(self):
        """Build the non-stream response object from the translator state."""
        obj = {"choices": [{"message": self.assistant_message(),
                            "finish_reason": self.finish_reason,
                            "index": 0}],
               "model": self.model,
               "usage": self.usage}
        body = openai_to_responses(obj, model=self.model,
                                   previous_response_id=self.previous_response_id,
                                   store=self.store)
        body["id"] = self.response_id
        body["created_at"] = self.created_at
        return body
