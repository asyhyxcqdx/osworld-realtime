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

from mm_agents.realtime_config import validate_key_env, validate_thinking
from mm_agents.realtime_coordinates import CoordinateAdapter
from mm_agents.realtime_stream import IncompleteStreamError, collect_stream

from mm_agents.realtime_protocol import (
    ACTION_TOOLS,
    ACTION_TOOL_TYPES,
    FRAME_TOOL,
    GetFramesArgs,
    AgentProtocolError,
    ForbiddenShortcutError,
    validate_action,
)


RETRY_STATUS = {402, 429, 500, 502, 503, 504}
RETRY_ATTEMPTS = 3
# 402 is the gateway's upstream channel temporarily having no balance. The gateway
# balances across channels, so a retry often lands on a healthy one; give it more
# attempts than a plain server error, with the same exponential backoff.
CHANNEL_BALANCE_STATUS = 402
CHANNEL_BALANCE_ATTEMPTS = 5


def text_block(text):
    return {"type": "text", "text": text}


def rounded_time_fields(value, key=""):
    """Round model-facing time metadata without changing execution or raw logs."""
    if isinstance(value, dict):
        return {name: rounded_time_fields(item, name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [rounded_time_fields(item, key) for item in value]
    if type(value) is float and key.endswith(("_s", "_ms")):
        return round(value, 3)
    return value


def result_blocks(result):
    blocks = []
    if "task_time_s" in result:
        blocks.append(
            text_block(json.dumps(rounded_time_fields({"query_completed_time_s": result["task_time_s"]})))
        )
    for frame in result.get("frames", [result]):
        metadata = {k: v for k, v in frame.items() if k != "image"}
        blocks.append(text_block(json.dumps(rounded_time_fields(metadata), ensure_ascii=False)))
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
        _scrub_inline_images(message)
    return logged


def _scrub_inline_images(node):
    """Replace inline image bytes wherever the protocol put them.

    A message's own ``content`` blocks used to be the only place inspected, which
    misses the frames that come back as tool results: anthropic nests them at
    ``content[i]["content"][j]["source"]["data"]`` and the responses protocol
    puts them at ``output[i]["image_url"]``. Walk the whole copy instead; the
    three shapes below are the only rewritten cases, and nothing but image bytes
    is touched.
    """
    if isinstance(node, list):
        for item in node:
            _scrub_inline_images(item)
        return
    if not isinstance(node, dict):
        return
    source = node.get("source")
    if isinstance(source, dict) and isinstance(source.get("data"), str):
        data = source.pop("data")
        source["data_sha256"] = hashlib.sha256(data.encode("ascii")).hexdigest()
        source["data_length"] = len(data)
    image_url = node.get("image_url")
    if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
        url = image_url["url"]
        if url.startswith("data:"):
            image_url["url"] = "data:image/png;base64,[omitted]"
            image_url["url_sha256"] = hashlib.sha256(url.encode("ascii")).hexdigest()
            image_url["url_length"] = len(url)
    elif isinstance(image_url, str) and image_url.startswith("data:"):
        node["image_url"] = "data:image/png;base64,[omitted]"
        node["image_url_sha256"] = hashlib.sha256(image_url.encode("ascii")).hexdigest()
        node["image_url_length"] = len(image_url)
    for value in node.values():
        _scrub_inline_images(value)


def completed_responses_stream(response):
    """Collect a complete Responses SSE turn before dispatching any actions."""
    try:
        return collect_stream(response, "openai_responses")
    finally:
        response.close()


class ModelWire:
    def __init__(
        self,
        model,
        api_format="auto",
        base_url=None,
        timeout=120,
        thinking_summary=False,
        thinking_enabled=None,
        thinking_effort=None,
        api_key_env=None,
    ):
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
        if thinking_enabled is None:
            thinking_enabled = thinking_summary
        if thinking_summary and not thinking_enabled:
            raise ValueError("thinking_summary requires thinking_enabled")
        validate_thinking(model, self.protocol, thinking_enabled, thinking_effort)
        validate_key_env(api_key_env)
        self.api_key_env = api_key_env
        self.thinking_enabled = bool(thinking_enabled)
        self.thinking_summary = thinking_summary
        self.thinking_effort = thinking_effort
        anthropic = self.protocol == "anthropic_messages"
        self.base_url = (
            base_url
            or os.getenv("PACKY_API_BASE_URL")
            or os.getenv("ANTHROPIC_BASE_URL" if anthropic else "OPENAI_BASE_URL")
            or ("https://api.anthropic.com" if anthropic else "https://api.openai.com")
        ).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.last_response_streamed = None

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
        parallel_tool_calls=None,
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
        key_name = self.api_key_env or "PACKY_API_KEY"
        key = os.getenv(key_name)
        if not key and self.api_key_env:
            raise RuntimeError(f"Set the configured API key environment variable {self.api_key_env}.")
        if not key:
            key_name = (
                "ANTHROPIC_API_KEY"
                if self.protocol == "anthropic_messages"
                else "OPENAI_API_KEY"
            )
            key = os.getenv(key_name)
        if not key and self.protocol == "anthropic_messages":
            key_name = "ANTHROPIC_AUTH_TOKEN"
            key = os.getenv(key_name)
        if not key:
            raise RuntimeError(
                "Set PACKY_API_KEY or the selected provider's API key in the environment."
            )
        headers = {"content-type": "application/json"}
        payload = {"model": self.model, "stream": True}
        if self.protocol == "anthropic_messages":
            path = "messages"
            if key_name == "ANTHROPIC_AUTH_TOKEN":
                headers["Authorization"] = f"Bearer {key}"
            else:
                headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
            payload.update(
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if self.thinking_enabled:
                payload["thinking"] = {"type": "adaptive"}
                if self.thinking_summary and self.model != "MiniMax-M3":
                    payload["thinking"]["display"] = "summarized"
                if self.thinking_effort:
                    payload["output_config"] = {"effort": self.thinking_effort}
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
                elif parallel_tool_calls is False:
                    payload["tool_choice"] = {
                        "type": "auto", "disable_parallel_tool_use": True,
                    }
        else:
            headers["Authorization"] = f"Bearer {key}"
            if self.protocol == "openai_responses":
                path = "responses"
                payload.update(
                    instructions=system,
                    input=messages,
                    max_output_tokens=max_tokens,
                    store=False,
                    stream=True,
                    include=["reasoning.encrypted_content"],
                )
                if parallel_tool_calls is not None:
                    payload["parallel_tool_calls"] = bool(parallel_tool_calls)
                if self.thinking_enabled:
                    payload["reasoning"] = {}
                    if self.thinking_effort:
                        payload["reasoning"]["effort"] = self.thinking_effort
                    if self.thinking_summary:
                        payload["reasoning"]["summary"] = "auto"
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
                    stream_options={"include_usage": True},
                )
                if self.thinking_enabled:
                    # Gemini's documented OpenAI-compatible extension. Do not also
                    # send reasoning_effort: the two controls overlap.
                    thinking_config = {
                        "include_thoughts": self.thinking_summary,
                    }
                    if self.thinking_effort:
                        thinking_config["thinking_level"] = self.thinking_effort
                    payload["extra_body"] = {"google": {"thinking_config": thinking_config}}
                    payload.pop("temperature")
                if parallel_tool_calls is not None:
                    payload["parallel_tool_calls"] = bool(parallel_tool_calls)
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
        self.last_response_streamed = None
        last_error = None
        attempt = 0
        attempts_allowed = RETRY_ATTEMPTS
        while True:
            response = None
            status_code = None
            try:
                response = self.session.post(
                    url, headers=headers, json=payload, timeout=self.timeout,
                    stream=True,
                )
                status_code = response.status_code
                if status_code == 200:
                    self.last_response_streamed = "text/event-stream" in getattr(response, "headers", {}).get("content-type", "").lower()
                    if self.last_response_streamed:
                        return collect_stream(response, self.protocol)
                    return response.json()
                detail = ""
                try:
                    error = response.json().get("error", {})
                    if isinstance(error, dict):
                        detail = str(error.get("message", error.get("type", ""))).replace(key, "[REDACTED]")[:300]
                except ValueError:
                    pass
                last_error = RuntimeError(
                    f"Model API HTTP {status_code} ({self.protocol}): {detail or 'request failed'}"
                )
                if status_code not in RETRY_STATUS:
                    raise last_error
                if status_code == CHANNEL_BALANCE_STATUS:
                    attempts_allowed = max(attempts_allowed, CHANNEL_BALANCE_ATTEMPTS)
            except (requests.RequestException, IncompleteStreamError) as exc:
                last_error = exc
            finally:
                if response is not None and callable(getattr(response, "close", None)):
                    response.close()
            attempt += 1
            if attempt >= attempts_allowed:
                break
            time.sleep(2 ** (attempt - 1))
        raise last_error

    def unpack(self, response):
        stop_reason = response.get("stop_reason")
        if self.protocol == "openai_chat":
            stop_reason = response.get("choices", [{}])[0].get("finish_reason")
        if stop_reason in {"max_tokens", "length"} or response.get("status") == "incomplete":
            raise RuntimeError("Model response hit its output limit or is incomplete; no actions dispatched.")
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
            has_images = any(b["type"] == "image" for b in blocks)
            # Chat carries images in a user message. Keep frame metadata beside
            # those images instead of repeating it in the text-only tool reply.
            content = (
                "Frame results and images are in the following user message "
                f"labeled Images for tool_call_id={call['id']}."
                if has_images
                else "\n".join(b["text"] for b in blocks if b["type"] == "text")
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": content,
                }
            )
            if has_images:
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
        sequence=None,
        frames=None,
        model="claude-fable-5",
        api_format="auto",
        api_base_url=None,
        api_key_env=None,
        max_tokens=128000,
        temperature=1.0,
        max_trajectory_length=3,
        max_sequence_actions=100,
        max_frame_queries=0,
        tool_format="native",
        thinking_enabled=None,
        thinking_effort=None,
        thinking_summary=False,
        system_prompt_text,
        user_prompt_text,
        coordinate_system="native_pixels",
        wire=None,
    ):
        if sequence is None or frames is None:
            raise ValueError("sequence and frames must come from a validated Agent config")
        if tool_format != "native":
            raise ValueError("Realtime Agents require native tool use")
        if not isinstance(system_prompt_text, str) or not system_prompt_text.strip():
            raise ValueError("system_prompt_text must be the non-empty Agent system prompt")
        if not isinstance(user_prompt_text, str) or not user_prompt_text.strip():
            raise ValueError("user_prompt_text must be the non-empty Agent user prompt")
        self.user_prompt = user_prompt_text.strip()
        self.variant = variant
        self.sequence = bool(sequence)
        self.frames = bool(frames)
        self.max_tokens, self.temperature = max_tokens, temperature
        if max_trajectory_length is not None and max_trajectory_length < 0:
            raise ValueError("max_trajectory_length must be nonnegative or None")
        self.history_length = max_trajectory_length
        if max_frame_queries < 0:
            raise ValueError("max_frame_queries must be nonnegative (0 means unlimited)")
        self.max_actions, self.max_queries = max_sequence_actions, max_frame_queries
        self.tool_format = tool_format
        self.coordinates = CoordinateAdapter(coordinate_system)
        self.coordinate_system = self.coordinates.name
        self.action_tools = self.coordinates.action_tools(ACTION_TOOLS)
        self.wire = wire or ModelWire(
            model,
            api_format,
            api_base_url,
            thinking_summary=thinking_summary,
            thinking_enabled=thinking_enabled,
            thinking_effort=thinking_effort,
            api_key_env=api_key_env,
        )
        self.system = system_prompt_text + "\n" + self.coordinates.guidance
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
        execution_records = (info or {}).get("sequence_actions", [])
        results = []
        for index, (call, action) in enumerate(zip(calls, actions)):
            result = {
                "action_type": action["action_type"],
                "executed": error is None,
                "reward": reward,
                "done": bool(done),
                "last_in_decision": index == len(actions) - 1,
            }
            if index < len(execution_records):
                # The runner retains the complete VM receipt in action_executed.
                # Each model-facing result needs only this call's measured times;
                # its original arguments already remain in assistant history.
                record = execution_records[index]
                for field in ("started_s", "finished_s", "duration_s"):
                    if field in record:
                        result[field] = record[field]
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
        # ``max_actions`` limits actions within one decision. The overall
        # max_steps budget is enforced by the runner in decision rounds.
        max_allowed = self.max_actions
        if not self.sequence:
            max_allowed = 1
        if len(calls) > max_allowed:
            raise AgentProtocolError("Invalid action count for this agent group.")
        actions = []
        for call in calls:
            action_type = ACTION_TOOL_TYPES.get(call["name"])
            if action_type is None:
                raise AgentProtocolError("Unknown action tool.")
            arguments = call.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError) as exc:
                    raise AgentProtocolError("Action tool arguments are not valid JSON.") from exc
            if arguments is None:
                arguments = {}
            action = {"action_type": action_type, "parameters": arguments}
            try:
                action = self.coordinates.to_native_action(action)
            except ValueError as exc:
                raise AgentProtocolError(str(exc)) from exc
            try:
                validate_action(action)
            except ForbiddenShortcutError:
                self.emit({
                    "event": "action_rejected",
                    "reason": "forbidden_browser_shortcut",
                    "action": action,
                })
                raise
            actions.append(action)
        if any(action["action_type"] == "DONE" for action in actions[:-1]):
            raise AgentProtocolError("DONE must be the last action.")
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
                f"Screenshot task time: {obs['task_time_s']:.3f} seconds. "
                "This timestamp describes the screenshot, not the time your reply will execute."
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
        # Keep one task message at the start of the conversation, separate from
        # observation rounds so even an explicitly bounded history retains it.
        # Its text is the configured user prompt; ``instruction`` stays in the
        # signature because the shared runner passes it to every Agent, but the
        # environment state decides success, so nothing per-run is injected.
        history = [self.wire.user([text_block(self.user_prompt)])]
        history.extend(
            m
            for r in self._history_rounds()
            for m in r
        )
        queries, errors, requests_in_decision = 0, 0, 0
        while True:
            # Preserve the legacy finite mode only when explicitly requested.
            if self.max_queries and requests_in_decision >= self.max_queries + 4:
                raise RuntimeError("Model request budget exhausted before an action was produced.")
            requests_in_decision += 1
            start = time.monotonic()
            self.counters["model_requests"] += 1
            request_id = self.counters["model_requests"]
            tools_enabled = self.frames and (
                not self.max_queries or queries < self.max_queries
            )
            request_tools = []
            if self.tool_format == "native":
                request_tools.extend(self.action_tools)
                if tools_enabled:
                    request_tools.insert(0, FRAME_TOOL)
            self.emit({
                "event": "model_request", "request_id": request_id,
                "observation": observation, "history_observations": list(self.observations),
                "tools_enabled": tools_enabled,
                "protocol": self.wire.protocol,
                "coordinate_system": self.coordinate_system,
                "stream_requested": True,
                    "model": self.wire.model,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "thinking": {
                        "enabled": self.wire.thinking_enabled,
                        "effort": self.wire.thinking_effort,
                        "summary": self.wire.thinking_summary,
                    },
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
                    parallel_tool_calls=self.sequence,
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
                "stream_received": self.wire.last_response_streamed,
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
            if calls:
                frame_calls = [call for call in calls if call["name"] == FRAME_TOOL["name"]]
                action_calls = [call for call in calls if call["name"] != FRAME_TOOL["name"]]
                mixed_calls = bool(frame_calls and action_calls)
                too_many_frame_calls = not self.sequence and len(frame_calls) > 1
                if mixed_calls or too_many_frame_calls:
                    errors += 1
                    message = (
                        "Do not mix get_frames with action tools in one response. "
                        "No actions were executed. Query historical frames first, "
                        "then submit a separate response containing only actions."
                    ) if mixed_calls else (
                        "This atomic agent allows only one get_frames call per response. "
                        "No frame queries or actions were executed. Submit one get_frames "
                        "call and wait for its result before querying again or acting."
                    )
                    self.emit({
                        "event": "format_error",
                        "request_id": request_id,
                        "message": message,
                        "correction": errors,
                        "will_retry": errors <= 2,
                    })
                    if errors > 2:
                        raise AgentProtocolError(message)
                    # Close every native call before asking for a corrected response.
                    # In particular, never execute a prefix of the action calls.
                    rejected = [
                        (call, {
                            "status": "error",
                            "executed": False,
                            "message": message,
                        })
                        for call in calls
                    ]
                    round_messages.extend(self.wire.tool_results(rejected))
                    round_messages.append(self.wire.user([text_block(message)]))
                    continue
                if action_calls:
                    self.counters["tool_calls"] += len(action_calls)
                    try:
                        actions = self._decode_action_calls(action_calls, obs)
                    except ValueError as exc:
                        errors += 1
                        # The exception text is already a sentence; drop its final
                        # period so embedding it does not produce "forbidden..".
                        detail = str(exc).strip()
                        if detail.endswith('.'):
                            detail = detail[:-1]
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
                        # Rejected calls still need matching native tool results
                        # before the provider can accept a corrected response.
                        round_messages.extend(self.wire.tool_results([
                            (call, {
                                "status": "error", "executed": False,
                                "message": f"Invalid tool call: {detail}. No action in this sequence was executed.",
                            })
                            for call in action_calls
                        ]))
                        round_messages.append(
                            self.wire.user(
                                [text_block(f"Invalid tool call: {detail}. Correct it; no action was executed.")]
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
                    raise AgentProtocolError("Unknown native tool call.")
                if not self.frames:
                    raise AgentProtocolError("This agent group has no frame query tool.")
                results = []
                for call in frame_calls:
                    self.counters["tool_calls"] += 1
                    started = time.monotonic()
                    try:
                        if self.max_queries and queries >= self.max_queries:
                            raise AgentProtocolError(
                                "Frame query budget exhausted. Return actions now."
                            )
                        queries += 1
                        if call["name"] != "get_frames":
                            raise AgentProtocolError(
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
                round_messages.extend(self.wire.tool_results(results))
                continue
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
                raise AgentProtocolError(message)
            round_messages.append(self.wire.user([text_block(message)]))
