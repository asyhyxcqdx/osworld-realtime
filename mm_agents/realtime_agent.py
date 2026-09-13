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
    FRAME_TOOL,
    GetFramesArgs,
    MODES,
    load_output,
    parse_actions,
    system_prompt,
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
        self, system, messages, *, tools_enabled, native, max_tokens, temperature
    ):
        # Keep definitions for prior tool messages after the query budget is used.
        has_tool_history = any(
            m.get("tool_calls")
            or m.get("type") in {"function_call", "function_call_output"}
            or (
                isinstance(m.get("content"), list)
                and any(b.get("type") == "tool_use" for b in m["content"])
            )
            for m in messages
        )
        send_tools = native and (tools_enabled or has_tool_history)
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
                        "name": FRAME_TOOL["name"],
                        "description": FRAME_TOOL["description"],
                        "input_schema": FRAME_TOOL["parameters"],
                    }
                ]
                if not tools_enabled:
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
                        {"type": "function", **FRAME_TOOL, "strict": False}
                    ]
                    if not tools_enabled:
                        payload["tool_choice"] = "none"
            else:
                path = "chat/completions"
                payload.update(
                    messages=[{"role": "system", "content": system}] + messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                if send_tools:
                    payload["tools"] = [{"type": "function", "function": FRAME_TOOL}]
                    if not tools_enabled:
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
        model="claude-sonnet-4-6",
        api_format="auto",
        api_base_url=None,
        max_tokens=1500,
        temperature=1.0,
        max_trajectory_length=3,
        pause=0.0,
        max_sequence_actions=16,
        max_frame_queries=0,
        tool_format="native",
        thinking_summary=False,
        wire=None,
    ):
        self.mode = MODES[variant]
        self.variant = variant
        self.max_tokens, self.temperature = max_tokens, temperature
        self.history_length = max_trajectory_length
        if max_frame_queries < 0:
            raise ValueError("max_frame_queries must be nonnegative (0 means unlimited)")
        self.max_actions, self.max_queries = max_sequence_actions, max_frame_queries
        self.tool_format = tool_format
        self.wire = wire or ModelWire(
            model, api_format, api_base_url, thinking_summary=thinking_summary
        )
        self.system = system_prompt(
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

    def emit(self, event):
        self.last_events.append(event)
        if self.event_sink is not None:
            self.event_sink(event)

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
            for r in (
                self.rounds[-self.history_length :] if self.history_length else []
            )
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
            tools_enabled = self.mode.frames and (not self.max_queries or queries < self.max_queries)
            self.emit({
                "event": "model_request", "request_id": request_id,
                "observation": observation, "history_observations": list(self.observations),
                "tools_enabled": tools_enabled,
            })
            try:
                raw = self.wire.request(
                    self.system,
                    history + round_messages,
                    tools_enabled=tools_enabled,
                    native=self.tool_format == "native",
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
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
            if not calls:
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
                if not self.mode.frames:
                    raise ValueError(
                        "This agent group has no tools; refusing an unexpected tool call."
                    )
                results = []
                for call in calls:
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
            if len(self.rounds) > self.history_length:
                self.rounds = (
                    self.rounds[-self.history_length :] if self.history_length else []
                )
            self.observations = self.observations[-self.history_length:] if self.history_length else []
            return text, actions
