"""Shared screenshot agent for the four real-time ablations.

Wire protocols are explicit. Native tool replies are retained in full, including
call IDs and provider reasoning blocks. History is pruned only at round boundaries.
"""
import base64
import copy
import hashlib
import json
import os
import time

import requests

from mm_agents.realtime_protocol import (
    ACTION_TOOLS,
    ACTION_TOOL_TYPES,
    FRAME_TOOL,
    GetFramesArgs,
    MODES,
    load_output,
    parse_actions,
    system_prompt,
    validate_action,
)


def text_block(text):
    return {"type": "text", "text": text}


def result_blocks(result):
    blocks = []
    if "task_time_s" in result:
        blocks.append(
            text_block(json.dumps({"query_completed_time_s": result["task_time_s"]}))
        )
    for frame in result.get("frames", [result]):
        metadata = {k: v for k, v in frame.items() if k != "image"}
        blocks.append(text_block(json.dumps(metadata, ensure_ascii=False)))
        if frame.get("status") == "ok" and frame.get("image"):
            blocks.append({"type": "image", "source": frame["image"]})
    return blocks


def response_reasoning(response, protocol):
    """Record only reasoning text actually returned by the provider, never infer it."""
    texts, opaque = [], False
    if protocol == "anthropic_messages":
        for block in response.get("content", []):
            if block.get("type") == "thinking":
                if block.get("thinking"):
                    texts.append(block["thinking"])
                else:
                    opaque = True
            elif block.get("type") == "redacted_thinking":
                opaque = True
    elif protocol == "openai_responses":
        for item in response.get("output", []):
            if item.get("type") == "reasoning":
                texts.extend(b["text"] for b in item.get("summary", []) if b.get("text"))
                opaque |= bool(item.get("encrypted_content"))
    else:
        for choice in response.get("choices", []):
            message = choice.get("message", {})
            for name in ("reasoning_content", "reasoning"):
                if isinstance(message.get(name), str) and message[name]:
                    texts.append(message[name])
    return {
        "reasoning": texts,
        "reasoning_status": "returned" if texts else "opaque" if opaque else "not_returned",
    }


def loggable_messages(messages):
    """Copy request messages while replacing inline image bytes with hashes."""
    logged = copy.deepcopy(messages)
    for message in logged:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            source = block.get("source")
            if isinstance(source, dict) and isinstance(source.get("data"), str):
                data = source.pop("data")
                source["data_sha256"] = hashlib.sha256(data.encode("ascii")).hexdigest()
                source["data_length"] = len(data)
            image_url = block.get("image_url")
            if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                url = image_url["url"]
                if url.startswith("data:"):
                    image_url["url"] = "data:image/png;base64,[omitted]"
                    image_url["url_sha256"] = hashlib.sha256(url.encode("ascii")).hexdigest()
                    image_url["url_length"] = len(url)
    return logged


class ModelWire:
    def __init__(self, model, api_format="auto", base_url=None, timeout=120, thinking_summary=False):
        self.model = model
        self.protocol = (
            ("anthropic_messages" if model.startswith("claude") else "openai_chat")
            if api_format == "auto"
            else api_format
        )
        if self.protocol not in {
            "openai_chat",
            "anthropic_messages",
            "openai_responses",
        }:
            raise ValueError("Unsupported model API protocol")
        if thinking_summary and self.protocol != "anthropic_messages":
            raise ValueError("thinking_summary requires the Anthropic Messages protocol")
        self.thinking_summary = thinking_summary
        anthropic = self.protocol == "anthropic_messages"
        self.base_url = (
            base_url
            or os.getenv("PACKY_API_BASE_URL")
            or os.getenv("ANTHROPIC_BASE_URL" if anthropic else "OPENAI_BASE_URL")
            or ("https://api.anthropic.com" if anthropic else "https://api.openai.com")
        ).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def user(self, blocks):
        if self.protocol == "anthropic_messages":
            return {"role": "user", "content": blocks}
        parts = []
        for b in blocks:
            if b["type"] == "text":
                parts.append(
                    {
                        "type": "input_text"
                        if self.protocol == "openai_responses"
                        else "text",
                        "text": b["text"],
                    }
                )
            else:
                s = b["source"]
                url = f"data:{s['media_type']};base64,{s['data']}"
                parts.append(
                    {"type": "input_image", "image_url": url}
                    if self.protocol == "openai_responses"
                    else {"type": "image_url", "image_url": {"url": url}}
                )
        return {"role": "user", "content": parts}

    def request(
        self,
        system,
        messages,
        *,
        tools_enabled,
        native,
        max_tokens,
        temperature,
        tools=None,
    ):
        has_tool_history = any(
            m.get("tool_calls")
            or m.get("type") in {"function_call", "function_call_output"}
            or (
                isinstance(m.get("content"), list)
                and any(b.get("type") in {"tool_use", "tool_result"} for b in m["content"])
            )
            for m in messages
        )
        if tools is None:
            tools = [FRAME_TOOL] if tools_enabled or has_tool_history else []
        send_tools = native and bool(tools)
        key = (
            os.getenv("PACKY_API_KEY")
            or os.getenv(
                "ANTHROPIC_API_KEY"
                if self.protocol == "anthropic_messages"
                else "OPENAI_API_KEY"
            )
            or os.getenv("ANTHROPIC_AUTH_TOKEN")
        )
        if not key:
            raise RuntimeError(
                "Set PACKY_API_KEY or the selected provider's API key in the environment."
            )
        headers = {"content-type": "application/json"}
        payload = {"model": self.model}
        if self.protocol == "anthropic_messages":
            path = "messages"
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            payload.update(
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if self.thinking_summary:
                payload["thinking"] = {"type": "adaptive", "display": "summarized"}
                # Adaptive thinking controls its own sampling configuration.
                payload.pop("temperature")
            if send_tools:
                payload["tools"] = [
                    {
                        "name": tool["name"],
                        "description": tool["description"],
                        "input_schema": tool["parameters"],
                    }
                    for tool in tools
                ]
                if not tools_enabled and not any(
                    tool["name"] != FRAME_TOOL["name"] for tool in tools
                ):
                    payload["tool_choice"] = {"type": "none"}
        else:
            headers["Authorization"] = f"Bearer {key}"
            if self.protocol == "openai_responses":
                path = "responses"
                payload.update(
                    instructions=system,
                    input=messages,
                    max_output_tokens=max_tokens,
                    store=False,
                    include=["reasoning.encrypted_content"],
                )
                if send_tools:
                    payload["tools"] = [
                        {
                            "type": "function",
                            "name": tool["name"],
                            "description": tool["description"],
                            "parameters": tool["parameters"],
                            "strict": False,
                        }
                        for tool in tools
                    ]
                    if not tools_enabled and not any(
                        tool["name"] != FRAME_TOOL["name"] for tool in tools
                    ):
                        payload["tool_choice"] = "none"
            else:
                path = "chat/completions"
                payload.update(
                    messages=[{"role": "system", "content": system}] + messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                if send_tools:
                    payload["tools"] = [
                        {
                            "type": "function",
                            "function": {
                                "name": tool["name"],
                                "description": tool["description"],
                                "parameters": tool["parameters"],
                            },
                        }
                        for tool in tools
                    ]
                    if not tools_enabled and not any(
                        tool["name"] != FRAME_TOOL["name"] for tool in tools
                    ):
                        payload["tool_choice"] = "none"
        url = self.base_url + ("/" if self.base_url.endswith("/v1") else "/v1/") + path
        response = self.session.post(
            url, headers=headers, json=payload, timeout=self.timeout
        )
        if response.status_code != 200:
            detail = ""
            try:
                error = response.json().get("error", {})
                if isinstance(error, dict):
                    detail = str(error.get("message", error.get("type", ""))).replace(
                        key, "[REDACTED]"
                    )[:300]
            except ValueError:
                pass
            raise RuntimeError(
                f"Model API HTTP {response.status_code} ({self.protocol}): {detail or 'request failed'}"
            )
        return response.json()

    def unpack(self, response):
        calls = []
        if self.protocol == "anthropic_messages":
            blocks = response.get("content", [])
            text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
            calls = [
                {"id": b["id"], "name": b["name"], "arguments": b["input"]}
                for b in blocks
                if b.get("type") == "tool_use"
            ]
            assistant = [{"role": "assistant", "content": blocks}]
        elif self.protocol == "openai_responses":
            assistant = response.get("output", [])
            text = "\n".join(
                b["text"]
                for item in assistant
                if item.get("type") == "message"
                for b in item.get("content", [])
                if b.get("type") == "output_text"
            )
            calls = [
                {"id": i["call_id"], "name": i["name"], "arguments": i["arguments"]}
                for i in assistant
                if i.get("type") == "function_call"
            ]
        else:
            message = response["choices"][0]["message"]
            text = message.get("content") or ""
            calls = [
                {
                    "id": c["id"],
                    "name": c["function"]["name"],
                    "arguments": c["function"]["arguments"],
                }
                for c in message.get("tool_calls", [])
            ]
            assistant = [message]
        return text, calls, assistant

    def tool_results(self, results):
        if self.protocol == "anthropic_messages":
            return [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": result_blocks(result),
                        }
                        for call, result in results
                    ],
                }
            ]
        if self.protocol == "openai_responses":
            return [
                {
                    "type": "function_call_output",
                    "call_id": call["id"],
                    "output": self.user(result_blocks(result))["content"],
                }
                for call, result in results
            ]
        messages, images = [], []
        for call, result in results:
            blocks = result_blocks(result)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "\n".join(
                        b["text"] for b in blocks if b["type"] == "text"
                    ),
                }
            )
            if any(b["type"] == "image" for b in blocks):
                images.extend(
                    [text_block(f"Images for tool_call_id={call['id']}")] + blocks
                )
        # Every pending call must have a tool reply before the user image message.
        if images:
            messages.append(self.user(images))
        return messages


class RealtimeAgent:
    def __init__(
        self,
        *,
        variant="agent1",
        model="claude-sonnet-5",
        api_format="auto",
        api_base_url=None,
        max_tokens=128000,
        temperature=1.0,
        max_trajectory_length=3,
        pause=0.0,
        max_sequence_actions=100,
        max_frame_queries=0,
        tool_format="native",
        thinking_summary=False,
        system_prompt_text=None,
        wire=None,
    ):
        self.mode = MODES[variant]
        self.variant = variant
        self.max_tokens, self.temperature = max_tokens, temperature
        if max_trajectory_length is not None and max_trajectory_length < 0:
            raise ValueError("max_trajectory_length must be nonnegative or None")
        self.history_length = max_trajectory_length
        if max_frame_queries < 0:
            raise ValueError("max_frame_queries must be nonnegative (0 means unlimited)")
        self.max_actions, self.max_queries = max_sequence_actions, max_frame_queries
        self.tool_format = tool_format
        self.wire = wire or ModelWire(
            model, api_format, api_base_url, thinking_summary=thinking_summary
        )
        self.system = system_prompt_text or system_prompt(
            self.mode, pause, self.max_actions, self.max_queries
        )
        if self.mode.frames and tool_format == "json":
            self.system += (
                '\nTo query instead of acting, return {"tool_call":{"tool_name":"get_frames","times_s":[3.0]}}.\n'
                + json.dumps(FRAME_TOOL)
            )
        self.frame_query = None
        self.event_sink = None
        self.reset()

    def reset(self, *args, **kwargs):
        self.rounds = []
        self.observations = []
        self.last_events = []
        self.pending_action_calls = None
        self.active_round_messages = None
        self.counters = {
            "model_requests": 0,
            "tool_calls": 0,
            "frame_queries": 0,
            "images_returned": 0,
        }

    def bind_frame_query(self, callback):
        self.frame_query = callback

    def bind_event_sink(self, callback):
        self.event_sink = callback

    def _history_rounds(self):
        if self.history_length is None:
            return self.rounds
        return self.rounds[-self.history_length:] if self.history_length else []

    def _retain_history(self):
        if self.history_length is None:
            return
        if len(self.rounds) > self.history_length:
            self.rounds = self.rounds[-self.history_length:] if self.history_length else []
        self.observations = (
            self.observations[-self.history_length:]
            if self.history_length
            else []
        )

    def emit(self, event):
        self.last_events.append(event)
        if self.event_sink is not None:
            self.event_sink(event)

    def record_action_result(self, actions, *, reward=0, done=False, info=None, error=None):
        """Close the previous assistant action calls with environment results."""
        calls = self.pending_action_calls
        if not calls:
            raise RuntimeError("No pending native action calls to close.")
        if len(calls) != len(actions):
            raise ValueError("Environment returned a different number of action results.")
        results = []
        for index, (call, action) in enumerate(zip(calls, actions)):
            result = {
                "action_type": action["action_type"],
                "executed": error is None,
                "reward": reward,
                "done": bool(done),
                "last_in_decision": index == len(actions) - 1,
            }
            if info is not None:
                result["info"] = info
            if error is not None:
                result["error"] = error
            results.append((call, result))
        if self.active_round_messages is None:
            raise RuntimeError("The pending action round is no longer available.")
        self.active_round_messages.extend(self.wire.tool_results(results))
        self.emit({"event": "action_tool_result", "calls": results})
        self.pending_action_calls = None
        self.active_round_messages = None

    def _decode_action_calls(self, calls, obs):
        max_allowed = min(
            self.max_actions,
            obs.get("remaining_actions", self.max_actions),
        )
        if not self.mode.sequence:
            max_allowed = 1
        if len(calls) > max_allowed:
            raise ValueError("Invalid action count for this agent group.")
        actions = []
        for call in calls:
            action_type = ACTION_TOOL_TYPES.get(call["name"])
            if action_type is None:
                raise ValueError("Unknown action tool.")
            arguments = call.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Action tool arguments are not valid JSON.") from exc
            if arguments is None:
                arguments = {}
            action = {"action_type": action_type, "parameters": arguments}
            validate_action(action)
            actions.append(action)
        if any(action["action_type"] == "DONE" for action in actions[:-1]):
            raise ValueError("DONE must be the last action.")
        return actions

    def predict(self, instruction, obs):
        self.last_events = []
        observation = {
            "screenshot_file": obs.get("screenshot_file"),
            "task_time_s": obs["task_time_s"],
            "sha256": hashlib.sha256(obs["screenshot"]).hexdigest(),
        }
        blocks = [
            text_block(
                f"Task: {instruction}\nScreenshot task time: {obs['task_time_s']:.6f} seconds. "
                "This timestamp describes the screenshot, not the time your reply will execute. "
                f"Remaining action budget: {obs.get('remaining_actions', self.max_actions)}."
            )
        ]
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(obs["screenshot"]).decode("ascii"),
                },
            }
        )
        round_messages = [self.wire.user(blocks)]
        history = [
            m
            for r in self._history_rounds()
            for m in r
        ]
        queries, errors, requests_in_decision = 0, 0, 0
        while True:
            # Preserve the legacy finite mode only when explicitly requested.
            if self.max_queries and requests_in_decision >= self.max_queries + 4:
                raise RuntimeError("Model request budget exhausted before an action was produced.")
            requests_in_decision += 1
            start = time.monotonic()
            self.counters["model_requests"] += 1
            request_id = self.counters["model_requests"]
            tools_enabled = self.mode.frames and (
                not self.max_queries or queries < self.max_queries
            )
            request_tools = []
            if self.tool_format == "native":
                request_tools.extend(ACTION_TOOLS)
                if tools_enabled:
                    request_tools.insert(0, FRAME_TOOL)
            self.emit({
                "event": "model_request", "request_id": request_id,
                "observation": observation, "history_observations": list(self.observations),
                "tools_enabled": tools_enabled,
                "protocol": self.wire.protocol,
                "model": self.wire.model,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "tools": [tool["name"] for tool in request_tools],
                    "request_messages": loggable_messages(history + round_messages),
            })
            try:
                raw = self.wire.request(
                    self.system,
                    history + round_messages,
                    tools_enabled=tools_enabled,
                    native=self.tool_format == "native",
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    tools=request_tools,
                )
            except Exception as exc:
                self.emit({
                    "event": "model_error", "request_id": request_id,
                    "latency_s": time.monotonic() - start, "error_type": type(exc).__name__,
                    "message": str(exc),
                })
                raise
            # Preserve provider fields before parsing, including thinking/summary
            # blocks, signatures and stop reasons. No request headers are logged.
            event = {
                "event": "model_response", "request_id": request_id,
                "latency_s": time.monotonic() - start,
                "provider_response": copy.deepcopy(raw),
                **response_reasoning(raw, self.wire.protocol),
            }
            try:
                text, calls, assistant = self.wire.unpack(raw)
            except Exception:
                self.emit(event)
                raise
            round_messages.extend(assistant)
            self.emit(
                {
                    **event,
                    "text": text,
                    "calls": copy.deepcopy(calls),
                    "usage": raw.get("usage", {}),
                }
            )
            structured = False
            if not calls and self.tool_format != "native":
                try:
                    value = load_output(text)
                    if isinstance(value, dict) and "tool_call" in value:
                        tool = value["tool_call"]
                        calls = [
                            {
                                "id": f"json_{queries}",
                                "name": tool["tool_name"],
                                "arguments": {
                                    k: v for k, v in tool.items() if k != "tool_name"
                                },
                            }
                        ]
                        structured = True
                except (ValueError, KeyError, TypeError):
                    pass
            if calls:
                frame_calls = [call for call in calls if call["name"] == FRAME_TOOL["name"]]
                action_calls = [call for call in calls if call["name"] != FRAME_TOOL["name"]]
                if frame_calls and action_calls:
                    raise ValueError("A model response cannot mix frame queries and actions.")
                if action_calls:
                    if self.tool_format != "native":
                        raise ValueError("Native action tools are required for action calls.")
                    self.counters["tool_calls"] += len(action_calls)
                    try:
                        actions = self._decode_action_calls(action_calls, obs)
                    except ValueError as exc:
                        errors += 1
                        self.emit(
                            {
                                "event": "format_error",
                                "request_id": request_id,
                                "message": str(exc),
                                "correction": errors,
                                "will_retry": errors <= 2,
                            }
                        )
                        if errors > 2:
                            raise
                        round_messages.append(
                            self.wire.user(
                                [text_block(f"Invalid tool call: {exc}. Correct it; no action was executed.")]
                            )
                        )
                        continue
                    self.rounds.append(round_messages)
                    self.observations.append(observation)
                    self.pending_action_calls = action_calls
                    self.active_round_messages = round_messages
                    self._retain_history()
                    return text, actions
                if not frame_calls:
                    raise ValueError("Unknown native tool call.")
                if not self.mode.frames:
                    raise ValueError("This agent group has no frame query tool.")
                results = []
                for call in frame_calls:
                    self.counters["tool_calls"] += 1
                    started = time.monotonic()
                    try:
                        if self.max_queries and queries >= self.max_queries:
                            raise ValueError(
                                "Frame query budget exhausted. Return actions now."
                            )
                        queries += 1
                        if call["name"] != "get_frames":
                            raise ValueError(
                                "Unknown tool; only get_frames is available."
                            )
                        args = (
                            GetFramesArgs.model_validate_json(call["arguments"])
                            if isinstance(call["arguments"], str)
                            else GetFramesArgs.model_validate(call["arguments"])
                        )
                        if self.frame_query is None:
                            raise RuntimeError("Frame query callback is not connected.")
                        self.counters["frame_queries"] += 1
                        result = self.frame_query(args.times_s)
                    except (ValueError, RuntimeError, requests.RequestException) as exc:
                        result = {"status": "error", "message": str(exc)}
                    self.counters["images_returned"] += sum(
                        bool(f.get("image")) for f in result.get("frames", [])
                    )
                    self.emit(
                        {
                            "event": "tool_result",
                            "request_id": request_id,
                            "call_id": call["id"],
                            "latency_s": time.monotonic() - started,
                            "result": {
                                **result,
                                "frames": [
                                    {k: v for k, v in f.items() if k != "image"}
                                    for f in result.get("frames", [])
                                ],
                            },
                        }
                    )
                    results.append((call, result))
                if structured:
                    for call, result in results:
                        round_messages.append(
                            self.wire.user(
                                [text_block(f"Result of {call['name']}")]
                                + result_blocks(result)
                            )
                        )
                else:
                    round_messages.extend(self.wire.tool_results(results))
                continue
            if self.tool_format == "native":
                errors += 1
                message = "Native tool use is required; return a computer action tool call."
                self.emit(
                    {
                        "event": "format_error",
                        "request_id": request_id,
                        "message": message,
                        "correction": errors,
                        "will_retry": errors <= 2,
                    }
                )
                if errors > 2:
                    raise ValueError(message)
                round_messages.append(self.wire.user([text_block(message)]))
                continue
            try:
                actions = parse_actions(
                    text,
                    sequence=self.mode.sequence,
                    max_actions=min(
                        self.max_actions, obs.get("remaining_actions", self.max_actions)
                    ),
                )
            except ValueError as exc:
                errors += 1
                self.emit({
                    "event": "format_error", "request_id": request_id,
                    "message": str(exc), "correction": errors, "will_retry": errors <= 2,
                })
                if errors > 2:
                    raise
                round_messages.append(
                    self.wire.user(
                        [
                            text_block(
                                f"Invalid output: {exc}. Correct it; no action was executed."
                            )
                        ]
                    )
                )
                continue
            self.rounds.append(round_messages)
            self.observations.append(observation)
            self._retain_history()
            return text, actions
