"""Assemble complete model SSE replies without exposing partial tool actions."""
import copy
import json


class IncompleteStreamError(RuntimeError):
    """The connection ended without the protocol's completion marker."""


def _events(response):
    data, name = [], None
    for line in response.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = line.lstrip("\ufeff")
        if not line:
            if data:
                encoded = "\n".join(data)
                yield name, "[DONE]" if encoded == "[DONE]" else json.loads(encoded)
            data, name = [], None
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line.startswith("event:"):
            name = line[6:].strip()
    if data:
        encoded = "\n".join(data)
        yield name, "[DONE]" if encoded == "[DONE]" else json.loads(encoded)


def _merge(target, delta, text_fields=()):
    """Usage is cumulative; append only known textual delta fields."""
    for key, value in delta.items():
        if value is None:
            continue
        if isinstance(value, str) and key in text_fields:
            target[key] = (target.get(key) or "") + value
        elif isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge(target[key], value, text_fields)
        elif isinstance(value, list):
            if not isinstance(target.get(key), list):
                target[key] = []
            target[key].extend(copy.deepcopy(value))
        else:
            target[key] = value


def _index(value):
    if type(value) is not int or value < 0:
        raise RuntimeError("Invalid index in model stream; no actions dispatched.")
    return value


def _ordered(values):
    if sorted(values) != list(range(len(values))):
        raise RuntimeError("Missing indexed content in model stream; no actions dispatched.")
    return [values[i] for i in sorted(values)]


def _responses(response):
    for name, event in _events(response):
        if event == "[DONE]":
            break
        kind = event.get("type", name)
        if kind == "response.completed":
            return event["response"]
        if kind in {"response.failed", "response.incomplete", "error"}:
            detail = event.get("response", {}).get("error") or event.get("message")
            raise RuntimeError(f"Responses stream {kind}: {str(detail)[:300]}")
    raise IncompleteStreamError("Responses stream ended before response.completed; no actions dispatched.")


def _messages(response):
    message, blocks, partial_json, open_blocks = None, {}, {}, set()
    for name, event in _events(response):
        if event == "[DONE]":
            break
        kind = event.get("type", name)
        if kind == "error":
            raise RuntimeError(f"Messages stream error: {str(event.get('error'))[:300]}")
        if kind == "message_start":
            if message is not None:
                raise RuntimeError("Duplicate message_start; no actions dispatched.")
            message = copy.deepcopy(event["message"])
            blocks = dict(enumerate(message.get("content", [])))
        elif kind == "content_block_start":
            index = _index(event["index"])
            if message is None or index in blocks:
                raise RuntimeError("Invalid content_block_start; no actions dispatched.")
            blocks[index] = copy.deepcopy(event["content_block"])
            open_blocks.add(index)
        elif kind == "content_block_delta":
            index = _index(event["index"])
            if index not in open_blocks:
                raise RuntimeError("Delta outside an open content block; no actions dispatched.")
            delta, block = event["delta"], blocks[index]
            delta_type = delta.get("type")
            if delta_type == "input_json_delta":
                partial_json[index] = partial_json.get(index, "") + delta["partial_json"]
            elif delta_type in {"text_delta", "thinking_delta", "signature_delta"}:
                field = {"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "signature"}[delta_type]
                block[field] = block.get(field, "") + delta[field]
            elif delta_type == "citations_delta":
                block.setdefault("citations", []).append(copy.deepcopy(delta["citation"]))
            else:
                _merge(block, {k: v for k, v in delta.items() if k != "type"})
        elif kind == "content_block_stop":
            index = _index(event["index"])
            if index not in open_blocks:
                raise RuntimeError("Invalid content_block_stop; no actions dispatched.")
            open_blocks.remove(index)
        elif kind == "message_delta":
            if message is None:
                raise RuntimeError("message_delta before message_start; no actions dispatched.")
            _merge(message, event.get("delta") or {})
            _merge(message.setdefault("usage", {}), event.get("usage") or {})
        elif kind == "message_stop":
            if message is None or open_blocks:
                raise IncompleteStreamError("Incomplete Messages content blocks; no actions dispatched.")
            for index, encoded in partial_json.items():
                if encoded.strip():
                    try:
                        blocks[index]["input"] = json.loads(encoded)
                    except ValueError:
                        # Keep the raw text instead of failing the episode here.
                        # A stream that terminated cleanly but carried unparsable
                        # arguments is the model breaking the action protocol, and
                        # the correction loop above ModelWire.unpack is what has to
                        # handle it: action calls are told to correct themselves,
                        # frame calls get an error tool result. It also preserves
                        # arguments truncated by the output limit, which unpack
                        # still rejects before any action; a stream that never
                        # terminated is rejected above.
                        blocks[index]["input"] = encoded
            message["content"] = _ordered(blocks)
            return message
    raise IncompleteStreamError("Messages stream ended before message_stop; no actions dispatched.")


class _ChatTools:
    """Use call IDs to keep gateway calls with reused indices distinct."""
    def __init__(self):
        self.calls, self.indices, self.by_index, self.by_id = [], [], {}, {}

    def _arguments_complete(self, position):
        try:
            return isinstance(json.loads(self.calls[position].get('function', {}).get('arguments', '')), dict)
        except (TypeError, ValueError):
            return False

    def add(self, delta):
        delta = copy.deepcopy(delta)
        for field in ('id', 'type'):
            if delta.get(field) == '':
                delta.pop(field)
        if isinstance(delta.get('function'), dict) and delta['function'].get('name') == '':
            delta['function'].pop('name')
        index, call_id = _index(delta['index']), delta.get('id')
        candidates = self.by_index.get(index, [])
        position = self.by_id.get(call_id) if call_id else None
        if position is None:
            if call_id:
                candidates = [p for p in candidates if not self.calls[p].get('id')]
            elif len(candidates) > 1:
                # Some gateways reuse index=0 while streaming successive calls.
                # An argument fragment can only extend the sole unfinished JSON
                # object; two unfinished calls are ambiguous and must fail.
                fragment = (delta.get('function') or {}).get('arguments')
                unfinished = [p for p in candidates if not self._arguments_complete(p)]
                if isinstance(fragment, str) and fragment.strip() and len(unfinished) == 1:
                    candidates = unfinished
            if len(candidates) > 1:
                raise RuntimeError('Ambiguous tool delta: reused index without a call ID; no actions dispatched.')
            if candidates:
                position = candidates[0]
            else:
                position = len(self.calls)
                self.calls.append({})
                self.indices.append(index)
                self.by_index.setdefault(index, []).append(position)
        if self.indices[position] != index:
            raise RuntimeError('Tool call ID changed index within a stream; no actions dispatched.')
        if call_id:
            self.by_id[call_id] = position
        target = self.calls[position]
        previous_name = target.get('function', {}).get('name')
        new_name = (delta.get('function') or {}).get('name')
        if previous_name and new_name and previous_name != new_name:
            raise RuntimeError('Tool call ID changed function name within a stream; no actions dispatched.')
        _merge(target, {k: v for k, v in delta.items() if k != 'index'}, {'arguments'})

    def values(self):
        _ordered(self.by_index)  # A reused index is allowed; a missing index is not.
        return [self.calls[p] for p in sorted(range(len(self.calls)), key=lambda p: (self.indices[p], p))]


def _chat(response):
    result, choices, tool_calls = {}, {}, {}
    for name, chunk in _events(response):
        if chunk == "[DONE]":
            if not choices or any(c.get("finish_reason") is None for c in choices.values()):
                raise IncompleteStreamError("Chat stream missing finish_reason; no actions dispatched.")
            for index, calls in tool_calls.items():
                choices[index]["message"]["tool_calls"] = calls.values()
            result["choices"] = _ordered(choices)
            result["object"] = "chat.completion"
            return result
        if name == "error" or chunk.get("error"):
            raise RuntimeError(f"Chat stream error: {str(chunk.get('error', chunk))[:300]}")
        for key, value in chunk.items():
            if key != "choices" and value is not None:
                if key == "usage":
                    _merge(result.setdefault("usage", {}), value)
                else:
                    result[key] = copy.deepcopy(value)
        for incoming in chunk.get("choices", []):
            index = _index(incoming["index"])
            choice = choices.setdefault(index, {
                "index": index, "message": {"role": "assistant", "content": None}, "finish_reason": None,
            })
            for key, value in incoming.items():
                if key not in {"delta", "message"} and value is not None:
                    choice[key] = copy.deepcopy(value)
            delta = incoming.get("delta") or incoming.get("message") or {}
            _merge(choice["message"], {k: v for k, v in delta.items() if k != "tool_calls"},
                   {"content", "refusal", "reasoning_content", "reasoning"})
            for position, call in enumerate(delta.get("tool_calls") or []):
                call_index = _index(call.get("index", position if "message" in incoming else None))
                tool_calls.setdefault(index, _ChatTools()).add({**call, 'index': call_index})
    raise IncompleteStreamError("Chat stream ended before [DONE]; no actions dispatched.")


def collect_stream(response, protocol):
    """Return the protocol's normal full-response shape; caller closes response."""
    return {
        "anthropic_messages": _messages,
        "openai_chat": _chat,
        "openai_responses": _responses,
    }[protocol](response)
