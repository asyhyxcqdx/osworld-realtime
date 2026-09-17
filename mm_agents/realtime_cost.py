"""Token and cost accounting shared by the batch runner and the result exporter.

Two provider families report usage differently, and getting this wrong silently
mis-prices a run:

* Anthropic Messages reports ``input_tokens`` **excluding** cache reads and cache
  writes, so the prompt total is ``input_tokens + cache_read + cache_creation``;
  only ``cache_read_input_tokens`` gets the discounted rate.
* OpenAI Chat / Responses already include cached tokens in
  ``prompt_tokens`` / ``input_tokens``, with the cached part in
  ``*_tokens_details.cached_tokens``.
"""
from __future__ import annotations

import json
from pathlib import Path


def usage_tokens(usage):
    """Prompt/completion/cached token counts from one recorded model response."""
    usage = usage or {}
    prompt = usage.get('prompt_tokens')
    if prompt is None:
        prompt = usage.get('input_tokens')
    completion = usage.get('completion_tokens')
    if completion is None:
        completion = usage.get('output_tokens')
    prompt = int(prompt or 0)
    completion = int(completion or 0)

    if 'cache_read_input_tokens' in usage or 'cache_creation_input_tokens' in usage:
        cached = int(usage.get('cache_read_input_tokens') or 0)
        prompt += cached + int(usage.get('cache_creation_input_tokens') or 0)
    else:
        details = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
        cached = int(details.get('cached_tokens') or 0)

    return {'input': prompt, 'output': completion, 'cached_input': min(cached, prompt)}


def load_prices(path):
    """USD per million tokens: {"<model>": {"input_per_mtok": 3, "output_per_mtok": 15}, "*": {...}}."""
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if 'models' in payload and isinstance(payload['models'], dict):
        payload = payload['models']
    prices = {}
    for name, value in payload.items():
        if not isinstance(value, dict) or 'output_per_mtok' not in value:
            raise SystemExit(f'price entry {name!r} must map to '
                             '{"input_per_mtok": ..., "output_per_mtok": ...}')
        prices[name] = {
            'input': float(value.get('input_per_mtok', 0)),
            'cached_input': float(value.get('cached_input_per_mtok',
                                            value.get('input_per_mtok', 0))),
            'output': float(value['output_per_mtok']),
        }
    return prices


def price_for(prices, model):
    return prices.get(model) or prices.get('*')


def compute_charge(tokens, price):
    """Cost of one task from its token usage; None without a price entry."""
    if not price:
        return None
    cached = min(tokens.get('cached_input_tokens', 0), tokens.get('input_tokens', 0))
    fresh = max(tokens.get('input_tokens', 0) - cached, 0)
    return round((fresh * price['input'] + cached * price['cached_input']
                  + tokens.get('output_tokens', 0) * price['output']) / 1_000_000, 6)
